"""Developer-only admin page for the trigger-word review. A SEPARATE server on its own port and link, not part of the chatbot or the user-facing app.
Same pattern as the app's devconsole: HTTP Basic Auth, fails CLOSED when no password is configured. One admin user (no audit trail by design).

  set TRIGGER_ADMIN_PASSWORD=...        (any username)
  python tools/trigger_words_admin/server.py [--port 5055] [--db data/trigger_words.db]

It reads and writes the same SQLite file the chatbot uses (TRIGGER_WORDS_DB, default data/trigger_words.db). Submit embeds the approved words with the paid
embedding model (OPENROUTER_API_KEY from .env.dev) and the running chatbot picks the new words up within seconds, with no restart.
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

from flask import Flask, Response, jsonify, request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.rag import word_store as store  # noqa: E402

PAGE = Path(__file__).with_name("page.html")


def create_app(db_path: str, embed_fn=None, password: str | None = None) -> Flask:
    app = Flask(__name__)
    pw = password if password is not None else os.environ.get("TRIGGER_ADMIN_PASSWORD", "")
    embed = embed_fn or store.openrouter_embed

    @app.before_request
    def _auth():
        if not pw:                                                     # fail closed: no password configured -> nothing is served
            return Response("admin password not configured", 503)
        a = request.authorization
        if not a or not secrets.compare_digest(a.password or "", pw):
            return Response("login required", 401, {"WWW-Authenticate": 'Basic realm="trigger words"'})

    def conn():
        return store.connect(db_path)

    @app.get("/")
    def page():
        return Response(PAGE.read_text(encoding="utf-8"), mimetype="text/html")

    @app.get("/api/words")
    def words():
        c = conn()
        return jsonify({"words": store.list_words(c), "labels": list(store.LABELS), "version": store.dictionary_version(c)})

    @app.post("/api/decisions")
    def decisions():
        body = request.get_json(silent=True) or {}
        return jsonify({"changed": store.decide(conn(), body.get("decisions") or [])})

    @app.post("/api/submit")
    def submit():
        try:
            return jsonify(store.submit(conn(), embed))
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": f"could not embed the words, nothing was changed: {e}"}), 502

    @app.post("/api/remove")
    def remove():
        body = request.get_json(silent=True) or {}
        return jsonify(store.remove_words(conn(), body.get("words") or []))

    return app


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("TRIGGER_WORDS_DB") or str(ROOT / "data" / "trigger_words.db"))
    ap.add_argument("--port", type=int, default=5055)
    a = ap.parse_args()
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env.dev")
    except Exception:  # noqa: BLE001
        pass
    create_app(a.db).run(host="127.0.0.1", port=a.port)
