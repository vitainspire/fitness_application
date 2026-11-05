"""The PROGRAM AGENT: a small code step (no AI call, so no extra cost) that reads the user's REAL exercise player and tells the answer model and the button builder what is in it.
It runs only when the router decided the user wants exercise suggestions (or ideas for a sport), never on every message.

What it fixes: the answer model used to see only the user's own earlier messages, so it believed a refused "remove walking" had happened, suggested exercises that were already in the
player, and never said that a blocked exercise sitting in the player cannot be removed from chat. Now it is told the facts, and code adds the same facts to the reply:
  - which suggested exercises are already in the player (no button for those) and which can be added (buttons only for those);
  - which exercises in the player are NOT approved for the user's saved conditions, with the line that the app cannot remove them and the user can remove them in the exercise player.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalog.models import ActivityTaxonomy
from app.profile.models import UserProgram


@dataclass
class PlayerState:
    active: list[str] = field(default_factory=list)                # display names of the exercises active in the player
    paused: list[str] = field(default_factory=list)                # in the player but paused (can be resumed with the Add button)
    approved_missing: list[str] = field(default_factory=list)      # approved for the user's conditions but not in the player (neither active nor paused)
    active_not_approved: list[str] = field(default_factory=list)   # active in the player although NOT approved for the user's conditions


def read(session: Session, user_id: str, allowed_names: list[str]) -> PlayerState:
    rows = session.execute(
        select(ActivityTaxonomy.display_name, UserProgram.status)
        .join(UserProgram, UserProgram.activity_type == ActivityTaxonomy.activity_type)
        .where(UserProgram.user_id == user_id)).all()
    active = sorted(n for n, st in rows if st == "active")
    paused = sorted(n for n, st in rows if st != "active")
    in_player = {n for n, _ in rows}
    return PlayerState(active=active, paused=paused,
                       approved_missing=sorted(n for n in allowed_names if n not in in_player),
                       active_not_approved=[n for n in active if n not in set(allowed_names)])


def _names(items: list[str]) -> str:
    return ", ".join(items) if items else "none"


def block(state: PlayerState) -> str:
    """The facts for the answer model (added to the user message, only when this agent runs)."""
    return ("PLAYER STATE (the user's real exercise player, read just now; this is the only truth about what is in it): "
            f"active: {_names(state.active)}. Paused: {_names(state.paused)}. Approved for the user but not in the player: {_names(state.approved_missing)}. "
            f"In the player but NOT approved for the user's conditions: {_names(state.active_not_approved)}. "
            "The app cannot add or remove exercises from chat. Never say or imply that the user added or removed an exercise; only the state above says what is in the player. "
            "Do not tell the user to add something that is already active.")


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def note(state: PlayerState, suggested: list[dict]) -> tuple[str, list[dict]]:
    """(text to append to the reply, the suggestion buttons to keep). Buttons only for suggested exercises that are not active in the player; paused ones stay (the button resumes them)."""
    by_name = {a["display_name"]: a for a in suggested}
    already = [n for n in by_name if n in state.active]
    keep = [a for a in suggested if a["display_name"] not in state.active]
    parts: list[str] = []
    if already:
        parts.append(f"{_join(already)} {'is' if len(already) == 1 else 'are'} already in your exercise player." + (" Tap the button to add the other" + ("s." if len(keep) > 1 else ".") if keep else ""))
    if state.active_not_approved:
        one = len(state.active_not_approved) == 1
        parts.append(f"Please note: {_join(state.active_not_approved)} {'is' if one else 'are'} in your exercise player but {'is' if one else 'are'} not approved for your health conditions. "
                     f"I can't remove {'it' if one else 'them'} for you; you can remove {'it' if one else 'them'} yourself in the exercise player.")
    return "\n\n".join(parts), keep
