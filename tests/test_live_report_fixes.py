"""Fixes from the live-application report: atomic counters (water, workout-session count),
fewer repeated queries per read, the managed person's name, bounded password hashing,
extra headers, and the onboarding-wipe log."""
import contextlib
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import event, select

from app.auth import passwords
from app.platform import db as dbmod
from app.platform.config import load_config
from app.platform.db import session_scope
from app.platform.errors import ValidationError
from app.profile import service
from app.profile.models import WaterIntakeLog, WorkoutSession
from app.relationships import service as rel_service


def _parallel(n, fn):
    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = [pool.submit(fn, i) for i in range(n)]
        out = []
        for f in futures:
            try:
                out.append(f.result())
            except Exception as exc:  # noqa: BLE001 - the tests inspect the exceptions
                out.append(exc)
        return out


# ── F1: water counter ──────────────────────────────────────────────────────

def test_parallel_water_taps_are_all_counted(make_user):
    user = make_user()

    def tap(_):
        with session_scope() as s:
            return service.add_water_glass(s, user["user_id"])["glasses_count"]

    results = _parallel(8, tap)
    assert not [r for r in results if isinstance(r, Exception)], results
    assert sorted(results) == list(range(1, 9))               # every tap got its own number
    with session_scope() as s:
        assert service.get_water_today(s, user["user_id"])["glasses_count"] == 8


def test_water_cap_is_enforced_under_parallel_taps(make_user):
    user = make_user()
    with session_scope() as s:
        s.add(WaterIntakeLog(user_id=user["user_id"], local_date=service.today_ist(),
                             glasses_count=service.WATER_DAILY_CAP - 2))

    def tap(_):
        with session_scope() as s:
            return service.add_water_glass(s, user["user_id"])["glasses_count"]

    results = _parallel(5, tap)
    errors = [r for r in results if isinstance(r, ValidationError)]
    assert len(errors) == 3 and len([r for r in results if isinstance(r, int)]) == 2
    with session_scope() as s:
        assert service.get_water_today(s, user["user_id"])["glasses_count"] == service.WATER_DAILY_CAP


def test_water_sequential_behaviour_is_unchanged(make_user):
    user = make_user()
    with session_scope() as s:
        assert service.get_water_today(s, user["user_id"])["glasses_count"] == 0
        for expected in (1, 2, 3):
            assert service.add_water_glass(s, user["user_id"])["glasses_count"] == expected
            # read in the SAME session after each write must not be stale
            assert service.get_water_today(s, user["user_id"])["glasses_count"] == expected


def test_water_http_route_still_works(client, make_user):
    user = make_user()
    for n in (1, 2):
        r = client.post("/v1/water-intake/add", headers=user["headers"])
        assert r.status_code == 200 and r.get_json()["glasses_count"] == n
    assert client.get("/v1/water-intake/today", headers=user["headers"]).get_json()["glasses_count"] == 2


# ── F1b: workout-session counter ───────────────────────────────────────────

def test_parallel_logs_in_one_session_are_all_counted(make_user):
    user = make_user()
    with session_scope() as s:
        sid = service.start_workout_session(s, user["user_id"], ["glute_bridge"])["id"]

    def log(i):
        with session_scope() as s:
            return service.log_activity(s, user["user_id"], activity_type="glute_bridge", amount=5, sets=2,
                                        workout_session_id=sid, client_entry_id=f"par-{sid}-{i}")

    results = _parallel(8, log)
    assert not [r for r in results if isinstance(r, Exception)], results
    with session_scope() as s:
        assert s.get(WorkoutSession, sid).completed_activity_count == 8


# ── F3: fewer repeated queries ─────────────────────────────────────────────

class _QueryCounter:
    def __enter__(self):
        self.n = 0
        self._cb = lambda *a, **k: setattr(self, "n", self.n + 1)
        event.listen(dbmod._engine, "before_cursor_execute", self._cb)
        return self

    def __exit__(self, *exc):
        event.remove(dbmod._engine, "before_cursor_execute", self._cb)


def test_read_endpoints_stay_under_their_query_ceilings(client, make_user):
    """Measured after the fix: suggestions 12, summary 17, program 6. A few spare, but a repeated
    lookup creeping back in (the cause of the slow home screen) would break these."""
    user = make_user()
    client.post("/v1/activities", json={"activity_type": "glute_bridge", "amount": 5, "sets": 2},
                headers=user["headers"])
    for path, ceiling in (("/v1/suggestions", 14), ("/v1/dashboard/summary", 19), ("/v1/program", 8)):      # suggestions 13->14, summary 18->19, program 7->8: the audited condition_combinations lookup (one indexed read per request)
        with _QueryCounter() as q:
            assert client.get(path, headers=user["headers"]).status_code == 200
        assert q.n <= ceiling, f"{path} ran {q.n} queries (ceiling {ceiling})"


@pytest.mark.parametrize("codes", [["none"], ["chronic_back_pain"], ["arthritis_knee", "obesity"],
                                   ["herniated_disk", "arthritis_knee"]])
def test_cached_reads_equal_uncached_reads(make_user, monkeypatch, codes):
    user = make_user(condition_codes=codes)
    with session_scope() as s:
        cached = (service.get_program(s, user["user_id"]), service.suggestions_today(s, user["user_id"]),
                  service.list_catalogue(s, user["user_id"]))
    monkeypatch.setattr(service, "_read_cache", lambda session: contextlib.nullcontext())
    with session_scope() as s:
        plain = (service.get_program(s, user["user_id"]), service.suggestions_today(s, user["user_id"]),
                 service.list_catalogue(s, user["user_id"]))
    assert cached == plain


def test_cache_never_outlives_the_read_and_returns_private_copies(make_user):
    user = make_user()
    uid = user["user_id"]
    with session_scope() as s:
        with service._read_cache(s):
            blocked = service._blocked_activities(s, uid)
            blocked["tampered"] = "x"
            assert "tampered" not in service._blocked_activities(s, uid)      # callers get copies
            assert service.is_referred(s, uid) is False
        assert s.info.get("_read_cache") is None                              # gone after the block
        # a change made AFTER the read is seen by the next read (nothing stale is served)
        service.complete_onboarding(s, uid, age=30, goal="health", experience_band="beginner",
                                    condition_codes=["heart_condition"])
        assert service.is_referred(s, uid) is True


def test_nested_reads_share_one_cache(make_user):
    user = make_user(condition_codes=["arthritis_knee", "obesity"])
    with session_scope() as s:
        with _QueryCounter() as outer:
            service.suggestions_today(s, user["user_id"])
    with session_scope() as s:
        with _QueryCounter() as two:
            service.get_program(s, user["user_id"])
            service.get_program(s, user["user_id"])       # separate top-level calls: no sharing
    with session_scope() as s:
        with _QueryCounter() as one:
            service.get_program(s, user["user_id"])
    assert two.n == 2 * one.n                              # confirms the cache is per call, not global
    assert outer.n <= 16   # two conditions add the combined-conditions lookups (measured: 14, 16 with the audited condition_combinations lookup)


# ── F8: managed person's name ──────────────────────────────────────────────

def test_managed_account_profile_carries_the_managers_name(client, make_user):
    manager = make_user()
    inv = client.post("/v1/relationships", json={"relation_type": "student", "name": "Priya"},
                      headers=manager["headers"]).get_json()
    opened = client.post("/v1/relationships/open", json={"invite_token": inv["invite_token"]},
                         headers={"X-Device-Id": "pytest-managed-dev"}).get_json()
    headers = {"X-Device-Id": "pytest-managed-dev", "Authorization": "Bearer " + opened["access_token"]}
    p = client.get("/v1/profile", headers=headers).get_json()
    assert p["is_managed_account"] is True and p["display_name"] == "Priya"
    assert client.get("/v1/profile", headers=manager["headers"]).get_json()["display_name"] is None


def test_display_name_is_null_after_the_plan_is_revoked(client, make_user):
    manager = make_user()
    with session_scope() as s:
        inv = rel_service.create_invite(s, manager["user_id"], "child", "Rahul")
        _p, user, _r = rel_service.open_invite(s, load_config(), inv["invite_token"], "dev")
        assert service.profile_summary(s, user.id)["display_name"] == "Rahul"
        rel_service.revoke_relationship(s, manager["user_id"], inv["id"])
        assert service.profile_summary(s, user.id)["display_name"] is None


# ── F4: bounded password hashing ───────────────────────────────────────────

def test_password_hashing_is_bounded_and_still_correct(monkeypatch):
    state = {"now": 0, "max": 0}
    lock = threading.Lock()
    real = passwords._hasher

    class Fake:
        def hash(self, plain):
            with lock:
                state["now"] += 1
                state["max"] = max(state["max"], state["now"])
            time.sleep(0.05)
            with lock:
                state["now"] -= 1
            return real.hash(plain)

        def verify(self, stored, plain):
            return real.verify(stored, plain)

    monkeypatch.setattr(passwords, "_hasher", Fake())
    results = _parallel(8, lambda i: passwords.hash_password(f"pw-{i}-long-enough"))
    assert not [r for r in results if isinstance(r, Exception)]
    assert state["max"] <= 2                                  # never more than two at once
    h = real.hash("correct horse battery staple")
    monkeypatch.setattr(passwords, "_hasher", real)
    assert passwords.verify_password(h, "correct horse battery staple") is True
    assert passwords.verify_password(h, "wrong password here") is False


# ── F9: headers ────────────────────────────────────────────────────────────

def test_frame_and_referrer_headers_on_every_response(client, make_user):
    user = make_user()
    for path, headers in (("/healthz", {}), ("/v1/conditions", {}), ("/v1/profile", user["headers"]),
                          ("/v1/nope", {})):
        r = client.get(path, headers=headers)
        assert r.headers.get("X-Frame-Options") == "DENY", path
        assert r.headers.get("Referrer-Policy") == "no-referrer", path
        assert r.headers.get("X-Content-Type-Options") == "nosniff", path


# ── F10: onboarding re-run that clears conditions leaves a trace ───────────

def test_onboarding_rerun_that_clears_conditions_is_logged(client, make_user, caplog):
    user = make_user(condition_codes=["chronic_back_pain"])
    body = {"age": 30, "goal": "health", "experience_band": "beginner", "unit_preference": "km"}
    with caplog.at_level(logging.INFO, logger="app.profile.service"):
        caplog.clear()
        client.post("/v1/profile/onboarding", json={**body, "condition_codes": ["chronic_back_pain"]},
                    headers=user["headers"])
        assert "replaced" not in caplog.text                        # same conditions: no trace
        client.post("/v1/profile/onboarding", json={**body, "condition_codes": []}, headers=user["headers"])
        assert "replaced 1 condition(s) with none" in caplog.text
        assert client.get("/v1/profile", headers=user["headers"]).status_code == 200   # behaviour unchanged


def test_first_onboarding_is_not_logged(client, caplog):
    import uuid
    u = uuid.uuid4().hex[:10]
    h = {"X-Device-Id": f"first-{u}", "X-Forwarded-For": f"10.9.{int(u[:2], 16)}.{int(u[2:4], 16)}"}
    r = client.post("/v1/auth/signup", json={"email": f"first.{u}@example.com",
                                             "password": "A-good-long-passphrase-123"}, headers=h)
    h["Authorization"] = "Bearer " + r.get_json()["access_token"]
    with caplog.at_level(logging.INFO, logger="app.profile.service"):
        client.post("/v1/profile/onboarding", json={"age": 30, "goal": "health", "experience_band": "beginner",
                                                    "condition_codes": [], "unit_preference": "km"}, headers=h)
    assert "replaced" not in caplog.text
