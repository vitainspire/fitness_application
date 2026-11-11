"""The audited condition -> exercise table (794 rows, 12 conditions) used by the new chat pipeline. Ported from the tested prototype (variants/v3.py).

One row per exact SET of conditions: the exercises in best-first order, each "safe" or "modify" (with its own caution), plus a row caution. A set with no
row, or a condition outside the 12, is never answered with exercises. The table is read-only and held in memory (loaded once), so many requests can read it
at the same time. If the file cannot be loaded, `get()` returns an empty table and the caller treats that as a SYSTEM problem, not a medical refusal.
"""
from __future__ import annotations

import csv
import hashlib
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path

_log = logging.getLogger(__name__)
DATA = Path(__file__).resolve().parents[2] / "data"
TABLE_PATH = DATA / "condition_combinations_794_AUDITED.csv"

IN_TABLE = {"chronic_back_pain", "herniated_disk", "degenerative_disc", "arthritis_hip", "arthritis_knee", "bp_controlled",
            "diabetes_controlled", "prediabetes", "high_cholesterol", "obesity", "depression", "anxiety"}
NOT_IN_TABLE: set[str] = set()          # the 7 conditions this table did not cover were removed from the app; any other code with no row is reported as "no_table_row"


@dataclass
class Table:
    rows: dict = field(default_factory=dict)          # frozenset(condition codes) -> {"tops": [(activity_type, status, caution)], "row_caution": str}
    names: dict = field(default_factory=dict)         # activity_type -> display name
    version: str = ""


_LOCK = threading.Lock()
_TABLE: Table | None = None


def load(path: Path = TABLE_PATH) -> Table:
    t = Table()
    for r in csv.DictReader(open(DATA / "activity_taxonomy.csv", encoding="utf-8-sig", newline="")):
        t.names[r["activity_type"]] = r["display_name"]
    cond = {r["display_name"]: r["condition_code"] for r in csv.DictReader(open(DATA / "conditions.csv", encoding="utf-8-sig", newline=""))}
    raw = Path(path).read_bytes()
    t.version = hashlib.sha1(raw).hexdigest()[:10]
    for r in csv.DictReader(open(path, encoding="utf-8-sig", newline="")):
        names = [r[k] for k in ("condition_1", "condition_2", "condition_3", "condition_4") if r[k] != "None"]
        # a row with no condition at all (frozenset()) is the "no health condition saved" row: the healthy beginner gets its own reviewed exercise list
        t.rows[frozenset(cond[n] for n in names)] = {
            "tops": [(r[f"top_{i}"], r[f"status_{i}"], r[f"caution_{i}"]) for i in range(1, 6) if r[f"top_{i}"]],
            "verdicts": {a: r[a] for a in t.names if r.get(a) in ("safe", "modify", "avoid")},      # all 12 exercises: safe / modify / avoid for this exact set
            "row_caution": r["row_caution"]}
    return t


def get() -> Table:
    """The loaded table. An empty Table (rows == {}) means it could not be loaded: report a system problem, never a medical refusal."""
    global _TABLE
    if _TABLE is None:
        with _LOCK:
            if _TABLE is None:
                try:
                    _TABLE = load()
                except Exception:  # noqa: BLE001
                    _log.exception("exercise table could not be loaded from %s", TABLE_PATH)
                    return Table()
    return _TABLE


def reset_for_tests() -> None:
    global _TABLE
    _TABLE = None


def gate(codes: list[str]) -> str | None:
    """Reason (a chat_messages reason) why NO exercises may be offered for this set of SAVED conditions, or None when the table has a row for it."""
    s = {c for c in codes if c and c != "none"}
    if not s:
        return None                                             # nothing saved: the existing generic self-care behaviour, no exercise list
    if "pregnancy" in s:
        return "pregnancy"
    if "neurological" in s:
        return "red_flag"
    if "heart_condition" in s:
        return "heart_or_uncontrolled"
    if "acute_injury" in s:
        return "acute_injury"
    if "recent_surgery" in s:
        return "recent_surgery"
    if s & NOT_IN_TABLE:
        return "not_in_exercise_table"
    if "other_unlisted" in s:
        return "out_of_scope"
    if s & {"high_bp", "diabetes"}:
        return "control_not_stated"
    if frozenset(s) not in get().rows:
        return "no_table_row"
    return None


def row_for(codes: list[str]) -> dict | None:
    return get().rows.get(frozenset(c for c in codes if c and c != "none"))


def approved_names(codes: list[str]) -> list[str]:
    """Exercise display names for this set, best first (empty when there is no row)."""
    row = row_for(codes)
    t = get()
    return [t.names[a] for a, _, _ in row["tops"]] if row else []


def approved_types(codes: list[str]) -> set[str]:
    row = row_for(codes)
    return {a for a, _, _ in row["tops"]} if row else set()


def allowed_types(codes: list[str]) -> set[str]:
    """Every exercise the table does NOT mark `avoid` for this exact set (safe, or modify = allowed with caution). Wider than approved_types (the ranked top picks):
    this is what the user may ADD when they ask for it. Empty when there is no row (fail closed)."""
    row = row_for(codes)
    return {a for a, v in row["verdicts"].items() if v in ("safe", "modify")} if row else set()


def verdict_for(codes: list[str], activity_type: str) -> str | None:
    row = row_for(codes)
    return row["verdicts"].get(activity_type) if row else None


def caution_for(codes: list[str], activity_type: str) -> str:
    """The exercise's own caution from the top-pick list, in plain words ('' when the table gives none for it)."""
    row = row_for(codes)
    for act, _status, caution in (row["tops"] if row else []):
        if act == activity_type and caution:
            return caution.replace("CAUTION: ", "").replace("NEEDS CLEARANCE: ", "needs a doctor clearance first. ").strip()
    return ""


def approved_block(codes: list[str], categories: dict[str, str]) -> str:
    """The 'Approved exercises' text for the answer prompt: best first, 'modify' ones with their caution."""
    row = row_for(codes)
    t = get()
    if not row:
        return "Approved exercises for this condition: none listed."
    parts = []
    for act, status, caution in row["tops"]:
        name = t.names[act]
        parts.append(f"{name} ({categories.get(name, 'general movement')})" + (
            f" [USE WITH CAUTION: {caution.replace('CAUTION: ', '').replace('NEEDS CLEARANCE: ', 'needs a doctor clearance first. ')}]" if status == "modify" else ""))
    general = f"\nGeneral cautions: {row['row_caution']}" if row["row_caution"] else ""
    return "Approved exercises for this condition, best first (with what each helps with): " + "; ".join(parts) + general
