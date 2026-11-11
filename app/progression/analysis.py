"""Module 16 (Program Agent) — RLM design doc §5, Implementation plan Phase 3.

The on-demand recursive analysis agent: bounded decomposition, not a new model.
Every sub-check below is a plain deterministic query — the LLM's only job is
`_should_recurse`, deciding whether a finding warrants one deeper look. Depth
and breadth are capped in code (MAX_DEPTH, MAX_SUBCHECKS), never left to the
model's discretion (§5.1). This function never produces free text and never
writes anything — it returns the structured findings object from §5.3, which
Module 5's answer LLM phrases into a reply, and any escalation_proposal is a
proposal only, gated on user confirmation by a later phase.

Known gap inherited from Module 15 (see app/progression/service.py and
documentation_AGENT_RLM.md §11 / Implementation_RLM_plan.md §0a): Module 14.6
(discomfort reporting) is not built. `_check_discomfort` is honest about that —
it reports "not tracked" rather than fabricating a "no discomfort found"
result, since the latter would read as a real safety signal that does not
exist. Nothing here treats discomfort-unavailable as license to escalate.
"""
from __future__ import annotations

import json
import re
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.profile.models import UserCondition
from app.profile.service import streak as _streak
from app.profile.service import today_ist
from app.progression.models import ProgressionHistory
from app.rag import vectorstore
from app.rag.vectorstore import ResponseTruncated, UpstreamUnavailable

# Both LLM calls in this module (_should_recurse, _pick_escalation_step) must
# fail closed on EITHER of chat_complete's two distinct failure exceptions —
# UpstreamUnavailable (provider down/rate-limited) and ResponseTruncated
# (found live: gpt-oss-20b is a reasoning model that spends tokens on an
# internal "reasoning" field before the actual answer — app/rag/vectorstore.py
# already documents this same lesson for the main chat path — so a tight
# max_tokens budget on a short structured reply can burn out on reasoning
# before ever emitting the answer). They are siblings, not one a subclass of
# the other, so catching only UpstreamUnavailable here left this uncaught.
#
# Widened to RuntimeError (their shared base) for the same reason app/rag/
# service.py was: chat_complete also raises a BARE RuntimeError for any
# non-retryable HTTP status — 402 when the OpenRouter account runs out of
# credit, 400, or 404 for a retired model slug, all of which vectorstore.py
# documents as things that have actually happened. Those escaped this tuple
# entirely, and on the escalation-confirmation path there is no handler
# further up: the user got a bare 500 AFTER their escalation slot had already
# been consumed and committed. Failing closed here turns that into the
# intended "no escalation proposed" outcome.
_CHAT_FAILURE_EXCEPTIONS = (RuntimeError,)

MAX_DEPTH = 2
MAX_SUBCHECKS = 6

# §6 — "no RLM-proposed escalation may exceed 1.5x the current target in a
# single step" -> at most a 50% increase. This is the absolute worst-case
# backstop, not the target: a real proposal should almost never get near it.
# Independent of, and tighter than, Module 15's own per-adjustment/ceiling
# guards (app/progression/service.py).
ESCALATION_GUARD_PCT = 0.5

# The only step sizes the agent may ever propose. A user who's merely cleared
# 100% adherence gets the smallest nudge; someone consistently up around the
# 150% adherence cap (progression/service.py's ADHERENCE_CAP) gets the
# largest — but "largest" here is still 15%, nowhere near the 50% ceiling
# above. Which of the three fits is the one number in this module actually
# left to the LLM's judgment (_pick_escalation_step) — but only a choice
# among these three fixed values, never an arbitrary one.
ESCALATION_STEP_CHOICES = (0.05, 0.10, 0.15)
ESCALATION_DEFAULT_STEP = ESCALATION_STEP_CHOICES[0]  # fail-closed: smallest, on any LLM error

ESCALATION_STEP_PROMPT = """A user has been consistently meeting or exceeding their \
fitness program target for one activity. Rule-based adherence data (100% = exactly on \
target, capped at 150%):

{adherence_json}

Pick exactly one step size for how much to increase their target by — a bigger, more \
consistent overage above 100% deserves a bigger step, never more than a modest one. \
Reply with EXACTLY one of these numbers and nothing else: 0.05, 0.10, 0.15
"""

ROUTER_PROMPT = """A fitness app's rule-based sub-checks just ran for one user's \
progress question. Decide ONLY whether a deeper look is warranted. Reply with \
exactly this JSON shape and nothing else: {{"recurse": true or false, "reason": \
"short reason"}}

User question: "{question}"

Sub-check findings so far:
{findings_json}

Recurse only if a finding suggests something worth digging into further — e.g. \
adherence trending down, or a plateau detected. Do not recurse for routine, \
on-track results."""


def _check_adherence_trend(session: Session, user_id: str, activity_type: str,
                            *, window_weeks: int = 4) -> dict:
    """Last N vs previous N weekly ProgressionHistory rows for this entry."""
    rows = session.scalars(
        select(ProgressionHistory)
        .where(ProgressionHistory.user_id == user_id,
               ProgressionHistory.activity_type == activity_type)
        .order_by(ProgressionHistory.applied_at.desc())
        .limit(window_weeks * 2)
    ).all()
    if not rows:
        return {"check": "adherence_trend", "available": False,
                "window_weeks": window_weeks, "note": "no weekly history yet"}
    recent = rows[:window_weeks]
    prior = rows[window_weeks:window_weeks * 2]
    recent_avg = sum(float(r.adherence_pct) for r in recent) / len(recent)
    if not prior:
        return {"check": "adherence_trend", "value": round(recent_avg, 1),
                "window_weeks": len(recent), "trend": None}
    prior_avg = sum(float(r.adherence_pct) for r in prior) / len(prior)
    trend = ("improving" if recent_avg > prior_avg + 5 else
              "declining" if recent_avg < prior_avg - 5 else "steady")
    return {"check": "adherence_trend", "value": round(recent_avg, 1),
            "window_weeks": len(recent), "trend": trend}


def _check_plateau(session: Session, user_id: str, activity_type: str) -> dict:
    """Adjustment magnitude flattening: has the applied target stayed flat
    across the last 3 weekly runs despite adherence staying in a range that
    would normally have kept it moving?"""
    rows = session.scalars(
        select(ProgressionHistory)
        .where(ProgressionHistory.user_id == user_id,
               ProgressionHistory.activity_type == activity_type)
        .order_by(ProgressionHistory.applied_at.desc())
        .limit(3)
    ).all()
    if len(rows) < 3:
        return {"check": "plateau", "detected": False}
    flat = all(float(r.new_target) == float(r.old_target) for r in rows)
    reasonable_effort = all(float(r.adherence_pct) >= 50.0 for r in rows)
    return {"check": "plateau", "detected": flat and reasonable_effort,
            "window_weeks": 3}


def _check_streak(session: Session, user_id: str) -> dict:
    s = _streak(session, user_id)
    return {"check": "streak", "current": s["current"], "active_days": s["active_days"]}


def _check_discomfort(activity_type: str) -> dict:
    return {"check": "discomfort", "available": False,
            "note": "not tracked yet — Module 14.6 is not built"}


def _check_vs_curve(session: Session, user_id: str, activity_type: str) -> dict:
    """Most recent week's actually-applied change vs. what the diminishing
    curve predicted before guard clamping (app/progression/service.py)."""
    row = session.scalar(
        select(ProgressionHistory)
        .where(ProgressionHistory.user_id == user_id,
               ProgressionHistory.activity_type == activity_type)
        .order_by(ProgressionHistory.applied_at.desc())
        .limit(1)
    )
    if row is None:
        return {"check": "vs_curve", "available": False}
    applied_pct = (float(row.new_target) / float(row.old_target) - 1) * 100 \
        if row.old_target else 0.0
    return {"check": "vs_curve", "predicted_pct": float(row.curve_pct),
            "applied_pct": round(applied_pct, 2), "reason": row.reason}


def _condition_gate(session: Session, user_id: str, activity_type: str) -> dict:
    """13.3 safety-grid check, re-queried live every call — no caching layer
    exists in this codebase (the Implementation plan's §9.2 assumed default),
    so `cached` is reported false rather than claiming infrastructure that
    isn't there."""
    from app.profile.service import is_activity_blocked       # the audited table decides, same as the program screen
    safe = not is_activity_blocked(session, user_id, activity_type)
    return {"checked": True, "cached": False, "safe": safe}


def _should_recurse(question: str, findings: list[dict], api_key: str) -> bool:
    """The router's one real decision. Fails closed: any LLM error, timeout,
    or unparseable reply means "don't recurse" — an analysis failure should
    shrink scope, not expand it (mirrors 15.5's conservative-by-default)."""
    prompt = ROUTER_PROMPT.format(question=question[:500],
                                   findings_json=json.dumps(findings))
    try:
        reply = vectorstore.chat_complete(
            [{"role": "user", "content": prompt}], api_key, temperature=0, max_tokens=300,
        )
    except _CHAT_FAILURE_EXCEPTIONS:
        return False
    try:
        data = json.loads(reply.strip())
    except json.JSONDecodeError:
        return False
    return bool(isinstance(data, dict) and data.get("recurse") is True)


def _snap_to_step_choice(reply: str) -> float:
    """Pure parsing half of _pick_escalation_step: pulls the first number out of
    whatever text came back and snaps it to the nearest of the three allowed
    step sizes, no matter how it's formatted ("0.10", "10", "10%", ...).
    Never returns anything outside ESCALATION_STEP_CHOICES."""
    match = re.search(r"\d+(\.\d+)?", reply)
    if not match:
        return ESCALATION_DEFAULT_STEP
    value = float(match.group())
    if value > 1:  # the model replied "10" or "15" (percent), not "0.10"
        value /= 100
    return min(ESCALATION_STEP_CHOICES, key=lambda c: abs(c - value))


def _pick_escalation_step(adherence: dict, api_key: str) -> float:
    """The one number in this module actually left to the LLM's judgment — but
    bounded to a fixed choice set it can never expand (ESCALATION_STEP_CHOICES),
    and snapped to the nearest of those three no matter what text comes back
    (_snap_to_step_choice). Fails closed to the smallest step on any LLM error —
    an analysis failure should shrink the proposal, never grow it."""
    prompt = ESCALATION_STEP_PROMPT.format(adherence_json=json.dumps(adherence))
    try:
        reply = vectorstore.chat_complete(
            [{"role": "user", "content": prompt}], api_key, temperature=0, max_tokens=200,
        )
    except _CHAT_FAILURE_EXCEPTIONS:
        return ESCALATION_DEFAULT_STEP
    return _snap_to_step_choice(reply)


def _escalation_proposal(adherence: dict, plateau: dict, condition_gate: dict, *,
                          api_key: str) -> dict:
    eligible = (
        condition_gate["safe"]
        and not plateau.get("detected")
        and adherence.get("value", 0) >= 100
        and adherence.get("trend") != "declining"
    )
    if not eligible:
        return {"eligible": False}
    step = _pick_escalation_step(adherence, api_key)
    return {
        "eligible": True,
        # min() here is a belt-and-suspenders backstop, not the normal path —
        # every ESCALATION_STEP_CHOICES value is already far under the guard.
        "proposed_pct_increase": min(step, ESCALATION_GUARD_PCT),
        "capped_at": ESCALATION_GUARD_PCT,
        # Deliberately does not claim "no discomfort flags" — that isn't a
        # knowable fact yet (see _check_discomfort / module docstring).
        "reason": (
            f"adherence {adherence.get('value')}% over the last "
            f"{adherence.get('window_weeks')} weeks, no plateau detected. "
            f"Discomfort is not yet tracked by this app, so this is not a "
            f"discomfort-cleared recommendation."
        ),
    }


def _summary_signal(adherence: dict, plateau: dict) -> str:
    if plateau.get("detected"):
        return "plateaued"
    value = adherence.get("value")
    if value is None:
        return "on_track"
    if adherence.get("trend") == "declining" or value < 50:
        return "behind"
    if value >= 100 and adherence.get("trend") != "declining":
        return "ahead_of_plan"
    return "on_track"


def analyze_performance(session: Session, user_id: str, activity_type: str,
                         question: str, *, api_key: str,
                         today: date | None = None) -> dict:
    # Found live (fourth-pass audit, cross-check for regressions): currently
    # dead — `today` is never referenced again in this function or passed to
    # any sub-check, so this has zero behavioral effect today. Fixed to
    # today_ist() anyway (matching the rest of this session's IST fix)
    # specifically because it's a landmine: it would silently reintroduce
    # the UTC/IST off-by-one-day bug the moment someone wires `today` into a
    # sub-check without noticing this default was UTC-based.
    today = today or today_ist()

    findings = [
        _check_adherence_trend(session, user_id, activity_type),
        _check_plateau(session, user_id, activity_type),
        _check_streak(session, user_id),
        _check_discomfort(activity_type),
        _check_vs_curve(session, user_id, activity_type),
    ]

    depth = 1
    if depth < MAX_DEPTH and len(findings) < MAX_SUBCHECKS:
        if _should_recurse(question, findings, api_key):
            findings.append(
                _check_adherence_trend(session, user_id, activity_type, window_weeks=8)
            )

    # reversed: if a deeper (8-week) adherence_trend got appended by recursion,
    # it should override the shallow 4-week one for the summary/proposal below.
    adherence = next((f for f in reversed(findings) if f["check"] == "adherence_trend"), {})
    plateau = next((f for f in findings if f["check"] == "plateau"), {"detected": False})
    condition_gate = _condition_gate(session, user_id, activity_type)

    return {
        "summary_signal": _summary_signal(adherence, plateau),
        "findings": findings,
        "escalation_proposal": _escalation_proposal(
            adherence, plateau, condition_gate, api_key=api_key,
        ),
        "condition_gate": condition_gate,
    }
