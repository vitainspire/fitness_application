"""What the chat user reads when a request is refused or fails. Ported from the tested prototype (variants/v3.py).

DRAFT WORDING. Rules for every message: say what happens next, never mention medicines, never show model text, never tell the user to go to a hospital or
call an emergency number (every risky case says: consult a doctor), and keep a SYSTEM problem (try again) clearly apart from a MEDICAL refusal.
"""
from __future__ import annotations

import logging
import uuid

NO_INFO = "I don't have enough verified information about this. Please speak to a doctor about it."
MESSAGES = {'urgent': "This could be serious. Please consult a doctor as soon as you can, and don't exercise or try home treatment until a doctor has seen you.", 'see_doctor': "This sounds like something a doctor should look at before you start any exercise. Please see a doctor first. I can't suggest exercises for it.", 'pregnancy': "Exercise during pregnancy or after delivery needs your own doctor's advice. Please check with your doctor first.", 'controls': 'Because of your blood pressure, sugar or heart, a doctor should check you before you start exercising. Please speak to your doctor first.', 'injury': "A recent injury or surgery needs a doctor's check before you exercise. Please see your doctor first.", 'not_covered': "This app helps with joint, back and exercise problems, so I can't help with this one. Please speak to a doctor about it.", 'no_exercises_for_this': "I don't have verified exercises for this combination of conditions. Please ask your doctor which exercises are safe for you.", 'need_more': "I couldn't tell which problem you mean. Please tell me where it hurts and for how long, for example: knee pain for 2 weeks.", 'no_info': "I don't have enough verified information about this. Please speak to a doctor about it.", 'system': "Sorry, this couldn't be answered just now because of a problem on our side, not because of anything you did. Please try again in about 30 seconds. If it keeps happening, quote this reference: {ref}"}
_REASON_MESSAGE = {'red_flag': 'see_doctor', 'symptom_needs_doctor': 'see_doctor', 'pregnancy': 'pregnancy', 'heart_or_uncontrolled': 'controls', 'control_not_stated': 'controls', 'acute_injury': 'injury', 'recent_surgery': 'injury', 'not_in_exercise_table': 'no_exercises_for_this', 'no_table_row': 'no_exercises_for_this', 'out_of_scope': 'not_covered', 'insufficient_information': 'need_more'}
_log = logging.getLogger("app.rag.chat_messages")


def refusal_message(reason: str) -> str:
    return MESSAGES[_REASON_MESSAGE.get(reason, "no_info")]


def new_error_ref() -> str:
    return uuid.uuid4().hex[:8].upper()


def system_error_message(ref: str) -> str:
    return MESSAGES["system"].format(ref=ref)


def log_system_error(ref: str, kind: str, stage: str, exc: BaseException | None = None) -> None:
    _log.error("chat system error ref=%s kind=%s stage=%s cause=%s", ref, kind, stage, f"{type(exc).__name__}: {str(exc)[:160]}" if exc is not None else "")

UNAVAILABLE_NOTE = "\n\nIf any of this feels urgent, please see a doctor right away."     # added when the AI urgent check could not run

MESSAGES["no_exercises_single"] = "I don't have verified exercises for your condition yet. Please ask your doctor which exercises are safe for you."


def gate_message(reason: str, n_conditions: int) -> str:
    """The doctor message for a saved condition set with no exercise list. One condition does not say 'combination'."""
    if n_conditions <= 1 and _REASON_MESSAGE.get(reason) == "no_exercises_for_this":
        return MESSAGES["no_exercises_single"]
    return refusal_message(reason)
