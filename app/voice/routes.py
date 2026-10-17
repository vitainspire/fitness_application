from flask import Blueprint, Response, current_app, g, jsonify, request

from app.gateway.middleware import _incr_window, require_auth
from app.platform.db import session_scope
from app.platform.errors import ValidationError
from app.voice import elevenlabs, service

bp = Blueprint("voice", __name__, url_prefix="/v1/voice")


def _body() -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValidationError("Expected a JSON object body.")
    return data


def _str_field(d: dict, key: str, default: str = "") -> str:
    value = d.get(key, default)
    if value is None:
        value = default
    if not isinstance(value, str):
        raise ValidationError(f"{key!r} must be a string.")
    return value.strip()


def _voice_config():
    cfg = current_app.config["APP_CONFIG"]
    if not cfg.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not configured; voice logging cannot run yet.")
    return cfg


@bp.post("/extract")
@require_auth
def extract():
    d = _body()
    transcript = _str_field(d, "transcript")
    cfg = _voice_config()
    with session_scope() as s:
        return jsonify(service.extract_activities(
            s, g.user_id, transcript, openrouter_api_key=cfg.openrouter_api_key,
        ))


# Per-user rolling 24h caps on the ElevenLabs proxy routes. These calls spend
# real money on a third-party account, so — unlike /extract, which is already
# capped by its own daily quota — they get their own ceiling. Kept in the
# durable Redis rate-limit store (not the rehab_quota table) because that
# table's quota_type column has a CHECK constraint, and adding a value would
# need a production migration for no benefit. Each voice-log attempt uses ~1
# STT and ~2 TTS calls (greeting + reply), so these allow well over the 60
# daily /extract attempts.
TTS_DAILY_LIMIT = 200
STT_DAILY_LIMIT = 100
_DAY_SECONDS = 24 * 3600


@bp.post("/tts")
@require_auth
def tts():
    d = _body()
    text = _str_field(d, "text")
    if not text:
        raise ValidationError("'text' is required.")
    if len(text) > elevenlabs.MAX_TTS_CHARS:
        raise ValidationError(f"'text' must be at most {elevenlabs.MAX_TTS_CHARS} characters.")
    if not elevenlabs.is_configured():
        raise elevenlabs.VoiceUnavailable("Premium voice is not configured on the server.")
    _incr_window(f"rl:voice_tts:{g.user_id}", TTS_DAILY_LIMIT, _DAY_SECONDS)
    return Response(elevenlabs.text_to_speech(text), mimetype="audio/mpeg",
                    headers={"Cache-Control": "no-store"})


@bp.post("/stt")
@require_auth
def stt():
    """Body is the raw recorded audio (not JSON); Content-Type says which
    container it is. Returns {"text": "..."}."""
    filename = elevenlabs.audio_filename_for(request.headers.get("Content-Type", ""))
    if filename is None:
        raise ValidationError("Unsupported audio type.")
    audio = request.get_data(cache=False)
    if not audio:
        raise ValidationError("Audio body is required.")
    if len(audio) > elevenlabs.MAX_AUDIO_BYTES:
        raise ValidationError("Audio clip is too long.")
    if not elevenlabs.is_configured():
        raise elevenlabs.VoiceUnavailable("Premium voice is not configured on the server.")
    _incr_window(f"rl:voice_stt:{g.user_id}", STT_DAILY_LIMIT, _DAY_SECONDS)
    content_type = request.headers.get("Content-Type", "").split(";")[0].strip().lower()
    return jsonify({"text": elevenlabs.speech_to_text(audio, filename, content_type)})
