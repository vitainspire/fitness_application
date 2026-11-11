"""The app's own safety check (starting program, add-exercise screen, managed-plan picker) now reads the AUDITED 794-combination table,
the same table the chat answers from. Before this, a knee-pain user got Running in the starting program because the old per-condition grid only said "modify".

No AI calls: these run against the local dev Postgres only.
"""
import csv
from pathlib import Path

import pytest

from app.catalog import loader as catalog_loader
from app.catalog.models import ConditionCombinationCell
from app.platform.db import session_scope
from app.profile import service as profile_service
from app.profile.models import UserProgram
from app.relationships import service as relationships_service

DATA = Path(__file__).resolve().parents[1] / "data"
KNEE = "arthritis_knee"
BACK = "chronic_back_pain"
RUNNING = "running"


def _active(session, user_id):
    return {r.activity_type for r in session.query(UserProgram).filter_by(user_id=user_id, status="active").all()}


@pytest.fixture(autouse=True)
def _restore_real_combinations(monkeypatch):
    """A loader test below swaps the table for a temp CSV; put the real audited data back so no later test sees a fixture."""
    yield
    monkeypatch.undo()
    with session_scope() as s:
        catalog_loader.load_condition_combinations(s)


# --- loader ------------------------------------------------------------------

def test_loader_loads_all_794_sets(app):
    with session_scope() as s:
        sets = {r[0] for r in s.query(ConditionCombinationCell.condition_set).distinct().all()}
        cells = s.query(ConditionCombinationCell).count()
    assert len(sets) == 794
    assert cells == 794 * 12


def test_database_matches_the_csv_exactly(app):
    """Every verdict for every set in the database equals the audited CSV (so the app and the chat can never disagree)."""
    with session_scope() as s:
        names = {c.display_name: c.condition_code for c in s.query(catalog_loader.Condition).all()}
        db = {(c.condition_set, c.activity_type): c.verdict for c in s.query(ConditionCombinationCell).all()}
    for r in csv.DictReader((DATA / "condition_combinations_794_AUDITED.csv").open(encoding="utf-8-sig", newline="")):
        key = "|".join(sorted(names[r[k]] for k in ("condition_1", "condition_2", "condition_3", "condition_4") if r[k] != "None"))
        for a in catalog_loader.COMBINATION_ACTIVITY_COLUMNS:
            assert db[(key, a)] == r[a], (key, a)


def _run_loader_with_csv(tmp_path, monkeypatch, text):
    (tmp_path / catalog_loader.COMBINATIONS_FILE).write_text(text, encoding="utf-8")
    monkeypatch.setattr(catalog_loader, "DATA", tmp_path)
    with session_scope() as s:
        return catalog_loader.load_condition_combinations(s)


HEADER = "combo_size,condition_1,condition_2,condition_3,condition_4," + ",".join(catalog_loader.COMBINATION_ACTIVITY_COLUMNS) + "\n"


def _row(c1="Knee pain", verdict="safe", n=1):
    return f"{n},{c1},None,None,None," + ",".join([verdict] * 12) + "\n"


def test_loader_rejects_a_blank_verdict(tmp_path, monkeypatch, app):
    with pytest.raises(catalog_loader.CoverageError, match="must be safe/modify/avoid"):
        _run_loader_with_csv(tmp_path, monkeypatch, HEADER + _row(verdict=""))


def test_loader_rejects_an_unknown_condition(tmp_path, monkeypatch, app):
    with pytest.raises(catalog_loader.CoverageError, match="unknown condition name"):
        _run_loader_with_csv(tmp_path, monkeypatch, HEADER + _row(c1="Not a condition"))


def test_loader_rejects_a_duplicate_set(tmp_path, monkeypatch, app):
    with pytest.raises(catalog_loader.CoverageError, match="duplicate condition set"):
        _run_loader_with_csv(tmp_path, monkeypatch, HEADER + _row() + _row())


def test_loader_refuses_a_missing_file(tmp_path, monkeypatch, app):
    monkeypatch.setattr(catalog_loader, "DATA", tmp_path)
    with session_scope() as s:
        with pytest.raises(catalog_loader.CoverageError, match="is missing"):
            catalog_loader.load_condition_combinations(s)


# --- the starting program (the reported bug) ---------------------------------

def test_knee_pain_alone_no_longer_gets_running_in_the_starting_program(make_user):
    user = make_user(condition_codes=[KNEE])
    with session_scope() as s:
        assert RUNNING in profile_service._blocked_activities(s, user["user_id"])
        assert RUNNING not in _active(s, user["user_id"])


def test_two_conditions_together_block_running_even_if_each_alone_does_not(make_user):
    user = make_user(condition_codes=[KNEE, BACK])
    with session_scope() as s:
        assert RUNNING in profile_service._blocked_activities(s, user["user_id"])
        assert RUNNING not in _active(s, user["user_id"])
        assert RUNNING not in profile_service._modified_activities(s, user["user_id"])    # avoid, not "modify"


def test_no_condition_still_gets_everything(make_user):
    user = make_user(condition_codes=["none"])
    with session_scope() as s:
        assert profile_service._blocked_activities(s, user["user_id"]) == {}
        assert RUNNING in _active(s, user["user_id"])


def test_every_audited_avoid_is_blocked_and_never_seeded_for_every_set(app):
    """For ALL 794 condition sets: what the audited table marks avoid is in the blocked list (and so never seeded by build_program)."""
    with session_scope() as s:
        rows = s.query(ConditionCombinationCell).filter_by(verdict="avoid").all()
        by_set = {}
        for r in rows:
            by_set.setdefault(r.condition_set, set()).add(r.activity_type)
        for key, avoided in by_set.items():
            codes = [c for c in key.split("|") if c]
            verdicts = profile_service.audited_verdicts_for_codes(s, codes)
            assert {a for a, v in verdicts.items() if v == "avoid"} == avoided, key
    assert len(by_set) > 100                                  # the table really blocks things for many sets


# --- the manual add-exercise screen ------------------------------------------

def test_manual_add_of_a_blocked_exercise_for_one_condition_needs_the_doctor_confirmation(make_user):
    user = make_user(condition_codes=[KNEE])
    with session_scope() as s:
        result = profile_service.add_program_entry(s, user["user_id"], RUNNING)
    assert result["needs_confirmation"] is True and result["added"] is False
    assert result["condition_code"] == KNEE


def test_manual_add_for_a_set_names_every_condition_and_records_the_consent(make_user):
    user = make_user(condition_codes=[KNEE, BACK])
    with session_scope() as s:
        worst = profile_service._worst_verdict(s, user["user_id"], RUNNING)
        assert worst["kind"] == "set" and worst["verdict"] == "avoid"
        result = profile_service.add_program_entry(s, user["user_id"], RUNNING)
    assert result["needs_confirmation"] is True and result["added"] is False
    assert {result["condition_code"], result["condition_code_2"]} == {KNEE, BACK}
    assert "Together" in result["warning_text"]
    with session_scope() as s:
        confirmed = profile_service.add_program_entry(s, user["user_id"], RUNNING, override_confirmed=True)
        assert confirmed["added"] is True
        assert s.get(UserProgram, (user["user_id"], RUNNING)).source == "user_override"


def test_catalogue_marks_a_blocked_exercise_as_avoid(make_user):
    user = make_user(condition_codes=[KNEE])
    with session_scope() as s:
        by_type = {c["activity_type"]: c for c in profile_service.list_catalogue(s, user["user_id"])}
    assert by_type[RUNNING]["verdict"] == "avoid"


# --- managed plans (staged conditions with no account yet) -------------------

def test_managed_plan_picker_blocks_by_the_audited_table(app):
    with session_scope() as s:
        blocked = relationships_service.blocked_activities_for_codes(s, [KNEE])
    assert RUNNING in blocked
