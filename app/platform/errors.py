"""Consistent error shape across every endpoint.

REQUIREMENTS → API Contract → Conventions:
    { "error": { "code": "...", "message": "..." } }
429 carries Retry-After; 401 means re-authenticate; 403 means revoked or forbidden.
"""
from __future__ import annotations

import redis.exceptions
from flask import Flask, g, jsonify, request
from werkzeug.exceptions import HTTPException


class ApiError(Exception):
    status = 400
    code = "bad_request"

    def __init__(self, message: str, *, code: str | None = None,
                 status: int | None = None, retry_after: int | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status:
            self.status = status
        self.retry_after = retry_after


class ValidationError(ApiError):
    status, code = 422, "validation_failed"


class Unauthorized(ApiError):
    """401 — credentials absent, malformed, or expired. Re-authenticate."""
    status, code = 401, "unauthorized"


class Revoked(ApiError):
    """403 — the token was explicitly revoked, or the session was killed."""
    status, code = 403, "revoked"


class Forbidden(ApiError):
    status, code = 403, "forbidden"


class NotFound(ApiError):
    status, code = 404, "not_found"


class Conflict(ApiError):
    status, code = 409, "conflict"


class RateLimited(ApiError):
    status, code = 429, "rate_limited"


class QuotaExhausted(ApiError):
    """A user-facing exhausted state — never a silent failure (5.5)."""
    status, code = 429, "quota_exhausted"


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(ApiError)
    def _handle_api_error(exc: ApiError):
        body = {"error": {"code": exc.code, "message": exc.message}}
        response = jsonify(body)
        response.status_code = exc.status
        if exc.status >= 500:
            # e.g. a 502 from the voice proxy when the provider rejects our key: not a
            # crash, but still something a person should hear about.
            try:
                from app.platform import alerts
                alerts.notify_exception(exc, status=exc.status)
            except Exception:
                pass
        if exc.retry_after is not None:
            response.headers["Retry-After"] = str(exc.retry_after)
        return response

    @app.errorhandler(404)
    def _handle_404(_):
        return jsonify({"error": {"code": "not_found", "message": "No such endpoint"}}), 404

    @app.errorhandler(HTTPException)
    def _handle_http_exception(exc: HTTPException):
        # A routing/protocol-level error (wrong HTTP method, malformed body,
        # oversized payload, ...) is normal client behaviour, not a server
        # bug — @app.errorhandler(Exception) below would otherwise catch
        # these too (HTTPException is itself an Exception subclass), return
        # the wrong status code, and durably log them as "server_error"
        # issues, which is exactly what AppIssue's own docstring says must
        # never happen for expected/non-bug errors. Flask resolves the more
        # specific @app.errorhandler(404) above ahead of this for plain 404s.
        code = exc.name.lower().replace(" ", "_")
        return jsonify({"error": {"code": code, "message": exc.description}}), exc.code

    @app.errorhandler(Exception)
    def _handle_unexpected(exc: Exception):
        app.logger.exception("Unhandled exception", exc_info=exc)
        # 10.1/10.2 — write it down somewhere durable, not just this log
        # line. Failure to record must never hide the real error response
        # from the client, so this is its own best-effort try/except.
        #
        # Found live (code audit): a Redis outage means every single
        # request that touches the rate limiter or token revocation check
        # lands here — and each one used to also attempt a Postgres write.
        # That turns one infrastructure blip into a write storm against the
        # database at exactly the moment things are already degraded, and
        # each row is identical noise ("Redis down") rather than a distinct
        # bug worth its own issue record. Log-only for this one class.
        # Deliberately the connectivity subset, NOT the RedisError base
        # class: that base also covers ResponseError/DataError, which are
        # genuine application bugs (a WRONGTYPE, a malformed command) and
        # must keep landing in the issue log where someone will see them.
        # Only "Redis is unreachable right now" is filtered out here.
        if isinstance(exc, (redis.exceptions.ConnectionError,
                            redis.exceptions.TimeoutError,
                            redis.exceptions.BusyLoadingError)):
            app.logger.error("Redis unavailable (not recorded as an AppIssue): %r", exc)
        else:
            try:
                from app.platform.db import session_scope
                from app.platform.models import record_issue
                with session_scope() as session:
                    record_issue(
                        session, issue_type="server_error", message=repr(exc),
                        path=request.path, user_id=getattr(g, "user_id", None),
                        request_id=getattr(g, "request_id", None),
                    )
            except Exception:
                app.logger.exception("Failed to record the above exception as an issue")
        # Tell Slack (off unless configured). notify_exception only queues a small
        # summary for a background thread and never raises, so it cannot change the
        # response below; the extra try/except guards even the import.
        try:
            from app.platform import alerts
            alerts.notify_exception(exc, status=500)
        except Exception:
            app.logger.warning("alerts hook failed", exc_info=True)
        # Never leak internals to a mobile client.
        return jsonify({
            "error": {"code": "internal_error", "message": "Something went wrong"}
        }), 500
