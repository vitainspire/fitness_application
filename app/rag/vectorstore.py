"""Talks to the Supabase/pgvector database holding rag_chunks — a separate physical
database from the app's own Postgres (STRATEGY §5: keep the vector store out of the
primary datastore's blast radius). Plain psycopg, not the app's SQLAlchemy engine,
since this is a different database entirely.

This is the single canonical place these OpenRouter calls are made — scripts/
embed_chunks.py and scripts/rag_query.py both import from here rather than keeping
their own copies, so there is exactly one place the embedding/chat request shapes
can drift.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

import psycopg

EMBEDDING_MODEL = "openai/text-embedding-3-small"
EMBEDDING_URL = "https://openrouter.ai/api/v1/embeddings"
CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
VOICE_CHAT_MODEL = "openai/gpt-4.1-mini"  # tested on a 7-exercise voice log: 8/8 correct, ~4.4 s (4o-mini: 7/8, ~7 s)
CHAT_MODEL = "openai/gpt-oss-20b"  # paid — OpenRouter discontinued the :free tier
# for this exact model (confirmed live, 2026-08-24: the :free slug now 404s with
# "use this slug instead: openai/gpt-oss-20b"). Deliberately the SAME model, not
# a switch to a different one — every prompt in this file (SYSTEM_PROMPT,
# CONDITION_MATCH_PROMPT, the RLM router/step prompts in app/progression/analysis.py)
# and the medication/surgery safety-filter testing were tuned and verified against
# gpt-oss-20b's specific behavior. Swapping to a different model would silently
# reopen that safety testing, for a fractional cost difference not worth the risk.
# At $0.03/$0.13 per 1M input/output tokens, this is one of the cheapest paid
# models on OpenRouter regardless.
TOP_K = 5

# Moved here from app/rag/service.py (which still imports it as
# vectorstore.RELEVANCE_THRESHOLD) so retrieve_chunks below can use the same
# bar for its own-condition-first search, not just the off-topic check it was
# originally measured for.
#
# Measured live: off-topic questions ("capital of France", "15 times 34",
# "prime minister of India", "write me a poem") top out around 0.10-0.18
# similarity; real on-topic questions, even vague ones ("is walking good for
# me", "my joints hurt sometimes"), start around 0.38 and go up from there —
# a wide, clean gap. 0.25 sits in the middle of that gap with real margin on
# both sides.
RELEVANCE_THRESHOLD = 0.25


class UpstreamUnavailable(RuntimeError):
    """The LLM/embedding provider is rate-limited or down after retries.

    Distinct from a generic RuntimeError so callers (app/rag/service.py) can turn
    this into a proper 429 the client can act on, instead of it falling through to
    the generic 500 "Something went wrong" handler — free-tier OpenRouter models
    share capacity across everyone using them, so this is expected to happen
    occasionally, not a sign the app is broken.
    """


class ResponseTruncated(RuntimeError):
    """The model hit the max_tokens cap before naturally finishing its answer —
    a real, occasionally-observed free-model failure mode (a degenerate
    repetition loop that never reaches a natural stopping point). The response
    is cut off mid-thought, not a complete, valid answer — never usable as-is,
    whether that's for a real user or for evaluation."""


def embed_text(text: str, api_key: str, retries: int = 2) -> list[float]:
    body = json.dumps({"model": EMBEDDING_MODEL, "input": text}).encode()
    req = urllib.request.Request(
        EMBEDDING_URL,
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    # Found live (code audit): with the old retries=3/timeout=30 defaults, a
    # single request touching this function 1-2 times could add up with
    # chat_complete's own worst case (see that function's comment) to pin a
    # whole worker thread for 15-20+ minutes under real upstream slowness —
    # enough to exhaust the app's worker pool, not just degrade one chat
    # reply. Tightened defaults cut this function's own worst case from
    # ~93s to ~31s. This is a mitigation, not a real fix: there is still no
    # overall per-request deadline shared across the several sequential LLM
    # calls one /v1/rehab/query request can make — that needs a deadline
    # object threaded through every call site, a larger change than
    # tightening these two numbers.
    for attempt in range(retries):
        try:
            # req is built above from the hardcoded EMBEDDING_URL constant
            # (always https://openrouter.ai/...), never from request input —
            # a scanner flagging urlopen for scheme injection is a false
            # positive here specifically because nothing user-controlled
            # reaches this URL.
            with urllib.request.urlopen(req, timeout=15) as resp:  # nosec B310
                raw = resp.read()
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    # Found live (third-pass code audit): a malformed-but-200
                    # response body raised an uncaught JSONDecodeError here —
                    # neither HTTPError/TimeoutError/URLError below, so it
                    # propagated straight past every caller's UpstreamUnavailable
                    # handler instead of degrading gracefully like every other
                    # provider-misbehavior case in this function already does.
                    if attempt < retries - 1:
                        time.sleep(2 ** attempt)
                        continue
                    raise UpstreamUnavailable(
                        f"embedding provider returned a malformed response: {raw[:500]!r}")
                items = data.get("data")
                if not items or not items[0].get("embedding"):
                    detail = json.dumps(data)[:500]
                    if attempt < retries - 1:
                        time.sleep(2 ** attempt)
                        continue
                    raise UpstreamUnavailable(f"embedding provider returned no embedding: {detail}")
                return items[0]["embedding"]
        except urllib.error.HTTPError as e:
            # Found live (code audit): only 429 was ever treated as a
            # retryable, refundable infrastructure failure. A 5xx from the
            # provider, or a 401/403 from a misconfigured/revoked API key —
            # neither the user's fault either — raised a bare HTTPError
            # instead, which callers' `except UpstreamUnavailable` blocks
            # didn't catch: the user got a generic 500 AND their quota slot
            # (see app/rag/service.py's _consume_quota/_refund_quota) was
            # never refunded for an outage they had nothing to do with.
            retryable = e.code == 429 or e.code >= 500 or e.code in (401, 403)
            if retryable and attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            if retryable:
                raise UpstreamUnavailable(f"embedding provider failed: {e.code}") from e
            raise
        except (TimeoutError, urllib.error.URLError) as e:
            # A genuine timeout/connection failure — not an HTTP error response at
            # all, so it was never caught by the except above. Previously this
            # propagated straight past every caller's UpstreamUnavailable handler
            # and crashed the whole run instead of skipping one row.
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise UpstreamUnavailable(f"embedding provider unreachable: {e}") from e
    raise UpstreamUnavailable("embed_text: exhausted retries")


def chat_complete(messages: list[dict], api_key: str, model: str = CHAT_MODEL,
                   retries: int = 3, temperature: float = 0.2, max_tokens: int = 2000) -> str:
    """CHAT_MODEL (gpt-oss-20b) is a reasoning model — it spends tokens on an
    internal "reasoning" field before writing the final answer, and both count
    against max_tokens. 600 was tuned only for the final "short, plain-
    language" answer (system prompt rule 4) and left no room for reasoning,
    causing legitimate answers to get cut off before any content was written
    at all. 2000 leaves real headroom for both, while still being a hard
    ceiling nowhere near the ~8,000+ tokens the degenerate-repetition failure
    mode produced (a 32,768-character answer)."""
    body = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }).encode()
    req = urllib.request.Request(
        CHAT_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Title": "ViTAinspire",
        },
        method="POST",
    )
    # Found live (code audit): see embed_text's comment above — same class of
    # fix. Old retries=5/timeout=60 gave this function alone a worst case of
    # roughly 5-6 minutes; a single /v1/rehab/query request can call it
    # several times sequentially (condition classification, intent
    # classification, the main answer, memory summarization), which is what
    # actually produced the 15-20+ minute worst case. Tightened to ~90s per
    # call. Same caveat as embed_text: this narrows the blast radius, it
    # does not add a real overall per-request deadline.
    #
    # Found live (fourth-pass audit, deployment-config review): the Procfile's
    # gunicorn --timeout must stay comfortably above the realistic chained
    # worst case here (multiple ~90s calls back to back), or a single slow
    # chat reply gets the WHOLE worker process SIGKILLed on timeout — with
    # --workers 1, that drops every OTHER concurrent user's in-flight request
    # on the other threads too, not just the slow one. Set to 300s as a
    # deliberate middle ground: comfortably covers the realistic multi-call
    # chain, without going all the way to the rarer 15-20 minute pathological
    # tail (which would make gunicorn's timeout nearly useless for catching a
    # genuinely hung process for unrelated reasons). The real fix is still
    # the per-request deadline object this comment already asks for above —
    # this Procfile number is a mitigation, not a substitute for that.
    for attempt in range(retries):
        try:
            # Same false positive as embed_text's matching urlopen call —
            # req is built from the hardcoded CHAT_URL constant, never from
            # request input.
            with urllib.request.urlopen(req, timeout=25) as resp:  # nosec B310
                raw = resp.read()
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    # Same gap as embed_text (found live, third-pass code
                    # audit): a malformed-but-200 body raised an uncaught
                    # JSONDecodeError, which none of this function's callers'
                    # UpstreamUnavailable handlers caught either.
                    if attempt < retries - 1:
                        time.sleep(5 * (attempt + 1))
                        continue
                    raise UpstreamUnavailable(
                        f"chat model returned a malformed response: {raw[:500]!r}")
                choices = data.get("choices")
                if not choices:
                    # A 200 response with no "choices" — OpenRouter proxies many
                    # different backend providers, and some return an embedded
                    # error, an empty/moderated completion, or a differently-
                    # shaped body instead of raising an HTTP error status for it.
                    # Surface the actual body instead of a bare KeyError so the
                    # real cause (rate limit, moderation, provider outage) is
                    # visible, and treat it as retryable like a 429.
                    detail = json.dumps(data)[:500]
                    if attempt < retries - 1:
                        time.sleep(5 * (attempt + 1))
                        continue
                    raise UpstreamUnavailable(f"chat model returned no choices: {detail}")
                choice = choices[0]
                message = choice.get("message") or {}
                content = message.get("content")
                if choice.get("finish_reason") == "length":
                    # Hit the max_tokens cap before naturally finishing. Some
                    # models (this one included — "reasoning" models like
                    # gpt-oss) spend tokens on an internal "reasoning" field
                    # before writing the final answer — if the cap is hit
                    # during that phase, content is null/empty, which is NOT
                    # the same thing as the provider returning a broken empty
                    # response (checked further below); it means the model was
                    # still working (very likely a repetition loop) when cut
                    # off. One retry in case it was a one-off; a second
                    # truncation means give up on this row rather than accept
                    # an incomplete, unusable answer.
                    if attempt < retries - 1:
                        time.sleep(2)
                        continue
                    raise ResponseTruncated(
                        f"chat model hit max_tokens ({max_tokens}) without finishing — "
                        f"likely a repetition loop, not a valid answer"
                    )
                if not content:
                    detail = json.dumps(data)[:500]
                    raise UpstreamUnavailable(f"chat model returned empty content: {detail}")
                return content
        except urllib.error.HTTPError as e:
            # Found live (code audit): see embed_text's matching comment —
            # same fix. Only 429 used to be treated as retryable/refundable;
            # a 5xx or a 401/403 (e.g. a revoked API key) raised a bare
            # RuntimeError instead, silently burning the user's quota for an
            # outage that wasn't their fault.
            retryable = e.code == 429 or e.code >= 500 or e.code in (401, 403)
            if retryable and attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
                continue
            detail = e.read().decode(errors="replace")
            if retryable:
                raise UpstreamUnavailable(f"chat model failed: {e.code} {detail[:300]}") from e
            raise RuntimeError(f"chat_complete failed: {e.code} {detail[:300]}") from e
        except (TimeoutError, urllib.error.URLError) as e:
            # A genuine timeout/connection failure, not an HTTP error response —
            # previously uncaught here, so it propagated straight past
            # ragas_eval.py's per-row UpstreamUnavailable handler and would have
            # crashed the whole run on the first slow/dropped request instead of
            # skipping just that one row.
            if attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
                continue
            raise UpstreamUnavailable(f"chat model unreachable: {e}") from e
    raise UpstreamUnavailable("chat_complete: exhausted retries")


def vector_literal(values: list[float]) -> str:
    # pgvector's text input format: '[0.1,0.2,...]'
    return "[" + ",".join(repr(float(v)) for v in values) + "]"


# 5.8 — backend-only knowledge topics: real condition_code values in rag_chunks,
# tagged and stored exactly like gout or bursitis, but deliberately never added to
# the app's own `conditions` table, never offered during onboarding, and never
# given a safety_grid entry. A user can't select "frozen shoulder" as their
# condition, but if they ask about it, retrieval can still surface genuinely
# relevant reference material instead of nothing. The safety guarantee this app
# is built on is unaffected either way: exercise-naming is gated separately, in
# service.py's _approved_activities(), against the user's ACTUAL selected
# condition's real, human-reviewed safety grid — never against whatever chunk
# happened to be retrieved. Widening this list only changes what informational
# content the model can draw from, never what it's allowed to recommend.
EXTENDED_TOPICS = [
    # Wave 1 (17 topics, 2+ trusted sources each)
    "rheumatoid_arthritis", "ankylosing_spondylitis", "osteoporosis", "fibromyalgia",
    "sciatica", "scoliosis", "pagets_disease", "frozen_shoulder", "rotator_cuff",
    "tennis_elbow", "plantar_fasciitis", "neck_pain", "polymyalgia_rheumatica",
    "repetitive_strain_injury", "shin_splints", "trigger_finger", "whiplash",
    # Wave 2 (57 topics, AAOS OrthoInfo — filtered from a 215-page catalog down to
    # conservative-care, adult-relevant, non-emergency topics; tumors, pediatric-only
    # conditions, fractures/dislocations, and acute/surgical-emergency topics were
    # deliberately excluded as out of scope for this app)
    "arthritis_shoulder", "shoulder_labral_tear", "slap_tear", "shoulder_impingement",
    "calcific_tendinitis", "biceps_tendinitis", "biceps_tendon_tear",
    "thoracic_outlet_syndrome", "snapping_scapula", "golfers_elbow", "arthritis_elbow",
    "elbow_bursitis", "cubital_tunnel_syndrome",
    "arthritis_hand", "arthritis_wrist", "arthritis_thumb", "de_quervains",
    "dupuytrens_disease", "kienbocks_disease", "ganglion_cyst", "ulnar_tunnel_syndrome",
    "finger_sprain", "thumb_sprain", "wrist_sprain",
    "fai_hip", "hip_bursitis", "hip_strain", "snapping_hip", "meralgia_paresthetica",
    "hamstring_injury", "thigh_strain", "spondylolisthesis", "cervical_radiculopathy",
    "muscle_cramps",
    "arthritis_knee", "patellofemoral_arthritis", "patellofemoral_pain_syndrome",
    "acl_injury", "pcl_injury", "collateral_ligament_injury", "meniscus_tear",
    "bakers_cyst", "it_band_syndrome", "pes_anserine_bursitis", "prepatellar_bursitis",
    "arthritis_foot_ankle", "bunions", "hallux_rigidus", "claw_toe", "hammer_toe",
    "turf_toe", "mortons_neuroma", "sesamoiditis", "flatfoot_ptt_dysfunction",
    "achilles_tendinitis", "heel_pain", "ankle_sprain",
]


def _search_chunks(vec_str: str, codes: list[str], rag_db_url: str, top_k: int) -> list[tuple]:
    try:
        with psycopg.connect(rag_db_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select chunk_id, source_note, url, text, condition_code,
                           1 - (embedding <=> %s::vector) as similarity
                    from rag_chunks
                    where condition_code = any(%s)
                    order by embedding <=> %s::vector
                    limit %s;
                    """,
                    (vec_str, codes, vec_str, top_k),
                )
                return cur.fetchall()
    except psycopg.OperationalError as e:
        # Found live (third-pass code audit): an unreachable/down vector DB
        # (Supabase outage, network blip) raised this uncaught — the user got
        # a generic 500 instead of the same graceful "try again" response an
        # OpenRouter outage already gets, even though it's equally not their
        # fault.
        raise UpstreamUnavailable(f"vector store unreachable: {e}") from e


def retrieve_chunks(question: str, condition_codes: list[str], *, rag_db_url: str,
                     api_key: str, top_k: int = TOP_K) -> list[dict]:
    """Nearest chunks scoped to whichever of the user's selected conditions we have
    RAG content for, plus the general WHO physical-activity content as a baseline
    (5.2 — "the user's selected conditions", so this never searches conditions the
    user didn't report for anything SAFETY-relevant), plus EXTENDED_TOPICS — backend-
    only reference material that's always in scope regardless of the user's own
    conditions (5.8).

    Found live: a single flat search across the user's own condition(s) AND all
    ~70 EXTENDED_TOPICS ranked everything by similarity alone, with no priority
    for the user's own selected condition. A generically-worded question could
    out-rank the user's own condition's genuinely relevant content with an
    unrelated extended-topic chunk whose wording happened to match more closely
    — e.g. a carpal_tunnel user asking "what helps wrist pain from typing" got
    back golfer's elbow / ulnar tunnel syndrome chunks, nothing from
    carpal_tunnel, even though 19 relevant carpal_tunnel chunks exist. Fixed
    with a two-stage search: try the user's own condition(s) + general first,
    and only widen to EXTENDED_TOPICS if that top match isn't even reliably
    on-topic (same RELEVANCE_THRESHOLD bar app/rag/service.py already uses to
    tell a genuine answer from an off-topic one) — same embedding, so this
    never costs a second embedding call, only (occasionally) a second cheap
    local query.
    """
    vec_str = vector_literal(embed_text(question, api_key))
    own_codes = list(dict.fromkeys([*condition_codes, "general"]))

    rows = _search_chunks(vec_str, own_codes, rag_db_url, top_k)
    if not rows or float(rows[0][5]) < RELEVANCE_THRESHOLD:
        all_codes = list(dict.fromkeys([*own_codes, *EXTENDED_TOPICS]))
        rows = _search_chunks(vec_str, all_codes, rag_db_url, top_k)

    return [
        {"chunk_id": r[0], "source_note": r[1], "url": r[2], "text": r[3], "condition_code": r[4],
         "similarity": float(r[5])}
        for r in rows
    ]


def closest_topic(question: str, api_key: str, rag_db_url: str) -> tuple[str, float] | None:
    """The single closest chunk across the ENTIRE knowledge base — official
    conditions AND EXTENDED_TOPICS alike — with no condition_code scoping at all.

    Used as a sanity check against the disambiguation classifier (5.6), which can
    only ever pick from the closed official list and has no way to say "actually,
    something else you already know about is a much better fit" (5.9). This reuses
    the same embedding-based search already proven for retrieval, rather than
    relying on an LLM to reliably follow a nuanced "don't force a match" instruction."""
    vec_str = vector_literal(embed_text(question, api_key))
    try:
        with psycopg.connect(rag_db_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select condition_code, 1 - (embedding <=> %s::vector) as similarity
                    from rag_chunks
                    order by embedding <=> %s::vector
                    limit 1;
                    """,
                    (vec_str, vec_str),
                )
                row = cur.fetchone()
    except psycopg.OperationalError as e:
        raise UpstreamUnavailable(f"vector store unreachable: {e}") from e
    if row is None:
        return None
    return row[0], float(row[1])


SUMMARY_PROMPT = """Summarize ONE exchange from a rehab/pain-guidance chatbot, as strict JSON \
only — no markdown fences, no extra text before or after — with exactly these keys:

{{
  "summary": "one short, plain sentence: what the user asked and what they were told. \
Include any personal detail the user shared about themselves (e.g. their name), if any \
— that's the main thing that gives a later conversation real continuity",
  "exercises_mentioned": ["exact exercise names the answer named, if any — [] if none"],
  "pain_persisting": true or false — true ONLY if the user's OWN message says their pain is \
continuing, not improving, or getting worse despite something they already tried; false for a \
first-time question or one with no such signal
}}

User's question: {question}
Chatbot's answer: {answer}

Respond with ONLY the JSON object."""


def summarize_exchange(question: str, answer: str, api_key: str) -> dict | None:
    """Structured extraction for chat memory (5.7) — best-effort, never raises:
    a failure here must not break the real answer the user already received.
    Uses the same free model that generates answers, not the paid judge, since
    this runs on every question rather than a one-off evaluation. Lower
    retries than chat_complete's default: a failed summary is silently
    skipped, not worth the same retry budget as a failed real answer.

    max_tokens=1200, not a small number despite the tiny JSON output: this
    model (gpt-oss-20b) spends tokens on an internal "reasoning" field before
    writing content, same as chat_complete's own docstring already notes for
    real answers — 300 was entirely consumed by reasoning, so every call was
    silently hitting ResponseTruncated and summarize_exchange returned None
    every time (verified directly)."""
    try:
        raw = chat_complete(
            [{"role": "user", "content": SUMMARY_PROMPT.format(question=question, answer=answer)}],
            api_key, retries=2, temperature=0, max_tokens=1200,
        )
    except Exception:
        # Found live (code audit): this function's own docstring promises
        # "never raises — a failure here must not break the real answer the
        # user already received," but only two specific exception types were
        # actually caught. Any other failure (e.g. a non-retryable HTTPError,
        # a raw RuntimeError) propagated past this point, turning an
        # already-successful, already-quota-charged answer into a 500 the
        # user actually saw. Broad by design here, matching the documented
        # contract exactly — this is the one function meant to swallow
        # everything, not a place hiding an unrelated bug.
        return None

    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw[:4].lower() == "json":
            raw = raw[4:]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("summary"), str):
        return None

    return {
        "summary": data["summary"][:500],
        "exercises_mentioned": [x for x in (data.get("exercises_mentioned") or []) if isinstance(x, str)][:10],
        "pain_persisting": bool(data.get("pain_persisting")),
    }
