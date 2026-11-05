"""The two cheap layers in front of the AI: hardcoded dictionary (exact word) and embedding similarity, both read from the store.

Failure rule (decided with the owner): if the embedding service fails, is too slow, or is switched off by the breaker, the request is NOT refused and
NOT failed: it simply passes on to the AI, exactly as if the embedding layer did not exist. The dictionary (exact word) never needs the embedding service.
Paid embedding model (baai/bge-base-en-v1.5, $0.005 per million tokens): steady latency; the free model had calls of 8 s and more.
"""
from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

import numpy as np

from app.rag import word_store as store

TEMPLATE = re.compile(r"complain of|since some days|no trauma or injury noted|no other abnormal noted|not recorded", re.I)
STOP = set("the and for with from not noted days day week weeks month months year years ago yrs yr ma pt follow followup up fuc case report reports "
           "check vital virtual since some also all both left right lt rt bilateral under evaluation eval one two three four five six seven eight nine ten".split())


def tokens(text: str) -> list[str]:
    """Distinct lower-case words of 3+ letters, without template phrases, filler words and normal exercise-complaint words."""
    t = TEMPLATE.sub(" ", (text or "").lower())
    return [w for w in dict.fromkeys(re.findall(r"[a-z]+", t)) if len(w) >= 3 and w not in STOP and w not in store.NORMAL]


class DictionaryEmbeddingLayer:
    def __init__(self, provider: store.DictionaryProvider, embed_fn=None, threshold: float = 0.75, timeout: float = 3.0,
                 breaker_failures: int = 3, breaker_seconds: float = 30.0, cache_size: int = 5000):
        self.provider, self.threshold, self.timeout = provider, threshold, timeout
        self.embed_fn = embed_fn or (lambda ws: store.openrouter_embed(ws, timeout=timeout))
        self.breaker_failures, self.breaker_seconds = breaker_failures, breaker_seconds
        self._cache: "OrderedDict[str, np.ndarray]" = OrderedDict()
        self._cache_size = cache_size
        self._lock = threading.Lock()
        self._fails = 0
        self._open_until = 0.0
        self._pool = ThreadPoolExecutor(max_workers=4)
        self.embed_calls = 0

    # ---- embedding with timeout, cache and breaker
    def _vectors(self, words: list[str]) -> tuple[dict[str, np.ndarray] | None, str]:
        with self._lock:
            have = {w: self._cache[w] for w in words if w in self._cache}
            for w in have:
                self._cache.move_to_end(w)
        need = [w for w in words if w not in have]
        if not need:
            return have, "cached"
        if time.monotonic() < self._open_until:
            return None, "skipped_breaker"          # recent failures: do not make every request wait for a dead service
        try:
            self.embed_calls += 1
            vecs = self._pool.submit(self.embed_fn, need).result(timeout=self.timeout)
            if len(vecs) != len(need):
                raise ValueError("wrong number of vectors")
        except (FutureTimeout, Exception):          # noqa: BLE001  any failure = fall through to the AI
            with self._lock:
                self._fails += 1
                if self._fails >= self.breaker_failures:
                    self._open_until = time.monotonic() + self.breaker_seconds
            return None, "failed"
        with self._lock:
            self._fails = 0
            for w, v in zip(need, vecs):
                a = np.asarray(v, dtype=np.float32); self._cache[w] = a / (np.linalg.norm(a) or 1.0)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
            have.update({w: self._cache[w] for w in need})
        return have, "ok"

    def check_dictionary(self, text: str) -> dict:
        """Free step: exact dictionary word only (no AI, no embedding call)."""
        t0 = time.perf_counter()
        d = self.provider.get()
        out = {"layer": None, "matched": "", "label": None, "embedding": "not_needed", "fell_back_to_ai": False}
        hit = [w for w in tokens(text) if w in d.words]
        if hit:
            out.update(layer="dictionary", matched=", ".join(hit), label=d.words[hit[0]])
        out["seconds"] = round(time.perf_counter() - t0, 4)
        return out

    def check_embedding(self, text: str) -> dict:
        """Paid step (tiny): a word similar to a dictionary word. On any failure the request simply goes on to the AI."""
        return self._check(text, dictionary=False)

    def check(self, text: str) -> dict:
        """Both steps. {layer: 'dictionary' | 'embedding' | None, matched, label, embedding: ok|cached|not_needed|failed|skipped_breaker, fell_back_to_ai: bool, seconds}"""
        return self._check(text, dictionary=True)

    def _check(self, text: str, dictionary: bool) -> dict:
        t0 = time.perf_counter()
        d = self.provider.get()
        toks = tokens(text)
        out = {"layer": None, "matched": "", "label": None, "embedding": "not_needed", "fell_back_to_ai": False}
        hit = [w for w in toks if w in d.words] if dictionary else []
        if hit:
            out.update(layer="dictionary", matched=", ".join(hit), label=d.words[hit[0]])
        elif toks and len(d.order):
            vecs, status = self._vectors(toks)
            out["embedding"] = status
            if vecs is None:
                out["fell_back_to_ai"] = True       # embedding unavailable: the request goes on to the AI
            else:
                for w in toks:
                    sims = d.vectors @ vecs[w]
                    j = int(np.argmax(sims))
                    if sims[j] >= self.threshold:
                        out.update(layer="embedding", matched=f"{w} ~ {d.order[j]} ({sims[j]:.2f})", label=d.words[d.order[j]])
                        break
        out["seconds"] = round(time.perf_counter() - t0, 4)
        return out
