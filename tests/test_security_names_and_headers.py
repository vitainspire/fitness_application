"""Invite names may not contain markup; private responses carry no-store and every
response carries nosniff (and public reference data stays cacheable)."""
import pytest

from app.platform.db import session_scope
from app.relationships import service as rel_service


# ── invite names ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["<img src=x onerror=alert(1)>", "\"><script>alert(1)</script>", "a<b", "a>b"])
def test_invite_name_with_angle_brackets_is_rejected(client, make_user, bad):
    user = make_user()
    r = client.post("/v1/relationships", json={"relation_type": "student", "name": bad}, headers=user["headers"])
    assert r.status_code == 422
    assert "<" in r.get_json()["error"]["message"] and ">" in r.get_json()["error"]["message"]
    assert client.get("/v1/relationships", headers=user["headers"]).get_json()["relationships"] == []


@pytest.mark.parametrize("ok", ["Priya", "Dr. O'Neil", "Anaïs & Sam", "田中 太郎", "Kid #2"])
def test_normal_invite_names_still_work(client, make_user, ok):
    user = make_user()
    r = client.post("/v1/relationships", json={"relation_type": "child", "name": ok}, headers=user["headers"])
    assert r.status_code == 201, r.get_json()
    assert r.get_json()["name"] == ok


def test_empty_and_overlong_names_still_rejected_as_before(make_user):
    user = make_user()
    with session_scope() as s:
        from app.platform.errors import ValidationError
        with pytest.raises(ValidationError):
            rel_service.create_invite(s, user["user_id"], "student", "   ")
        with pytest.raises(ValidationError):
            rel_service.create_invite(s, user["user_id"], "student", "x" * 101)


# ── headers ────────────────────────────────────────────────────────────────

def test_every_response_has_nosniff(client, make_user):
    user = make_user()
    for path, headers in (("/healthz", {}), ("/v1/conditions", {}), ("/v1/profile", user["headers"]),
                          ("/v1/does-not-exist", {})):
        r = client.get(path, headers=headers)
        assert r.headers.get("X-Content-Type-Options") == "nosniff", path


def test_private_responses_are_no_store(client, make_user):
    user = make_user()
    assert client.get("/v1/profile", headers=user["headers"]).headers.get("Cache-Control") == "no-store"
    assert client.get("/v1/program", headers=user["headers"]).headers.get("Cache-Control") == "no-store"
    # an error on a private route is private too
    assert client.get("/v1/relationships/00000000-0000-0000-0000-000000000000/activity",
                      headers=user["headers"]).headers.get("Cache-Control") == "no-store"


def test_public_reference_data_is_not_forced_to_no_store(client):
    r = client.get("/v1/conditions", headers={"X-Device-Id": "pytest-public-hdr"})
    assert r.status_code == 200
    assert r.headers.get("Cache-Control") != "no-store"
    assert client.get("/healthz").headers.get("Cache-Control") != "no-store"


def test_cors_preflight_still_works(client):
    r = client.options("/v1/profile", headers={
        "Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "authorization,x-device-id"})
    assert r.status_code in (200, 204)


def test_a_routes_own_cache_header_is_kept(client, make_user, monkeypatch):
    from app.voice import elevenlabs
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key-not-real")
    monkeypatch.setattr(elevenlabs, "text_to_speech", lambda text: b"mp3")
    user = make_user()
    r = client.post("/v1/voice/tts", json={"text": "hi"}, headers=user["headers"])
    assert r.status_code == 200 and r.headers.get("Cache-Control") == "no-store"
