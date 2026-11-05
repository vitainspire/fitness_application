"""The merged router call (NEW_ROUTER=true): ONE AI call that decides how a chat message is handled. Replaces the separate condition check, intent check and
English-rewrite calls. Prototype and test: testing_temparary/plan_proto (54 of 54 correct on the real issue messages with gpt-4.1-mini).

Returns None on ANY failure (provider error, bad JSON): the caller then falls back to the old separate calls, so the chat never breaks because of the router.
"""
from __future__ import annotations

import json
import logging
import re

from app.rag import urgent_check, vectorstore
from app.rag import v2 as v2_checks

_log = logging.getLogger(__name__)
INTENTS = ("medical", "conversation", "program_targets", "activity_recall", "performance", "program_revert", "add_exercise", "out_of_scope")

PROMPT = 'A user of a fitness/rehab app sent a chat message. Decide how the app should handle it and reply with EXACTLY one JSON object and nothing else:\n\n{{"intent": "medical" | "conversation" | "program_targets" | "activity_recall" | "performance" | "program_revert", "start_date": "YYYY-MM-DD or null", "end_date": "YYYY-MM-DD or null", "condition": "<one code from the CONDITIONS list, or NONE>", "wants_exercises": true or false, "add_mode": "add" or "switch", "search_query": "short self-contained English search query about the health topic, or empty"}}\n\nToday\'s date is {today} ({weekday}).\n{history_bounds}\n\nINTENT\n- "conversation": the user tells the app something about themselves (their name, a greeting, thanks) or asks what THEY said or asked earlier in this chat, e.g. "Rohan here", "do you remember my name", "what was my last question", "thank you so much", "good evening". This is not a health question. A short statement or greeting stays "conversation" even if the previous message was about exercises. General-knowledge, off-topic or technical questions (capital cities, maths, code, shopping, "write me a poem") are NEVER "conversation": they are "medical" so the app can refuse them.\n- "program_targets": the user asks how many reps, how many sets, how long or how far they should do an exercise, or what their current target is, e.g. "number of repetitions I should do", "walking distance goal for today", "sets for my dead bug". Only questions about NUMBERS (reps, sets, duration, distance, target). Asking WHICH exercises to do, or for MORE or DIFFERENT exercises ("show me additional movements", "what else can I do", "besides what I do now"), is NOT program_targets even when the previous message was about reps: it is "medical". Judge the NEW message on its own.\n- "activity_recall": the user asks what they literally logged or did on a specific day or range of days, e.g. "which workouts did I finish on Friday", "my log for the past ten days", "what had I done two days back". Resolve start_date and end_date to real calendar dates (a single day has start_date == end_date; relative phrases count back from today; a weekday name means the most recent such day on or before today). Only for questions about their WORKOUT log.\n- "performance": the user asks about their own progress, trend, or whether they are improving, e.g. "am I getting better", "give me a summary of my consistency", "have I been regular lately". Dates null.\n- "program_revert": the user wants to UNDO a recent change to their exercise program or get an old exercise back, e.g. "put my program back the way it was", "I want the paused exercises again", "cancel the last change you made to my plan". Dates null.\n- "medical": anything else, including health, pain, exercise guidance, medicine and off-topic questions. Dates null.\n\nCONDITION\nThey have ALREADY told the app about: {existing}.\nDoes the message clearly describe a DIFFERENT problem that matches one of these NOT-yet-selected conditions? If yes give its code, otherwise NONE. A general or follow-up question that could relate to a condition they already reported is NONE. A body part or symptom that does not genuinely match any listed option is NONE (do not force the closest one). The message may be English, Hindi, Hinglish or misspelt.\nCONDITIONS:\n{options}\n\nWANTS_EXERCISES\ntrue only if the user is asking the app to SUGGEST exercises for them, e.g. "which movements suit me", "what should I do for a stiff neck", "suggest a plan", "where do I begin", "what is good for playing badminton". A message that simply DESCRIBES a pain or problem the user has ("my lower back hurts after sitting", "I have knee pain") counts as true, because the app answers it with exercises; EXCEPT when it says the pain is not improving or getting worse (false). false for everything else: explaining one exercise, ANY yes/no or "is X good for me / can I do X / should I do X" question about a specific exercise or activity (yoga, swimming, running, squats, walking...), asking about medicine or surgery, how many reps, small talk, data questions. The user must be asking WHICH exercises to do, not whether one named activity is OK.\n\nADD_MODE\n"add" if the user asks for exercises IN ADDITION to what they already do (also, besides, more, in addition, iske alawa, aur). Otherwise "switch".\n\nSEARCH_QUERY: for a medical message, a short English search query for the notes (translate Hindi or Hinglish; fix spelling). Empty for other intents.\n\nPrevious message in this conversation (for follow-ups; may be blank):\n{previous}\n\nMessage: "{question}"\n'


# ---- PIPELINE V2 (NEW_PIPELINE_V2=true): the tested prompt above plus the "add_exercise" intent, the sport / exercise / claims_healthy / unsupported_data fields,
# the stricter wants_exercises rule, and the previous ANSWER (so "add that" can be resolved).
PROMPT_V2 = PROMPT.replace(
    '"program_revert", "start_date"', '"program_revert" | "add_exercise", "start_date"').replace(
    '"add_mode": "add" or "switch", "search_query"',
    '"sport": "<sport or activity the user wants to play or do, or empty>", "exercise": "<the exercise the user wants to add, as written, or empty>", '
    '"claims_healthy": true or false, "unsupported_data": true or false, "search_query"').replace(
    '- "medical": anything else,',
    '- "add_exercise": the user asks to ADD, include, put or resume a specific exercise or activity in their program / plan / routine, e.g. "include wall calf stretch in my routine", "I want neck hold added", '
    '"can you put shoulder circles in my program", "resume the one I paused", "add those too". Put the exercise name in "exercise" exactly as the user wrote it. If the user says "that", "it", '
    '"this" or "these", leave "exercise" empty (the app reads the previous answer). Asking for MORE or DIFFERENT exercises without naming one ("show me additional movements", '
    '"anything else I could try") is NOT add_exercise: it is "medical". Use add_exercise ONLY when the message has an add / include / put / resume verb or says "in my plan"; a request for '
    'exercise ADVICE ("suggest something for cycling", "what should I do for my hip") is "medical". A request to REMOVE or DELETE an exercise from the plan is also add_exercise '
    '(put the name in "exercise").\n- "medical": anything else,').replace(
    'Judge the NEW message on its own.',
    'Rest time between sets, technique, posture, breathing and how often to train are normal health questions: "medical", not program_targets. Judge the NEW message on its own.').replace(
    'The message may be English, Hindi, Hinglish or misspelt.\nCONDITIONS:',
    'The message may be English, Hindi, Hinglish or misspelt. Thoughts of self-harm, not wanting to live or feeling hopeless are NEVER a condition: answer NONE.\nCONDITIONS:').replace(
    '\n\nADD_MODE\n"add" if the user asks for exercises IN ADDITION to what they already do (also, besides, more, in addition, iske alawa, aur). Otherwise "switch".', '').replace(
    'The user must be asking WHICH exercises to do, not whether one named activity is OK.',
    'The user must be asking WHICH exercises to do, not whether one named activity is OK. "describe X", "what does X do" and "how does X help" are false.').replace(
    'Only questions about NUMBERS (reps, sets, duration, distance, target).',
    'Only questions about NUMBERS (reps, sets, duration, distance, target). Asking whether they can INCREASE their target or do more, and by how much ("is it time to push harder", "can my goal go up", "should I raise the difficulty"), is "performance", not program_targets.').replace(
    'A message that simply DESCRIBES a pain or problem the user has ("my lower back hurts after sitting", "I have knee pain") counts as true, because the app answers it with exercises; EXCEPT when it says the pain is not improving or getting worse (false).',
    'A message that only DESCRIBES a pain, injury or problem without asking what to do ("my hip aches after long drives", "I get shoulder pain at night", "after a twisted ankle I limp", "I cannot squat down") is false; a message that also asks what to do ("neck pain what should I do") is true.').replace(
    '\n\nSEARCH_QUERY:',
    '\n\nSPORT: the sport or activity the user wants to play or do ("badminton", "kabaddi", "trekking", "a yoga class"), else empty.\n\n'
    'CLAIMS_HEALTHY: true only if the user says they are healthy now, have no pain or no condition any more, asks the app to forget, ignore or change their saved condition, or tells it to pretend their condition is none, usually to get exercises the app would not give. false for ordinary statements like "the pain is slightly less today".\n\n'
    'UNSUPPORTED_DATA: true only if the user asks about their OWN data that the app does not track: calories burned, heart rate, weight, BMI, blood pressure readings, sleep, steps, food eaten. The app tracks only exercises, sets, reps, time and distance. false for everything else.\n\n'
    'SEARCH_QUERY:').replace(
    'Previous message in this conversation (for follow-ups; may be blank):\n{previous}',
    'Previous message in this conversation (for follow-ups; may be blank):\n{previous}\nPrevious answer from the app (may be blank):\n{previous_answer}')
PROMPT_V2 = PROMPT_V2.replace(
    '(put the name in "exercise").\n- "medical": anything else,',
    '(put the name in "exercise"). If the user names several exercises, put all of them in "exercise", joined with " and ". '
    'If the message only answers a question the app asked in its previous answer (which exercise the user meant), treat it as that answer: add_exercise with an empty "exercise", never a health question. '
    'The same holds when the user simply agrees to exercises the app just suggested: add_exercise with an empty "exercise".\n- "medical": anything else,')
assert 'put all of them in "exercise"' in PROMPT_V2
PROMPT_V2 = PROMPT_V2.replace(
    '"program_revert" | "add_exercise", "start_date"', '"program_revert" | "add_exercise" | "out_of_scope", "start_date"').replace(
    '- "medical": anything else,',
    '- "out_of_scope": the user asks the app to do something it cannot do in chat (account or data deletion, reminders, alarms, notifications, opening links, booking or buying) or asks for entertainment. '
    'It is not for questions about health, exercise, the user\'s program or progress, and never for anything that could mean giving up on life or hurting oneself: those stay "medical" so the risk check handles them. '
    'A question about whether the app does one of these things belongs here too, even right after small talk. A request for entertainment is never "conversation": it is "out_of_scope".\n- "medical": anything else,').replace(
    'Judge the NEW message on its own.',
    'A question about whether the user can or should do one named activity, including a short follow-up that only names another activity, is a question about that activity: wants_exercises is false. Judge the NEW message on its own.')
assert 'out_of_scope' in PROMPT_V2 and 'can or should do one named activity' in PROMPT_V2
for needle in ('ONLY when the message has an add', 'normal health questions: "medical", not program_targets', 'NEVER a condition', 'describe X', '"add_exercise": the user asks', '"claims_healthy": true or false', 'SPORT: the sport', 'CLAIMS_HEALTHY: true only', 'UNSUPPORTED_DATA: true only',
               '{previous_answer}', 'after a twisted ankle I limp'):
    assert needle in PROMPT_V2, needle


def route(question: str, *, saved_labels: list[str], options: list[dict], previous_question: str, today, history_bounds: str, api_key: str,
          previous_answer: str = "", v2: bool = False) -> dict | None:
    codes = {o["code"] for o in options}
    prompt = (PROMPT_V2 if v2 else PROMPT).format(
        today=today.isoformat(), weekday=today.strftime("%A"), history_bounds=history_bounds,
        existing=", ".join(saved_labels) or "nothing yet",
        options="\n".join(f"{o['code']}: {o['label']}" for o in options),
        previous=previous_question or "", previous_answer=(previous_answer or "")[:600], question=question[:500])
    try:
        reply = vectorstore.chat_complete([{"role": "user", "content": prompt}], api_key, model=urgent_check.URGENT_MODEL, retries=2, temperature=0, max_tokens=400)
        m = re.search(r"\{.*\}", reply or "", re.S)
        d = json.loads(m.group(0)) if m else None
    except Exception:  # noqa: BLE001 - any failure: fall back to the old separate calls
        _log.warning("router call failed; falling back to the separate calls", exc_info=True)
        return None
    if not isinstance(d, dict):
        return None
    cond = d.get("condition")
    intent = d.get("intent") if d.get("intent") in INTENTS else "medical"
    if intent == "add_exercise" and not v2:
        intent = "medical"
    rr = {
        "intent": intent,
        "start_date": d.get("start_date"), "end_date": d.get("end_date"),
        "condition": cond if cond in codes else None,
        "wants_exercises": d.get("wants_exercises") is True,
        "add_mode": "add" if d.get("add_mode") == "add" else "switch",
        "search_query": str(d.get("search_query") or "").strip()[:200],
        "sport": str(d.get("sport") or "").strip()[:60] if v2 else "",
        "exercise": str(d.get("exercise") or "").strip()[:80] if v2 else "",
        "claims_healthy": v2 and d.get("claims_healthy") is True,
        "unsupported_data": v2 and d.get("unsupported_data") is True,
    }
    return v2_checks.apply_router_checks(question, rr, codes, set(), previous_answer) if v2 else rr       # the router only SUGGESTS; these code checks CONFIRM
