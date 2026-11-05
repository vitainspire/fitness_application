"""Regression test for the off-topic-question gap found live this session:
retrieve_chunks is a nearest-neighbor search — it always returns its top_k
rows no matter how irrelevant the question is (there's no such thing as "no
match"). An off-topic question ("what is the capital of France") still came
back with real-looking chunks, and the model was left to notice on its own
that none of them actually answer the question — which it doesn't always do,
answering from its own general knowledge instead (verified live: "15 times
34" got answered "510").

RELEVANCE_THRESHOLD closes this in code, not in the prompt: a weak best-match
similarity score is treated the same as no chunks at all, BEFORE the model is
ever asked to write an answer — deterministic, free (no LLM call), and
doesn't depend on model behavior.
"""
from app.platform.db import session_scope
from app.rag import service as rag_service
from app.rag import vectorstore

LOW_SIMILARITY_CHUNK = {
    "chunk_id": 1, "source_note": "test", "url": "", "text": "irrelevant text",
    "condition_code": "gout", "similarity": 0.15,
}
HIGH_SIMILARITY_CHUNK = {
    "chunk_id": 2, "source_note": "test", "url": "", "text": "genuinely relevant text",
    "condition_code": "gout", "similarity": 0.6,
}


def test_weak_retrieval_declines_without_ever_calling_the_answer_llm(make_user, monkeypatch):
    user = make_user(condition_codes=["gout"])
    monkeypatch.setattr(vectorstore, "retrieve_chunks", lambda *a, **k: [LOW_SIMILARITY_CHUNK])

    def fail_if_called(*a, **k):
        raise AssertionError("chat_complete must not be called once retrieval is below threshold")
    monkeypatch.setattr(vectorstore, "chat_complete", fail_if_called)

    with session_scope() as s:
        result = rag_service._retrieve_and_answer(
            s, user["user_id"], "what is the capital of France",
            rag_db_url="unused", openrouter_api_key="unused", rlm_enabled=False,
        )
    assert result["answer"] == rag_service.NO_CONTENT_ANSWER


def test_strong_retrieval_still_reaches_the_answer_llm(make_user, monkeypatch):
    """The threshold must not accidentally block genuinely relevant questions."""
    user = make_user(condition_codes=["gout"])
    monkeypatch.setattr(vectorstore, "retrieve_chunks", lambda *a, **k: [HIGH_SIMILARITY_CHUNK])

    called = {"hit": False}

    def fake_chat_complete(*a, **k):
        called["hit"] = True
        return "a real answer using the retrieved text about what causes gout flare-ups"
    monkeypatch.setattr(vectorstore, "chat_complete", fake_chat_complete)

    with session_scope() as s:
        result = rag_service._retrieve_and_answer(
            s, user["user_id"], "what causes gout flare-ups",
            rag_db_url="unused", openrouter_api_key="unused", rlm_enabled=False,
        )
    assert called["hit"] is True
    assert result["answer"] != rag_service.NO_CONTENT_ANSWER
