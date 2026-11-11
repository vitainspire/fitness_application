"""Module 5 — Rehab & Pain Guidance.

The safety boundary that shapes everything here: the model explains using only the
retrieved text handed to it. It may name a specific exercise ONLY if the reviewed
safety grid (13.3) has verdicted it "safe" for the user's condition — that closed,
human-reviewed list is fed into every prompt; the model never invents or picks one
outside it. If nothing approved fits the question, it falls back to general
self-care basics (gentle stretching, ice/heat, gentle movement, rest, sleep,
nutrition) rather than declining outright. It never names a medication, dose,
brand, or surgical procedure — those always go back to a doctor or pharmacist, and
that rule is enforced twice: once in the prompt, and again by a runtime keyword
check afterward (`_enforce_no_medication_or_surgery`), since a free-tier model
doesn't always obey a prompt instruction 100% of the time. Referred-bucket users
get the referral message and nothing else (13.3b), and never consume quota
getting it.

5.6 — a symptom description ("my lower back hurts") is not a condition. The model may
suggest a match from the closed list, but recording it always requires a separate,
explicit confirmation from the client (a tap), never the suggestion step itself.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import date, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.catalog.models import ActivityTaxonomy, Condition
from app.platform import redis_clients as rc
from app.platform.errors import Forbidden, QuotaExhausted, RateLimited, ValidationError
from app.profile.models import ActivityLog, UserCondition, UserProfile, UserProgram
from app.profile import service as profile_service
from app.profile.service import (
    IST, REFERRAL_MESSAGE, activity_log_date_bounds, activity_log_detail,
    complete_onboarding, is_referred, restore_program_activities,
    set_program_to_activities, switch_added_activity_types, today_ist, undo_switch_additions,
)
from app.relationships.service import is_managed_account
from app.progression import analysis as progression_analysis
from app.progression import service as progression_service
from app.rag import chat_messages, chat_prompts, chat_rules, exercise_table, new_flow, program_agent, router as chat_router, targets_answer, v2, vectorstore
from app.rag.models import RehabChatLog, RehabMemory, RehabQuota
from app.rag.vectorstore import ResponseTruncated, UpstreamUnavailable

_log = logging.getLogger(__name__)

QUOTA_TYPE = "rehab_llm"
# Lowered from 15 (explicit product decision): every message now counts
# unconditionally (see answer_query), closing the earlier loophole where
# some paths didn't touch this counter at all — 15 was sized against that
# leakier design, not against real per-message cost. 5 matches the number
# _refund_quota's own docstring already referenced.
DAILY_LIMIT = int(os.environ.get("REHAB_DAILY_LIMIT", "5") or 5)   # the product limit is 5; REHAB_DAILY_LIMIT only raises it for local testing
VOICE_QUOTA_TYPE = "voice_llm"
VOICE_DAILY_LIMIT = 60

# Found live (third-pass code audit): escalation_confirmation is deliberately
# exempt from DAILY_LIMIT above (confirming a suggestion is a UI action, not a
# new question — see _confirm_escalation's docstring), but that also meant it
# had NO cap of its own — every confirmed:true tap triggers a real, billed
# analyze_performance call (progression/analysis.py), and a client can replay
# that request as many times as it likes. This is a separate counter (its own
# quota_type row) precisely so it doesn't eat into a user's 5 real questions —
# it exists purely to bound raw request volume, not to be felt by normal use
# (nobody legitimately re-confirms the same or even several offers 10+ times
# in a day).
ESCALATION_QUOTA_TYPE = "escalation_confirm"
ESCALATION_DAILY_LIMIT = 10

MAX_QUESTION_LENGTH = 500
MIN_LENGTH_WORTH_CLASSIFYING = 15  # skip the extra LLM call for "hi"-length messages

# How long a "you were asked which condition, and already paid for that turn"
# marker stays redeemable (see rc.key_pending_condition_confirm). Long enough
# for a user to read the options and tap one; far too short to bank a free
# answer for later.
PENDING_CONFIRM_TTL_SECONDS = 15 * 60

SYSTEM_PROMPT = """You are a rehab and pain guidance assistant inside a beginner-level \
fitness app for adults in India. For medical/exercise questions, answer using ONLY the \
"Retrieved information" provided in each message — never add outside medical knowledge, \
and never guess if the retrieved information doesn't cover the question.

Separately, you may also be given past conversation context from this SAME user — a \
short list of older, still-relevant notes (e.g. a persisting symptom, an exercise \
already discussed), plus their most recent few exchanges in full (their real questions \
and your real answers). A simple personal-continuity question — like "what's my \
name?" — should be answered directly from that context, even though it isn't \
"Retrieved information" and rule 4 below doesn't apply to it. Only medical/exercise \
content is restricted to the retrieved text; recalling what the user themselves \
already told you, or what you already told them, is not.

Strict rules, no exceptions:
1. You may name a specific exercise ONLY if it appears in the "Approved exercises for \
this condition" list given to you in the message. That list comes from a separate, \
reviewed safety system — never invent, pick, or suggest an exercise outside it, even \
if the retrieved text names one. If none of the approved exercises fit the question, \
or the list says none are listed, do not just decline — instead offer general \
self-care basics (e.g. gentle stretching, applying ice or heat, gentle movement, \
rest, adequate sleep, balanced nutrition) without naming any specific unlisted \
exercise. The approved list is annotated with what each exercise actually helps \
with, in parentheses. When the question is about a specific sport or physical \
activity (e.g. "what helps me play football/kabaddi/cricket"), reason about what \
THAT activity genuinely demands (endurance, agility, explosive power, joint \
stability, flexibility, grip, balance, etc.) and name ONLY the approved exercises \
whose noted purpose actually supports that demand — never default to listing the \
whole approved set regardless of fit. Naming an approved exercise that doesn't \
really help this specific activity is not safer than naming fewer exercises; it's \
just a worse answer.
2. Never name a specific medication, drug class, dose, or brand, even if one appears in \
the retrieved text. If the question is about medication, say this is something to \
discuss with a doctor or pharmacist, and stop there.
3. Never name a specific surgical procedure or intervention (e.g. laminectomy, spinal \
fusion, discectomy, carpal tunnel release), even if one appears in the retrieved \
text. If the question is about surgery, say this is something to discuss with a \
doctor, and stop there.
4. If the retrieved information doesn't answer the question, say so plainly and suggest \
seeing a doctor — do not fill the gap with general knowledge.
5. If asked which exercises to avoid, do NOT claim that everything not on the approved \
list must be avoided — the safety grid may rate other exercises as usable with a \
modification, which this prompt is never given, so that claim would be false. Say \
plainly that specific avoid-guidance isn't something you have, and point to the app's \
own program screen or a doctor/physiotherapist instead.
6. Keep answers short, plain-language, and calm. This is a general-audience app, not a \
clinical tool. Format with short paragraphs and, when listing several exercises, a plain \
bulleted list ("- Exercise: reason") — never a markdown table. The chat screen this \
renders in only understands bold text and bullet lists; a table shows up as broken, \
literal pipe characters, not an actual table.
7. Do not add any "this is general information, not medical advice"-style disclaimer. \
Just answer directly.
8. Past conversation context is never a source of new MEDICAL guidance — if it \
mentions a medication, exercise, or symptom, rules 1-5 above still apply in full; it \
only ever supplies personal continuity (like a name or an earlier symptom), never a \
fact that changes what you're allowed to say medically.
9. You may separately be given "Program data" — the app's own deterministic tracking \
of this user's logged activity and progress for one exercise (adherence, streak, \
whether they've plateaued). This is not retrieved medical text and rule 4 does not \
apply to it: use it directly to answer a progress/performance question like "how am \
I doing" or "can I do more this week." If it includes an escalation_proposal with \
eligible=true, you may mention it as a possible next step and ask if they'd like to \
try it — never say you've changed their program; only the app's own confirmation \
screen does that. If a finding says discomfort isn't tracked, that is not the same as \
"no discomfort" — never rephrase it as an all-clear.
10. You may separately be given "Actual logged activity" — the literal, itemized exercise \
entries the app recorded for a specific day or range, straight from the user's own log. \
This is not retrieved medical text and rule 4 does not apply to it: use it directly to \
answer a question like "what did I do yesterday." List only what is actually in that \
block — never add an exercise, set, or amount that isn't there, and if it says nothing \
was logged, say so plainly rather than guessing. This block is freshly computed THIS \
turn and is always more current than anything said in past conversation context — if an \
earlier exchange stated a different date or a different set of exercises for the same \
question, that earlier answer was based on incomplete or since-corrected data. Always \
follow the Actual logged activity block given now, never repeat what an earlier answer said.
11. After your whole answer, on a NEW final line, add exactly one line in this exact \
format and nothing else on it: SUGGESTED_EXERCISES: <name>, <name> — or \
SUGGESTED_EXERCISES: NONE. List an exercise there ONLY if your answer is actually \
recommending the user add it to their program as a specific action to take right now \
— names must come only from the approved list, exactly as written there. Do NOT list an \
exercise just because you mentioned it in passing as one example of general self-care \
(e.g. "gentle movement such as walking" mentions walking without recommending it — that \
does not belong on this line). When your answer gave general self-care advice instead of \
naming a specific exercise (rule 1), or discussed medication/surgery (rules 2-3), or said \
the information isn't available (rule 4), always write SUGGESTED_EXERCISES: NONE — those \
answers are never a specific program suggestion, no matter what words appear in them. \
This line is read by the app, not shown to the user — it must still appear even though \
it isn't part of the natural-language answer.
"""

CONDITION_MATCH_PROMPT = """A user of a fitness/rehab app wrote a message describing pain \
or discomfort. They have ALREADY told the app about: {existing}.

Decide: does the message describe a DIFFERENT problem than what's already known, that \
clearly matches one of these NOT-yet-selected conditions?

{options}

Message: "{question}"

Reply with exactly NONE if the message is a general or follow-up question that could \
reasonably relate to a condition they've already told the app about — most back/spine \
questions from a user who already reported a back/spine condition fall here, even if \
worded generally. Only reply with one of the codes above if the message clearly points \
to a genuinely different, not-yet-reported problem. This is only a suggestion the user \
confirms or declines afterward, so when it's a close call between "already covered" and \
"new," prefer NONE — the point is to catch clearly new complaints, not to relabel a \
question about something already known.

Also reply with exactly NONE if the message describes a body part or symptom that \
doesn't genuinely match any of the listed options well — do NOT force it onto the \
closest-sounding one just because it's the nearest available choice. For example, a \
shoulder complaint should get NONE, not "bursitis," if bursitis isn't actually what the \
message describes — being offered the wrong condition to confirm is worse than being \
offered none at all.

Reply with ONLY the exact code or exactly NONE, nothing else.
"""

NO_CONTENT_ANSWER = (
    "I don't have verified information for this yet. Please speak to a doctor about "
    "this question."
)

# Found live: retrieve_chunks is a nearest-neighbor search — it always returns
# its top_k rows no matter how irrelevant the question is, there's no such
# thing as "no results." An off-topic question ("what is the capital of
# France") still got real chunks back, and the model was left to notice on
# its own that nothing retrieved actually answers it (rule 4 of SYSTEM_PROMPT)
# — the same "don't trust the model's compliance" lesson already learned for
# medication/surgery/exercise-naming, just for topic relevance instead.
# Threshold itself now lives in vectorstore.RELEVANCE_THRESHOLD — retrieve_chunks
# uses the same number for its own-condition-first search (see its docstring).

SAFETY_FALLBACK_ANSWER = (
    "I'm not able to provide a specific medication or surgery name — that's something "
    "to discuss directly with a doctor or pharmacist."
)

NO_APPROVED_EXERCISE_ANSWER = (
    "I don't have a specific exercise I can safely recommend for this — please speak to "
    "a doctor about what's appropriate for you."
)

# Canonical lists for the runtime backstop below AND for tests/test_rag_safety.py
# (imported from there, not duplicated) — the prompt rules above ask the model not
# to say these, but a free-tier model doesn't always obey a prompt instruction, so
# this is checked again in code after the model answers.
MEDICATION_KEYWORDS = [
    "allopurinol", "febuxostat", "probenecid", "colchicine", "pegloticase",
    "nsaid", "ibuprofen", "naproxen", "acetaminophen", "aspirin", "paracetamol",
    "corticosteroid", "codeine", "tramadol", "gabapentin", "opioid", "diuretic",
    # Brand/generic names confirmed present in the wave-2 AAOS source content
    # (ligament/knee pages) — found live: "nsaid" alone doesn't catch a specific
    # brand name, same class of gap as the surgery-suffix one below.
    "celebrex", "celecoxib", "mobic", "meloxicam", "voltaren", "diclofenac",
    "tylenol", "advil", "motrin", "aleve", "cortisone",
]
# Generic (non-brand) drug names mostly aren't random — WHO assigns each drug
# class a shared naming "stem" (this is a real pharmaceutical convention, not
# something invented for this filter). Checking these catches drugs we've
# never explicitly listed, the same way SURGERY_SUFFIXES below catches
# procedure names we've never listed — a fixed brand/generic list is always
# one step behind. Deliberately scoped to stems for pain/inflammation drug
# classes actually relevant to this app's domain (NSAIDs, COX-2 inhibitors,
# corticosteroids, local anesthetics) rather than every stem that exists
# (e.g. skipping blood-pressure or cholesterol drug stems), to keep the list
# meaningful instead of just large.
MEDICATION_SUFFIXES = ["profen", "fenac", "coxib", "olone", "caine"]

# A specific medication DOSE AMOUNT is itself a rule-2 violation ("never name
# a... dose") even when no drug name is said at all — "take 400mg" is
# prescribing information regardless of which drug. Deliberately just the
# unit-based amount (mg/mcg/ml), NOT frequency phrases like "twice a day" or
# "every 2 hours" — those are also completely normal, desired language for
# exercise and self-care instructions this app WANTS to give ("do this
# stretch twice a day," "apply ice every 2 hours"), so including them would
# false-positive on exactly the safe advice this app exists to provide.
DOSAGE_PATTERN = re.compile(
    r"\b\d+\s?(mg|mcg|milligrams?|micrograms?|ml|milliliters?|millilitres?)\b",
)

SURGERY_KEYWORDS = [
    "laminectomy", "discectomy", "spinal fusion", "microdiscectomy",
    "decompression", "decompressive",
    "arthroscopy", "arthroscopic", "arthroplasty", "carpal tunnel release",
    "fasciotomy", "reconstruction", "open surgery", "surgical repair",
    "knee replacement", "hip replacement", "joint replacement", "replacement surgery", "surgery", "surgical",
]
# Broader net than an exact-name list can ever be: common surgical-procedure
# suffixes catch specific procedures across all 84 conditions without having to
# enumerate every one by hand (meniscectomy, osteotomy, tenotomy, arthrodesis,
# tenodesis, synovectomy, bunionectomy...) — found live that "meniscectomy" and
# "arthroscopic repair" both leaked through the old exact-name-only list, and a
# fixed list will always be one step behind whatever the model actually says
# next. A false positive here just means a clean decline instead of a real
# answer; a false negative means a surgery name reaching a real user — the
# tradeoff clearly favors casting wide.
SURGERY_SUFFIXES = ["ectomy", "otomy", "oplasty", "odesis", "orrhaphy"]


def _enforce_no_medication_or_surgery(answer: str) -> str:
    """Runtime backstop for prompt rules 2-3: a free-tier model occasionally names a
    medication or surgical procedure despite being told not to (verified directly —
    the same question phrased two ways produced one compliant and one non-compliant
    answer, and separately confirmed live for meniscus/rotator-cuff surgery names
    once the knowledge base grew to 84 conditions). Rather than try to redact just
    the offending phrase from an otherwise coherent answer, replace the whole thing
    — a partially-redacted medical answer is worse than a clean decline."""
    lowered = answer.lower()
    has_medication = (
        any(kw in lowered for kw in MEDICATION_KEYWORDS)
        or any(re.search(rf"\w*{suffix}\b", lowered) for suffix in MEDICATION_SUFFIXES)
        or DOSAGE_PATTERN.search(lowered)
    )
    has_surgery = (
        any(kw in lowered for kw in SURGERY_KEYWORDS)
        or any(re.search(rf"\w*{suffix}\b", lowered) for suffix in SURGERY_SUFFIXES)
    )
    if has_medication or has_surgery:
        return SAFETY_FALLBACK_ANSWER
    return answer


ACTIVITY_CATEGORIES = {
    # Coarse movement-type tags for the app's own exercise catalog — product
    # judgment, not clinically reviewed, same convention as the safety grid
    # itself. Found live: a sport question ("what helps with kabaddi") got
    # back nearly the user's ENTIRE approved list with a generic bridging
    # sentence, not a real filtered answer, because the model had no signal
    # to reason about which approved exercise actually fits a given sport's
    # demands. These tags are that signal — see SYSTEM_PROMPT rule 1.
    "Walking": "endurance/low-impact cardio",
    "Running": "endurance/cardio",
    "Glute Bridge": "hip and glute strength",
    "Dead Bug": "core stability",
    "Ankle Circles": "ankle mobility/joint stability",
    "Wrist Circles": "wrist mobility/joint stability",
    "Shoulder Circles": "shoulder mobility/joint stability",
    "Neck Hold": "neck strength/stability",
    "Seated Hamstring Stretch": "hamstring flexibility",
    "Side-Lying Thigh Stretch": "thigh/quad flexibility",
    "Wall Calf Stretch": "calf flexibility",
    "Seated Side Stretch": "torso/side flexibility",
}


def _avoided_activity_types(session: Session, condition_codes: list[str]) -> set[str]:
    """Exercises that must NOT be offered for these conditions, from the AUDITED 794-row table for this exact set (owner rule: that table everywhere).
    A set with no audited row has NO audited exercises, so every exercise is avoided (fail closed). The older per-condition grid no longer exists."""
    if not condition_codes:
        return set()
    audited = profile_service.audited_verdicts_for_codes(session, condition_codes)
    if audited is None:
        return {a for (a,) in session.execute(select(ActivityTaxonomy.activity_type)).all()}
    return {a for a, v in audited.items() if v == "avoid"}


def _approved_activities(session: Session, condition_codes: list[str]) -> list[str]:
    """Display names of the exercises the AUDITED table marks `safe` for this exact set of conditions: the ONLY exercises the prompt may let the
    model name. Everything else (modify, avoid, or a set with no audited row) is excluded; the model falls back to generic self-care guidance."""
    if not condition_codes:
        return []
    audited = profile_service.audited_verdicts_for_codes(session, condition_codes)
    if audited is None:
        return []
    safe = [a for a, v in audited.items() if v == "safe"]
    return sorted(n for (n,) in session.execute(
        select(ActivityTaxonomy.display_name).where(ActivityTaxonomy.activity_type.in_(safe))).all())


def _approved_activity_types(session: Session, condition_codes: list[str]) -> set[str]:
    """Same as _approved_activities, but activity_type codes: used to re-validate a client-supplied list of activities against what the audited table
    approves RIGHT NOW (re-verify, never trust a possibly-stale list the client echoes back) before set_program_to_activities touches the program."""
    if not condition_codes:
        return set()
    audited = profile_service.audited_verdicts_for_codes(session, condition_codes)
    if audited is None:
        return set()
    return {a for a, v in audited.items() if v == "safe"}


def _hidden_by_sport_focus_change(session: Session, user_id: str) -> list[dict]:
    """Which of this user's program entries got hidden by a PREVIOUS
    sport-suggestion confirmation (see set_program_to_activities) — the
    candidate list for the chat's "revert what I did previously?" flow
    (found live: a user wanted to undo a sport-focus change by asking the
    chatbot directly, not by manually re-adding each exercise on the
    dashboard).

    Deliberately scoped to suspended_reason="sport_focus_change" only, never
    "user_removed" or "condition_safety" — a manual removal was the user's
    own deliberate choice through a DIFFERENT action (remove_program_entry),
    which already has its own recovery path (the dashboard's own Add-exercise
    button); and a condition-safety suspension already auto-resurrects on its
    own once the blocking condition is removed (build_program). Conflating
    either of those here would silently offer to undo a choice — or a safety
    decision — the user never made through this specific chat flow at all.
    """
    rows = session.execute(
        select(UserProgram.activity_type, ActivityTaxonomy.display_name)
        .join(ActivityTaxonomy, ActivityTaxonomy.activity_type == UserProgram.activity_type)
        .where(
            UserProgram.user_id == user_id,
            UserProgram.status == "suspended",
            UserProgram.suspended_reason == "sport_focus_change",
        )
    ).all()
    return [{"activity_type": a, "display_name": d} for a, d in rows]


def _revert_candidates(session: Session, user_id: str) -> list[dict]:
    """Everything "undo my changes" would change, each with what it does: action "restore" (a Switch paused it: bring it back) or
    "pause" (a Switch put it in the plan: put it back to what it was before - removed again, or paused). An exercise a switch both added
    and a later switch paused counts as "pause", because the plan from before the switches did not have it."""
    added = set(switch_added_activity_types(session, user_id))
    restore = [{**h, "action": "restore"} for h in _hidden_by_sport_focus_change(session, user_id) if h["activity_type"] not in added]
    pause = []
    if added:
        names = dict(session.execute(select(ActivityTaxonomy.activity_type, ActivityTaxonomy.display_name).where(ActivityTaxonomy.activity_type.in_(added))).all())
        pause = [{"activity_type": a, "display_name": names.get(a, a), "action": "pause"} for a in sorted(added)]
    return restore + pause


def _enforce_approved_exercises_only(session: Session, answer: str,
                                      approved: list[str]) -> tuple[str, bool]:
    """Runtime backstop for prompt rule 1 (found live, third-pass code audit): the
    module's own docstring calls this the CENTRAL safety boundary — a free-tier
    model doesn't always obey a prompt instruction 100% of the time (the same
    justification _enforce_no_medication_or_surgery above already documents and
    guards for rules 2-3), but until now nothing checked rule 1 the same way.

    Unlike the medication/surgery check, a name match here doesn't call for a
    generic decline: the user asked a real, answerable question, and we already
    know exactly what WOULD have been safe to recommend (the same `approved` list
    the prompt itself was built from) — so this substitutes a specific, useful
    alternative instead of a dead end."""
    lowered = answer.lower()
    approved_lower = {a.lower() for a in approved}
    catalog = session.execute(select(ActivityTaxonomy.display_name)).scalars().all()
    offending = next(
        (name for name in catalog
         if _mentions(lowered, name.lower()) and name.lower() not in approved_lower),
        None,
    )
    if offending is None:
        return answer, False
    if not approved:
        # No exercise at all is approved for this user's condition(s) — dead-end,
        # since there's no safe alternative to offer instead. Found live (this
        # session): this used to return SAFETY_FALLBACK_ANSWER, whose wording is
        # specifically about medication/surgery — confusing and just plain wrong
        # to show someone who asked an exercise question and got a mismatched
        # canned response about a topic they never raised.
        return NO_APPROVED_EXERCISE_ANSWER, True
    alternatives = ", ".join(approved)
    return (
        f"I can't recommend that exercise for your condition — it isn't on your approved "
        f"exercise list. Exercises that ARE approved for you: {alternatives}. Ask me about "
        f"any of these instead."
    ), True


def _mentions(lowered_text: str, lowered_name: str) -> bool:
    """Word-boundary containment, not a bare substring test. Found live (code
    audit): plain `name in text` matches inside an unrelated longer word and,
    more importantly, matches inside a NEGATION ("avoid running for now") just
    as happily as a genuine mention — this doesn't fix the negation case (that
    needs real language understanding, not a string check), but it does close
    the cheaper false-positive of a name matching as a substring of something
    else entirely."""
    return re.search(rf"\b{re.escape(lowered_name)}\b", lowered_text) is not None


SUGGESTED_EXERCISES_MARKER = re.compile(
    r"\n*SUGGESTED_EXERCISES:\s*(.*?)\s*$", re.IGNORECASE | re.DOTALL,
)


def _extract_suggested_exercises(session: Session, raw_answer: str,
                                  approved: list[str]) -> tuple[str, list[dict]]:
    """Splits rule 11's machine-readable marker line off the end of the raw
    model output — returns (visible_answer_with_marker_removed, suggestions).

    Found live (real user report, this session): the previous approach
    (_named_approved_activities, now removed) guessed what the model had
    "suggested" by scanning the finished prose for approved exercise names
    appearing anywhere in it. A user asked about tight hamstrings, the model
    correctly gave generic self-care advice ("...low-impact activities such
    as walking to maintain circulation...") without recommending any specific
    exercise — but the word "walking" was still a literal match, so the app
    offered a one-tap "suggestion" chip for it anyway. Confirming it invoked
    the "switch program focus" behavior (set_program_to_activities, see
    _confirm_program_update's own docstring) and hid the user's entire
    program down to just Walking, over a passing mention in an unrelated
    sentence, not anything the user or the model actually asked to change.
    No amount of smarter text-scanning closes this reliably — the model
    already KNOWS whether it's making a recommendation, so rule 11 now asks
    it to say so directly and unambiguously, on its own dedicated line,
    instead of the app trying to infer intent from prose after the fact.

    Fails CLOSED: a missing or malformed marker line (a free-tier model
    doesn't always follow instructions) yields zero suggestions rather than
    falling back to the old prose-scanning behaviour — silently offering no
    "add to program" button is a minor missed convenience; silently offering
    one based on a misread is a safety-relevant mistake, per the incident
    above. Every name is still re-validated against the approved list here
    (case-insensitively) and, again, against the CURRENT approved list at
    confirmation time (_confirm_program_update) — this function only trims
    the model's claim down to what it's allowed to claim, never trusts it."""
    match = SUGGESTED_EXERCISES_MARKER.search(raw_answer)
    if match is None:
        return raw_answer, []
    visible = raw_answer[:match.start()].rstrip()
    claimed = [n.strip() for n in match.group(1).split(",") if n.strip()]
    if not claimed or claimed == ["NONE"]:
        return visible, []
    approved_lower = {a.lower(): a for a in approved}
    valid_names = {approved_lower[c.lower()] for c in claimed if c.lower() in approved_lower}
    if not valid_names:
        return visible, []
    rows = session.execute(
        select(ActivityTaxonomy.activity_type, ActivityTaxonomy.display_name)
        .where(ActivityTaxonomy.display_name.in_(valid_names))
    ).all()
    return visible, [
        {"activity_type": activity_type, "display_name": display_name}
        for activity_type, display_name in rows
    ]


# --- Cross-session chat memory (5.7) ----------------------------------------

RECENT_FULL_LIMIT = 5  # the most recent exchanges get fed back in FULL (real
# question + real answer, from rehab_chat_log) — a fixed, small horizon, not a
# token-budget calculation; more directly costs tokens against the free
# model's per-request budget.
PAIN_PERSISTING_THRESHOLD = 3  # this many "pain still there" reports for the SAME
# condition, and the app says so itself in code — never left for the model to
# notice a pattern spread across separate days/sessions on its own.

DOCTOR_ESCALATION_NOTE = (
    "\n\nA note from the app: you've mentioned this pain isn't improving on {n} separate "
    "occasions now. At this point, please see a doctor rather than continuing to manage "
    "it through this chat."
)


def _recent_full_exchanges(session: Session, user_id: str,
                           limit: int = RECENT_FULL_LIMIT) -> list[RehabChatLog]:
    rows = session.scalars(
        select(RehabChatLog).where(RehabChatLog.user_id == user_id)
        .order_by(RehabChatLog.created_at.desc()).limit(limit)
    ).all()
    return list(reversed(rows))  # oldest first — reads like a timeline


MAX_OLDER_MEMORY = 30  # queue cap — beyond this, the oldest important memories
# stop being pulled into the prompt (they stay in the table, just out of
# context). Without a cap this query has no LIMIT at all: a long-running
# account's prompt grows every single day it chats, and eventually exceeds
# the model's context window, permanently failing every future message for
# that user. 30 is generous (~6 days at the 5/day quota, most of them
# unlikely to be "important") while keeping prompt size bounded for good.


def _older_important_memory(session: Session, user_id: str,
                            recent_limit: int = RECENT_FULL_LIMIT) -> list[RehabMemory]:
    """Everything BEYOND the most recent full exchanges (those are covered in full
    by _recent_full_exchanges above), filtered down to what's actually worth
    remembering long-term: a persisting-pain report, or an exchange that named an
    approved exercise. A routine informational question ("what causes gout")
    carries nothing forward for a future exchange, so it's deliberately left out
    — keeps this block naturally small instead of growing without bound as an
    account accumulates history, with no extra LLM call needed to compress it."""
    # Over-fetch before filtering (some rows in this page won't be "important"),
    # then hard-cap the filtered result — the actual bound on what reaches the
    # prompt is MAX_OLDER_MEMORY, not however many rows happen to match here.
    rows = session.scalars(
        select(RehabMemory).where(RehabMemory.user_id == user_id)
        .order_by(RehabMemory.created_at.desc())
        .offset(recent_limit)
        .limit(MAX_OLDER_MEMORY * 4)
    ).all()
    important = [r for r in rows if r.pain_persisting or r.exercises_mentioned][:MAX_OLDER_MEMORY]
    return list(reversed(important))


def _memory_context_block(session: Session, user_id: str) -> str:
    older = _older_important_memory(session, user_id)
    recent = _recent_full_exchanges(session, user_id)
    if not older and not recent:
        return "Past conversation: none yet — this is the first exchange."
    parts = []
    if older:
        lines = "\n".join(f"- {m.summary}" for m in older)
        parts.append(f"Earlier history worth remembering:\n{lines}")
    if recent:
        turns = "\n\n".join(f"Q: {r.question}\nA: {r.answer}" for r in recent)
        parts.append(f"Most recent exchanges, in full:\n{turns}")
    return "\n\n".join(parts)


def _pain_persisting_count(session: Session, user_id: str, condition_codes: list[str]) -> int:
    """How many PAST exchanges (not counting this one) reported persisting/worsening
    pain for any of these conditions — scoped per condition so an old, resolved gout
    flare doesn't get conflated with a new, separate back problem."""
    if not condition_codes:
        return 0
    return session.scalar(
        select(func.count()).select_from(RehabMemory).where(
            RehabMemory.user_id == user_id,
            RehabMemory.pain_persisting.is_(True),
            RehabMemory.condition_codes.overlap(condition_codes),
        )
    ) or 0


def _save_memory(session: Session, user_id: str, condition_codes: list[str],
                  question: str, answer: str, api_key: str) -> dict | None:
    """Best-effort: summarize this exchange and store it. Never raises — a failure
    here must not break the real answer the user already received."""
    extracted = vectorstore.summarize_exchange(question, answer, api_key)
    if extracted is None:
        return None
    session.add(RehabMemory(
        user_id=user_id, condition_codes=condition_codes,
        summary=extracted["summary"], exercises_mentioned=extracted["exercises_mentioned"],
        pain_persisting=extracted["pain_persisting"],
    ))
    session.flush()
    return extracted


CHAT_HISTORY_LIMIT = 200  # a generous, bounded cap — "keep the whole conversation
# forever" without an unbounded query/payload as an account accumulates years
# of exchanges; far beyond what the 15/day quota lets anyone reach quickly.


def _save_chat_log(session: Session, user_id: str, question: str, answer: str) -> None:
    """The actual transcript, kept indefinitely per account (not per day, not per
    login) so the chat screen has something real to redraw on login — plain DB
    write, no LLM call involved, so it's not subject to the free-tier's flakiness
    the way _save_memory's summarization is."""
    session.add(RehabChatLog(user_id=user_id, question=question, answer=answer))
    session.flush()


def _require_not_managed(session: Session, user_id: str) -> None:
    """A managed account (child/student/patient on someone else's plan) has
    no business talking to the rehab chatbot on their own — the plan is
    built and monitored by whoever manages them, same reasoning as
    app.profile.service.exercise_library's own guard."""
    if is_managed_account(session, user_id):
        raise Forbidden("Ask the person managing your plan about this.")


def chat_history(session: Session, user_id: str, limit: int = CHAT_HISTORY_LIMIT) -> list[dict]:
    _require_not_managed(session, user_id)
    rows = session.scalars(
        select(RehabChatLog).where(RehabChatLog.user_id == user_id)
        .order_by(RehabChatLog.created_at.desc()).limit(limit)
    ).all()
    return [
        {"question": r.question, "answer": r.answer, "created_at": r.created_at.isoformat()}
        for r in reversed(rows)  # oldest first, exactly how a transcript reads
    ]


def _reset_at_iso() -> str:
    """Midnight IST — the start of the next quota window (5.4, single-market IST per 3.3)."""
    tomorrow = (datetime.now(IST) + timedelta(days=1)).date()
    return datetime.combine(tomorrow, datetime.min.time(), tzinfo=IST).isoformat()


def remaining_quota(session: Session, user_id: str) -> dict:
    _require_not_managed(session, user_id)
    row = session.get(RehabQuota, (user_id, QUOTA_TYPE, today_ist()))
    used = row.count if row else 0
    return {
        "limit": DAILY_LIMIT,
        "used": used,
        "remaining": max(0, DAILY_LIMIT - used),
        "resets_at": _reset_at_iso(),
        # So the chat UI can show the referral message up front instead of
        # inviting a referred user to type into a box that will just reject them
        # (13.3b) — this is a courtesy for that screen, not itself the safety gate;
        # answer_query() below enforces the real block server-side regardless.
        "referred": is_referred(session, user_id),
        "referral_message": REFERRAL_MESSAGE if is_referred(session, user_id) else None,
    }


def _consume_quota(session: Session, user_id: str, *,
                    quota_type: str = QUOTA_TYPE, limit: int = DAILY_LIMIT) -> bool:
    """5.4 — atomic increment-if-under-limit.

    A plain read-then-write would let two concurrent requests both read count=4 and
    both proceed. The UPDATE's WHERE clause makes the check and the increment one
    atomic operation: only a request that actually flips the row through 4->5 gets
    to say it was allowed. Row updated -> allow. No row updated -> block.

    quota_type/limit are parameterized (found live, third-pass code audit) so the
    same atomic-counter machinery can back a SEPARATE daily cap for escalation
    confirmations (see ESCALATION_QUOTA_TYPE below) without duplicating this logic.
    """
    today = today_ist()
    session.execute(
        pg_insert(RehabQuota)
        .values(user_id=user_id, quota_type=quota_type, local_date=today, count=0)
        .on_conflict_do_nothing(index_elements=["user_id", "quota_type", "local_date"])
    )
    result = session.execute(
        update(RehabQuota)
        .where(
            RehabQuota.user_id == user_id,
            RehabQuota.quota_type == quota_type,
            RehabQuota.local_date == today,
            RehabQuota.count < limit,
        )
        .values(count=RehabQuota.count + 1)
    )
    return result.rowcount > 0


def _refund_quota(session: Session, user_id: str, *, quota_type: str = QUOTA_TYPE) -> None:
    """Undoes _consume_quota's increment when the reserved slot went to waste on
    an infrastructure failure (the LLM provider being rate-limited), not on the
    user asking something we couldn't answer. A user should not lose one of
    their 5 daily questions because OpenRouter's free tier was briefly overloaded
    — a real, recurring situation, not a rare edge case (see PART2 chat log)."""
    today = today_ist()
    session.execute(
        update(RehabQuota)
        .where(
            RehabQuota.user_id == user_id,
            RehabQuota.quota_type == quota_type,
            RehabQuota.local_date == today,
            RehabQuota.count > 0,
        )
        .values(count=RehabQuota.count - 1)
    )


def _condition_options(session: Session, user_id: str) -> list[dict]:
    """Supported conditions the user hasn't already selected — the only things
    it's meaningful to suggest (3.2's closed list, never anything else)."""
    existing = set(session.scalars(
        select(UserCondition.condition_code).where(UserCondition.user_id == user_id)
    ).all())
    rows = session.execute(
        select(Condition.condition_code, Condition.display_name)
        .where(Condition.bucket == "supported", Condition.condition_code != "none")
        .order_by(Condition.display_order)
    ).all()
    return [{"code": c, "label": n} for c, n in rows if c not in existing]


def _existing_condition_labels(session: Session, user_id: str) -> list[str]:
    rows = session.execute(
        select(Condition.display_name)
        .join(UserCondition, UserCondition.condition_code == Condition.condition_code)
        .where(UserCondition.user_id == user_id, Condition.condition_code != "none")
    ).all()
    return [r[0] for r in rows]


EXTENDED_TOPIC_OVERRIDE_THRESHOLD = 0.35  # a starting heuristic, not empirically
# tuned — raise it if this starts overriding genuine official-condition matches
# too eagerly, lower it if it's letting obvious extended-topic mismatches through.


def _demote_if_extended_topic_fits_better(question: str, matched_code: str, *,
                                           rag_db_url: str, api_key: str) -> str | None:
    """Sanity-checks the classifier's forced pick (5.6) against the RAG's own,
    broader knowledge (5.9) — the classifier can only ever choose from the closed
    official list, so it has no way to defer to a much better-fitting extended
    topic (5.8) on its own (e.g. a shoulder complaint force-matched to "bursitis"
    when frozen_shoulder is the real, better answer). Best-effort: if this check
    itself fails, keep the classifier's original pick rather than block on it."""
    try:
        closest = vectorstore.closest_topic(question, api_key, rag_db_url)
    except Exception:
        # Found live (code audit): the docstring above promises graceful
        # degradation ("if this check itself fails, keep the classifier's
        # original pick") on ANY failure, but only UpstreamUnavailable was
        # actually caught — a non-retryable HTTPError or any other failure
        # crashed the whole request instead of degrading as documented.
        return matched_code
    if closest is None:
        return matched_code
    closest_code, similarity = closest
    if closest_code in vectorstore.EXTENDED_TOPICS and similarity >= EXTENDED_TOPIC_OVERRIDE_THRESHOLD:
        return None
    return matched_code


def _run_router(session: Session, user_id: str, question: str, api_key: str) -> dict | None:
    """NEW_ROUTER: the one merged call. None on any failure (the caller then uses the old separate calls)."""
    try:
        previous = _recent_full_exchanges(session, user_id, limit=1)
        earliest, latest = activity_log_date_bounds(session, user_id)
        bounds = (f"The user's real logged history runs from {earliest.isoformat()} to {latest.isoformat()}."
                  if earliest else "The user has no logged activity history yet.")
        return chat_router.route(
            question, saved_labels=_existing_condition_labels(session, user_id), options=_condition_options(session, user_id),
            previous_question=previous[0].question if previous else "", previous_answer=previous[0].answer if previous else "",
            today=today_ist(), history_bounds=bounds, api_key=api_key, v2=v2.enabled())
    except Exception:  # noqa: BLE001
        _log.warning("router could not run; using the old separate calls", exc_info=True)
        return None


def _intent_from_router(session: Session, user_id: str, question: str, rr: dict) -> dict:
    """The router's answer in the shape _retrieve_and_answer already uses; activity_recall dates get the SAME real-date handling as _classify_data_intent."""
    fallback = {"intent": "medical", "start_date": None, "end_date": None}
    intent = rr["intent"]
    if intent != "activity_recall":
        return {"intent": intent, "start_date": None, "end_date": None}
    today = today_ist()
    earliest, _latest = activity_log_date_bounds(session, user_id)
    start = _safe_parse_date(rr.get("start_date"), today)
    end = _safe_parse_date(rr.get("end_date"), today)
    if start is None or end is None:
        return fallback
    if start > end:
        start, end = end, start
    weekday_date = _resolve_weekday_name(question, today)
    if weekday_date is not None:
        start = end = weekday_date
    if (end - start).days > MAX_RECALL_RANGE_DAYS:
        start = end - timedelta(days=MAX_RECALL_RANGE_DAYS)
    if earliest is not None and start < earliest:
        start = earliest
        end = max(end, start)
    return {"intent": "activity_recall", "start_date": start, "end_date": end}


def _note_shown_today(session: Session, user_id: str) -> bool:
    """True when the 'pain is not improving' doctor note was already shown to this user today (IST)."""
    start = datetime.combine(today_ist(), datetime.min.time(), tzinfo=IST)
    return session.scalar(
        select(func.count()).select_from(RehabChatLog).where(
            RehabChatLog.user_id == user_id, RehabChatLog.created_at >= start, RehabChatLog.answer.like("%A note from the app: you%"))) > 0


def _allowed_names(session: Session, codes: list[str]) -> list[str]:
    """Display names the app lets the user ADD: the audited table's ranked picks plus every other exercise the table does not mark `avoid` for their exact conditions
    (safe, or modify = allowed with caution) — the same set the app itself allows (same set as the typed-add route)."""
    names = dict(session.execute(select(ActivityTaxonomy.activity_type, ActivityTaxonomy.display_name)).all())
    extra = exercise_table.allowed_types(codes) - exercise_table.approved_types(codes)
    return list(exercise_table.approved_names(codes)) + [names[a] for a in sorted(extra) if a in names]


def _blocked_named(session: Session, text: str, approved: list[str]) -> list[str]:
    """Display names of the app's exercises that `text` names and that are NOT approved for this user (blocked by a saved condition)."""
    catalog = {at: {"name": n, "synonyms": list(syn or [])}
               for at, n, syn in session.execute(select(ActivityTaxonomy.activity_type, ActivityTaxonomy.display_name, ActivityTaxonomy.synonyms)).all()}
    return [catalog[a]["name"] for a in v2.match_exercises(text, catalog) if catalog[a]["name"] not in approved]


def _sport_instruction(rr: dict, approved: list[str]) -> str:
    """The line added to the answer prompt when the router named a sport / activity. An activity that is NOT one of the 12 checked exercises, asked about (how to, can I) and not as a
    request for suggestions, must first be admitted as outside the app's knowledge, then answered with approved exercises only. A listed exercise, or a request for suggestions, keeps the plain rule."""
    sport = rr["sport"]
    def _match(names) -> bool:
        return any(v2.norm(sport) in v2.norm(n) or v2.norm(n) in v2.norm(sport) for n in names)
    if _match(approved):
        return f"\n\nThe user wants exercises for: {sport}. Build your main answer ONLY from the approved exercises and say how each one helps with it."
    if _match(ACTIVITY_CATEGORIES):                                             # one of the 12, but not approved for THIS user's conditions
        return (f"\n\nThe user asked about {sport}, which is NOT approved for their conditions. Begin by saying plainly, in the user's language, that it is not recommended for their condition "
                "and that they should ask their doctor or physiotherapist first. If the user says a doctor has allowed it, explain that you still cannot suggest it while the saved condition blocks it: "
                "they can remove that condition in their profile (profile icon, then Edit) only if the problem is really solved, or log the exercise themselves as a custom exercise. "
                "Then name ONLY approved exercises, saying how each one helps. Do not say the user has a pain or condition unless it is listed or stated.")
    return (f"\n\nThe user mentioned an activity that is not one of the app's checked exercises, for: {sport}. You have no verified knowledge of the activity itself. "
            "If the user asks how to do it, learn it, whether they can do it, or for a plan for it, begin by saying so plainly, in the user's language: you cannot explain how to do it, coach it or confirm it for them. "
            "Then say you can suggest exercises you do know that build what that activity needs (for example stability, strength or fitness), and name ONLY approved exercises, saying how each one helps. "
            "If the user only asks which exercises help with it, build your main answer ONLY from the approved exercises and say how each one helps. "
            "Do not say the user has a pain or condition unless it is listed or stated.")


def _plain_response(session: Session, user_id: str, question: str, answer: str) -> dict:
    _save_chat_log(session, user_id, question, answer)
    return {
        "referred": False, "message": None, "answer": answer,
        "needs_condition_selection": False, "condition_options": None,
        "needs_escalation_confirmation": False, "escalation_proposal": None,
        "needs_program_update_confirmation": False, "suggested_activities": None,
        "needs_revert_confirmation": False, "revert_candidates": None,
        "quota": remaining_quota(session, user_id),
    }


def _conversation_answer(session: Session, user_id: str, question: str, api_key: str) -> dict:
    """Chat / memory route: answered from the user's past chat only (no notes search, no exercise button, no doctor note)."""
    if v2.enabled() and v2.is_greeting_or_intro(question):
        _mem = "Past conversation: not needed for this message."              # a greeting or a name must not bring up old topics
    else:
        _mem = _memory_context_block(session, user_id)
    messages = [{"role": "system", "content": chat_prompts.SHORT_SYSTEM_PROMPT + chat_prompts.CONVERSATION_RULE},
                {"role": "user", "content": f"{_mem}\n\nQuestion: {question}"}]
    raw = vectorstore.chat_complete(messages, api_key)
    match = SUGGESTED_EXERCISES_MARKER.search(raw)
    answer = (raw[:match.start()] if match else raw).strip()
    low = answer.lower()
    if low.startswith(chat_messages.NO_INFO.lower()[:60]):
        answer = chat_messages.NO_INFO
    elif any(k in low for k in chat_prompts.LEAK_MARKERS):
        answer = NO_CONTENT_ANSWER
    elif len(answer) < 3:
        raise UpstreamUnavailable("chat model returned an unusable answer")
    if v2.enabled():
        answer = v2.strip_offers(answer)
    return _plain_response(session, user_id, question, _enforce_no_medication_or_surgery(answer))


def _targets_answer(session: Session, user_id: str, question: str) -> dict:
    """Reps / sets / targets route: built from the user's own program rows (no AI call)."""
    rows = session.execute(
        select(ActivityTaxonomy.display_name, UserProgram.status, UserProgram.target_sets, UserProgram.target_amount, UserProgram.unit,
               UserProgram.set_targets, UserProgram.auto_progress)
        .join(ActivityTaxonomy, ActivityTaxonomy.activity_type == UserProgram.activity_type)
        .where(UserProgram.user_id == user_id).order_by(ActivityTaxonomy.display_name)).all()
    names = [r[0] for r in rows] or [n for (n,) in session.execute(select(ActivityTaxonomy.display_name)).all()]
    asked = chat_rules.exercise_named(question, names)
    if asked is None and v2.enabled():                           # "how long should I walk?": "walk" names Walking
        _hits = [n for n in names if v2.question_names(question, n)]
        if len(_hits) == 1:
            asked = _hits[0]
    # a short follow-up like "how many reps?" uses the exercise the last exchange was about; a long or general question never borrows an old one ("how many push ups ...")
    if asked is None and not (v2.enabled() and (v2.wants_all_targets(question) or len(question.split()) > 4)):
        prev = _recent_full_exchanges(session, user_id, limit=1)
        if prev:
            asked = chat_rules.exercise_named(prev[0].question, names) or chat_rules.exercise_named(prev[0].answer, names)
    return _plain_response(session, user_id, question, targets_answer.answer([tuple(r) for r in rows], asked))


def _v2_rule_response(session: Session, user_id: str, question: str, rr: dict) -> dict | None:
    """PIPELINE V2 code rules on the router's answer (fixed messages, no answer call): "I am healthy now" and data the app does not track."""
    if rr.get("intent") == "remove_exercise":
        return _plain_response(session, user_id, question, v2.REMOVE_MESSAGE)
    if rr.get("intent") == "set_target":
        return _plain_response(session, user_id, question, v2.SET_TARGET_MESSAGE)
    if rr.get("claims_healthy"):
        labels = _existing_condition_labels(session, user_id)
        if labels:
            return _plain_response(session, user_id, question, v2.healthy_message(labels))
    if rr.get("unsupported_data") and v2.mentions_untracked(question):
        return _plain_response(session, user_id, question, v2.UNSUPPORTED_MESSAGE)
    return None


def _memory_questions_block(session: Session, user_id: str) -> str:
    """Only what the USER wrote earlier (their name, their symptoms), never the old answers: old progress numbers and advice must not leak into an unrelated reply."""
    recent = _recent_full_exchanges(session, user_id)
    if not recent:
        return "Past conversation: none yet - this is the first exchange."
    return "The user's own earlier messages (for their name and context only, never for facts):\n" + "\n".join(f"- {r.question}" for r in recent)


def _routine_or_avoid_answer(session: Session, user_id: str, question: str, condition_codes: list[str]) -> dict | None:
    """'give me a routine for the week' and 'what should I avoid?': built in code from the audited table row (its cautions) and the user's own targets. No invented numbers."""
    want_avoid, want_routine = v2.asks_what_to_avoid(question), v2.asks_routine(question)
    if not (want_avoid or want_routine) or exercise_table.gate(condition_codes):
        return None
    row = exercise_table.row_for(condition_codes)
    if not row:
        return None
    cautions = v2.clean_cautions(row.get("row_caution", ""))
    approved = exercise_table.approved_names(condition_codes)
    _blocked = _blocked_named(session, question, approved) if want_routine else []                  # "a running routine" when running is blocked: say so, never silently leave it out
    _lead = (v2.blocked_protocol("recommend", _blocked[0], _existing_condition_labels(session, user_id), []) + "\n\nHere is a week with the exercises I can suggest for you.\n\n") if _blocked else ""
    if want_avoid:
        return _plain_response(session, user_id, question, v2.avoid_text(cautions, approved))
    rows = session.execute(
        select(ActivityTaxonomy.display_name, UserProgram.status, UserProgram.target_sets, UserProgram.target_amount, UserProgram.unit, UserProgram.set_targets, UserProgram.auto_progress)
        .join(ActivityTaxonomy, ActivityTaxonomy.activity_type == UserProgram.activity_type)
        .where(UserProgram.user_id == user_id, UserProgram.status == "active")).all()
    by_name = {r[0]: r for r in rows}
    items = []
    for name in approved:
        r = by_name.get(name)
        if r:
            items.append(targets_answer.one(r[0], r[2], r[3], r[4], r[5], r[6]).strip())
    if not items:
        return None
    return _plain_response(session, user_id, question, _lead + v2.weekly_routine(items, cautions))


def _increase_target_answer(session: Session, user_id: str, question: str, api_key: str) -> dict:
    """'can I increase my target? by how much?': answered from the user's real adherence numbers. Eligible (adherence 100% or more, not declining, no plateau, safe for the
    user's conditions) -> the offer and the confirm button; otherwise a plain 'not yet' with the numbers."""
    entries = session.execute(
        select(UserProgram.activity_type, ActivityTaxonomy.display_name).join(ActivityTaxonomy, ActivityTaxonomy.activity_type == UserProgram.activity_type)
        .where(UserProgram.user_id == user_id, UserProgram.status == "active").order_by(ActivityTaxonomy.display_name)).all()
    if not entries:
        return _plain_response(session, user_id, question, "You don't have any active exercises yet, so there is nothing to increase.")
    lines, payload = [], None
    for at, dn in entries:
        adh = progression_analysis._check_adherence_trend(session, user_id, at)
        plat = progression_analysis._check_plateau(session, user_id, at)
        if adh.get("value") is None:
            lines.append(f"- {dn}: no weekly history yet")
            continue
        gate = progression_analysis._condition_gate(session, user_id, at)
        why = []
        if adh["value"] < 100:
            why.append(f"adherence {adh['value']}% (an increase needs 100% or more)")
        if adh.get("trend") == "declining":
            why.append("adherence is declining")
        if plat.get("detected"):
            why.append("your progress has plateaued")
        if not gate["safe"]:
            why.append("not safe to raise for your conditions")
        if not why and payload is None:
            proposal = progression_analysis._escalation_proposal(adh, plat, gate, api_key=api_key)
            payload = _escalation_confirmation_payload(at, dn, {"escalation_proposal": proposal, "findings": [adh]})
        lines.append(f"- {dn}: " + ("ready for an increase" if not why else "; ".join(why)))
    if payload:
        text = payload["prompt_text"] + " Tap the button below to confirm.\n\nYour other exercises:\n" + "\n".join(l for l in lines if payload["display_name"] not in l)
    else:
        text = ("Not yet. A target is raised when adherence is 100% or more and not declining. Right now:\n" + "\n".join(lines)
                + "\n\nYour targets also go up automatically every week when you complete them.")
    resp = _plain_response(session, user_id, question, text)
    if payload:
        resp["needs_escalation_confirmation"], resp["escalation_proposal"] = True, payload
    return resp


def _fast_add_reply(session: Session, user_id: str, question: str) -> dict | None:
    """A short message that is only "add <exercise>": answered by the add route directly, with no embedding check, urgent AI check, router or daily question."""
    prev = _recent_full_exchanges(session, user_id, limit=1)
    if prev and v2.is_choice_followup(question) and prev[-1].answer.startswith("Which one do you mean: "):
        _names = [n.strip() for n in prev[-1].answer[len("Which one do you mean: "):].rstrip("? ").split(",") if n.strip()]
        if _names:
            new_flow._bump_free_counter(sys.modules[__name__], user_id)
            return _add_exercise_answer(session, user_id, question, {"exercise": " and ".join(_names)})
    name_text = v2.fast_add_name(question)
    if not name_text:
        return None
    catalog = {at: {"name": n, "synonyms": list(syn or [])}
               for at, n, syn in session.execute(select(ActivityTaxonomy.activity_type, ActivityTaxonomy.display_name, ActivityTaxonomy.synonyms)).all()}
    if not all(v2.match_exercises(part, catalog) for part in v2.split_exercise_parts(name_text)):
        return None
    new_flow._bump_free_counter(sys.modules[__name__], user_id)
    return _add_exercise_answer(session, user_id, question, {"exercise": name_text})


def _add_exercise_answer(session: Session, user_id: str, question: str, rr: dict) -> dict:
    """PIPELINE V2 "add this exercise" route (code only, no answer call). The router names the exercise (or, for "add that", the previous answer does);
    the audited table decides if it is approved; the user's program row says if it is already in or paused. Approved and new/paused -> Add button."""
    codes = list(session.scalars(select(UserCondition.condition_code).where(UserCondition.user_id == user_id)).all())
    catalog = {at: {"name": n, "synonyms": list(syn or [])}
               for at, n, syn in session.execute(select(ActivityTaxonomy.activity_type, ActivityTaxonomy.display_name, ActivityTaxonomy.synonyms)).all()}
    names = {at: i["name"] for at, i in catalog.items()}
    reason = exercise_table.gate(codes)
    if reason:
        _gm = chat_messages.gate_message(reason, len([c for c in codes if c != "none"]))
        return _plain_response(session, user_id, question, v2.gate_message_named(reason, _existing_condition_labels(session, user_id), _gm))
    _extra = exercise_table.allowed_types(codes) - exercise_table.approved_types(codes)       # not `avoid` in the audited table (safe, or modify = with caution): fine when the user asks to add it
    approved_names = exercise_table.approved_names(codes) + [names[a] for a in sorted(_extra) if a in names]
    approved_types = exercise_table.approved_types(codes) | _extra
    status = dict(session.execute(select(UserProgram.activity_type, UserProgram.status).where(UserProgram.user_id == user_id)).all())
    raw = (rr.get("exercise") or "").strip()
    _multi = False
    if raw:
        parts = v2.split_exercise_parts(raw)
        if len(parts) > 1:                                                          # "running and ankle circles": every part must name exactly one exercise
            found = []
            for _p in parts:
                _f = v2.match_exercises(_p, catalog)
                if len(_f) != 1:
                    found, raw = (_f if len(_f) > 1 else []), _p
                    break
                found.extend(_f)
            else:
                found, _multi = list(dict.fromkeys(found)), True
        else:
            found = v2.match_exercises(raw, catalog)
        if not found:
            _did = v2.suggest_exercise(raw, catalog)
            if _did:
                return _plain_response(session, user_id, question, f'I couldn\'t find "{raw[:40]}". Did you mean {_did}? Type "add {_did.lower()}" to add it.')
            return _plain_response(session, user_id, question, v2.not_in_app_message(raw, approved_names))
        if len(found) > 1 and not _multi:                                           # "stretch", "circles": ask which one, never pick the first
            return _plain_response(session, user_id, question, v2.AMBIGUOUS_EXERCISE.format(names=", ".join(names[a] for a in found)))
        targets = found
    else:                                                                       # "add that / add these": the exercises the previous answer named
        prev = _recent_full_exchanges(session, user_id, limit=1)
        text = prev[0].answer if prev else ""
        targets = [at for at, n in names.items() if v2.mentioned(text, n)]
        if not targets:
            return _plain_response(session, user_id, question, v2.ASK_WHICH_EXERCISE.format(names=", ".join(approved_names)))
    ok = [a for a in targets if a in approved_types]
    if not ok:
        return _plain_response(session, user_id, question, v2.not_approved_message(", ".join(names[a] for a in targets), approved_names, _existing_condition_labels(session, user_id)))
    _active = [a for a in ok if status.get(a) == "active"]
    _visible = profile_service._dashboard_slice(session, user_id)[1]               # what the exercise player shows right now
    show = [a for a in _active if a not in _visible]                              # active in the plan but hidden by the player's level limit: offer to show it
    already = [a for a in _active if a in _visible]
    resume = [a for a in ok if a in status and status[a] != "active"]
    offer = [a for a in ok if a not in already]
    message = v2.add_message([names[a] for a in offer if a not in show], [names[a] for a in resume], [names[a] for a in already], [names[a] for a in show])
    _modify = [a for a in offer if exercise_table.verdict_for(codes, a) == "modify"]
    if _modify:                                                                  # allowed with caution: say so, with the table's own words
        _row = exercise_table.row_for(codes) or {}
        message += v2.add_caution([names[a] for a in _modify], {names[a]: exercise_table.caution_for(codes, a) for a in _modify}, v2.clean_cautions(_row.get("row_caution", "")))
    _bad = [a for a in targets if a not in approved_types]
    if _bad:
        message += " " + v2.not_approved_note([names[a] for a in _bad], _existing_condition_labels(session, user_id))
    if not offer:
        return _plain_response(session, user_id, question, message)
    _save_chat_log(session, user_id, question, message)
    return {
        "referred": False, "message": None, "answer": message,
        "needs_condition_selection": False, "condition_options": None,
        "needs_escalation_confirmation": False, "escalation_proposal": None,
        "needs_program_update_confirmation": True,
        "suggested_activities": [{"activity_type": a, "display_name": names[a]} for a in offer],
        "program_update_mode": "add", "program_update_modes": ["add"],
        "needs_revert_confirmation": False, "revert_candidates": None,
        "quota": remaining_quota(session, user_id),
    }


def _program_performance_context(session: Session, user_id: str, question: str, *, openrouter_api_key: str) -> tuple[str | None, dict | None]:
    """PIPELINE V2 progress route: the numbers for EVERY active exercise in the program (code, no AI), so the answer covers the whole program. A question that
    names one exercise keeps the single-exercise analysis (and its "increase your target" offer). The offer for the whole-program summary is made for the
    first exercise that is eligible."""
    entries = session.execute(
        select(UserProgram.activity_type, ActivityTaxonomy.display_name, ActivityTaxonomy.synonyms)
        .join(ActivityTaxonomy, ActivityTaxonomy.activity_type == UserProgram.activity_type)
        .where(UserProgram.user_id == user_id, UserProgram.status == "active").order_by(ActivityTaxonomy.display_name)).all()
    if not entries:
        return None, None
    lowered = question.lower()
    if any(any(n and n in lowered for n in {dn.lower(), *((x or "").lower() for x in (syn or []))}) for _at, dn, syn in entries):
        return _performance_context(session, user_id, question, openrouter_api_key=openrouter_api_key)
    lines, payload = [], None
    for at, dn, _syn in entries:
        adh = progression_analysis._check_adherence_trend(session, user_id, at)
        plat = progression_analysis._check_plateau(session, user_id, at)
        curve = progression_analysis._check_vs_curve(session, user_id, at)
        if adh.get("available") is False:
            text = "no weekly history yet"
        else:
            text = f"adherence {adh.get('value')}% over the last {adh.get('window_weeks')} weeks" + (f" ({adh['trend']})" if adh.get("trend") else "")
        text += "; plateau detected" if plat.get("detected") else "; no plateau"
        if curve.get("available") is not False and "applied_pct" in curve:
            text += f"; last weekly change applied {curve['applied_pct']}% vs {curve['predicted_pct']}% predicted"
        lines.append(f"- {dn}: {text}")
        if payload is None and adh.get("value") is not None:
            gate = progression_analysis._condition_gate(session, user_id, at)
            if gate["safe"] and not plat.get("detected") and adh.get("value", 0) >= 100 and adh.get("trend") != "declining":
                proposal = progression_analysis._escalation_proposal(adh, plat, gate, api_key=openrouter_api_key)
                payload = _escalation_confirmation_payload(at, dn, {"escalation_proposal": proposal, "findings": [adh]})
    streak = progression_analysis._check_streak(session, user_id)
    block = ("Program data for ALL of the user's active exercises (from the app's own tracking, not retrieved medical text). Summarise EVERY exercise listed below in one short "
             "line each, with its percentages. Never say there is no data for an exercise that is listed. Discomfort is not tracked yet.\n" + "\n".join(lines)
             + f"\nOverall: current streak {streak['current']} day(s), {streak['active_days']} active days in total.")
    return block, payload


def _classify_condition(question: str, options: list[dict], existing_labels: list[str],
                         api_key: str) -> str | None:
    """Best-effort match of a symptom description to the closed condition list.

    Never trusted directly: the caller only uses this to SUGGEST, and the return
    value is validated against `options` before use either way. If the LLM is
    rate-limited, this just returns None (skip the suggestion, fall through to a
    normal answer attempt) rather than blocking the whole request on it.
    """
    if not options or len(question) < MIN_LENGTH_WORTH_CLASSIFYING:
        return None
    listing = "\n".join(f"{o['code']}: {o['label']}" for o in options)
    existing = ", ".join(existing_labels) if existing_labels else "nothing yet"
    prompt = CONDITION_MATCH_PROMPT.format(existing=existing, options=listing, question=question)
    try:
        reply = vectorstore.chat_complete(
            [{"role": "user", "content": prompt}], api_key, temperature=0,
        )
    except RuntimeError:
        # Found live (third-pass code audit): only UpstreamUnavailable was
        # caught, but chat_complete also raises the sibling ResponseTruncated
        # for a real, documented failure mode (a reasoning-model repetition
        # loop) and a bare RuntimeError for a non-retryable upstream status —
        # both propagated uncaught here, contradicting this function's own
        # docstring claim of always degrading gracefully. RuntimeError is the
        # shared base of all three, so this now covers every variant — this
        # function is a best-effort SUGGESTION only (never trusted directly,
        # see docstring), so it's correct for it to swallow any LLM failure
        # rather than let it crash the whole request.
        return None
    reply = reply.strip().strip('"').strip("'")
    valid_codes = {o["code"] for o in options}
    if reply in valid_codes:
        return reply
    # The model doesn't always follow "reply with ONLY the code" to the letter —
    # tolerate a code appearing inside extra text/punctuation. None of our codes
    # are substrings of each other, so this can't cross-match the wrong one.
    lowered = reply.lower()
    for code in valid_codes:
        if code in lowered:
            return code
    return None


def _add_condition(session: Session, user_id: str, condition_code: str) -> dict:
    """Adds one condition to the user's existing profile without discarding the
    others — reuses the exact mechanism the app's own condition-editor screen
    already calls (profile.service.complete_onboarding with the merged list), so
    this isn't a second, divergent way to change conditions and doesn't risk
    the safety-grid rebuild behaving differently than it does elsewhere.

    Found live (fourth-pass audit): this used to be an unguarded read-modify-
    write — two near-simultaneous chat-confirmed condition additions (a
    double-tap, two devices) could both read the same starting condition
    list and each add a different condition; whichever complete_onboarding
    call committed last would silently win, dropping the other one entirely.
    Locking the profile row first serializes the two calls, so the second
    one to run reads the FIRST one's already-merged list, not a stale one.
    """
    profile = session.scalar(
        select(UserProfile).where(UserProfile.user_id == user_id).with_for_update()
    )
    if profile is None:
        # Found live (code audit): nothing requires onboarding before
        # /v1/rehab/query, so a user who signed up and went straight to the
        # chat could reach here with no profile row — `profile.age` below
        # then raised AttributeError as an uncaught 500, AFTER their quota
        # slot had already been committed. They paid for the crash.
        raise ValidationError(
            "Complete your profile setup before recording a condition from chat."
        )
    existing_codes = list(session.scalars(
        select(UserCondition.condition_code).where(UserCondition.user_id == user_id)
    ).all())
    merged = [c for c in existing_codes if c != "none"]
    if condition_code not in merged:
        merged.append(condition_code)

    return complete_onboarding(
        session, user_id,
        age=profile.age, goal=profile.goal, experience_band=profile.experience_band,
        condition_codes=merged, unit_preference=profile.unit_preference,
    )


# --- App-data intent routing (RLM: activity recall / performance analysis,
# vs. RAG: medical retrieval) ------------------------------------------------
#
# Originally two independent regex gates (ACTIVITY_RECALL_PATTERN,
# PERFORMANCE_INTENT_PATTERN). Replaced after repeatedly finding live gaps no
# fixed pattern list kept up with: "analyze my growth" (missing the word
# "growth" entirely), "what I have done" vs "what have I done" (word order),
# "day before yesterday" mis-parsed as plain "yesterday" (naive substring
# match), "3 days ago" not recognized as a specific day at all, and "what
# about the day before that" (a follow-up with no keywords of its own,
# needing the previous question for context) — five distinct regex misses
# across two sessions of live testing. A real LLM classification handles
# open-ended natural phrasing and date arithmetic far more robustly than an
# ever-growing keyword list, at the cost of one extra cheap call per message.

DATA_INTENT_VALUES = {"activity_recall", "performance", "medical", "program_revert"}
MAX_RECALL_RANGE_DAYS = 90  # a runaway "since 2020" request shouldn't scan years of logs

DATA_INTENT_PROMPT = """A user of a fitness/rehab app sent a chat message. Decide which kind \
of answer it needs, and reply with EXACTLY one JSON object and nothing else:

{{"intent": "activity_recall" | "performance" | "medical" | "program_revert", "start_date": "YYYY-MM-DD or null", "end_date": "YYYY-MM-DD or null"}}

Today's date is {today} ({today_weekday}).
{history_bounds}

- "activity_recall": the user is asking what they literally logged/did on a specific day or \
range of days — e.g. "what did I do yesterday", "show me last week", "what about the day \
before that", "3 days ago", "what about Thursday", "what did I do on my first day". If so, \
resolve start_date/end_date to real calendar dates. A single day has start_date == end_date.
  - Relative phrases ("yesterday", "3 days ago") count back from today above.
  - A weekday name alone ("Thursday") means the most recent such day on or before today — \
work it out from today's own weekday given above, don't guess.
  - "first day" / "when did I start" / "my earliest" means the real earliest-logged-day given \
above, not a guess — use that exact date.
  - A short follow-up with no date of its own ("what about the day before that") is relative \
to the previous message below.
  - If the message names MORE THAN ONE specific day or relative time in the same question \
(e.g. "day before yesterday and 3 days ago", "Monday and Wednesday"), this is still exactly \
one activity_recall, not two separate questions: set start_date to the EARLIEST of the named \
days and end_date to TODAY above (not just the latest named day) — the user wants one combined \
catch-up across that whole stretch.
- "performance": the user is asking about their own progress, trend, or whether they're \
improving, plateaued, or ready to do more — e.g. "how am I doing", "analyze my growth", \
"can I do more this week". start_date and end_date should be null.
- "program_revert": the user wants to UNDO a recent change to their exercise program, or get \
back an exercise that used to be there — e.g. "revert what I did", "bring back my previous \
exercise", "undo that change", "I want my old exercises back", "put glute bridge back". This \
is about restoring the PROGRAM ITSELF, not a question about health or exercise technique. \
start_date and end_date should be null.
- "medical": anything else — a question about a condition, pain, or exercise guidance that \
is not about the user's own personal log, trend, or program changes. start_date and end_date \
should be null.

Previous message in this conversation (for resolving follow-ups; may be blank):
{previous_question}

Message: "{question}"
"""


def _safe_parse_date(value, today: date) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed <= today else None  # never a future date


_WEEKDAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _resolve_weekday_name(question: str, today: date) -> date | None:
    """Found live: asked to resolve "Thursday" given today=Monday, the model
    correctly recognized it as an activity_recall question but miscounted
    the actual date by 2 days (real day-of-week arithmetic, not language
    understanding — exactly the kind of thing an LLM is unreliable at and
    code is not). If the question names a weekday explicitly, compute the
    most recent occurrence on or before today directly instead of trusting
    whatever date the model counted to."""
    lowered = question.lower()
    for i, name in enumerate(_WEEKDAY_NAMES):
        if name in lowered:
            days_back = (today.weekday() - i) % 7
            return today - timedelta(days=days_back)
    return None


def _classify_data_intent(session: Session, user_id: str, question: str, *,
                           api_key: str, today: date) -> dict:
    """Fails closed to "medical" (the original, safest fallback path) on any
    LLM error, unparseable reply, or unresolvable date — a classification
    failure should fall through to the normal medical-RAG path, never
    silently claim to have personal data it doesn't.

    Found live: "what about Thursday" and "show me my first day" need real
    facts this classifier didn't have — a weekday name needs to know what day
    today actually is, and "first day" needs the real earliest-logged date,
    not a guess. Both come from a real query against the user's own data
    (activity_log_date_bounds), not from the model inventing an answer."""
    fallback = {"intent": "medical", "start_date": None, "end_date": None}
    previous = _recent_full_exchanges(session, user_id, limit=1)
    previous_question = previous[0].question if previous else ""
    earliest, latest = activity_log_date_bounds(session, user_id)
    history_bounds = (
        f"The user's real logged history runs from {earliest.isoformat()} to {latest.isoformat()}."
        if earliest else
        "The user has no logged activity history yet."
    )
    prompt = DATA_INTENT_PROMPT.format(
        today=today.isoformat(), today_weekday=today.strftime("%A"),
        history_bounds=history_bounds,
        previous_question=previous_question, question=question[:500],
    )
    try:
        # 500, not 200: found live (and confirmed by repeating the identical
        # call several times) that this reasoning model sometimes burns its
        # whole budget on an internal "reasoning" field before ever writing
        # the actual JSON, hitting ResponseTruncated on a genuinely correct
        # classification and failing closed to "medical" — not a wrong
        # answer, just an avoidable one. Same lesson already documented for
        # the main chat path (vectorstore.chat_complete's own docstring).
        reply = vectorstore.chat_complete(
            [{"role": "user", "content": prompt}], api_key, temperature=0, max_tokens=500,
        )
    except RuntimeError:
        # Widened alongside _classify_condition (third-pass code audit): a
        # bare RuntimeError (non-retryable upstream status) is the same
        # "fail closed to fallback" case as UpstreamUnavailable/ResponseTruncated,
        # not a reason to crash a best-effort classification.
        return fallback
    try:
        data = json.loads(reply.strip())
    except json.JSONDecodeError:
        return fallback
    if not isinstance(data, dict) or data.get("intent") not in DATA_INTENT_VALUES:
        return fallback
    intent = data["intent"]
    if intent != "activity_recall":
        return {"intent": intent, "start_date": None, "end_date": None}

    start = _safe_parse_date(data.get("start_date"), today)
    end = _safe_parse_date(data.get("end_date"), today)
    if start is None or end is None:
        return fallback  # couldn't resolve real dates -> don't guess, fall through
    if start > end:
        start, end = end, start

    # A named weekday's exact date is real arithmetic, not a judgment call —
    # override whatever the model counted to with the one correct answer,
    # the same way _pick_escalation_step is bounded to fixed real choices
    # rather than trusted to compute a number freehand.
    weekday_date = _resolve_weekday_name(question, today)
    if weekday_date is not None:
        start = end = weekday_date

    if (end - start).days > MAX_RECALL_RANGE_DAYS:
        start = end - timedelta(days=MAX_RECALL_RANGE_DAYS)
    if earliest is not None and start < earliest:
        # Defensive clamp, not the primary fix: the real earliest date is
        # already handed to the model above, but this is never trusted blindly
        # — same "the model gets a real fact, code still bounds the outcome"
        # pattern as the escalation-step picker in app/progression/analysis.py.
        #
        # Found live (a 422 crash, not just a wrong answer): if the model
        # resolves a range that falls entirely BEFORE the user's real first
        # logged day (e.g. their history starts today and they ask about "3
        # days ago"), clamping start up to `earliest` alone can push it past
        # `end`, inverting the range — activity_log_detail correctly rejects
        # start > end, which surfaced as a 422 instead of a graceful answer.
        # Re-clamping end alongside start keeps the invariant true instead of
        # just moving the violation from "wrong" to "crashes."
        start = earliest
        end = max(end, start)
    return {"intent": "activity_recall", "start_date": start, "end_date": end}


def _recall_amount_text(e: dict) -> str:
    """One entry's amount in plain words, with the real per-set numbers instead of
    an average: \"3 sets of 10, 10 and 11 reps (31 in total)\", \"2 sets of 11 reps
    (22 in total)\", \"2.5 km\". A blended average (3 x 10.33) was never the
    figure for any single set."""
    unit = "reps" if e["unit"] == "count" else e["unit"]
    fmt = lambda v: f"{v:g}"
    per_set = e.get("set_amounts")
    if not per_set:
        if e["sets"] <= 1:
            return f"{fmt(e['amount'])} {unit}"
        per_set = [e["amount"]] * e["sets"]   # an entry without a breakdown is uniform
    n = len(per_set)
    total = fmt(sum(per_set))
    if n == 1:
        return f"{fmt(per_set[0])} {unit}"
    if len(set(per_set)) == 1:
        values = fmt(per_set[0])
    else:
        values = ", ".join(fmt(v) for v in per_set[:-1]) + " and " + fmt(per_set[-1])
    return f"{n} sets of {values} {unit} ({total} in total)"


def _recall_group_text(group: list[dict]) -> str:
    """One exercise's entries for one day. A single entry reads as before; several
    become \"3 entries, 3 km in total (1, 1.5 and 0.5 km)\" so the answer says how
    much was done overall instead of repeating the exercise on every line."""
    if len(group) == 1:
        return _recall_amount_text(group[0])
    unit = "reps" if group[0]["unit"] == "count" else group[0]["unit"]
    total = round(sum(e["total_volume"] for e in group), 2)
    text = f"{len(group)} entries, {total:g} {unit} in total"
    if len(group) <= 5:
        single = all(e["sets"] <= 1 and not e.get("set_amounts") for e in group)
        if single:
            vals = [f"{e['amount']:g}" for e in group]
            joined = vals[0] if len(vals) == 1 else ", ".join(vals[:-1]) + " and " + vals[-1]
            text += f" ({joined} {unit})"
        else:
            text += " (" + "; ".join(_recall_amount_text(e) for e in group) + ")"
    return text


def _activity_recall_answer(session: Session, user_id: str, start: date, end: date) -> str:
    """The user-facing answer for an activity_recall question, built directly
    from real data — deliberately NOT run through the answer-writing LLM.

    Found live: even when the classifier and the data lookup were both
    provably correct (verified by calling them directly, immediately after a
    wrong answer), the answer-writing LLM still sometimes mislabeled the
    date in its prose — most likely pattern-matching onto some OTHER date
    mentioned elsewhere in conversation memory. There is no real language
    understanding needed to state "you did X, Y, Z on this date" — it's a
    formatted data dump, not a synthesis task — so this sidesteps the
    unreliable step entirely instead of trying to prompt-engineer around it
    again. (_activity_recall_block below still exists for anywhere else that
    genuinely needs this handed to an LLM as prompt context.)
    """
    entries = activity_log_detail(session, user_id, start_date=start, end_date=end)
    label = start.isoformat() if start == end else f"{start.isoformat()} to {end.isoformat()}"
    if not entries:
        return f"Nothing logged for {label}."
    single_day = start == end
    # Same exercise on the same day collapses to one line (entries arrive newest
    # first; the first appearance fixes the line's position).
    groups: dict[tuple, list[dict]] = {}
    for e in entries:
        groups.setdefault((e['date'], e['name']), []).append(e)
    lines = "\n".join(
        f"- {name} — {_recall_group_text(g)}" if single_day else
        f"- {day}: {name} — {_recall_group_text(g)}"
        for (day, name), g in groups.items()
    )
    return f"Your activity for {label}:\n{lines}"


def _activity_recall_block(session: Session, user_id: str, start: date, end: date) -> str:
    entries = activity_log_detail(session, user_id, start_date=start, end_date=end)
    label = start.isoformat() if start == end else f"{start.isoformat()} to {end.isoformat()}"
    if not entries:
        return f"Actual logged activity, {label} (from the app's own records): nothing logged."
    lines = "\n".join(
        f"- {e['date']}: {e['name']} — {e['sets']} x {e['amount']}{e['unit']}" for e in entries
    )
    return f"Actual logged activity, {label} (from the app's own records, not retrieved medical text):\n{lines}"


def _resolve_performance_activity(session: Session, user_id: str, question: str) -> str | None:
    """Which of the user's own active program entries a performance question is
    about (16.2 — the agent only ever analyzes something already in the user's
    program, never looks outside it). Tries an explicit name match against the
    user's active entries first, so a question naming one exercise among several
    doesn't get a random one; falls back to the single active entry if there's
    only one, or the most recently logged active entry otherwise."""
    entries = session.execute(
        select(UserProgram.activity_type, ActivityTaxonomy.display_name, ActivityTaxonomy.synonyms)
        .join(ActivityTaxonomy, ActivityTaxonomy.activity_type == UserProgram.activity_type)
        .where(UserProgram.user_id == user_id, UserProgram.status == "active")
    ).all()
    if not entries:
        return None
    lowered = question.lower()
    for activity_type, display_name, synonyms in entries:
        names = {display_name.lower(), *((s or "").lower() for s in (synonyms or []))}
        if any(n and n in lowered for n in names):
            return activity_type
    if len(entries) == 1:
        return entries[0][0]
    recent = session.scalar(
        select(ActivityLog.activity_type)
        .where(ActivityLog.user_id == user_id,
               ActivityLog.activity_type.in_([e[0] for e in entries]))
        .order_by(ActivityLog.logged_at.desc())
        .limit(1)
    )
    return recent or entries[0][0]


def _escalation_confirmation_payload(activity_type: str, display_name: str, findings: dict) -> dict | None:
    """Phase 5 — the structured "Add this to your dashboard?" offer (16.5/5.6's
    existing confirmation pattern), separate from the prose answer the LLM
    writes. None unless analyze_performance's escalation_proposal is eligible."""
    proposal = findings.get("escalation_proposal") or {}
    if not proposal.get("eligible"):
        return None
    pct = proposal["proposed_pct_increase"]
    adherence = next((f for f in findings["findings"] if f["check"] == "adherence_trend"), {})
    return {
        "activity_type": activity_type,
        "display_name": display_name,
        "proposed_pct_increase": pct,
        "adherence_pct_snapshot": adherence.get("value"),
        "reason": proposal["reason"],
        "prompt_text": f"You're consistently hitting your {display_name} target — "
                       f"increase it by {round(pct * 100)}%?",
    }


def _performance_context(session: Session, user_id: str, question: str, *,
                          openrouter_api_key: str) -> tuple[str | None, dict | None]:
    """(prompt block for the answer LLM, structured escalation-confirmation
    payload for the client) — only called once _classify_data_intent has
    already decided this is a "performance" question. None, None only when
    there's no active program entry for the agent to analyze, in which case
    _retrieve_and_answer falls through to its normal medical-only path."""
    activity_type = _resolve_performance_activity(session, user_id, question)
    if activity_type is None:
        return None, None
    findings = progression_analysis.analyze_performance(
        session, user_id, activity_type, question, api_key=openrouter_api_key,
    )
    display_name = session.get(ActivityTaxonomy, activity_type).display_name
    block = (
        f"Program data for {display_name} (from the app's own tracking, not "
        f"retrieved medical text): {json.dumps(findings)}"
    )
    return block, _escalation_confirmation_payload(activity_type, display_name, findings)


def _retrieve_and_answer(session: Session, user_id: str, question: str, *, rag_db_url: str,
                          openrouter_api_key: str, rlm_enabled: bool = True,
                          charged: bool = True, urgent_unavailable: bool = False, router_result: dict | None = None) -> dict:
    """Quota is consumed once, unconditionally, by answer_query before this
    is ever called (product decision, found live in a code audit: every
    real message now counts, no matter which path it takes) — this function
    no longer consumes it itself. The refund-on-UpstreamUnavailable logic
    below still operates on that same counter and is unchanged."""

    condition_codes = list(session.scalars(
        select(UserCondition.condition_code).where(UserCondition.user_id == user_id)
    ).all())
    escalation_confirmation = None
    suggested_activities = []
    v2_modes = None                                              # V2: which chips to offer; None = Add and Switch
    use_new = new_flow.enabled()
    gate_reason = None

    try:
        # A single LLM classification decides activity_recall / performance /
        # medical up front (see the module comment above _classify_data_intent
        # for why this replaced two regex gates). Medical retrieval below is
        # skipped entirely for either of the first two: found live ("analyze
        # my growth" pulling in 5 low-relevance osteoporosis chunks) that
        # irrelevant "Retrieved information" text makes the model latch onto
        # rule 4 and decline, ignoring real app data it was also given.
        if use_new and new_flow.router_enabled() and rlm_enabled and router_result is None:
            router_result = _run_router(session, user_id, question, openrouter_api_key)      # e.g. after a condition-button tap
        use_router = use_new and new_flow.router_enabled() and router_result is not None
        if not rlm_enabled:
            intent = {"intent": "medical", "start_date": None, "end_date": None}
        elif use_router:
            intent = _intent_from_router(session, user_id, question, router_result)
        else:
            intent = _classify_data_intent(session, user_id, question, api_key=openrouter_api_key, today=today_ist())

        if use_router and intent["intent"] == "conversation":
            return _conversation_answer(session, user_id, question, openrouter_api_key)
        if use_router and intent["intent"] == "program_targets":
            return _targets_answer(session, user_id, question)
        if use_router and v2.enabled() and intent["intent"] == "out_of_scope":
            return _plain_response(session, user_id, question, v2.OUT_OF_SCOPE_MESSAGE)
        if use_router and v2.enabled() and intent["intent"] == "add_exercise":
            return _add_exercise_answer(session, user_id, question, router_result)
        if use_router and v2.enabled() and intent["intent"] == "increase_target":
            return _increase_target_answer(session, user_id, question, openrouter_api_key)
        if use_router and v2.enabled() and intent["intent"] == "medical":
            _fixed = _routine_or_avoid_answer(session, user_id, question, condition_codes)
            if _fixed is not None:
                return _fixed

        if intent["intent"] == "activity_recall":
            # Bypasses the answer-writing LLM entirely, not just the medical
            # retrieval step. Found live: even with a provably correct
            # classification AND a provably correct data block (verified by
            # calling both directly right after a wrong answer), the
            # answer-writing LLM still sometimes mislabeled the date in its
            # prose — apparently pattern-matching onto some other date
            # mentioned elsewhere in conversation memory. This is a literal
            # data dump, not a synthesis task, so there is no real need for
            # an LLM here at all — see _activity_recall_answer's docstring.
            answer = _activity_recall_answer(
                session, user_id, intent["start_date"], intent["end_date"],
            )
            _save_chat_log(session, user_id, question, answer)
            return {
                "referred": False, "message": None, "answer": answer,
                "needs_condition_selection": False, "condition_options": None,
                "needs_escalation_confirmation": False, "escalation_proposal": None,
                "needs_program_update_confirmation": False, "suggested_activities": None,
                "needs_revert_confirmation": False, "revert_candidates": None,
                "quota": remaining_quota(session, user_id),
            }

        if intent["intent"] == "program_revert":
            # Same reasoning as activity_recall just above: this is a literal
            # database lookup ("what did a previous sport suggestion hide?"),
            # not a synthesis task — no need for the answer-writing LLM at all.
            _all = _revert_candidates(session, user_id)
            _no = _avoided_activity_types(session, condition_codes)                  # what the audited table does not approve for the user's conditions right now
            _refused = [h["display_name"] for h in _all if h["action"] == "restore" and h["activity_type"] in _no]
            hidden = [h for h in _all if not (h["action"] == "restore" and h["activity_type"] in _no)]        # never offer a button for something that would be refused
            _back = [h["display_name"] for h in hidden if h["action"] == "restore"]
            _paused = [h["display_name"] for h in hidden if h["action"] == "pause"]
            if not _all:
                answer = ("You don't have any recently changed exercises to revert — your "
                          "program hasn't been updated by a sport suggestion recently.")
            elif not hidden:
                answer = ("Earlier changes paused " + ", ".join(_refused) + ", but "
                          + ("it is" if len(_refused) == 1 else "they are") + " not approved for your health conditions now, so I can't bring "
                          + ("it" if len(_refused) == 1 else "them") + " back.")
            elif not _paused:
                answer = "Here's what changed recently: " + ", ".join(_back) + ". Want to bring these back?"
            else:
                answer = ("Here's what changed recently. I can bring back " + ", ".join(_back) + " and take out "
                          if _back else "Here's what changed recently. I can take out ") + ", ".join(_paused) \
                    + (" (the switch added " + ("it" if len(_paused) == 1 else "them") + "). Want me to put your plan back as it was?")
            if _refused and hidden:
                answer += (" Not brought back, because " + ("it is" if len(_refused) == 1 else "they are")
                           + " not approved for your health conditions now: " + ", ".join(_refused) + ".")
            _save_chat_log(session, user_id, question, answer)
            return {
                "referred": False, "message": None, "answer": answer,
                "needs_condition_selection": False, "condition_options": None,
                "needs_escalation_confirmation": False, "escalation_proposal": None,
                "needs_program_update_confirmation": False, "suggested_activities": None,
                "needs_revert_confirmation": bool(hidden), "revert_candidates": hidden or None,
                "quota": remaining_quota(session, user_id),
            }

        if intent["intent"] == "performance" and use_router and v2.enabled():
            _paused = session.execute(
                select(ActivityTaxonomy.display_name).join(UserProgram, UserProgram.activity_type == ActivityTaxonomy.activity_type)
                .where(UserProgram.user_id == user_id, UserProgram.status != "active")).scalars().all()
            _hit = next((n for n in _paused if v2.question_names(question, n)), None)
            if _hit:
                return _plain_response(session, user_id, question, v2.paused_message(_hit))
            performance_block, escalation_confirmation = _program_performance_context(session, user_id, question, openrouter_api_key=openrouter_api_key)
        else:
            performance_block, escalation_confirmation = (
                _performance_context(session, user_id, question, openrouter_api_key=openrouter_api_key)
                if intent["intent"] == "performance" else (None, None)
            )
        app_data_block = performance_block
        chunks = (
            [] if app_data_block is not None else
            vectorstore.retrieve_chunks(
                question, condition_codes, rag_db_url=rag_db_url, api_key=openrouter_api_key,
            )
        )
        # retrieve_chunks always returns its top_k rows regardless of how
        # irrelevant the question is (nearest-neighbor search never returns
        # "nothing") — a genuinely off-topic question still comes back with
        # real-looking chunks, just weakly-matched ones. Treat a weak best
        # match the same as no match at all, rather than handing the model
        # low-relevance content and hoping it independently notices none of
        # it actually answers the question (see RELEVANCE_THRESHOLD's comment).
        if use_new and app_data_block is None and (not chunks or chunks[0]["similarity"] < vectorstore.RELEVANCE_THRESHOLD):
            # weak first search (typical for Hinglish): try again with an English rewrite of the question and keep the better result
            _en = (router_result or {}).get("search_query") or new_flow.english_search_query(question, openrouter_api_key)
            if _en and _en.lower() != question.lower():
                _alt = vectorstore.retrieve_chunks(_en, condition_codes, rag_db_url=rag_db_url, api_key=openrouter_api_key)
                if _alt and (not chunks or _alt[0]["similarity"] > chunks[0]["similarity"]):
                    chunks = _alt
        if chunks and chunks[0]["similarity"] < vectorstore.RELEVANCE_THRESHOLD:
            chunks = []
        # A performance or activity-recall question legitimately has nothing in
        # the medical RAG corpus to retrieve — that must not fall through to
        # NO_CONTENT_ANSWER when there IS a real answer available from the
        # user's own tracked data (rules 9-10 above carve out this exception).
        if not chunks and app_data_block is None:
            answer = NO_CONTENT_ANSWER
            if use_new:
                new_flow.record_async(question, openrouter_api_key)      # learn the words behind this refusal (background; the answer never waits)
        else:
            # No "Retrieved information" section at all when answering from
            # the user's own data — even a "(nothing relevant found)"
            # placeholder gives rule 4 something to react to that isn't
            # actually about this question, which is exactly what caused the
            # bug above.
            retrieved_part = (
                "" if app_data_block is not None else
                "Retrieved information:\n"
                + ("\n\n".join(f"[{i + 1}] {c['text']}" for i, c in enumerate(chunks)) if chunks else
                   "(no directly relevant medical information retrieved for this question)")
                + "\n\n"
            )
            # NEW PIPELINE, medical answers only (progress / data answers keep the old prompt and list): the approved exercises come from the audited
            # 794-row table for the user's saved conditions, best first, with cautions; the answer prompt is the short one with the reply-language rule.
            medical_new = use_new and app_data_block is None
            v2_on = use_router and v2.enabled()
            want_ideas = False
            if medical_new:
                if not exercise_table.get().rows:
                    raise UpstreamUnavailable("exercise table could not be loaded")      # a SYSTEM problem: refund + "try again", never a medical refusal
                approved, approved_block, gate_reason = new_flow.medical_setup(condition_codes, ACTIVITY_CATEGORIES)
                want_ideas = bool(v2_on and not gate_reason and router_result.get("wants_exercises") and (router_result.get("sport") or (router_result.get("add_mode") == "add" and v2.asks_for_more(question))))
                if v2_on and not gate_reason and router_result.get("sport"):
                    _blocked = _blocked_named(session, router_result["sport"], approved)
                    if _blocked:
                        return _plain_response(session, user_id, question, v2.blocked_protocol("recommend", _blocked[0], _existing_condition_labels(session, user_id), approved))
            else:
                approved = _approved_activities(session, condition_codes)
                approved_block = (
                    "Approved exercises for this condition (with what each helps with): "
                    + ", ".join(f"{name} ({ACTIVITY_CATEGORIES.get(name, 'general movement')})"
                                for name in approved)
                    if approved else
                    "Approved exercises for this condition: none listed."
                )
            memory_block = _memory_questions_block(session, user_id) if v2_on else _memory_context_block(session, user_id)
            performance_part = f"{app_data_block}\n\n" if app_data_block else ""
            _player = (program_agent.read(session, user_id, _allowed_names(session, condition_codes))
                       if v2_on and medical_new and not gate_reason and (router_result.get("wants_exercises") or want_ideas) else None)      # PROGRAM AGENT: only for exercise requests
            messages = [
                {"role": "system", "content": (chat_prompts.SHORT_SYSTEM_PROMPT + (v2.V2_RULES if v2_on else "") + (v2.OTHER_IDEAS_RULE if want_ideas else "")) if medical_new else SYSTEM_PROMPT},
                {"role": "user",
                 "content": f"{retrieved_part}{approved_block}\n\n"
                            + (f"Saved conditions of this user: {', '.join(_existing_condition_labels(session, user_id)) or 'none'}. "
                               f"Mention only a pain or condition listed here or stated in the user's own message.\n\n" if v2_on else "")
                            + (program_agent.block(_player) + "\n\n" if _player is not None else "")
                            + f"{performance_part}{memory_block}\n\nQuestion: {question}"
                            + (_sport_instruction(router_result, approved) if v2_on and medical_new and router_result.get("sport") else "")
                            + (f"\n\n{chat_prompts.reply_language_line(question)}" if medical_new else "")},
            ]
            raw_answer = vectorstore.chat_complete(messages, openrouter_api_key)
            other_ideas: list[str] = []
            if v2_on:
                raw_answer, other_ideas = v2.split_other_ideas(raw_answer)              # the hidden OTHER_IDEAS line never reaches the user as text
                if not want_ideas:
                    other_ideas = []
            # Peel rule 11's marker line off BEFORE any safety backstop or the
            # user ever sees the text — it's for this app to read, never part
            # of the visible answer, and leaving it in would let a stray
            # "SUGGESTED_EXERCISES: ..." line either get shown verbatim or
            # trip the unapproved-exercise backstop on its own contents.
            answer, suggested_activities = _extract_suggested_exercises(
                session, raw_answer, approved
            )
            if medical_new:
                _low = answer.lower()
                if _low.lstrip().startswith(chat_messages.NO_INFO.lower()[:60]):
                    answer, suggested_activities = chat_messages.NO_INFO, []         # the model refused an override attempt: show only the standard refusal
                elif any(k in _low for k in chat_prompts.LEAK_MARKERS):
                    answer, suggested_activities = NO_CONTENT_ANSWER, []             # prompt text leaked into the reply: never show it
                elif len(answer.strip()) < 40:
                    raise UpstreamUnavailable("chat model returned an unusable answer")   # garbage reply: refund + "try again", never shown
            answer = _enforce_no_medication_or_surgery(answer)
            if v2_on:
                _catalog = list(session.execute(select(ActivityTaxonomy.display_name)).scalars().all())
                _allowed = list(approved)
                if app_data_block is not None:                                          # progress answers may name the exercises already in the user's program
                    _allowed += [n for (n,) in session.execute(
                        select(ActivityTaxonomy.display_name).join(UserProgram, UserProgram.activity_type == ActivityTaxonomy.activity_type)
                        .where(UserProgram.user_id == user_id)).all()]
                _before_cut = answer
                answer, _offending = v2.cut_unapproved(answer, _catalog, _allowed)
                was_corrected = False
                _asked = next((n for n in _offending if v2.question_names(question, n)), None)
                if _offending and (_asked or len(answer) < 80 or len(answer) < 0.35 * len(_before_cut)):
                    # the user ASKED about an unapproved exercise: say so plainly. Otherwise (a sport or a general question where the model drifted into one) keep the answer
                    # unless the cut left only a fragment, and then show what can be suggested instead of a gutted text or an irrelevant "can't recommend X".
                    answer = v2.fallback_after_cut([_asked], approved, _existing_condition_labels(session, user_id)) if _asked else v2.fallback_generic(approved)
                    was_corrected = True
            else:
                answer, was_corrected = _enforce_approved_exercises_only(session, answer, approved)
            # Drives the "add these suggested exercises to your program?"
            # confirmation (found live: a user wanted a one-tap way to
            # actually add exercises the chat just suggested, not just read
            # about them).
            #
            # Also cleared when the backstop corrected the answer (found live,
            # this session): that replacement text lists EVERY approved
            # exercise as "safe alternatives" in one sentence — the model's
            # own marker line was written for its ORIGINAL answer, not this
            # replacement, so it no longer describes what the user is
            # actually looking at. Confirming a suggestion here would
            # "switch focus" the whole program (see set_program_to_activities'
            # docstring) over a safety correction, not a recommendation.
            if was_corrected:
                suggested_activities = []
            if medical_new and gate_reason:
                # this set of conditions has no verified exercises: the notes-based answer stays, no exercise is offered, and the doctor message is added
                if use_router:
                    _gm = chat_messages.gate_message(gate_reason, len([c for c in condition_codes if c != "none"]))
                    if v2_on:
                        _gm = v2.gate_message_named(gate_reason, _existing_condition_labels(session, user_id), _gm)
                    answer = chat_rules.join_refusal(answer, _gm)
                else:
                    answer = answer.rstrip() + "\n\n" + chat_messages.refusal_message(gate_reason)
                suggested_activities = []
            if use_router and suggested_activities and not chat_rules.button_allowed(
                    router_result["wants_exercises"], answer, [a["display_name"] for a in suggested_activities]):
                suggested_activities = []                       # an information, medicine or yes/no answer: no add-exercises button
            if v2_on and len(suggested_activities) == 1 and not router_result.get("sport") and not v2.is_recommendation(answer):
                suggested_activities = []                       # an explanation of one exercise is not a recommendation
            if v2_on and suggested_activities:
                _active = set(session.scalars(select(UserProgram.activity_type).where(UserProgram.user_id == user_id, UserProgram.status == "active")).all())
                if _active == {a["activity_type"] for a in suggested_activities}:
                    suggested_activities = []                       # the program already is exactly this: nothing to add and nothing to switch
                elif all(a["activity_type"] in _active for a in suggested_activities):
                    # everything suggested is already in the program: "Add" would change nothing
                    if v2.asks_for_more(question):
                        suggested_activities = []
                    else:
                        v2_modes = ["switch"]
            if v2_on:
                answer = v2.clean_wording(answer)                  # no 'approved exercise list' / 'retrieved information' in the user's text
                answer = v2.strip_offers(answer)                   # no "Would you like me to set reminders?": the app cannot do it
            if _player is not None and not was_corrected:
                _before = len(suggested_activities)
                _note, suggested_activities = program_agent.note(_player, suggested_activities)
                if _note:
                    answer = answer.rstrip() + "\n\n" + _note
                if suggested_activities and len(suggested_activities) < _before:
                    v2_modes = ["add"]                              # some are already in the player: only ADD makes sense (Switch would pause what the user keeps)
            if other_ideas and not was_corrected and not answer.lower().lstrip().startswith(chat_messages.NO_INFO.lower()[:40]):
                _avoid_types = _avoided_activity_types(session, condition_codes)
                _avoid_names = ([n for (n,) in session.execute(select(ActivityTaxonomy.display_name).where(ActivityTaxonomy.activity_type.in_(_avoid_types))).all()]
                                if _avoid_types else [])
                _ideas = v2.filter_ideas(other_ideas, approved_names=approved, avoided_names=_avoid_names,
                                         catalog_names=list(session.execute(select(ActivityTaxonomy.display_name)).scalars().all()), condition_codes=condition_codes)
                if _ideas:
                    answer = answer.rstrip() + "\n\n" + v2.other_ideas_block(_ideas)
            if use_new and urgent_unavailable:
                answer += chat_messages.UNAVAILABLE_NOTE

            # Tag this exchange's memory row by what it was actually RETRIEVED as
            # being about, not by the user's onboarded condition_codes. Those two
            # can now differ — retrieval also covers EXTENDED_TOPICS (5.8) the user
            # never selected, e.g. someone onboarded for chronic_back_pain asking
            # about their shoulder. Tagging by the onboarded condition instead would
            # silently file a shoulder complaint under "back pain," corrupting the
            # persisting-pain count below (a real bug, found and fixed 5.9). This
            # never touches _approved_activities above, which must keep using the
            # user's real selected condition — exercise approval is a hard safety
            # boundary, unrelated to what topic this specific question was about.
            retrieved_topics = list(dict.fromkeys(c["condition_code"] for c in chunks))

            # Count PAST persisting-pain reports before adding this exchange's own,
            # so a fresh report crossing the threshold escalates on THIS answer,
            # not one exchange later.
            past_persisting = _pain_persisting_count(session, user_id, retrieved_topics)
            extracted = _save_memory(session, user_id, retrieved_topics, question, answer, openrouter_api_key)
            total_persisting = past_persisting + (1 if extracted and extracted["pain_persisting"] else 0)
            if total_persisting >= PAIN_PERSISTING_THRESHOLD:
                if not use_router or (extracted and extracted["pain_persisting"] and not _note_shown_today(session, user_id)):
                    answer += DOCTOR_ESCALATION_NOTE.format(n=total_persisting)
    except RuntimeError as exc:
        # Found live (third-pass code audit): this used to catch only
        # UpstreamUnavailable. chat_complete/vectorstore also raise the
        # sibling ResponseTruncated (a real, documented reasoning-model
        # failure mode, not hypothetical) and a bare RuntimeError for a
        # non-retryable upstream status — neither was caught here, so they
        # propagated past this handler and crashed the request instead of
        # degrading to a clean, refunded 429. RuntimeError is the shared base
        # of all three exceptions, so this now catches every variant.
        if not isinstance(exc, (UpstreamUnavailable, ResponseTruncated)):
            # Not one of the two expected/documented upstream failure modes —
            # log it so a genuine bug doesn't silently masquerade as "busy,
            # try again" with no trace anywhere (ApiError responses aren't
            # logged by the global handler, only truly unhandled exceptions are).
            _log.warning("Unexpected RuntimeError in _retrieve_and_answer for "
                         "user_id=%s, refunding quota and returning 429", user_id,
                         exc_info=exc)
        # Only refund what THIS request actually charged. On the
        # condition-confirmation path the slot was paid by the earlier
        # classify turn and redeemed via the pending marker, so refunding
        # here would hand back a slot this request never took — a free
        # question (and a free billed classify turn) for every upstream
        # hiccup.
        if charged:
            _refund_quota(session, user_id)
            # Found live (third-pass code audit, finding #5): _consume_quota's
            # increment is now committed durably up front (see answer_query), so
            # without an equally durable commit here, THIS decrement would be the
            # one silently lost to the rollback the RateLimited below triggers —
            # inverting the bug instead of fixing it. Commit the refund before
            # raising so the user doesn't lose a real question to an infra hiccup.
            session.commit()
        _busy = ("The guidance service is busy right now. Please try again in a moment — "
                 "this attempt was not counted against your daily questions.")
        if use_new:
            _ref = chat_messages.new_error_ref()
            chat_messages.log_system_error(_ref, type(exc).__name__, "chat", exc)
            _busy += f" (If it keeps happening, quote this reference: {_ref})"
        raise RateLimited(_busy, retry_after=30)

    # The transcript the user actually sees — including the escalation note, if
    # one got appended above, since that's genuinely what happened in this
    # exchange from their side of the screen.
    _save_chat_log(session, user_id, question, answer)

    return {
        "referred": False, "message": None, "answer": answer,
        "needs_condition_selection": False, "condition_options": None,
        "needs_escalation_confirmation": escalation_confirmation is not None,
        "escalation_proposal": escalation_confirmation,
        "needs_program_update_confirmation": bool(suggested_activities),
        "suggested_activities": suggested_activities or None,
        "program_update_mode": "switch" if v2_modes == ["switch"] else ("add" if (v2_modes == ["add"] or router_result and router_result.get("add_mode") == "add" and (not v2.enabled() or v2.asks_for_more(question))) else "switch"),
        **({"program_update_modes": v2_modes or ["add", "switch"]} if (use_router and v2.enabled() and suggested_activities) else {}),      # V2: the user chooses; Add first (changes nothing else)
        "needs_revert_confirmation": False, "revert_candidates": None,
        "quota": remaining_quota(session, user_id),
    }


def _confirm_escalation(session: Session, user_id: str, confirmation: dict, *,
                         openrouter_api_key: str, rlm_enabled: bool = True) -> dict:
    """Phase 5 write path. Deliberately does NOT trust whatever the client
    echoes back for proposed_pct_increase — re-derives a fresh proposal at
    write time (16.3's principle: re-verify, don't rely on a possibly-stale
    earlier computation) and applies THAT, never a client-supplied number.
    Not limited by the message quota (DAILY_LIMIT): confirming an
    already-shown proposal is a UI action, not a new question (16.6 is about
    the conversation itself). It IS bounded by its own separate, much larger
    ESCALATION_DAILY_LIMIT below — found live (third-pass code audit): every
    confirmed:true tap re-derives the proposal via a real, billed LLM call
    (analyze_performance), and with no cap of its own a client could replay
    that request indefinitely for unlimited billed usage."""
    activity_type = (confirmation.get("activity_type") or "").strip()
    if not activity_type:
        raise ValidationError("escalation_confirmation requires activity_type.")

    base_response = {
        "referred": False, "answer": None,
        "needs_condition_selection": False, "condition_options": None,
        "needs_escalation_confirmation": False, "escalation_proposal": None,
        "needs_program_update_confirmation": False, "suggested_activities": None,
        "needs_revert_confirmation": False, "revert_candidates": None,
        "quota": remaining_quota(session, user_id),
    }

    if not rlm_enabled:
        # RLM_CHAT_ANALYSIS_ENABLED=false (Phase 7) — a proposal could only
        # have been shown while the flag was on, so this is a stale confirm
        # tap from before it was switched off; decline the same way an
        # eligibility re-check failing would.
        return {
            **base_response, "escalation_applied": False,
            "message": "This offer isn't available anymore — your recent numbers have changed.",
        }

    if not confirmation.get("confirmed"):
        return {**base_response, "message": "No changes made.", "escalation_applied": False}

    if not _consume_quota(session, user_id,
                           quota_type=ESCALATION_QUOTA_TYPE, limit=ESCALATION_DAILY_LIMIT):
        raise QuotaExhausted(
            "You've confirmed several exercise updates today already — try again tomorrow."
        )
    # Durable immediately — same reasoning as the message quota in
    # answer_query: analyze_performance below is a real, billed LLM call, and
    # without an early commit a later failure elsewhere in this request would
    # roll this charge back too, silently reopening the unlimited-replay gap
    # this cap exists to close.
    session.commit()

    findings = progression_analysis.analyze_performance(
        session, user_id, activity_type, "escalation confirmation",
        api_key=openrouter_api_key,
    )
    proposal = findings["escalation_proposal"]
    if not proposal.get("eligible"):
        return {
            **base_response, "escalation_applied": False,
            "message": "This offer isn't available anymore — your recent numbers have changed.",
        }

    adherence = next((f for f in findings["findings"] if f["check"] == "adherence_trend"), {})
    row = progression_service.apply_escalation(
        session, user_id, activity_type, proposal["proposed_pct_increase"],
        adherence_pct_snapshot=adherence.get("value"),
    )
    taxonomy = session.get(ActivityTaxonomy, activity_type)
    return {
        **base_response, "escalation_applied": True,
        "message": f"Done — {taxonomy.display_name}'s target is updated.",
        "new_target": float(row.new_target),
    }


def _confirm_program_update(session: Session, user_id: str, confirmation: dict) -> dict:
    """Write path for the chat's "add these suggested exercises to your
    program?" confirmation (found live: a user asked the rehab chatbot for
    sport-specific exercises and wanted a one-tap way to actually add them).

    Deliberately does NOT trust the client-supplied activity_types list at
    face value — re-validates every one against the user's CURRENT approved
    list (16.3's principle: re-verify, don't rely on a possibly-stale earlier
    computation) before applying any of them, since a condition could have
    changed between when the chat answer was shown and this confirmation tap
    actually arriving. No LLM call and no quota cost — this only replays a
    set of exercises the answer already named and the safety grid already
    approved; there's nothing here to re-derive or bill for."""
    activity_types = confirmation.get("activity_types")
    if not isinstance(activity_types, list) or not activity_types:
        raise ValidationError("program_confirmation requires a non-empty activity_types list.")
    if not all(isinstance(a, str) for a in activity_types):
        raise ValidationError("program_confirmation.activity_types must be a list of strings.")

    base_response = {
        "referred": False, "answer": None,
        "needs_condition_selection": False, "condition_options": None,
        "needs_escalation_confirmation": False, "escalation_proposal": None,
        "needs_program_update_confirmation": False, "suggested_activities": None,
        "needs_revert_confirmation": False, "revert_candidates": None,
        "quota": remaining_quota(session, user_id),
    }

    if not confirmation.get("confirmed"):
        return {**base_response, "message": "No changes made.", "program_updated": False}

    condition_codes = list(session.scalars(
        select(UserCondition.condition_code).where(UserCondition.user_id == user_id)
    ).all())
    # Deliberately the SAFE-only list here, not merely "not avoided" — and
    # deliberately stricter than what the program itself would admit. This
    # path validates a CLIENT-SUPPLIED list against what the chat was ever
    # allowed to name (_approved_activities uses the same safe-only rule), so
    # anything outside it means the client sent something this server never
    # offered — exactly what 16.3's re-verification exists to catch. See
    # tests/test_rag_program_confirmation.py, which pins this behaviour.
    # (_confirm_revert intentionally differs: its candidates are the user's
    # OWN previously-active entries, which legitimately include `modify`.)
    if new_flow.enabled():
        approved_types = (set() if exercise_table.gate(condition_codes) else
                          exercise_table.approved_types(condition_codes) - _avoided_activity_types(session, condition_codes))
    else:
        approved_types = _approved_activity_types(session, condition_codes)
    if v2.enabled() and confirmation.get("mode") == "add" and not exercise_table.gate(condition_codes):
        approved_types = approved_types | (exercise_table.allowed_types(condition_codes) - _avoided_activity_types(session, condition_codes))
    safe_types = [a for a in activity_types if a in approved_types]
    if not safe_types:
        return {
            **base_response, "program_updated": False,
            "message": "None of those exercises are currently approved for you — nothing changed.",
        }

    result = set_program_to_activities(session, user_id, safe_types, hide_others=confirmation.get("mode") != "add")
    taxonomy_names = {
        t.activity_type: t.display_name for t in session.scalars(select(ActivityTaxonomy)).all()
    }
    added_names = [taxonomy_names.get(a, a) for a in result["added"]]
    shown_names = [taxonomy_names.get(a, a) for a in result.get("shown", [])]
    already_names = [taxonomy_names.get(a, a) for a in result["already_active"] if a not in result.get("shown", [])]
    hidden_names = [taxonomy_names.get(a, a) for a in result["hidden"]]
    # Found live (user report): "it suggested 5, only 4 got added" — every
    # suggested exercise WAS correctly active afterward; the message just
    # never mentioned the one that was already in the program before this
    # confirmation even ran, since it only reported what CHANGED. Silently
    # omitting an exercise looks identical to it having failed to add, even
    # though nothing was ever actually wrong — every suggested exercise's
    # outcome is accounted for explicitly now, not just the ones that changed.
    parts = []
    if added_names:
        parts.append(f"Added: {', '.join(added_names)}")
    if shown_names:
        parts.append(f"Now shown in your exercise player: {', '.join(shown_names)}")
    if already_names:
        parts.append(f"Already in your program: {', '.join(already_names)}")
    if hidden_names:
        parts.append(f"Hidden from your program (kept, not deleted — you can add it back "
                      f"any time): {', '.join(hidden_names)}")
    message = "; ".join(parts) if parts else "These were already in your program — nothing to add."
    if hidden_names and v2.enabled():
        message += "." + v2.SWITCH_HINT
    return {
        **base_response, "program_updated": True, "message": message,
        "program": result["program"],
    }


def _confirm_revert(session: Session, user_id: str, confirmation: dict) -> dict:
    """Write path for the chat's "bring these back?" revert confirmation
    (found live: a user asked the chatbot to undo a previous sport-focus
    program change, rather than manually re-adding each exercise on the
    dashboard).

    Restores specific hidden entries to active with whatever target they
    already had, and never hides anything else (unlike
    set_program_to_activities, which replaces the whole active set) - with
    one exception: an exercise a Switch itself put into the plan is put back
    to what it was before (removed again, or paused - undo_switch_additions),
    so the plan returns to exactly what it was before the Switch. Re-
    validates the client-supplied activity_types against what's ACTUALLY
    currently hidden by sport_focus_change for this user (16.3's principle:
    re-verify, don't trust a possibly-stale value the client echoes back) —
    a user_removed or condition_safety suspension is never touched here,
    same reasoning as _hidden_by_sport_focus_change's own docstring."""
    activity_types = confirmation.get("activity_types")
    if not isinstance(activity_types, list) or not activity_types:
        raise ValidationError("revert_confirmation requires a non-empty activity_types list.")
    if not all(isinstance(a, str) for a in activity_types):
        raise ValidationError("revert_confirmation.activity_types must be a list of strings.")

    base_response = {
        "referred": False, "answer": None,
        "needs_condition_selection": False, "condition_options": None,
        "needs_escalation_confirmation": False, "escalation_proposal": None,
        "needs_program_update_confirmation": False, "suggested_activities": None,
        "needs_revert_confirmation": False, "revert_candidates": None,
        "quota": remaining_quota(session, user_id),
    }

    if not confirmation.get("confirmed"):
        return {**base_response, "message": "No changes made.", "program_updated": False}

    _cands = {c["activity_type"]: c["action"] for c in _revert_candidates(session, user_id)}          # re-read from the database, never trusted from the client
    candidate_types = [a for a in activity_types if _cands.get(a) == "restore"]
    pause_types = [a for a in activity_types if _cands.get(a) == "pause"]                              # put in by a Switch: back to what they were before it
    if not candidate_types and not pause_types:
        return {
            **base_response, "program_updated": False,
            "message": "Nothing to revert — those exercises aren't currently hidden.",
        }

    # 16.3 re-verify, don't trust a possibly-stale value: an exercise hidden
    # by a sport-focus change can still have become unsafe since then (e.g. a
    # condition added in between) — restore_program_activities itself does
    # not check this (it's a generic "un-hide" used elsewhere), so the check
    # has to happen here.
    #
    # The gate is "not avoided", NOT "in the safe list". Those are very
    # different: the program admits everything the grid doesn't mark `avoid`
    # (profile.service._blocked_activities), so `modify` activities are
    # legitimately on a user's dashboard. Gating on the safe-only list would
    # refuse to restore an exercise the user is perfectly entitled to have —
    # e.g. a sprain_strain user has ELEVEN activities rated `modify` and zero
    # `avoid`, so a safe-only gate would refuse all but Walking and this whole
    # feature would appear broken to them.
    condition_codes = list(session.scalars(
        select(UserCondition.condition_code).where(UserCondition.user_id == user_id)
    ).all())
    avoided = _avoided_activity_types(session, condition_codes)
    safe_types = [a for a in candidate_types if a not in avoided]
    if candidate_types and not safe_types and not pause_types:
        return {
            **base_response, "program_updated": False,
            "message": "Those exercises are no longer approved for your current conditions — nothing restored.",
        }

    result = restore_program_activities(session, user_id, safe_types) if safe_types else {"restored": [], "program": None}
    paused_result = undo_switch_additions(session, user_id, pause_types) if pause_types else {"undone": [], "program": None}
    taxonomy_names = {
        t.activity_type: t.display_name for t in session.scalars(select(ActivityTaxonomy)).all()
    }
    restored_names = [taxonomy_names.get(a, a) for a in result["restored"]]
    paused_names = [taxonomy_names.get(a, a) for a in paused_result["undone"]]
    message = (
        f"Restored: {', '.join(restored_names)}" if restored_names
        else "Nothing to revert — those exercises aren't currently hidden."
    )
    if paused_names:
        message = (f"Restored: {', '.join(restored_names)}. " if restored_names else "") + f"Put back as before the switch: {', '.join(paused_names)}."
        _not_back = [taxonomy_names.get(a, a) for a in candidate_types if a not in safe_types]
        if _not_back:
            message += f" Not restored, no longer approved for your current conditions: {', '.join(_not_back)}."
    return {
        **base_response, "program_updated": bool(restored_names or paused_names), "message": message,
        "program": paused_result["program"] or result["program"],
    }


def answer_query(session: Session, user_id: str, question: str, *, rag_db_url: str,
                  openrouter_api_key: str, condition_confirmation: dict | None = None,
                  escalation_confirmation: dict | None = None,
                  program_confirmation: dict | None = None,
                  revert_confirmation: dict | None = None, rlm_enabled: bool = True) -> dict:
    _require_not_managed(session, user_id)
    # A tap confirming (or declining) an escalation proposal (16.5) — a
    # question-less UI action, so this is handled before the "ask a question
    # first" validation below even applies.
    if escalation_confirmation is not None:
        if is_referred(session, user_id):
            return {
                "referred": True, "message": REFERRAL_MESSAGE, "answer": None,
                "needs_condition_selection": False, "condition_options": None,
                "needs_escalation_confirmation": False, "escalation_proposal": None,
                "needs_program_update_confirmation": False, "suggested_activities": None,
                "needs_revert_confirmation": False, "revert_candidates": None,
                "escalation_applied": False,
                "quota": remaining_quota(session, user_id),
            }
        return _confirm_escalation(
            session, user_id, escalation_confirmation,
            openrouter_api_key=openrouter_api_key, rlm_enabled=rlm_enabled,
        )

    # A tap confirming "add these suggested exercises to my program?" — also
    # a question-less UI action, same reasoning as escalation_confirmation
    # above.
    if program_confirmation is not None:
        if is_referred(session, user_id):
            return {
                "referred": True, "message": REFERRAL_MESSAGE, "answer": None,
                "needs_condition_selection": False, "condition_options": None,
                "needs_escalation_confirmation": False, "escalation_proposal": None,
                "needs_program_update_confirmation": False, "suggested_activities": None,
                "needs_revert_confirmation": False, "revert_candidates": None,
                "program_updated": False,
                "quota": remaining_quota(session, user_id),
            }
        return _confirm_program_update(session, user_id, program_confirmation)

    # A tap confirming "bring back what a previous sport suggestion hid?" —
    # also a question-less UI action, same reasoning as the two above.
    if revert_confirmation is not None:
        if is_referred(session, user_id):
            return {
                "referred": True, "message": REFERRAL_MESSAGE, "answer": None,
                "needs_condition_selection": False, "condition_options": None,
                "needs_escalation_confirmation": False, "escalation_proposal": None,
                "needs_program_update_confirmation": False, "suggested_activities": None,
                "needs_revert_confirmation": False, "revert_candidates": None,
                "program_updated": False,
                "quota": remaining_quota(session, user_id),
            }
        return _confirm_revert(session, user_id, revert_confirmation)

    question = (question or "").strip()
    if not question:
        raise ValidationError("Ask a question first.")
    if len(question) > MAX_QUESTION_LENGTH:
        raise ValidationError(f"Keep questions under {MAX_QUESTION_LENGTH} characters.")

    if is_referred(session, user_id):
        # No guidance at all (13.3b) — and no quota spent getting told that.
        return {
            "referred": True, "message": REFERRAL_MESSAGE, "answer": None,
            "needs_condition_selection": False, "condition_options": None,
            "needs_escalation_confirmation": False, "escalation_proposal": None,
            "needs_program_update_confirmation": False, "suggested_activities": None,
            "needs_revert_confirmation": False, "revert_candidates": None,
            "quota": remaining_quota(session, user_id),
        }

    # NEW CHAT PIPELINE (NEW_CHAT_PIPELINE=true): the FREE steps (daily limit left, urgent-word rules, dictionary word) run BEFORE the daily question is
    # charged, so a request they refuse uses none of the 5. See app/rag/new_flow.py. Off by default: the code below is then exactly the old flow.
    new_pipeline = new_flow.enabled()
    if new_pipeline and condition_confirmation is None:
        early = new_flow.early_checks(sys.modules[__name__], session, user_id, question)
        if early is not None:
            return early
        if v2.enabled() and not new_flow.simple_flow():
            _fast = _fast_add_reply(session, user_id, question)
            if _fast is not None:
                return _fast

    # Found live (code audit) — explicit product decision closing the gap:
    # EVERY real message now consumes one quota slot immediately,
    # unconditionally, right here — before any classification or LLM work
    # happens, not just once a real *answer* comes out the other end. This
    # replaces the old design where only _retrieve_and_answer charged
    # anything, which meant the condition-classifier path below
    # (_classify_condition / _demote_if_extended_topic_fits_better) never
    # touched the counter at all: a user could send unlimited
    # symptom-sounding messages, each triggering 1-2 billed OpenRouter
    # calls, without ever spending a slot, bounded only by the generic
    # per-minute request throttle (not a cost control).
    # condition_confirmation is a continuation of the ORIGINAL question, not a
    # new one — that question already paid for its slot below the first time
    # it was sent (the classify-and-ask-which-condition turn). Charging again
    # would make a symptom-sounding question cost 2 of the user's 5 daily
    # slots instead of 1: one for being asked "which condition?", a second
    # for actually getting answered once they tapped a choice.
    #
    # But that exemption must be PROVEN server-side, never taken on the
    # client's word. The confirmation branch below runs a full, billed
    # retrieve-and-answer, so trusting a client-supplied
    # `condition_confirmation` field would let anyone get unlimited free LLM
    # answers just by always including it. The marker below is set only when
    # this server actually returned needs_condition_selection (having already
    # charged for that turn), and deleting it is atomic, so it can be
    # redeemed exactly once.
    already_paid = False
    if condition_confirmation is not None:
        try:
            already_paid = bool(
                rc.durable().delete(rc.key_pending_condition_confirm(user_id))
            )
        except Exception:  # noqa: BLE001
            # Redis unreachable: fall through to charging a slot. Wrong in the
            # user's favour is a second slot spent; wrong the other way is
            # free unlimited billed answers, so this must never fail open.
            _log.warning("pending-condition marker unreadable for user %s", user_id)
            already_paid = False
    if not already_paid:
        if not _consume_quota(session, user_id):
            raise QuotaExhausted(
                f"You've used all {DAILY_LIMIT} questions for today. Come back tomorrow, or "
                "speak to a doctor if this feels urgent."
            )
        # Found live (third-pass code audit, finding #1/#5): the whole request runs
        # inside ONE transaction (see routes.py's session_scope), which rolls back
        # on ANY uncaught exception raised later — including a real, billed LLM
        # failure this function doesn't even know how to anticipate. Without this
        # commit, that rollback silently undoes the increment above too, so a
        # user hitting any downstream failure gets a free, uncharged retry while
        # the LLM call was already paid for — exactly the "counts no matter what"
        # gap this counter was built to close. Committing here makes the charge
        # durable immediately, before any LLM work is attempted; a later
        # SQLAlchemy commit() only affects writes made AFTER this point.
        session.commit()

    # A tap confirming (or declining) a condition suggested for THIS question.
    # 5.6: the model only ever suggests — recording a condition always requires
    # this separate, explicit step, never the classification below on its own.
    if condition_confirmation is not None:
        code = condition_confirmation.get("code")
        if code:
            valid_codes = {o["code"] for o in _condition_options(session, user_id)}
            if code not in valid_codes:
                raise ValidationError("Unknown or already-selected condition.")
            result = _add_condition(session, user_id, code)
            if result.get("referred"):
                return {
                    "referred": True, "message": result["message"], "answer": None,
                    "needs_condition_selection": False, "condition_options": None,
                    "needs_escalation_confirmation": False, "escalation_proposal": None,
                    "needs_program_update_confirmation": False, "suggested_activities": None,
                    "needs_revert_confirmation": False, "revert_candidates": None,
                    "quota": remaining_quota(session, user_id),
                }
        return _retrieve_and_answer(
            session, user_id, question, rag_db_url=rag_db_url,
            openrouter_api_key=openrouter_api_key, rlm_enabled=rlm_enabled,
            # If the pending marker paid for this turn, this request charged
            # nothing — so there is nothing for a failure to refund.
            charged=not already_paid,
        )

    urgent_unavailable = False
    if new_pipeline:
        finished, urgent_unavailable = new_flow.paid_checks(sys.modules[__name__], session, user_id, question, openrouter_api_key)
        if finished is not None:
            return finished

    router_result = None
    if new_pipeline and new_flow.router_enabled():
        router_result = _run_router(session, user_id, question, openrouter_api_key)      # None = fall back to the old separate calls
        if router_result is not None and v2.enabled():
            _rule = _v2_rule_response(session, user_id, question, router_result)
            if _rule is not None:
                return _rule

    # Fresh message: check whether it looks like an undiagnosed symptom
    # description (5.6). Quota for this message was already consumed above,
    # unconditionally, before this point — _classify_condition and
    # _demote_if_extended_topic_fits_better are real, billed OpenRouter
    # calls, but both already fail closed to "no match" on any LLM problem
    # (see their own docstrings), so there's nothing further to refund here
    # specifically; a genuine failure only becomes visible (and refundable)
    # once it reaches _retrieve_and_answer's own answer attempt below.
    options = _condition_options(session, user_id)
    existing_labels = _existing_condition_labels(session, user_id)
    matched_code = (router_result["condition"] if router_result is not None
                    else _classify_condition(question, options, existing_labels, openrouter_api_key))
    if matched_code:
        matched_code = _demote_if_extended_topic_fits_better(
            question, matched_code, rag_db_url=rag_db_url, api_key=openrouter_api_key,
        )
    if matched_code and v2.enabled() and matched_code not in exercise_table.IN_TABLE:
        # A tap SAVES the condition, and a condition outside the audited table blocks every exercise answer afterwards. So the chat never offers to save one: it says
        # what it could be, that it has no verified exercises for it, and how to add it from the profile if the user really has it.
        _label = next((o["label"] for o in options if o["code"] == matched_code), matched_code)
        return _plain_response(session, user_id, question, v2.not_in_table_message(_label))
    if matched_code:
        matched_label = next(o["label"] for o in options if o["code"] == matched_code)
        # This turn charged a slot but returns no answer — mark that the
        # follow-up confirmation is already paid for. Short TTL: it covers a
        # user tapping a chip in the next few minutes, not a slot banked for
        # tomorrow. Best-effort — if Redis is unavailable the confirmation
        # simply charges its own slot (the old behaviour), never free.
        try:
            rc.durable().setex(
                rc.key_pending_condition_confirm(user_id), PENDING_CONFIRM_TTL_SECONDS, "1"
            )
        except Exception:  # noqa: BLE001 — never fail the response over this
            _log.warning("could not set pending-condition marker for user %s", user_id)
        return {
            "referred": False, "message": None, "answer": None,
            "needs_condition_selection": True,
            "condition_options": {
                "prompt_text": "That sounds like it could be one of these — which fits?",
                "matched_code": matched_code,
                "matched_label": matched_label,
                "description": v2.CONDITION_DESCRIPTIONS.get(matched_code) if v2.enabled() else None,
                "original_question": question,
            },
            "needs_escalation_confirmation": False, "escalation_proposal": None,
            "needs_program_update_confirmation": False, "suggested_activities": None,
            "needs_revert_confirmation": False, "revert_candidates": None,
            "quota": remaining_quota(session, user_id),
        }

    return _retrieve_and_answer(
        session, user_id, question, rag_db_url=rag_db_url,
        openrouter_api_key=openrouter_api_key, rlm_enabled=rlm_enabled,
        urgent_unavailable=urgent_unavailable, router_result=router_result,
    )
