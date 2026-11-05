"""The new chat pipeline pieces that sit around the app's existing flow (tested first as a prototype in testing_temparary/variants/v3.py).

Switched on by NEW_CHAT_PIPELINE=true (default OFF, so the old behaviour and the old tests are untouched). Undo = set it to false and restart.

Order for a fresh message (see service.answer_query):
  free checks  : managed/referred, daily limit left (read only), urgent-word rules, dictionary word      -> refusal, NO daily question used
  charge       : one daily question
  paid checks  : embedding (similar word), AI urgent check                                              -> refusal
  then the app's existing flow: condition check + button, intent, past workouts / undo / progress / medical answer.
Free refusals are capped per account per day in production (FREE_REFUSAL_DAILY_LIMIT, default 10, so 15 requests a day with the 5 AI questions) and are
unlimited outside production. Every step that can fail (word store, embedding service, urgent AI check) fails OPEN to the next step, never breaking the chat.
"""
from __future__ import annotations

import logging
import os
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.platform.errors import QuotaExhausted
from app.rag import chat_prompts, chat_rules, exercise_table, risk, urgent_check, v2, word_extract, word_store
from app.rag.chat_messages import MESSAGES, refusal_message
from app.rag.word_layers import DictionaryEmbeddingLayer

_log = logging.getLogger(__name__)
_LABEL_TO_REASON = {"emergency": "red_flag", "out_of_scope": "out_of_scope", "pregnancy": "pregnancy"}


def enabled() -> bool:
    return os.getenv("NEW_CHAT_PIPELINE", "false").strip().lower() == "true"


def simple_flow() -> bool:
    """SIMPLE_FLOW=true: the owner's flow. Free steps are only the input check and a tiny emergency / self-harm word list; everything else (minors, surgery, sugar, reminders,
    instructions, bare replies, add commands, 'both') is decided by the risk classifier and the router, and code confirms. Off: the longer word-rule flow."""
    return enabled() and os.getenv("SIMPLE_FLOW", "false").strip().lower() == "true"


def router_enabled() -> bool:
    """NEW_ROUTER=true (needs NEW_CHAT_PIPELINE=true too): one router call, chat and targets routes, button rule, once-a-day doctor note. Undo: false."""
    return enabled() and os.getenv("NEW_ROUTER", "false").strip().lower() == "true"


def free_refusal_limit() -> int:
    """0 = unlimited. Default: unlimited outside production, 10 a day in production (15 requests a day with the 5 AI questions)."""
    raw = os.getenv("FREE_REFUSAL_DAILY_LIMIT")
    if raw is not None and raw.strip() != "":
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return 10 if os.getenv("FLASK_ENV", "").strip().lower() == "production" else 0


def words_db_path() -> str:
    return os.getenv("TRIGGER_WORDS_DB") or str(Path(__file__).resolve().parents[2] / "data" / "trigger_words.db")


# ---- the dictionary + embedding layers (one shared instance; the dictionary reloads itself when a developer submits new words)
_KEY = {"v": None}
_LAYERS: dict = {"layer": None}
_LAYERS_LOCK = threading.Lock()


def layers(api_key: str) -> DictionaryEmbeddingLayer:
    _KEY["v"] = api_key
    if _LAYERS["layer"] is None:
        with _LAYERS_LOCK:
            if _LAYERS["layer"] is None:
                _LAYERS["layer"] = DictionaryEmbeddingLayer(
                    word_store.DictionaryProvider(words_db_path()),
                    embed_fn=lambda ws: word_store.openrouter_embed(ws, timeout=3.0, api_key=_KEY["v"]), threshold=0.75, timeout=3.0)
    return _LAYERS["layer"]


def reset_for_tests() -> None:
    _LAYERS["layer"] = None


# ---- free-refusal cap, kept in the same SQLite file as the dictionary so all server workers share it (a restart does not reset it)
def _bump_free_counter(svc, user_id: str) -> None:
    limit = free_refusal_limit()
    if limit <= 0:
        return                                                      # unlimited (local development)
    today = svc.today_ist().isoformat()
    try:
        conn = word_store.connect(words_db_path())
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS free_refusals (user_id TEXT NOT NULL, day TEXT NOT NULL, n INTEGER NOT NULL, PRIMARY KEY (user_id, day))")
            conn.execute("DELETE FROM free_refusals WHERE day < ?", (today,))
            cur = conn.execute("INSERT INTO free_refusals (user_id, day, n) VALUES (?, ?, 1) ON CONFLICT(user_id, day) DO UPDATE SET n = n + 1 WHERE n < ?",
                               (user_id, today, limit))
            allowed = cur.rowcount > 0
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - a broken counter must never block the chat
        _log.exception("free-refusal counter failed; allowing the request")
        return
    if not allowed:
        raise QuotaExhausted("You've sent a lot of messages today. Please come back tomorrow, or speak to a doctor if this feels urgent.")


# ---- responses
def refusal_response(svc, session, user_id: str, question: str, answer: str) -> dict:
    svc._save_chat_log(session, user_id, question, answer)
    return {
        "referred": False, "message": None, "answer": answer,
        "needs_condition_selection": False, "condition_options": None,
        "needs_escalation_confirmation": False, "escalation_proposal": None,
        "needs_program_update_confirmation": False, "suggested_activities": None,
        "needs_revert_confirmation": False, "revert_candidates": None,
        "quota": svc.remaining_quota(session, user_id),
    }


def _answers_our_choice(svc, session, user_id: str, question: str) -> bool:
    """True when this lone word ('both', 'dono', 'all') answers OUR last question 'Which one do you mean: A, B?'."""
    if not v2.is_choice_followup(question):
        return False
    prev = svc._recent_full_exchanges(session, user_id, limit=1)
    return bool(prev) and prev[-1].answer.startswith(v2.CHOICE_PREFIXES)


def early_checks(svc, session, user_id: str, question: str) -> dict | None:
    """The FREE steps, before the daily question is charged. A finished response (free refusal) or None to carry on."""
    if svc.remaining_quota(session, user_id)["remaining"] <= 0:
        raise QuotaExhausted(f"You've used all {svc.DAILY_LIMIT} questions for today. Come back tomorrow, or speak to a doctor if this feels urgent.")
    if router_enabled() and not chat_rules.has_real_letters(question):
        _bump_free_counter(svc, user_id)                                  # a stray key or a number: ask for more, free
        return refusal_response(svc, session, user_id, question, MESSAGES["need_more"])
    _forms = [question] + ([v2.normalize_for_rules(question)] if v2.enabled() else [])            # the original text AND the evasion-proof form
    _compact = v2.compact_hit(question) if v2.enabled() else None
    if v2.enabled() and (any(v2.self_harm(x) for x in _forms) or _compact == "self_harm"):
        _bump_free_counter(svc, user_id)                                      # self-harm: its own kind message (not the physical-injury text)
        return refusal_response(svc, session, user_id, question, v2.SELF_HARM_MESSAGE)
    if any(urgent_check.urgent_by_rules(x) for x in _forms) or _compact == "urgent":
        _bump_free_counter(svc, user_id)
        return refusal_response(svc, session, user_id, question, MESSAGES["urgent"])
    if simple_flow():
        return None                                                           # only emergency and self-harm words stay free; the classifier and router decide the rest
    if v2.enabled() and v2.is_minor(question):
        _bump_free_counter(svc, user_id)
        return refusal_response(svc, session, user_id, question, v2.MINOR_MESSAGE)
    if v2.enabled() and v2.is_postpartum(question):
        _bump_free_counter(svc, user_id)                                      # after delivery needs the doctor's advice, same as pregnancy
        return refusal_response(svc, session, user_id, question, MESSAGES["pregnancy"])
    if v2.enabled() and v2.asks_about_surgery(question):
        _bump_free_counter(svc, user_id)
        return refusal_response(svc, session, user_id, question, v2.SURGERY_MESSAGE)
    if v2.enabled() and v2.says_high_sugar_or_bp(question):
        _bump_free_counter(svc, user_id)                                      # high sugar / blood pressure: a doctor first, same as the saved-condition message
        return refusal_response(svc, session, user_id, question, MESSAGES["controls"])
    if v2.enabled() and any(v2.looks_like_instruction(x) for x in _forms):
        _bump_free_counter(svc, user_id)                                      # "system:", "ignore previous instructions", ...: the standard refusal, free
        return refusal_response(svc, session, user_id, question, MESSAGES["no_info"])
    if v2.enabled() and v2.is_bare_url(question):
        _bump_free_counter(svc, user_id)                                      # only a link: the chat cannot open it, and its words are not a question
        return refusal_response(svc, session, user_id, question, v2.BARE_URL_MESSAGE)
    if v2.enabled() and v2.asks_delete_account(question):
        _bump_free_counter(svc, user_id)
        return refusal_response(svc, session, user_id, question, v2.ACCOUNT_MESSAGE)
    if v2.enabled() and v2.asks_reminder(question):
        _bump_free_counter(svc, user_id)
        return refusal_response(svc, session, user_id, question, v2.REMINDER_MESSAGE)
    if v2.enabled() and v2.is_bare_reply(question) and not _answers_our_choice(svc, session, user_id, question):
        _bump_free_counter(svc, user_id)                                      # a lone yes / no: nothing to answer
        return refusal_response(svc, session, user_id, question, v2.BARE_REPLY_MESSAGE)
    if not chat_prompts.is_data_question(question):
        try:
            lr = layers(os.environ.get("OPENROUTER_API_KEY", "")).check_dictionary(question)
        except Exception:  # noqa: BLE001 - the word store must never break the chat: fail open to the next step
            _log.exception("dictionary check failed; continuing without it")
            lr = {}
        if lr.get("layer"):
            _bump_free_counter(svc, user_id)
            return refusal_response(svc, session, user_id, question, refusal_message(_LABEL_TO_REASON.get(lr["label"], "out_of_scope")))
    return None


def _reply_flags() -> dict:
    """Our fixed risk replies -> the risk class they were for, so the classifier is shown which earlier messages the app already flagged."""
    return {MESSAGES["urgent"]: "emergency", v2.SELF_HARM_MESSAGE: "self_harm", v2.MEDICINE_MESSAGE: "medicine", v2.MINOR_MESSAGE: "minor", MESSAGES["pregnancy"]: "pregnancy",
            v2.EATING_MESSAGE: "eating_disorder", MESSAGES["controls"]: "uncontrolled_condition", v2.DIAGNOSIS_MESSAGE: "diagnosis"}


def _previous_risk(svc, session, user_id: str) -> str | None:
    """The risk class of our own last reply, when that reply was one of the fixed risk messages (within the last 2 exchanges)."""
    fixed = {MESSAGES["urgent"]: "emergency", v2.SELF_HARM_MESSAGE: "self_harm", v2.MEDICINE_MESSAGE: "medicine", v2.MINOR_MESSAGE: "minor", MESSAGES["pregnancy"]: "pregnancy",
             v2.EATING_MESSAGE: "eating_disorder", MESSAGES["controls"]: "uncontrolled_condition", v2.DIAGNOSIS_MESSAGE: "diagnosis"}
    for r in reversed(svc._recent_full_exchanges(session, user_id, limit=2)):
        if r.answer in fixed:
            return fixed[r.answer]
    return None


def risk_message(svc, session, user_id: str, cls: str, question: str) -> str:
    """The FIXED message for a risk class: the model never writes free text for a risky case."""
    if cls == "emergency":
        return MESSAGES["urgent"]
    if cls == "self_harm":
        return v2.SELF_HARM_MESSAGE
    if cls == "medicine":
        return v2.MEDICINE_MESSAGE
    if cls == "minor":
        return v2.MINOR_MESSAGE
    if cls == "pregnancy":
        return MESSAGES["pregnancy"]
    if cls == "eating_disorder":
        return v2.EATING_MESSAGE
    if cls == "uncontrolled_condition":
        return MESSAGES["controls"]
    if cls == "diagnosis":
        return v2.DIAGNOSIS_MESSAGE
    labels = svc._existing_condition_labels(session, user_id)                 # override_attempt
    return v2.healthy_message(labels) if labels and not v2.looks_like_instruction(question) else MESSAGES["no_info"]


def paid_checks(svc, session, user_id: str, question: str, api_key: str) -> tuple[dict | None, bool]:
    """After the daily question is charged: embedding check, then the AI urgent check. (finished response or None, urgent_check_unavailable)."""
    if not simple_flow() and not chat_prompts.is_data_question(question):
        try:
            lr = layers(api_key).check_embedding(question)
        except Exception:  # noqa: BLE001
            _log.exception("embedding check failed; going on to the AI")
            lr = {}
        if lr.get("layer"):
            return refusal_response(svc, session, user_id, question, refusal_message(_LABEL_TO_REASON.get(lr["label"], "out_of_scope"))), False
    if risk.enabled():
        try:
            _flags = _reply_flags()
            ctx = [r.question + (f" [the app already gave a fixed doctor message: {_flags[r.answer]}]" if r.answer in _flags else "")
                   for r in svc._recent_full_exchanges(session, user_id, limit=3)]
            res = risk.classify(question, ctx, api_key)
        except Exception:  # noqa: BLE001
            _log.exception("risk classifier failed; using the old AI urgent check")
            res = None
        if res is not None:
            # a risk class acts when the model is reasonably sure (real risky messages scored 0.93 or more in the benchmark; unsure ones are short follow-ups like "add walking")
            if (res.get("probs") or {}).get("self_harm", 0) >= 0.3:
                res = {**res, "class": "self_harm", "confidence": 1.0}       # indirect wording: harmless look-alikes score 0.01 or less, a missed case costs far more than a false alarm
            if (res.get("probs") or {}).get("eating_disorder", 0) >= 0.45:
                res = {**res, "class": "eating_disorder", "confidence": 1.0}  # same reasoning as self-harm: a missed case costs far more than a false alarm
            if res["class"] != "none" and res["confidence"] >= 0.6:
                record_async(question, api_key)
                return refusal_response(svc, session, user_id, question, risk_message(svc, session, user_id, res["class"], question)), False
            # RISK CARRIED FORWARD: if our own previous reply was a fixed risk message and this message is not clearly a different topic ("ab kya karu?", "should I continue?",
            # "koi exercise bata do"), the risk still applies. Uses the model's own probability for "none": clearly separate questions score 0.9 or more.
            prev = _previous_risk(svc, session, user_id)
            p_none = (res.get("probs") or {}).get("none")
            if prev and p_none is not None and p_none < (0.9 if prev in ("minor", "pregnancy") else 0.8):
                return refusal_response(svc, session, user_id, question, risk_message(svc, session, user_id, prev, question)), False
            return None, False
    try:
        verdict = urgent_check.urgent_by_ai(question, api_key)
    except Exception:  # noqa: BLE001
        _log.exception("urgent AI check failed; treating it as unavailable")
        verdict = "unavailable"
    if verdict == "yes":
        record_async(question, api_key)
        return refusal_response(svc, session, user_id, question, MESSAGES["urgent"]), False
    return None, verdict == "unavailable"


# ---- the learning loop: after an AI refusal, the words that caused it are saved (in the background; the user's answer never waits)
BG_LOG: deque = deque(maxlen=2000)
_BG = {"pool": None, "pending": set(), "lock": threading.Lock(), "max_pending": 200}


def _record_words(question: str, api_key: str) -> dict:
    x = word_extract.extract(question, api_key=api_key)
    conn = word_store.connect(words_db_path())                       # one connection per job
    try:
        stored = [w for w in x["words"] if word_store.record(conn, w, x["label"])]
    finally:
        conn.close()
    return {"label": x["label"], "returned": x["words"], "stored": stored, "cost": x["cost"]}


def record_async(question: str, api_key: str) -> bool:
    """Best effort, never blocks: if too many jobs are waiting the words are simply not recorded this time."""
    with _BG["lock"]:
        if len(_BG["pending"]) >= _BG["max_pending"]:
            return False
        if _BG["pool"] is None:
            _BG["pool"] = ThreadPoolExecutor(max_workers=2, thread_name_prefix="trigger-words")
        fut = _BG["pool"].submit(_record_words, question, api_key)
        _BG["pending"].add(fut)

    def _done(f):
        try:
            BG_LOG.append(f.result())
        except Exception as e:  # noqa: BLE001
            BG_LOG.append({"error": str(e)[:120]})
        with _BG["lock"]:
            _BG["pending"].discard(f)
    fut.add_done_callback(_done)
    return True


def flush_background(timeout: float = 30.0) -> int:
    from concurrent.futures import wait
    with _BG["lock"]:
        futs = list(_BG["pending"])
    wait(futs, timeout=timeout)
    with _BG["lock"]:
        return len(_BG["pending"])


# ---- Hinglish / misspelt / very short questions: the notes are in English, so when the first search finds nothing relevant, ONE cheap call rewrites the
# question as a short English search query and the search is repeated (the better of the two is used). Only on a weak first search, so normal questions pay nothing.
_REWRITE_SYSTEM = ("Rewrite the user's health message (it may be Hindi, Hinglish, misspelt or very short) as one short, self-contained English search query "
                   "about the health topic, for example: knee pain exercises. Reply with the query only, no quotes, no explanation.")


def english_search_query(question: str, api_key: str) -> str | None:
    from app.rag import vectorstore
    try:
        out = vectorstore.chat_complete([{"role": "system", "content": _REWRITE_SYSTEM}, {"role": "user", "content": question[:500]}], api_key,
                                        model=urgent_check.URGENT_MODEL, retries=1, temperature=0, max_tokens=40)
    except Exception:  # noqa: BLE001 - best effort: without a rewrite the first search result is used
        _log.warning("English search-query rewrite failed; using the first search", exc_info=True)
        return None
    q = out.strip().strip('"').strip("'").splitlines()[0][:200] if out and out.strip() else ""
    return q or None


# ---- the medical answer
def medical_setup(condition_codes: list[str], categories: dict[str, str]) -> tuple[list[str], str, str | None]:
    """(approved exercise names, the prompt block, gate reason). When the gate says no exercises, the list is empty and the caller adds the doctor message."""
    gate = exercise_table.gate(condition_codes)
    if gate:
        return [], "Approved exercises for this condition: none listed.", gate
    return exercise_table.approved_names(condition_codes), exercise_table.approved_block(condition_codes, categories), None
