"""Load the reviewed CSVs into PostgreSQL, and gate on coverage.

Run:  python -m app.catalog.loader

The coverage check is the important part. A supported condition missing a verdict
for any activity is not "probably fine" — it is the failure mode the closed list
exists to prevent, where an absent row silently reads as "nothing to exclude,
therefore everything is safe". The loader refuses to finish rather than leave that
state in the database.
"""
from __future__ import annotations

import csv
import json
import sys
from datetime import date
from pathlib import Path

from sqlalchemy import delete, select

from app.catalog.models import (
    ActivityTaxonomy, Condition, ConditionInteractionCell, SafetyGridCell, VideoCatalog,
)
from app.platform.config import load_config
from app.platform.db import init_engine, session_scope

DATA = Path(__file__).resolve().parents[2] / "data"


class CoverageError(RuntimeError):
    """Raised when the safety grid has a gap. Never downgraded to a warning."""


def _split(value: str) -> list[str]:
    return [p.strip() for p in value.split("|") if p.strip()]


def _date_or_none(value: str):
    value = (value or "").strip()
    return date.fromisoformat(value) if value else None


def load_conditions(session) -> int:
    """Upsert. Never delete-and-recreate.

    `user_conditions` and `safety_grid` both reference these rows, so a wipe fails
    with a foreign-key violation the moment a single user has onboarded — i.e. every
    reload in production. Rows that disappear from the CSV are reported, not
    silently removed, because deleting a condition someone has selected is a data
    decision, not a reload side effect.
    """
    rows = list(csv.DictReader((DATA / "conditions.csv").open(encoding="utf-8")))
    incoming = {r["condition_code"].strip(): r for r in rows}

    for code, r in incoming.items():
        existing = session.get(Condition, code)
        if existing is None:
            session.add(Condition(
                condition_code=code,
                display_name=r["display_name"].strip(),
                bucket=r["bucket"].strip(),
                display_order=int(r["display_order"]),
                notes=(r.get("notes") or "").strip() or None,
            ))
        else:
            existing.display_name = r["display_name"].strip()
            existing.bucket = r["bucket"].strip()
            existing.display_order = int(r["display_order"])
            existing.notes = (r.get("notes") or "").strip() or None
    session.flush()

    stale = [c.condition_code for c in session.scalars(select(Condition)).all()
             if c.condition_code not in incoming]
    if stale:
        print(f"  note: {len(stale)} condition(s) in the database but not in the CSV: "
              f"{', '.join(stale)} — left in place (users may have selected them)")

    for structural in ("none", "other_unlisted"):
        if structural not in incoming:
            raise CoverageError(
                f"conditions.csv is missing the structural row '{structural}'. "
                "'none' lets a healthy user proceed; 'other_unlisted' is the "
                "fail-closed default for anything not listed."
            )
    return len(rows)


def load_taxonomy(session) -> int:
    rows = list(csv.DictReader((DATA / "activity_taxonomy.csv").open(encoding="utf-8")))
    demos_path = DATA / "demos.json"
    demos = json.loads(demos_path.read_text(encoding="utf-8")) if demos_path.exists() else {}
    # Upsert for the same reason as conditions: activity_logs and user_program
    # both hold foreign keys into this table.
    for r in rows:
        code = r["activity_type"].strip()
        session.merge(ActivityTaxonomy(
            activity_type=code,
            display_name=r["display_name"].strip(),
            prescription_type=r["prescription_type"].strip(),
            default_sets=int(r["default_sets"]),
            default_amount=float(r["default_amount"]),
            amount_unit=r["amount_unit"].strip(),
            rest_seconds=int(r["rest_seconds"] or 0),
            per_side=r["per_side"].strip().upper() == "TRUE",
            progression_axis=r["progression_axis"].strip(),
            progression_step=float(r["progression_step"]),
            max_sets=int(r["max_sets"]),
            max_amount=float(r["max_amount"]),
            plausible_max_total=float(r["plausible_max_total"]),
            est_pace_min_per_km=float(r["est_pace_min_per_km"])
                if (r.get("est_pace_min_per_km") or "").strip() else None,
            synonyms=_split(r["synonyms"]),
            source_exercise_id=(r.get("source_exercise_id") or "").strip() or None,
            priority_order=int(r["priority_order"]),
            youtube_url=(r.get("youtube_url") or "").strip() or None,
            demo_images=demos.get(r["activity_type"].strip(), {}).get("images", []),
            instructions=demos.get(r["activity_type"].strip(), {}).get("instructions", []),
            demo_gif=demos.get(r["activity_type"].strip(), {}).get("gif"),
            demo_gif_source=demos.get(r["activity_type"].strip(), {}).get("gif_source"),
            reviewer=r["reviewer"].strip(),
            reviewed_at=_date_or_none(r.get("reviewed_at", "")),
        ))
    session.flush()

    # A spoken phrase must resolve to exactly one activity (12.2). Two activities
    # claiming "stretch" would make the parser pick one at random.
    seen: dict[str, str] = {}
    for r in rows:
        for syn in _split(r["synonyms"]):
            key = syn.lower()
            if key in seen:
                raise CoverageError(
                    f"synonym {syn!r} claimed by both {seen[key]!r} and "
                    f"{r['activity_type']!r} — the voice parser could not disambiguate."
                )
            seen[key] = r["activity_type"]
    return len(rows)


def load_safety_grid(session) -> int:
    rows = list(csv.DictReader((DATA / "safety_grid.csv").open(encoding="utf-8")))
    # Nothing holds a foreign key into the grid, so a clean replace is safe
    # and is what makes a re-review land in full.
    session.execute(delete(SafetyGridCell))
    ranks_by_condition: dict[str, list[int]] = {}
    therapeutic_by_condition: dict[str, list[int]] = {}
    for r in rows:
        verdict = r["verdict"].strip()
        if not verdict:
            raise CoverageError(
                f"safety_grid.csv has an empty verdict for "
                f"{r['condition_code']} x {r['activity_type']}. A blank is not "
                "'probably safe' — fill it or the condition cannot be supported."
            )
        condition_code = r["condition_code"].strip()
        rank_raw = (r.get("usefulness_rank") or "").strip()
        if verdict == "avoid":
            if rank_raw:
                raise CoverageError(
                    f"safety_grid.csv has a usefulness_rank for "
                    f"{condition_code} x {r['activity_type']}, but its verdict is "
                    "'avoid' — a banned activity cannot also be ranked. Clear the rank."
                )
            usefulness_rank = None
        else:
            if not rank_raw:
                raise CoverageError(
                    f"safety_grid.csv is missing a usefulness_rank for "
                    f"{condition_code} x {r['activity_type']} (verdict={verdict}). "
                    "Every activity a user can actually be shown needs a rank, or "
                    "personalized ordering silently falls back for this cell."
                )
            usefulness_rank = int(rank_raw)
            ranks_by_condition.setdefault(condition_code, []).append(usefulness_rank)

        therapeutic_raw = (r.get("therapeutic_rank") or "").strip()
        if not therapeutic_raw:
            therapeutic_rank = None
        elif verdict == "avoid":
            raise CoverageError(
                f"safety_grid.csv has a therapeutic_rank for "
                f"{condition_code} x {r['activity_type']}, but its verdict is "
                "'avoid' — a banned activity cannot also be ranked as helpful. Clear it."
            )
        else:
            therapeutic_rank = int(therapeutic_raw)
            if not 1 <= therapeutic_rank <= 5:
                raise CoverageError(
                    f"therapeutic_rank for {condition_code} x {r['activity_type']} is "
                    f"{therapeutic_rank} — must be 1-5 (top 5 only) or blank."
                )
            therapeutic_by_condition.setdefault(condition_code, []).append(therapeutic_rank)

        session.add(SafetyGridCell(
            condition_code=condition_code,
            activity_type=r["activity_type"].strip(),
            verdict=verdict,
            note=(r.get("note") or "").strip() or None,
            usefulness_rank=usefulness_rank,
            therapeutic_rank=therapeutic_rank,
            reviewer=r["reviewer"].strip(),
            reviewed_at=_date_or_none(r.get("reviewed_at", "")),
        ))

    # Each condition's ranks must be a clean 1..N permutation (no ties, no
    # gaps) — a tie makes "most useful" ambiguous, and a gap means a rank was
    # typo'd (e.g. two rows both marked 5), silently producing a shorter
    # effective ordering than the activity count implies.
    for condition_code, ranks in ranks_by_condition.items():
        expected = list(range(1, len(ranks) + 1))
        if sorted(ranks) != expected:
            raise CoverageError(
                f"usefulness_rank for condition '{condition_code}' is not a clean "
                f"1..{len(ranks)} ordering (got {sorted(ranks)}) — check for a "
                "duplicate or skipped rank."
            )

    # Same no-ties/no-gaps rule as usefulness_rank above, but the count is
    # whatever genuinely qualifies (0-5), not every safe/modify activity —
    # most conditions have far fewer than 5 activities that actually help.
    for condition_code, ranks in therapeutic_by_condition.items():
        expected = list(range(1, len(ranks) + 1))
        if sorted(ranks) != expected:
            raise CoverageError(
                f"therapeutic_rank for condition '{condition_code}' is not a clean "
                f"1..{len(ranks)} ordering (got {sorted(ranks)}) — check for a "
                "duplicate or skipped rank."
            )

    session.flush()
    return len(rows)


def assert_full_coverage(session) -> None:
    """Every supported condition needs a verdict for every activity."""
    supported = session.scalars(
        select(Condition.condition_code)
        .where(Condition.bucket == "supported", Condition.condition_code != "none")
    ).all()
    activities = session.scalars(select(ActivityTaxonomy.activity_type)).all()
    have = {
        (c, a) for c, a in session.execute(
            select(SafetyGridCell.condition_code, SafetyGridCell.activity_type)
        ).all()
    }

    missing = [(c, a) for c in supported for a in activities if (c, a) not in have]
    if missing:
        shown = ", ".join(f"{c}x{a}" for c, a in missing[:8])
        raise CoverageError(
            f"safety grid is missing {len(missing)} cell(s): {shown}"
            f"{' …' if len(missing) > 8 else ''}. "
            "Suggestions would fail open for these conditions."
        )


def load_condition_interactions(session) -> int:
    """Combined-conditions safety check (MULTI_CONDITION_INTERACTION_SAFETY_PLAN.md).

    Deliberately NOT subject to assert_full_coverage — this table only ever
    ESCALATES a verdict on top of the single-condition grid (which IS fully
    covered and fail-closed on its own), so a missing pair+activity row just
    means "no extra risk currently known," never "assumed safe."
    """
    path = DATA / "condition_interactions.csv"
    rows = list(csv.DictReader(path.open(encoding="utf-8"))) if path.exists() else []
    session.execute(delete(ConditionInteractionCell))

    supported = set(session.scalars(
        select(Condition.condition_code)
        .where(Condition.bucket == "supported", Condition.condition_code != "none")
    ).all())
    activities = set(session.scalars(select(ActivityTaxonomy.activity_type)).all())

    seen: set[tuple[str, str, str]] = set()
    for r in rows:
        a = r["condition_a"].strip()
        b = r["condition_b"].strip()
        activity_type = r["activity_type"].strip()
        verdict = r["verdict"].strip()

        if a == b:
            raise CoverageError(
                f"condition_interactions.csv: {a!r} paired with itself for "
                f"{activity_type!r} — an interaction needs two DIFFERENT conditions."
            )
        for code in (a, b):
            if code not in supported:
                raise CoverageError(
                    f"condition_interactions.csv: {code!r} ({a}+{b} x {activity_type}) "
                    "is not a supported-bucket condition — a refer-bucket condition "
                    "already blocks all content (13.3b), so pairing it here is meaningless."
                )
        if activity_type not in activities:
            raise CoverageError(
                f"condition_interactions.csv: unknown activity_type {activity_type!r} "
                f"for pair {a}+{b}."
            )
        if verdict not in ("modify", "avoid"):
            raise CoverageError(
                f"condition_interactions.csv: verdict for {a}+{b} x {activity_type} "
                f"must be 'modify' or 'avoid', got {verdict!r}. An interaction can only "
                "escalate restriction — a 'safe' row means nothing, since a missing row "
                "already means 'no extra restriction known'."
            )

        # Canonical order: the smaller code always stored as condition_a, so a
        # lookup never has to check both orders and a reversed-order duplicate
        # is a real conflict, not a silent second row.
        cond_a, cond_b = sorted((a, b))
        key = (cond_a, cond_b, activity_type)
        if key in seen:
            raise CoverageError(
                f"condition_interactions.csv: duplicate row for {cond_a}+{cond_b} "
                f"x {activity_type}."
            )
        seen.add(key)

        session.add(ConditionInteractionCell(
            condition_a=cond_a,
            condition_b=cond_b,
            activity_type=activity_type,
            verdict=verdict,
            note=(r.get("note") or "").strip() or None,
            reviewer=r["reviewer"].strip(),
            reviewed_at=_date_or_none(r.get("reviewed_at", "")),
        ))

    session.flush()
    return len(rows)


def seed_videos(session) -> int:
    """Create one catalogue row per activity, awaiting curation.

    Deliberately NOT a search URL. A search page is the manual YouTube experience
    the app exists to replace — it returns a wall of results the user has to judge,
    which is exactly the judgement a curated library is supposed to have already
    made. A row with no `youtube_video_id` renders as "not curated yet" rather than
    pretending to be content.

    Curation is an admin job (7.1): paste one watched, approved URL per exercise.
    Existing curated rows are preserved on reload.
    """
    existing = {
        v.activity_type: v
        for v in session.scalars(select(VideoCatalog)).all()
    }
    n = 0
    for row in session.scalars(select(ActivityTaxonomy)).all():
        cur = existing.get(row.activity_type)
        if cur is not None:
            continue  # never clobber a curated pick
        session.add(VideoCatalog(
            activity_type=row.activity_type,
            title=f"{row.display_name} — demonstration",
            youtube_video_id="",
            watch_url="",
        ))
        n += 1
    return n


def main() -> int:
    cfg = load_config()
    init_engine(cfg.database_url)
    try:
        with session_scope() as s:
            c = load_conditions(s)
            t = load_taxonomy(s)
            g = load_safety_grid(s)
            assert_full_coverage(s)
            i = load_condition_interactions(s)
            v = seed_videos(s)
    except CoverageError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1

    print(f"conditions   {c:4}")
    print(f"activities   {t:4}")
    print(f"grid cells   {g:4}  (coverage complete)")
    print(f"interactions {i:4}  (sparse, escalation-only)")
    print(f"video links  {v:4}  (placeholder search links)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
