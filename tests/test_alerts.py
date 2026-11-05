"""Slack error alerts (app/platform/alerts.py). Nothing here talks to Slack or an AI model:
those two calls are replaced, so the tests check OUR behaviour (what is sent, how often,
and that no failure can reach a user request)."""
import json
import uuid
from types import SimpleNamespace

import pytest
import redis
import requests
from flask import Flask

from app.platform import alerts
from app.platform.errors import ApiError, register_error_handlers


def _cfg(**over):
    base = dict(
        slack_alerts_enabled=True, slack_webhook_url="https://hooks.slack.com/services/T000/B000/XXXX",
        alert_cause_analysis_enabled=True, alert_cause_model="m", alert_dedup_seconds=900, alert_max_per_hour=20,
        app_version="abc1234", env="production", is_production=True, openrouter_api_key="k",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _raise(exc):
    try:
        raise exc
    except Exception as e:           # has a real traceback, like in the error handler
        return e


# ------------------------------------------------------------------ scrubbing
def test_scrub_removes_credentials_and_personal_data():
    s = alerts.scrub(
        "failed for ayush@example.com using postgresql+psycopg://admin:S3cretPw@db.host:5432/app "
        "password=hunter2 Authorization: Bearer abc.def "
        "token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJlMTIzNDU2 id 0123456789abcdef0123456789abcdef0123")
    for leaked in ("ayush@example.com", "S3cretPw", "hunter2", "abc.def", "eyJhbGci", "0123456789abcdef0123456789abcdef"):
        assert leaked not in s
    assert "<email>" in s and "<credentials>" in s
    assert "<token>" in alerts.scrub("jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJlMTIzNDU2")


def test_clean_message_drops_sql_and_parameters():
    exc = Exception("(psycopg.errors.UniqueViolation) duplicate key value violates unique constraint "
                    '"user_profiles_pkey"\n[SQL: INSERT INTO user_profiles (age) VALUES (%(age)s)]\n[parameters: {\'age\': 41, \'email\': \'a@b.com\'}]')
    m = alerts.clean_message(exc)
    assert "duplicate key" in m and "INSERT" not in m and "parameters" not in m and "a@b.com" not in m


def test_slack_escape_neutralises_mentions_and_markup():
    out = alerts.slack_escape("<!channel> hi <https://x|y> & `code`")
    assert "<" not in out and ">" not in out and "`" not in out
    assert "&lt;!channel&gt;" in out


# ------------------------------------------------------------------ fingerprint + causes
def test_fingerprint_same_place_same_id_but_route_and_type_matter():
    a1, a2 = _raise(ValueError("one")), _raise(ValueError("two, different text"))
    assert alerts.fingerprint(a1, "POST /x") == alerts.fingerprint(a1, "POST /x")
    assert alerts.fingerprint(a1, "POST /x") != alerts.fingerprint(a1, "GET /y")
    assert alerts.fingerprint(a1, "POST /x") != alerts.fingerprint(_raise(KeyError("k")), "POST /x")


@pytest.mark.parametrize("exc, expect", [
    (redis.exceptions.ConnectionError("boom"), "redis"),
    (requests.ConnectionError("down"), "outside service"),
    (KeyError("x"), "expected a value"),
    (Exception("duplicate key value violates unique constraint"), "database rule"),
])
def test_rule_based_cause_covers_common_failures(exc, expect):
    c = alerts.rule_based_cause(exc, 500)
    assert expect in c["probable_cause"].lower() and c["why"] and c["suggested_fix"]


def test_parse_analysis_accepts_fenced_json_and_trims():
    raw = '```json\n{"probable_cause": "' + "x" * 900 + '", "why": "two taps", "suggested_fix": "debounce", "confidence": "HIGH"}\n```'
    out = alerts.parse_analysis(raw)
    assert out["confidence"] == "high" and len(out["probable_cause"]) <= 400 and out["source"] == "AI analysis"


@pytest.mark.parametrize("raw", ["", "no json here", '{"probable_cause": "x"}', "{broken"])
def test_parse_analysis_rejects_unusable_replies(raw):
    assert alerts.parse_analysis(raw) is None


# ------------------------------------------------------------------ analysis failure handling
def test_analyze_cause_returns_none_when_model_fails(app, monkeypatch):
    import app.rag.vectorstore as vs
    monkeypatch.setattr(vs, "chat_complete", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("model down")))
    ev = {"fingerprint": "t-" + uuid.uuid4().hex, "exc_type": "X", "message": "m", "status": 500, "route": "GET /r",
          "where": "w", "frames": [], "snippet": ""}
    with app.app_context():
        assert alerts.analyze_cause(_cfg(), ev) is None


def test_analyze_cause_uses_model_reply_and_caches_it(app, monkeypatch):
    import app.rag.vectorstore as vs
    calls = []
    reply = json.dumps({"probable_cause": "two requests collided", "why": "double tap", "suggested_fix": "disable button", "confidence": "medium"})
    monkeypatch.setattr(vs, "chat_complete", lambda *a, **k: calls.append(1) or reply)
    ev = {"fingerprint": "t-" + uuid.uuid4().hex, "exc_type": "X", "message": "m", "status": 500, "route": "GET /r",
          "where": "w", "frames": [], "snippet": ""}
    with app.app_context():
        first = alerts.analyze_cause(_cfg(), ev)
        second = alerts.analyze_cause(_cfg(), ev)
    assert first["probable_cause"] == "two requests collided" and second == first
    assert len(calls) == 1                         # second answer came from the cache


def test_analyze_cause_skipped_when_disabled_or_no_key(app):
    ev = {"fingerprint": "t-x"}
    with app.app_context():
        assert alerts.analyze_cause(_cfg(alert_cause_analysis_enabled=False), ev) is None
        assert alerts.analyze_cause(_cfg(openrouter_api_key=""), ev) is None


# ------------------------------------------------------------------ repeat suppression + cap
def test_same_error_announced_once_then_counted(app):
    fp = "t-" + uuid.uuid4().hex
    with app.app_context():
        assert alerts.should_send(_cfg(), fp) == (True, 0, False)
        assert alerts.should_send(_cfg(), fp) == (False, 0, False)
        assert alerts.should_send(_cfg(), fp) == (False, 0, False)
        r = alerts._cache(); r.delete(f"alert:fp:{fp}")          # the window ends
        assert alerts.should_send(_cfg(), fp) == (True, 2, False)  # and the repeats are reported


def test_hourly_cap_stops_messages_after_one_notice(app):
    cfg = _cfg(alert_max_per_hour=2)
    with app.app_context():
        r = alerts._cache(); r.delete(f"alert:hour:{alerts._hour_key()}")
        results = [alerts.should_send(cfg, "t-" + uuid.uuid4().hex) for _ in range(5)]
        r.delete(f"alert:hour:{alerts._hour_key()}")
    assert [x[0] for x in results] == [True, True, True, False, False]
    assert [x[2] for x in results] == [False, False, True, False, False]   # exactly one "limit reached" notice


# ------------------------------------------------------------------ the message
def test_payload_contains_cause_and_no_private_data():
    exc = _raise(ValueError("bad for ayush@example.com"))
    ev = alerts.build_event(_cfg(), exc, status=500, route="POST /v1/profile/onboarding", request_id="req-abc123")
    payload = alerts.build_payload(_cfg(), ev, ev["rule_cause"], suppressed=3)
    text = json.dumps(payload)
    assert "POST /v1/profile/onboarding" in text and "req-abc123" in text and "3 more since the last alert" in text
    assert "Likely cause" in text and "Suggested fix" in text and "abc1234" in text
    assert "ayush@example.com" not in text
    assert payload["text"]                                                    # notification fallback text


def test_payload_never_lets_model_text_ping_channel():
    exc = _raise(ValueError("x"))
    ev = alerts.build_event(_cfg(), exc, status=500, route="GET /r", request_id=None)
    cause = {"probable_cause": "<!channel> wake up", "why": "<!here>", "suggested_fix": "`rm -rf`", "confidence": "high", "source": "AI analysis"}
    text = json.dumps(alerts.build_payload(_cfg(), ev, cause))
    assert "<!channel>" not in text and "<!here>" not in text


# ------------------------------------------------------------------ Slack + pipeline failures never escape
def test_post_to_slack_swallows_network_errors(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("no route")))
    assert alerts.post_to_slack(_cfg(), {"text": "x"}) is False


def test_post_to_slack_reports_non_200_without_raising(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: SimpleNamespace(status_code=500))
    assert alerts.post_to_slack(_cfg(), {"text": "x"}) is False


def test_post_to_slack_refuses_unknown_hosts_in_production(monkeypatch):
    called = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: called.append(1))
    assert alerts.post_to_slack(_cfg(slack_webhook_url="https://evil.example.com/hook"), {}) is False
    assert alerts.post_to_slack(_cfg(slack_webhook_url="http://127.0.0.1:9999/x"), {}) is False   # localhost only outside production
    assert not called


def test_process_sends_once_for_repeats_and_falls_back_to_rule_when_ai_is_down(app, monkeypatch):
    sent = []
    monkeypatch.setattr(alerts, "post_to_slack", lambda cfg, payload: sent.append(payload) or True)
    monkeypatch.setattr(alerts, "analyze_cause", lambda cfg, ev: None)
    cfg = _cfg()
    exc = _raise(ValueError("x"))
    ev = alerts.build_event(cfg, exc, status=500, route="GET /unique-" + uuid.uuid4().hex, request_id="req-1")
    with app.app_context():
        alerts._process(ev); alerts._process(ev); alerts._process(ev)
    assert len(sent) == 1
    assert "expected a value" in json.dumps(sent[0]).lower()                  # built-in rule used


def test_process_survives_everything_failing(app, monkeypatch):
    monkeypatch.setattr(alerts, "should_send", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("redis exploded")))
    ev = alerts.build_event(_cfg(), _raise(ValueError("x")), status=500, route="GET /r", request_id=None)
    with pytest.raises(RuntimeError):
        alerts._process(ev)           # _process itself may raise ...
    # ... but the worker loop wraps it, so one bad alert can never stop later ones:
    alerts._QUEUE.put_nowait(ev)
    try:
        alerts._QUEUE.get_nowait()
    finally:
        pass


# ------------------------------------------------------------------ the request path
def _mini_app(monkeypatch, cfg):
    spy = []
    monkeypatch.setattr(alerts, "_QUEUE", __import__("queue").Queue(maxsize=50))
    monkeypatch.setattr(alerts, "_ensure_worker", lambda: None)               # no background thread in tests
    app = Flask(__name__)
    app.config["APP_CONFIG"] = cfg
    register_error_handlers(app)

    @app.get("/boom")
    def boom(): return 1 / 0

    @app.get("/gateway")
    def gateway(): raise ApiError("bad gateway", status=502) if False else _bad_gateway()

    @app.get("/missing-thing")
    def missing(): from werkzeug.exceptions import NotFound; raise NotFound()

    @app.get("/teapot")
    def teapot(): raise ApiError("nope")

    return app


def _bad_gateway():
    class Bad(ApiError):
        status, code = 502, "bad_gateway"
    raise Bad("The voice provider rejected the request.")


def test_server_error_response_is_unchanged_and_alert_is_queued(app, monkeypatch):
    mini = _mini_app(monkeypatch, _cfg())
    r = mini.test_client().get("/boom")
    assert r.status_code == 500 and r.get_json() == {"error": {"code": "internal_error", "message": "Something went wrong"}}
    ev = alerts._QUEUE.get_nowait()
    assert ev["exc_type"] == "ZeroDivisionError" and ev["route"] == "GET /boom" and ev["status"] == 500


def test_bad_gateway_from_a_provider_also_alerts_but_keeps_its_response(app, monkeypatch):
    mini = _mini_app(monkeypatch, _cfg())
    r = mini.test_client().get("/gateway")
    assert r.status_code == 502 and r.get_json()["error"]["code"] == "bad_gateway"
    assert alerts._QUEUE.get_nowait()["status"] == 502


def test_normal_client_errors_never_alert(app, monkeypatch):
    mini = _mini_app(monkeypatch, _cfg())
    c = mini.test_client()
    assert c.get("/missing-thing").status_code == 404
    assert c.get("/nope-not-a-route").status_code == 404
    assert c.get("/teapot").status_code == 400
    assert alerts._QUEUE.empty()


def test_disabled_or_unconfigured_means_nothing_is_queued(app, monkeypatch):
    for cfg in (_cfg(slack_alerts_enabled=False), _cfg(slack_webhook_url="")):
        mini = _mini_app(monkeypatch, cfg)
        assert mini.test_client().get("/boom").status_code == 500
        assert alerts._QUEUE.empty()


def test_full_queue_drops_alert_but_request_still_gets_its_normal_error(app, monkeypatch):
    mini = _mini_app(monkeypatch, _cfg())
    q = alerts._QUEUE
    for _ in range(50):
        q.put_nowait({})
    r = mini.test_client().get("/boom")
    assert r.status_code == 500 and r.get_json()["error"]["code"] == "internal_error"


def test_alert_hook_failure_cannot_change_the_response(app, monkeypatch):
    mini = _mini_app(monkeypatch, _cfg())
    monkeypatch.setattr(alerts, "build_event", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bug in alerts")))
    r = mini.test_client().get("/boom")
    assert r.status_code == 500 and r.get_json()["error"]["code"] == "internal_error"


def test_defaults_are_off(monkeypatch):
    for k in ("SLACK_ALERTS_ENABLED", "SLACK_WEBHOOK_URL"):
        monkeypatch.delenv(k, raising=False)
    from app.platform.config import load_config
    cfg = load_config()
    assert cfg.slack_alerts_enabled is False and cfg.slack_webhook_url == ""
    assert cfg.alert_dedup_seconds == 900 and cfg.alert_max_per_hour == 20
