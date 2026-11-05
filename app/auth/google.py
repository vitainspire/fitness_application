"""Server-side check of a Google Sign-In ID token.

The browser hands us a signed JWT ("credential"). We never trust an email the browser merely CLAIMS: the
signature is checked against Google's public keys, and the audience must be OUR client id, so a token issued
to some other app can't be replayed here.
"""
from __future__ import annotations

import logging
import threading
import time

from google.auth.transport import requests as g_requests
from google.oauth2 import id_token

from app.platform.errors import Unauthorized

log = logging.getLogger(__name__)

_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}
MAX_CREDENTIAL_LENGTH = 4096


class _CachedRequest(g_requests.Request):
    """Google's signing keys change rarely; caching them for an hour saves a network round trip per sign-in."""

    _cache: dict = {}
    _lock = threading.Lock()
    TTL = 3600

    def __call__(self, url, method="GET", body=None, headers=None, timeout=10, **kwargs):
        if method == "GET":
            with self._lock:
                hit = self._cache.get(url)
                if hit and time.time() - hit[0] < self.TTL:
                    return hit[1]
        resp = super().__call__(url, method=method, body=body, headers=headers, timeout=timeout, **kwargs)
        if method == "GET" and resp.status == 200:
            with self._lock:
                self._cache[url] = (time.time(), resp)
        return resp


def verify_google_id_token(credential: str, client_id: str) -> dict:
    """Returns {"sub", "email", "email_verified", "name"} or raises Unauthorized."""
    if not credential or len(credential) > MAX_CREDENTIAL_LENGTH:
        raise Unauthorized("Google sign-in failed. Please try again.")
    try:
        info = id_token.verify_oauth2_token(credential, _CachedRequest(), client_id)
    except Exception as exc:  # noqa: BLE001 - bad signature, wrong audience, expired, keys unreachable...
        log.warning("Google ID token rejected: %s", type(exc).__name__)  # never log the token itself
        raise Unauthorized("Google sign-in failed. Please try again.") from exc
    if info.get("iss") not in _ISSUERS or not info.get("sub") or not info.get("email"):
        raise Unauthorized("Google sign-in failed. Please try again.")
    verified = info.get("email_verified")
    return {
        "sub": str(info["sub"]),
        "email": str(info["email"]),
        "email_verified": verified is True or str(verified).lower() == "true",
        "name": info.get("name"),
    }
