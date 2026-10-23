"""Slack alerts for server errors, with a plain-language "likely cause".

When the server hits an unexpected error (the same place that already writes an
AppIssue row), `notify_exception` hands a *scrubbed* summary of it to a background
thread, which decides whether it is worth announcing, asks an AI model to explain the
probable cause in plain words (falling back to a built-in rule if the model is down),
and posts one message to a Slack incoming webhook.

Design rules, each of which exists so this feature can never hurt the app:
  * The request path only builds a small dict and does a non-blocking queue put. All
    network work (AI model, Slack) happens in a daemon thread. Nothing here can raise
    into, slow down, or change the response of a user request.
  * Off by default: needs SLACK_ALERTS_ENABLED=true and SLACK_WEBHOOK_URL.
  * No alert floods: the same error (same type + code location + route) is announced
    once per ALERT_DEDUP_SECONDS with a count of repeats, and there is an hourly cap.
  * Nothing private leaves the server: emails, tokens, passwords, connection strings and
    SQL parameters are stripped; request bodies and headers are never read.
  * Every failure (Slack down, model down, Redis down) is swallowed and logged.

Manual check once the webhook is configured:  python -m app.platform.alerts --test
"""
from __future__ import annotations

import hashlib
import json
import linecache
import logging
import os
import queue
import re
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_QUEUE: "queue.Queue[dict]" = queue.Queue(maxsize=50)
_worker_lock = threading.Lock()
_worker_started = False
# Used only when Redis is unreachable, so a Redis outage (itself worth an alert) cannot
# turn into a flood of identical messages.
_local_seen: dict[str, float] = {}
_local_counts: dict[str, int] = {}
_local_hour = {"key": "", "n": 0}


# --------------------------------------------------------------------------- scrubbing
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*")
_URL_CREDS = re.compile(r"(://)[^/@\s:]+:[^/@\s]+@")
_BEARER = re.compile(r"(?i)\bbearer\s+\S+")
_KV_SECRET = re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization|bearer)\b(\s*[=:]\s*|\s+)([^\s,;&'\"]+)")
_LONG_SECRET = re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")


def scrub(text: str, limit: int = 300) -> str:
    """Remove anything that looks like a credential or a personal identifier."""
    s = str(text)
    s = _URL_CREDS.sub(r"\1<credentials>@", s)
    s = _JWT.sub("<token>", s)
    s = _BEARER.sub("Bearer <hidden>", s)
    s = _EMAIL.sub("<email>", s)
    s = _KV_SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}<hidden>", s)
    s = _LONG_SECRET.sub("<secret>", s)
    s = re.sub(r"[\x00-\b\x0b-\x1f\x7f]", " ", s)
    return s[:limit]


def clean_message(exc: BaseException, limit: int = 300) -> str:
    """The exception text without SQL statements or their bound parameters (those carry
    user data), then scrubbed."""
    raw = str(exc) or type(exc).__name__
    raw = raw.split("\n[SQL:")[0].split("[SQL:")[0]
    return scrub(" ".join(raw.split()), limit)


def slack_escape(text: str, limit: int = 600) -> str:
    """Slack treats &, < and > as markup (<!channel> would ping everyone), so neutralise
    them, and keep backticks from breaking out of inline code."""
    s = str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("`", "'")
    return s[:limit]


# --------------------------------------------------------------------------- locating the error
def _app_frames(tb) -> list[tuple[str, int, str]]:
    """(relative path, line, function) for traceback frames that live in this project."""
    out = []
    for fs in traceback.extract_tb(tb) if tb is not None else []:
        fn = os.path.abspath(fs.filename)
        if fn.startswith(_ROOT) and "site-packages" not in fn and f"{os.sep}venv{os.sep}" not in fn:
            out.append((os.path.relpath(fn, _ROOT).replace(os.sep, "/"), fs.lineno, fs.name))
    return out


def fingerprint(exc: BaseException, route: str) -> str:
    """Same error type at the same place on the same route = same problem. Line numbers
    are left out on purpose so a small edit elsewhere in the file does not re-announce it."""
    frames = _app_frames(exc.__traceback__)
    where = f"{frames[-1][0]}:{frames[-1][2]}" if frames else "unknown"
    return hashlib.sha1(f"{type(exc).__name__}|{where}|{route}".encode(), usedforsecurity=False).hexdigest()[:16]


def _code_snippet(frames: list[tuple[str, int, str]], radius: int = 6) -> str:
    if not frames:
        return ""
    rel, lineno, _ = frames[-1]
    path = os.path.join(_ROOT, rel)
    lines = []
    for n in range(max(1, lineno - radius), lineno + radius + 1):
        text = linecache.getline(path, n).rstrip()
        if text or n == lineno:
            lines.append(f"{'>>' if n == lineno else '  '} {n}: {scrub(text, 160)}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- built-in cause rules
def rule_based_cause(exc: BaseException, status: int) -> dict:
    """A deliberately plain fallback so the alert is useful even if the AI model is down."""
    name = type(exc).__name__
    mod = type(exc).__module__ or ""
    msg = str(exc).lower()
    if "redis" in mod and name in ("ConnectionError", "TimeoutError", "BusyLoadingError"):
        c = ("Redis (used for rate limits, login sessions and caching) could not be reached.",
             "The Redis service is down, restarting, or its URL/credentials changed.",
             "Check the Redis instance on Render and the REDIS_*_URL settings.")
    elif name in ("IntegrityError", "UniqueViolation", "ForeignKeyViolation") or "duplicate key" in msg:
        c = ("A database rule was broken: the app tried to save a row that already exists or points to something missing.",
             "Most often two requests wrote the same record at the same moment (a race), e.g. a double-tapped button.",
             "Make the save safe to repeat (look up first, handle the 'already exists' case) and disable the button while it sends.")
    elif name in ("OperationalError", "InterfaceError", "DBAPIError") or "could not connect" in msg or "connection" in msg and "database" in msg:
        c = ("The database could not be reached or ran out of connections.",
             "The Postgres service may be sleeping, restarting, overloaded, or its connection limit was reached.",
             "Check the database service status and connection limits on Render.")
    elif "requests" in mod or "urllib" in mod or name in ("ConnectionError", "Timeout", "ReadTimeout", "ConnectTimeout", "URLError", "HTTPError"):
        c = ("A call to an outside service (AI model, voice, email) failed or timed out.",
             "That provider is slow or down, or the API key/quota is wrong.",
             "Check the provider's status page, your API key and remaining credits.")
    elif name in ("KeyError", "AttributeError", "TypeError", "IndexError", "ValueError", "ZeroDivisionError"):
        c = ("The code expected a value that was missing or the wrong type.",
             "A code path was not prepared for this input or this state of the data (a bug in the app's logic).",
             "Look at the code location below and add a check for the missing/invalid value.")
    elif status >= 500 and name == "ApiError" or "badgateway" in name.lower():
        c = ("An outside service the app depends on answered with an error.",
             "The provider rejected the request (bad or expired key, no credits) or is unavailable.",
             "Check the provider's key, quota and status page.")
    else:
        c = ("An unexpected error stopped this request.",
             "The error type is not one the built-in rules recognise.",
             "Open the code location below and the request ID in the server logs.")
    return {"probable_cause": c[0], "why": c[1], "suggested_fix": c[2], "confidence": "low", "source": "built-in rule"}


# --------------------------------------------------------------------------- AI explanation
_SYSTEM_PROMPT = (
    "You are a senior backend engineer on call for a Flask + PostgreSQL + Redis fitness app hosted on "
    "Render. Explain a production error to a non-expert founder. Reply with ONLY a JSON object with the keys "
    '"probable_cause", "why", "suggested_fix" and "confidence" (one of low, medium, high). Each text value is '
    "at most 50 words, plain language, no code blocks, no secrets. Base your answer on the exception, the "
    "code location and the code excerpt provided. If you are not sure, say so and use low confidence."
)


def _hour_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H")


def _cache():
    from app.platform import redis_clients as rc
    return rc.cache()


def parse_analysis(raw: str) -> dict | None:
    """Pull the JSON object out of a model reply and keep only short plain strings."""
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    out = {}
    for k in ("probable_cause", "why", "suggested_fix"):
        v = data.get(k)
        if not isinstance(v, str) or not v.strip():
            return None
        out[k] = " ".join(v.split())[:400]
    conf = str(data.get("confidence", "low")).lower()
    out["confidence"] = conf if conf in ("low", "medium", "high") else "low"
    out["source"] = "AI analysis"
    return out


def analyze_cause(cfg, event: dict) -> dict | None:
    """Ask the model for a plain-language cause. Cached per error for a day, capped per
    hour, and any failure just returns None (the caller falls back to the built-in rule)."""
    if not cfg.alert_cause_analysis_enabled or not getattr(cfg, "openrouter_api_key", ""):
        return None
    fp = event["fingerprint"]
    try:
        r = _cache()
        cached = r.get(f"alert:cause:{fp}")
        if cached:
            return json.loads(cached if isinstance(cached, str) else cached.decode())
        n = r.incr(f"alert:llm:{_hour_key()}")
        r.expire(f"alert:llm:{_hour_key()}", 7200)
        if n > 10:
            return None
    except Exception:
        r = None
    try:
        from app.rag.vectorstore import chat_complete
        user = json.dumps({
            "exception_type": event["exc_type"], "message": event["message"], "http_status": event["status"],
            "route": event["route"], "code_location": event["where"], "stack_frames_in_app": event["frames"][-8:],
            "code_excerpt": event["snippet"],
        }, ensure_ascii=False)
        raw = chat_complete(
            [{"role": "system", "content": _SYSTEM_PROMPT}, {"role": "user", "content": user}],
            cfg.openrouter_api_key, model=cfg.alert_cause_model, retries=1, temperature=0.1, max_tokens=500)
        result = parse_analysis(raw)
        if result and r is not None:
            try:
                r.set(f"alert:cause:{fp}", json.dumps(result), ex=86400)
            except Exception:
                pass
        return result
    except Exception:
        log.warning("alerts: cause analysis failed", exc_info=True)
        return None


# --------------------------------------------------------------------------- de-duplication
def should_send(cfg, fp: str) -> tuple[bool, int, bool]:
    """(send?, repeats suppressed since the last alert, is_cap_notice).

    Announce a given error once per window; count the repeats; and cap the messages per
    hour. Uses Redis so every server process agrees; falls back to per-process memory."""
    window = max(int(cfg.alert_dedup_seconds), 1)
    cap = max(int(cfg.alert_max_per_hour), 1)
    hour = _hour_key()
    try:
        r = _cache()
        if not r.set(f"alert:fp:{fp}", "1", nx=True, ex=window):
            r.incr(f"alert:cnt:{fp}"); r.expire(f"alert:cnt:{fp}", 86400)
            return False, 0, False
        raw = r.get(f"alert:cnt:{fp}")
        suppressed = int(raw) if raw else 0
        r.delete(f"alert:cnt:{fp}")
        n = r.incr(f"alert:hour:{hour}"); r.expire(f"alert:hour:{hour}", 7200)
    except Exception:
        now = time.time()
        last = _local_seen.get(fp)
        if last is not None and now - last < window:
            _local_counts[fp] = _local_counts.get(fp, 0) + 1
            return False, 0, False
        _local_seen[fp] = now
        suppressed = _local_counts.pop(fp, 0)
        if _local_hour["key"] != hour:
            _local_hour.update(key=hour, n=0)
        _local_hour["n"] += 1
        n = _local_hour["n"]
    if n > cap + 1:
        return False, suppressed, False
    return True, suppressed, n == cap + 1


# --------------------------------------------------------------------------- Slack
def _webhook_ok(cfg) -> bool:
    url = cfg.slack_webhook_url or ""
    if url.startswith("https://hooks.slack.com/"):
        return True
    # Local testing against a throw-away listener is fine, never in production.
    return (not cfg.is_production) and url.startswith(("http://127.0.0.1:", "http://localhost:"))


def build_payload(cfg, event: dict, cause: dict, suppressed: int = 0, cap_notice: bool = False, test: bool = False) -> dict:
    env = (cfg.env or "unknown")
    when = datetime.fromtimestamp(event["ts"], IST).strftime("%d %b %H:%M:%S IST")
    seen = "first time" if not suppressed else f"{suppressed} more since the last alert"
    title = f"{'TEST — ' if test else ''}Server error in {env}"
    f = lambda label, value: {"type": "mrkdwn", "text": f"*{label}*\n{value}"}
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"\U0001F6A8 {title}"[:150]}},
        {"type": "section", "fields": [
            f("Where", f"`{slack_escape(event['route'], 120)}`"),
            f("Status", str(event["status"])),
            f("Error type", f"`{slack_escape(event['exc_type'], 80)}`"),
            f("Request ID", f"`{slack_escape(event['request_id'] or 'n/a', 40)}`"),
            f("When", when),
            f("Seen", seen),
        ]},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Likely cause*\n{slack_escape(cause['probable_cause'], 500)}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Why it happens*\n{slack_escape(cause['why'], 500)}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Suggested fix*\n{slack_escape(cause['suggested_fix'], 500)}"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": slack_escape(
            f"Code: {event['where']} • confidence: {cause.get('confidence', 'low')} ({cause.get('source', 'rule')}) • "
            f"env: {env} • version: {cfg.app_version or 'unknown'}", 300)}]},
    ]
    if cap_notice:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": (
            "_Alert limit reached for this hour — further errors are still recorded but not posted here._")}})
    return {"text": slack_escape(f"{title}: {event['route']} ({event['exc_type']})", 200), "blocks": blocks}


def post_to_slack(cfg, payload: dict) -> bool:
    if not _webhook_ok(cfg):
        log.warning("alerts: SLACK_WEBHOOK_URL is missing or not a hooks.slack.com URL; not sending")
        return False
    try:
        resp = requests.post(cfg.slack_webhook_url, json=payload, timeout=5)
        if resp.status_code != 200:
            log.warning("alerts: Slack answered HTTP %s", resp.status_code)
            return False
        return True
    except Exception:
        log.warning("alerts: could not reach Slack", exc_info=True)
        return False


# --------------------------------------------------------------------------- pipeline
def _process(event: dict) -> None:
    cfg = event["cfg"]
    send, suppressed, cap_notice = should_send(cfg, event["fingerprint"])
    if not send:
        return
    cause = analyze_cause(cfg, event) or event["rule_cause"]
    post_to_slack(cfg, build_payload(cfg, event, cause, suppressed, cap_notice))


def _worker() -> None:
    while True:
        ev = _QUEUE.get()
        try:
            _process(ev)
        except Exception:
            log.exception("alerts: unexpected failure while processing an alert")


def _ensure_worker() -> None:
    global _worker_started
    if _worker_started:
        return
    with _worker_lock:
        if not _worker_started:
            threading.Thread(target=_worker, name="slack-alerts", daemon=True).start()
            _worker_started = True


def build_event(cfg, exc: BaseException, *, status: int, route: str, request_id: str | None) -> dict:
    frames = _app_frames(exc.__traceback__)
    where = f"{frames[-1][0]}:{frames[-1][1]} {frames[-1][2]}" if frames else "unknown location"
    return {
        "cfg": cfg, "ts": time.time(), "status": status, "route": route, "request_id": request_id,
        "exc_type": type(exc).__name__, "message": clean_message(exc), "where": where,
        "frames": [f"{p}:{n} {fn}" for p, n, fn in frames], "snippet": _code_snippet(frames),
        "fingerprint": fingerprint(exc, route), "rule_cause": rule_based_cause(exc, status),
    }


def notify_exception(exc: BaseException, *, status: int = 500) -> None:
    """Called from the server-error handler. Never raises, never blocks."""
    try:
        from flask import current_app, g, request
        cfg = current_app.config.get("APP_CONFIG")
        if cfg is None or not cfg.slack_alerts_enabled or not cfg.slack_webhook_url:
            return
        rule = request.url_rule.rule if request.url_rule is not None else "(unmatched path)"
        event = build_event(cfg, exc, status=status, route=f"{request.method} {rule}",
                            request_id=getattr(g, "request_id", None))
        _ensure_worker()
        _QUEUE.put_nowait(event)
    except queue.Full:
        log.warning("alerts: queue full, dropping an alert")
    except Exception:
        log.warning("alerts: could not queue an alert", exc_info=True)


# --------------------------------------------------------------------------- manual check
def _send_test(use_ai: bool = True) -> int:
    from app.platform.config import load_config
    cfg = load_config()
    if not cfg.slack_webhook_url:
        print("SLACK_WEBHOOK_URL is not set.")
        return 2
    try:                        # a realistic sample error, raised and caught so it carries a traceback
        from sqlalchemy.exc import IntegrityError
        raise IntegrityError("INSERT INTO user_profiles ...", {}, Exception(
            'duplicate key value violates unique constraint "user_profiles_pkey"'))
    except Exception as e:
        raise_it = e
    event = build_event(cfg, raise_it, status=500, route="POST /v1/profile/onboarding (sample)", request_id="req-sample000000")
    cause = (analyze_cause(cfg, {**event, "fingerprint": "test-" + event["fingerprint"]}) if use_ai else None) or event["rule_cause"]
    ok = post_to_slack(cfg, build_payload(cfg, event, cause, test=True))
    print("Test alert sent." if ok else "Test alert FAILED - see the log lines above.")
    return 0 if ok else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    if "--test" in sys.argv:
        sys.exit(_send_test(use_ai="--no-ai" not in sys.argv))
    print(__doc__)
