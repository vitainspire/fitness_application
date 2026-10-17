"""/v1/voice/tts and /v1/voice/stt — the server-side ElevenLabs proxy.

ElevenLabs itself is always faked here (no network, no credits spent); what is
under test is our own validation, auth, rate-limit and error-mapping behavior.
"""
import urllib.error

import pytest

from app.platform.errors import ApiError
from app.voice import elevenlabs, routes


@pytest.fixture
def key_configured(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key-not-real")


@pytest.fixture
def key_missing(monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)


def _audio_headers(user, content_type="audio/webm;codecs=opus"):
    return {**user["headers"], "Content-Type": content_type}


# ── /tts ────────────────────────────────────────────────────────────────

def test_tts_requires_auth(client, key_configured):
    r = client.post("/v1/voice/tts", json={"text": "hello"},
                    headers={"X-Device-Id": "pytest-noauth"})
    assert r.status_code == 401


def test_tts_returns_audio(client, make_user, key_configured, monkeypatch):
    user = make_user()
    seen = {}

    def fake_tts(text):
        seen["text"] = text
        return b"ID3-fake-mp3-bytes"
    monkeypatch.setattr(elevenlabs, "text_to_speech", fake_tts)

    r = client.post("/v1/voice/tts", json={"text": "  Vita here.  "}, headers=user["headers"])
    assert r.status_code == 200
    assert r.mimetype == "audio/mpeg"
    assert r.data == b"ID3-fake-mp3-bytes"
    assert seen["text"] == "Vita here."  # trimmed


@pytest.mark.parametrize("body", [{}, {"text": ""}, {"text": "   "}, {"text": 123}, {"text": None}])
def test_tts_rejects_bad_text(client, make_user, key_configured, body):
    user = make_user()
    r = client.post("/v1/voice/tts", json=body, headers=user["headers"])
    assert r.status_code == 422, r.get_json()


def test_tts_rejects_overlong_text(client, make_user, key_configured):
    user = make_user()
    r = client.post("/v1/voice/tts", json={"text": "x" * (elevenlabs.MAX_TTS_CHARS + 1)},
                    headers=user["headers"])
    assert r.status_code == 422


def test_tts_503_when_server_has_no_key(client, make_user, key_missing):
    user = make_user()
    r = client.post("/v1/voice/tts", json={"text": "hello"}, headers=user["headers"])
    assert r.status_code == 503
    assert r.get_json()["error"]["code"] == "voice_unavailable"


def test_tts_502_when_upstream_fails_and_never_leaks_key(client, make_user, key_configured, monkeypatch):
    user = make_user()

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)
    monkeypatch.setattr(elevenlabs.urllib.request, "urlopen", boom)

    r = client.post("/v1/voice/tts", json={"text": "hello"}, headers=user["headers"])
    assert r.status_code == 502
    assert r.get_json()["error"]["code"] == "voice_upstream_error"
    assert "test-key-not-real" not in r.get_data(as_text=True)


def test_tts_rate_limited_per_user(client, make_user, key_configured, monkeypatch):
    user = make_user()
    monkeypatch.setattr(routes, "TTS_DAILY_LIMIT", 2)
    monkeypatch.setattr(elevenlabs, "text_to_speech", lambda text: b"mp3")
    for _ in range(2):
        assert client.post("/v1/voice/tts", json={"text": "hi"}, headers=user["headers"]).status_code == 200
    r = client.post("/v1/voice/tts", json={"text": "hi"}, headers=user["headers"])
    assert r.status_code == 429
    assert r.get_json()["error"]["code"] == "rate_limited"
    # A different user is unaffected.
    other = make_user()
    assert client.post("/v1/voice/tts", json={"text": "hi"}, headers=other["headers"]).status_code == 200


def test_tts_validation_errors_do_not_use_up_the_limit(client, make_user, key_configured, monkeypatch):
    user = make_user()
    monkeypatch.setattr(routes, "TTS_DAILY_LIMIT", 1)
    monkeypatch.setattr(elevenlabs, "text_to_speech", lambda text: b"mp3")
    for _ in range(3):
        assert client.post("/v1/voice/tts", json={"text": ""}, headers=user["headers"]).status_code == 422
    assert client.post("/v1/voice/tts", json={"text": "hi"}, headers=user["headers"]).status_code == 200


# ── /stt ────────────────────────────────────────────────────────────────

def test_stt_requires_auth(client, key_configured):
    r = client.post("/v1/voice/stt", data=b"abc",
                    headers={"X-Device-Id": "pytest-noauth", "Content-Type": "audio/webm"})
    assert r.status_code == 401


def test_stt_returns_text_and_forwards_container(client, make_user, key_configured, monkeypatch):
    user = make_user()
    seen = {}

    def fake_stt(audio, filename, content_type):
        seen.update(audio=audio, filename=filename, content_type=content_type)
        return "I did twenty pushups"
    monkeypatch.setattr(elevenlabs, "speech_to_text", fake_stt)

    r = client.post("/v1/voice/stt", data=b"\x1aE\xdf\xa3-webm", headers=_audio_headers(user))
    assert r.status_code == 200
    assert r.get_json() == {"text": "I did twenty pushups"}
    assert seen["audio"] == b"\x1aE\xdf\xa3-webm"
    assert seen["filename"] == "speech.webm"
    assert seen["content_type"] == "audio/webm"  # ;codecs=opus stripped


@pytest.mark.parametrize("content_type,filename", [
    ("audio/mp4", "speech.mp4"), ("audio/ogg;codecs=opus", "speech.ogg"), ("audio/wav", "speech.wav"),
])
def test_stt_accepts_other_browser_containers(client, make_user, key_configured, monkeypatch,
                                              content_type, filename):
    user = make_user()
    seen = {}
    monkeypatch.setattr(elevenlabs, "speech_to_text",
                        lambda audio, fn, ct: seen.update(fn=fn) or "ok")
    r = client.post("/v1/voice/stt", data=b"abc", headers=_audio_headers(user, content_type))
    assert r.status_code == 200 and seen["fn"] == filename


@pytest.mark.parametrize("content_type", ["application/json", "text/plain", "video/mp4", ""])
def test_stt_rejects_non_audio_content_type(client, make_user, key_configured, content_type):
    user = make_user()
    r = client.post("/v1/voice/stt", data=b"abc", headers=_audio_headers(user, content_type))
    assert r.status_code == 422


def test_stt_rejects_empty_body(client, make_user, key_configured):
    user = make_user()
    r = client.post("/v1/voice/stt", data=b"", headers=_audio_headers(user))
    assert r.status_code == 422


def test_stt_rejects_clip_over_our_limit(client, make_user, key_configured):
    user = make_user()
    r = client.post("/v1/voice/stt", data=b"a" * (elevenlabs.MAX_AUDIO_BYTES + 1),
                    headers=_audio_headers(user))
    assert r.status_code == 422


def test_stt_503_when_server_has_no_key(client, make_user, key_missing):
    user = make_user()
    r = client.post("/v1/voice/stt", data=b"abc", headers=_audio_headers(user))
    assert r.status_code == 503
    assert r.get_json()["error"]["code"] == "voice_unavailable"


def test_stt_rate_limited_per_user(client, make_user, key_configured, monkeypatch):
    user = make_user()
    monkeypatch.setattr(routes, "STT_DAILY_LIMIT", 1)
    monkeypatch.setattr(elevenlabs, "speech_to_text", lambda a, f, c: "ok")
    assert client.post("/v1/voice/stt", data=b"abc", headers=_audio_headers(user)).status_code == 200
    assert client.post("/v1/voice/stt", data=b"abc", headers=_audio_headers(user)).status_code == 429


# ── elevenlabs module internals ─────────────────────────────────────────

def test_multipart_body_is_well_formed():
    body, header = elevenlabs._multipart({"model_id": "scribe_v1"}, "file", "speech.webm",
                                         "audio/webm", b"\x00\x01DATA")
    boundary = header.split("boundary=")[1]
    assert body.startswith(f"--{boundary}\r\n".encode())
    assert body.endswith(f"--{boundary}--\r\n".encode())
    assert b'name="model_id"\r\n\r\nscribe_v1\r\n' in body
    assert b'name="file"; filename="speech.webm"\r\nContent-Type: audio/webm\r\n\r\n\x00\x01DATA\r\n' in body


def test_speech_to_text_malformed_upstream_json_is_502(key_configured, monkeypatch):
    monkeypatch.setattr(elevenlabs, "_send", lambda req: b"<html>not json</html>")
    with pytest.raises(ApiError) as exc:
        elevenlabs.speech_to_text(b"abc", "speech.webm", "audio/webm")
    assert exc.value.status == 502


def test_speech_to_text_missing_text_field_is_empty_string(key_configured, monkeypatch):
    monkeypatch.setattr(elevenlabs, "_send", lambda req: b'{"language_code":"en"}')
    assert elevenlabs.speech_to_text(b"abc", "speech.webm", "audio/webm") == ""


def test_existing_extract_route_is_unchanged_by_the_new_routes(client, make_user, key_missing):
    """/extract must not depend on ELEVENLABS_API_KEY at all."""
    user = make_user()
    r = client.post("/v1/voice/extract", json={"transcript": ""}, headers=user["headers"])
    assert r.status_code != 503
