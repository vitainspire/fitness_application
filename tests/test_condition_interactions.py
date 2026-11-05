"""Combined-conditions safety check (MULTI_CONDITION_INTERACTION_SAFETY_PLAN.md).

Seeded real pair for these tests: arthritis_knee + obesity x running = avoid
(data/condition_interactions.csv) — both conditions are individually only
`modify` for running (data/safety_grid.csv), so any escalation to `avoid`
proves the interaction layer actually fired, not just the single-condition
grid.
"""
import pytest

from app.catalog import loader as catalog_loader
from app.catalog.models import ConditionInteractionCell
from app.platform.db import session_scope
from app.profile import service as profile_service
from app.profile.models import OverrideConsent, UserProgram

ACTIVITY = "running"
COND_A = "arthritis_knee"
COND_B = "obesity"


def _onboard(session, user_id, condition_codes):
    profile_service.complete_onboarding(
        session, user_id, age=30, goal="health", experience_band="beginner",
        condition_codes=condition_codes, unit_preference="km",
    )


# --- loader validation -------------------------------------------------------

def _run_loader_with_csv(tmp_path, monkeypatch, rows_text):
    data_dir = tmp_path
    (data_dir / "condition_interactions.csv").write_text(rows_text, encoding="utf-8")
    monkeypatch.setattr(catalog_loader, "DATA", data_dir)
    with session_scope() as s:
        return catalog_loader.load_condition_interactions(s)


HEADER = "condition_a,condition_b,activity_type,verdict,note,reviewer,reviewed_at,version\n"


def test_loader_rejects_safe_verdict(tmp_path, monkeypatch, app):
    rows = HEADER + f"{COND_A},{COND_B},{ACTIVITY},safe,note,reviewer,2026-10-08,1\n"
    with pytest.raises(catalog_loader.CoverageError, match="must be 'modify' or 'avoid'"):
        _run_loader_with_csv(tmp_path, monkeypatch, rows)


def test_loader_rejects_self_pair(tmp_path, monkeypatch, app):
    rows = HEADER + f"{COND_A},{COND_A},{ACTIVITY},avoid,note,reviewer,2026-10-08,1\n"
    with pytest.raises(catalog_loader.CoverageError, match="paired with itself"):
        _run_loader_with_csv(tmp_path, monkeypatch, rows)


def test_loader_rejects_unknown_condition_code(tmp_path, monkeypatch, app):
    rows = HEADER + f"not_a_real_condition,{COND_B},{ACTIVITY},avoid,note,reviewer,2026-10-08,1\n"
    with pytest.raises(catalog_loader.CoverageError, match="not a supported-bucket condition"):
        _run_loader_with_csv(tmp_path, monkeypatch, rows)


def test_loader_rejects_refer_bucket_condition(tmp_path, monkeypatch, app):
    # pregnancy is a refer-bucket condition (data/conditions.csv) — already
    # blocks all content on its own, so pairing it here is meaningless.
    rows = HEADER + f"pregnancy,{COND_B},{ACTIVITY},avoid,note,reviewer,2026-10-08,1\n"
    with pytest.raises(catalog_loader.CoverageError, match="not a supported-bucket condition"):
        _run_loader_with_csv(tmp_path, monkeypatch, rows)


def test_loader_rejects_unknown_activity(tmp_path, monkeypatch, app):
    rows = HEADER + f"{COND_A},{COND_B},not_a_real_activity,avoid,note,reviewer,2026-10-08,1\n"
    with pytest.raises(catalog_loader.CoverageError, match="unknown activity_type"):
        _run_loader_with_csv(tmp_path, monkeypatch, rows)


def test_loader_rejects_duplicate_pair_activity(tmp_path, monkeypatch, app):
    row = f"{COND_A},{COND_B},{ACTIVITY},avoid,note,reviewer,2026-10-08,1\n"
    with pytest.raises(catalog_loader.CoverageError, match="duplicate row"):
        _run_loader_with_csv(tmp_path, monkeypatch, HEADER + row + row)


def test_loader_canonicalizes_reversed_order(tmp_path, monkeypatch, app):
    # Written as obesity, arthritis_knee (reverse of alphabetical) — loader
    # must store it as (arthritis_knee, obesity) regardless.
    rows = HEADER + f"{COND_B},{COND_A},{ACTIVITY},avoid,note,reviewer,2026-10-08,1\n"
    n = _run_loader_with_csv(tmp_path, monkeypatch, rows)
    assert n == 1
    with session_scope() as s:
        cell = s.get(ConditionInteractionCell, (COND_A, COND_B, ACTIVITY))
        assert cell is not None
        assert cell.verdict == "avoid"


@pytest.fixture(autouse=True)
def _restore_real_seed_data(monkeypatch):
    """Every test above replaces the table's contents via a temp CSV. Put the
    real seeded row back afterward so later tests in this file (and any other
    test module relying on the real data/condition_interactions.csv seed)
    see the actual reviewed data, not a leftover test fixture row.

    Found live (first run of this file): a fixture that DEPENDS ON monkeypatch
    keeps monkeypatch alive until AFTER it finishes, not before — the exact
    opposite of what's needed here. This fixture's own teardown used to run
    BEFORE monkeypatch's automatic undo, so it re-seeded from whichever temp
    CSV a loader-validation test had pointed catalog_loader.DATA at (e.g. the
    duplicate-row test's bad CSV) — raising the exact CoverageError this
    fixture exists to avoid, on every single raising test in the file.
    monkeypatch.undo() is explicitly documented as safe to call early and
    idempotent, so calling it here directly — rather than hoping teardown
    order cooperates — is what actually fixes it.
    """
    yield
    monkeypatch.undo()
    with session_scope() as s:
        catalog_loader.load_condition_interactions(s)


# --- service-layer: verdict escalation --------------------------------------

def test_single_condition_alone_does_not_escalate(make_user):
    # chronic_back_pain alone (not arthritis_knee: the audited 794 table now marks running `avoid` for knee pain on its own) keeps running at `modify`
    user = make_user(condition_codes=["chronic_back_pain"])
    with session_scope() as s:
        modified = profile_service._modified_activities(s, user["user_id"])
        blocked = profile_service._blocked_activities(s, user["user_id"])
        assert ACTIVITY in modified  # ordinary single-condition modify, unchanged
        assert ACTIVITY not in blocked  # no pair yet -> no escalation


def test_both_conditions_together_escalate_to_avoid(make_user):
    user = make_user(condition_codes=[COND_A, COND_B])
    with session_scope() as s:
        blocked = profile_service._blocked_activities(s, user["user_id"])
        assert ACTIVITY in blocked  # escalated by the interaction row


def test_worst_verdict_reports_pair_kind_with_both_names(make_user):
    user = make_user(condition_codes=[COND_A, COND_B])
    with session_scope() as s:
        worst = profile_service._worst_verdict(s, user["user_id"], ACTIVITY)
        assert worst["kind"] == "pair"
        assert worst["verdict"] == "avoid"
        assert {worst["condition_a_code"], worst["condition_b_code"]} == {COND_A, COND_B}


# --- add_program_entry: confirmation flow + OverrideConsent -----------------

def test_add_program_entry_needs_pair_confirmation(make_user):
    user = make_user(condition_codes=[COND_A, COND_B])
    with session_scope() as s:
        profile_service.remove_program_entry(s, user["user_id"], ACTIVITY)
    with session_scope() as s:
        result = profile_service.add_program_entry(s, user["user_id"], ACTIVITY)
        assert result["needs_confirmation"] is True
        assert result["added"] is False
        assert "condition_code_2" in result
        assert {result["condition_code"], result["condition_code_2"]} == {COND_A, COND_B}
        assert "Together" in result["warning_text"]


def test_add_program_entry_confirmed_writes_pair_override_consent(make_user):
    user = make_user(condition_codes=[COND_A, COND_B])
    with session_scope() as s:
        profile_service.remove_program_entry(s, user["user_id"], ACTIVITY)
    with session_scope() as s:
        result = profile_service.add_program_entry(
            s, user["user_id"], ACTIVITY, override_confirmed=True)
        assert result["added"] is True
    with session_scope() as s:
        consent = s.query(OverrideConsent).filter_by(
            user_id=user["user_id"], activity_type=ACTIVITY).one()
        assert {consent.condition_code, consent.condition_code_2} == {COND_A, COND_B}
        entry = s.get(UserProgram, (user["user_id"], ACTIVITY))
        assert entry.status == "active"
        assert entry.source == "user_override"


# --- build_program: auto-suspend / resurrect on condition change -----------

def test_build_program_auto_suspends_when_pair_completes(make_user):
    # Starts with obesity (running is `modify` for it alone, in the old grid AND the audited 794 table); knee pain alone is `avoid` in the audited table now.
    user = make_user(condition_codes=[COND_B])
    with session_scope() as s:
        entry = s.get(UserProgram, (user["user_id"], ACTIVITY))
        assert entry.status == "active"  # only modify so far, not blocked

    with session_scope() as s:
        _onboard(s, user["user_id"], [COND_A, COND_B])  # completes the pair
        entry = s.get(UserProgram, (user["user_id"], ACTIVITY))
        assert entry.status == "suspended"
        assert entry.suspended_reason == "condition_safety"


def test_build_program_resurrects_when_pair_breaks(make_user):
    # Starts with only COND_B (obesity) so the entry is created ACTIVE first (running is
    # only `modify` for obesity alone) — build_program never creates a
    # row at all for an activity that's already blocked the very first time it
    # sees a user (see its own "if entry is None: if not is_blocked: create"
    # logic), so a user onboarded with both conditions from day one would have
    # no row here to resurrect in the first place.
    user = make_user(condition_codes=[COND_B])
    with session_scope() as s:
        _onboard(s, user["user_id"], [COND_A, COND_B])  # completes the pair -> suspends
        entry = s.get(UserProgram, (user["user_id"], ACTIVITY))
        assert entry.status == "suspended"

    with session_scope() as s:
        _onboard(s, user["user_id"], [COND_B])  # breaks the pair
        entry = s.get(UserProgram, (user["user_id"], ACTIVITY))
        assert entry.status == "active"
        assert entry.suspended_reason is None
