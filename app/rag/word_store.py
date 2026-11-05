"""Trigger-word store: the words the AI refused requests for, the developer's decisions, and the dictionary + embeddings they feed.

One SQL database, tables written so they move to Postgres unchanged (plain SQL, ON CONFLICT upserts, BLOB -> BYTEA):
  trigger_words     word, label, times_seen, status (pending / approved / rejected / submitted)
  dictionary_words  word, category, embedding   (what the live pipeline reads)
  meta              dictionary_version          (bumped on every submit; the pipeline reloads when it changes)

Every write is a single atomic statement or one transaction, so many live requests recording words at once cannot overwrite each other.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import urllib.request

import numpy as np

LABELS = ("emergency", "out_of_scope", "pregnancy")
STATUSES = ("pending", "approved", "rejected", "submitted")
# normal exercise-complaint words are never stored, even if the AI returns them
NORMAL = {"pain", "pains", "ache", "aches", "back", "knee", "knees", "joint", "joints", "waist", "lower", "lbp", "lba", "stiffness", "muscle", "neck",
          "since", "long", "problem", "days", "day", "week", "weeks", "month", "months", "year", "years"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS trigger_words (
    word TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    times_seen INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'submitted'))
);
CREATE TABLE IF NOT EXISTS dictionary_words (
    word TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    embedding BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT OR IGNORE INTO meta (key, value) VALUES ('dictionary_version', '0');
"""


def connect(path: str) -> sqlite3.Connection:
    """One connection per thread / process. WAL + busy timeout: readers never block writers and writers queue instead of failing."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)   # autocommit; transactions are explicit
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    return conn


def normalise(word: str) -> str | None:
    """Lower-case single word of letters, at least 3 letters, not a normal complaint word. None = not storable."""
    w = (word or "").strip().lower()
    if not re.fullmatch(r"[a-z]{3,}", w) or w in NORMAL:
        return None
    return w


def record(conn: sqlite3.Connection, word: str, label: str) -> bool:
    """The AI refused a request because of `word`. One atomic upsert: new word -> pending; known word -> only the count goes up.
    An approved / rejected / submitted word keeps its status and its label. Returns False if the word is not storable."""
    w = normalise(word)
    if w is None or label not in LABELS:
        return False
    conn.execute("INSERT INTO trigger_words (word, label) VALUES (?, ?) ON CONFLICT(word) DO UPDATE SET times_seen = times_seen + 1", (w, label))
    return True


def list_words(conn: sqlite3.Connection, status: str | None = None) -> list[dict]:
    q = "SELECT word, label, times_seen, status FROM trigger_words" + (" WHERE status = ?" if status else "") + " ORDER BY times_seen DESC, word"
    return [dict(r) for r in conn.execute(q, (status,) if status else ())]


def decide(conn: sqlite3.Connection, decisions: list[dict]) -> int:
    """Developer decisions: {word, status in pending/approved/rejected, label (optional)}. A submitted word is locked. Returns rows changed."""
    n = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for d in decisions:
            if d.get("status") not in ("pending", "approved", "rejected"):
                continue
            label = d.get("label") if d.get("label") in LABELS else None
            cur = conn.execute("UPDATE trigger_words SET status = ?, label = COALESCE(?, label) WHERE word = ? AND status != 'submitted'", (d["status"], label, d.get("word")))
            n += cur.rowcount
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return n


def dictionary_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT value FROM meta WHERE key = 'dictionary_version'").fetchone()[0])


def submit(conn: sqlite3.Connection, embed_fn) -> dict:
    """Add every approved word to the dictionary and its embedding, mark it submitted, bump the version.
    All or nothing: if embedding fails nothing changes. The network call to embed happens outside the write lock; afterwards only words
    that are STILL approved with the same label are applied (a word the developer changed meanwhile is skipped and reported)."""
    snap = [(r["word"], r["label"]) for r in conn.execute("SELECT word, label FROM trigger_words WHERE status = 'approved'")]
    if not snap:
        return {"submitted": 0, "skipped": [], "version": dictionary_version(conn)}
    vecs = embed_fn([w for w, _ in snap])              # may raise: then nothing was written
    done, skipped = [], []
    conn.execute("BEGIN IMMEDIATE")
    try:
        for (w, label), v in zip(snap, vecs):
            row = conn.execute("SELECT status, label FROM trigger_words WHERE word = ?", (w,)).fetchone()
            if row is None or row["status"] != "approved" or row["label"] != label:
                skipped.append(w); continue
            blob = np.asarray(v, dtype=np.float32).tobytes()
            conn.execute("INSERT INTO dictionary_words (word, category, embedding) VALUES (?, ?, ?) ON CONFLICT(word) DO UPDATE SET category = excluded.category, embedding = excluded.embedding", (w, label, blob))
            conn.execute("UPDATE trigger_words SET status = 'submitted' WHERE word = ?", (w,))
            done.append(w)
        if done:
            conn.execute("UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'dictionary_version'")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"submitted": len(done), "words": done, "skipped": skipped, "version": dictionary_version(conn)}


def remove_words(conn: sqlite3.Connection, words: list[str]) -> dict:
    """Take submitted words OUT of the dictionary and the embeddings (a wrongly approved word). One transaction; version bumped so the live
    pipeline drops them without a restart. The word is kept in the review list as 'rejected', so it is not proposed again as pending."""
    removed = []
    conn.execute("BEGIN IMMEDIATE")
    try:
        for w in words:
            w = (w or "").strip().lower()
            if conn.execute("DELETE FROM dictionary_words WHERE word = ?", (w,)).rowcount:
                conn.execute("UPDATE trigger_words SET status = 'rejected' WHERE word = ?", (w,))
                removed.append(w)
        if removed:
            conn.execute("UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'dictionary_version'")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"removed": removed, "version": dictionary_version(conn)}


class DictionaryProvider:
    """What the live pipeline reads. Checks the version number (one tiny query, at most every `min_interval` seconds) and reloads only when a
    submit happened, so new words take effect without restarting anything."""

    def __init__(self, path: str, min_interval: float = 2.0):
        self.path, self.min_interval = path, min_interval
        self._local = threading.local()
        self._lock = threading.Lock()
        self._checked = 0.0
        self.version = -1
        self.words: dict[str, str] = {}
        self.vectors = np.zeros((0, 0), dtype=np.float32)
        self.order: list[str] = []

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._local.conn = connect(self.path)
        return c

    def get(self) -> "DictionaryProvider":
        now = time.monotonic()
        if now - self._checked >= self.min_interval or self.version < 0:
            with self._lock:
                self._checked = now
                conn = self._conn()
                v = dictionary_version(conn)
                if v != self.version:
                    rows = conn.execute("SELECT word, category, embedding FROM dictionary_words ORDER BY word").fetchall()
                    self.order = [r["word"] for r in rows]
                    self.words = {r["word"]: r["category"] for r in rows}
                    self.vectors = np.stack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows]) if rows else np.zeros((0, 0), dtype=np.float32)
                    self.version = v
        return self


EMBED_MODEL = "baai/bge-base-en-v1.5"      # paid, $0.005 per million tokens; steady latency (the free model had calls of 8 s and more)


def openrouter_embed(words: list[str], model: str = EMBED_MODEL, timeout: float = 60, api_key: str | None = None) -> list[list[float]]:
    """Real embedder (the model chosen in the mock test). Returns unit-length vectors. Raises on any failure or when `timeout` seconds pass."""
    key = api_key or os.environ["OPENROUTER_API_KEY"]
    out: list[list[float]] = []
    for i in range(0, len(words), 64):
        req = urllib.request.Request("https://openrouter.ai/api/v1/embeddings", method="POST",
                                     data=json.dumps({"model": model, "input": words[i:i + 64]}).encode(),
                                     headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
        for x in sorted(d["data"], key=lambda x: x["index"]):
            v = np.asarray(x["embedding"], dtype=np.float32)
            out.append((v / (np.linalg.norm(v) or 1.0)).tolist())
    return out
