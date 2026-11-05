"""Small code rules for the new chat router (pure code, no AI). Tested in testing_temparary/plan_proto/test_rules.py and tests/test_new_router.py."""
from __future__ import annotations

import re

REFUSAL_STARTS = ("i don't have enough verified information", "i don't have verified information", "i don't have verified exercises",
                  "i don't have a specific exercise", "i'm not able to provide a specific medication")
INTERNAL_WORDING = re.compile(r"retrieved information|the information i have|information provided|provided information|the notes (do|does|don)|approved (exercise )?list|"
                              r"approved exercises (list|for this)", re.I)


def has_real_letters(q: str) -> bool:
    """A message with fewer than 2 letters (any script) is 'tell me more', answered for free."""
    return sum(1 for ch in (q or "") if ch.isalpha()) >= 2


def button_allowed(wants_exercises: bool, visible_answer: str, suggested_names: list[str]) -> bool:
    """The add-exercises button only when the user asked for exercises, the answer is not a refusal or referral, and the text really names a suggested exercise."""
    low = (visible_answer or "").lower().strip()
    if not wants_exercises or not suggested_names or low.startswith(REFUSAL_STARTS):
        return False
    return any(n.lower() in low for n in suggested_names)


def join_refusal(model_text: str, message: str) -> str:
    """Add the standard message only if the model's own text does not already say the same thing (no stacked duplicate sentences)."""
    low = (model_text or "").lower()
    if low.strip().startswith(REFUSAL_STARTS) or "ask your doctor" in low or "speak to a doctor" in low:
        return model_text
    return (model_text or "").rstrip() + "\n\n" + message


def exercise_named(text: str, names: list[str]) -> str | None:
    """The one exercise display name mentioned in `text` (word-boundary, case-insensitive), or None when none or several are mentioned."""
    hit = [n for n in names if re.search(rf"\b{re.escape(n.lower())}\b", (text or "").lower())]
    return hit[0] if len(hit) == 1 else None
