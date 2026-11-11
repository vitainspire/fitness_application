"""Load the reviewed CSVs into PostgreSQL, and gate on coverage.

Run:  python -m app.catalog.loader

The only safety table is the audited 794-combination table (condition_combinations_794_AUDITED.csv). A set of
conditions with no row in it has NO audited exercises, and the app blocks every exercise for it. The loader
refuses to finish if that table is missing or malformed, rather than leave the database without it.
"""
from __future__ import annotations

import csv
import json
import sys
from datetime import date
from pathlib import Path

from sqlalchemy import delete, select

from app.catalog.models import (
    ActivityTaxonomy, Condition, ConditionCombinationCell, VideoCatalog,
)
from app.platform.config import load_config
from app.platform.db import init_engine, session_scope

DATA = Path(__file__).resolve().parents[2] / "data"


class CoverageError(RuntimeError):
    """Raised when the audited safety table has a gap. Never downgraded to a warning."""


def _split(value: str) -> list[str]:
    return [p.strip() for p in value.split("|") if p.strip()]


def _date_or_none(value: str):
    value = (value or "").strip()
    return date.fromisoformat(value) if value else None


def load_conditions(session) -> int:
    """Upsert. Never delete-and-recreate.

    `user_conditions` references these rows, so a wipe fails
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


COMBINATIONS_FILE = "condition_combinations_794_AUDITED.csv"
COMBINATION_ACTIVITY_COLUMNS = (
    "walk", "glute_bridge", "dead_bug", "shoulder_circles", "running", "ankle_circles", "wrist_circles",
    "calf_stretch", "hamstring_stretch", "quad_stretch", "neck_isometric", "lower_back_side_stretch",
)


def load_condition_combinations(session) -> int:
    """The audited 794-combination table -> `condition_combinations` (one row per exact condition SET x activity).

    Fail-closed like the grid: a missing/unknown verdict, an unknown condition name, an unknown activity, or a duplicate set refuses the load
    instead of leaving a half-filled table where an absent row would read as "nothing to block". Returns the number of condition sets loaded.
    """
    path = DATA / COMBINATIONS_FILE
    if not path.exists():
        raise CoverageError(f"{COMBINATIONS_FILE} is missing — the app would have NO audited exercises for any condition.")
    names = {c.display_name: c.condition_code for c in session.scalars(select(Condition)).all()}
    activities = set(session.scalars(select(ActivityTaxonomy.activity_type)).all())
    rows = list(csv.DictReader(path.open(encoding="utf-8-sig", newline="")))
    if not rows:
        raise CoverageError(f"{COMBINATIONS_FILE} has no rows.")
    unknown_cols = [a for a in COMBINATION_ACTIVITY_COLUMNS if a not in activities]
    if unknown_cols:
        raise CoverageError(f"{COMBINATIONS_FILE}: unknown activity column(s) {unknown_cols}.")

    session.execute(delete(ConditionCombinationCell))
    seen: set[str] = set()
    for r in rows:
        codes = []
        for k in ("condition_1", "condition_2", "condition_3", "condition_4"):
            name = (r.get(k) or "").strip()
            if name and name != "None":
                if name not in names:
                    raise CoverageError(f"{COMBINATIONS_FILE}: unknown condition name {name!r}.")
                codes.append(names[name])
        key = "|".join(sorted(codes))
        if len(set(codes)) != len(codes) or key in seen:
            raise CoverageError(f"{COMBINATIONS_FILE}: duplicate condition set {key or '(none)'!r}.")
        seen.add(key)
        for a in COMBINATION_ACTIVITY_COLUMNS:
            verdict = (r.get(a) or "").strip()
            if verdict not in ("safe", "modify", "avoid"):
                raise CoverageError(f"{COMBINATIONS_FILE}: verdict for {key or '(none)'} x {a} must be safe/modify/avoid, got {verdict!r}.")
            session.add(ConditionCombinationCell(condition_set=key, activity_type=a, condition_count=len(codes), verdict=verdict))
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
            cc = load_condition_combinations(s)
            v = seed_videos(s)
    except CoverageError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1

    print(f"conditions   {c:4}")
    print(f"activities   {t:4}")
    print(f"combinations {cc:4}  (audited condition sets: the only safety table)")
    print(f"video links  {v:4}  (placeholder search links)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
