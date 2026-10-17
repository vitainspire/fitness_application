"""Server-side proxy to ElevenLabs text-to-speech / speech-to-text.

Exists so the ElevenLabs API key lives only in the server's environment
(ELEVENLABS_API_KEY). It used to be read in the browser from a VITE_ variable,
which Vite compiles into the public JavaScript bundle — anyone could copy it
from the shipped site and spend the account's credits.

Uses urllib (not `requests`, which requirements.txt lists as dev/test only) so
this adds no runtime dependency. Error messages deliberately never include the
upstream response body or any header — nothing from ElevenLabs's reply, and
certainly not the key, is ever echoed back to a client.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
import uuid

from app.platform.errors import ApiError

log = logging.getLogger(__name__)

TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=mp3_44100_128"
STT_URL = "https://api.elevenlabs.io/v1/speech-to-text"
VITA_VOICE_ID = "EXAVITQu4vr4xnSDxMaL"
TTS_MODEL = "eleven_multilingual_v2"
STT_MODEL = "scribe_v1"

# Vita's longest spoken line (a voice-logging reply) is ~200 characters.
MAX_TTS_CHARS = 600
# Under the app-wide 1 MiB request-body cap (app/factory.py) with headroom for
# the request's own overhead. A MediaRecorder clip (opus/aac) is a few KB per
# second, so this is minutes of speech — far more than a workout report.
MAX_AUDIO_BYTES = 900 * 1024
UPSTREAM_TIMEOUT_SECONDS = 25

# Browsers record different containers (Chrome/Firefox: webm or ogg, Safari: mp4).
# Anything else is refused rather than forwarded to a paid API.
_AUDIO_TYPES = {
    "audio/webm": "speech.webm",
    "audio/ogg": "speech.ogg",
    "audio/mp4": "speech.mp4",
    "audio/mpeg": "speech.mp3",
    "audio/wav": "speech.wav",
    "audio/x-wav": "speech.wav",
}


class VoiceUnavailable(ApiError):
    """Server has no ElevenLabs key configured. The frontend treats any failure
    here as "use the free browser voice instead", so this is not user-facing."""
    status, code = 503, "voice_unavailable"


def _api_key() -> str:
    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not key:
        raise VoiceUnavailable("Premium voice is not configured on the server.")
    return key


def is_configured() -> bool:
    return bool(os.environ.get("ELEVENLABS_API_KEY", "").strip())


def audio_filename_for(content_type: str) -> str | None:
    """Maps a request Content-Type (parameters like ;codecs=opus ignored) to a
    filename ElevenLabs can sniff the format from, or None if unsupported."""
    base = (content_type or "").split(";")[0].strip().lower()
    return _AUDIO_TYPES.get(base)


def _send(req: urllib.request.Request) -> bytes:
    try:
        # req's URL is one of the two hardcoded https://api.elevenlabs.io/
        # constants above (voice id is a constant too) — nothing user-controlled
        # reaches the URL, so a scanner's urlopen scheme warning is a false positive.
        with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT_SECONDS) as resp:  # nosec B310
            return resp.read()
    except urllib.error.HTTPError as e:
        log.warning("ElevenLabs request failed: HTTP %s", e.code)
        raise ApiError("Premium voice is temporarily unavailable.",
                       code="voice_upstream_error", status=502) from e
    except (TimeoutError, urllib.error.URLError) as e:
        log.warning("ElevenLabs unreachable: %s", type(e).__name__)
        raise ApiError("Premium voice is temporarily unavailable.",
                       code="voice_upstream_error", status=502) from e


def text_to_speech(text: str) -> bytes:
    """Returns MP3 bytes for `text` in Vita's voice."""
    key = _api_key()
    body = json.dumps({
        "text": text,
        "model_id": TTS_MODEL,
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }).encode()
    req = urllib.request.Request(
        TTS_URL.format(voice_id=VITA_VOICE_ID),
        data=body,
        headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"},
        method="POST",
    )
    audio = _send(req)
    if not audio:
        raise ApiError("Premium voice is temporarily unavailable.",
                       code="voice_upstream_error", status=502)
    return audio


def _multipart(fields: dict, file_field: str, filename: str, content_type: str,
               data: bytes) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    chunks = []
    for name, value in fields.items():
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    chunks.append(
        (f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
         f'filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n').encode()
        + data + b"\r\n"
    )
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def speech_to_text(audio: bytes, filename: str, content_type: str) -> str:
    """Returns the transcript of `audio` (possibly empty for silence)."""
    key = _api_key()
    body, header = _multipart({"model_id": STT_MODEL}, "file", filename, content_type, audio)
    req = urllib.request.Request(
        STT_URL, data=body,
        headers={"xi-api-key": key, "Content-Type": header, "Accept": "application/json"},
        method="POST",
    )
    raw = _send(req)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ApiError("Premium voice is temporarily unavailable.",
                       code="voice_upstream_error", status=502) from e
    text = data.get("text") if isinstance(data, dict) else None
    return text if isinstance(text, str) else ""
