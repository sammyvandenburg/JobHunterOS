"""Dashboard security: loopback-only host check, origin check, CSRF tokens,
signed session cookie, security headers. No arbitrary filesystem access is
exposed; uploads go through the documents service with size and type limits.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from urllib.parse import urlparse

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse, Response

ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
SESSION_COOKIE = "jh_session"
STATE_CHANGING = {"POST", "PUT", "PATCH", "DELETE"}


def _host_only(value: str | None) -> str:
    if not value:
        return ""
    v = value.strip().lower()
    if v.startswith("["):
        return v.split("]")[0] + "]"
    return v.split(":")[0]


class LocalOnlyMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, secret: bytes, extra_hosts: set[str] | None = None):
        super().__init__(app)
        self.secret = secret
        self.allowed = ALLOWED_HOSTS | (extra_hosts or set())

    async def dispatch(self, request: Request, call_next) -> Response:
        host = _host_only(request.headers.get("host"))
        if host not in self.allowed:
            return PlainTextResponse("This dashboard only answers to localhost.", status_code=421)
        if request.method in STATE_CHANGING:
            origin = request.headers.get("origin") or request.headers.get("referer")
            if origin:
                o_host = _host_only(urlparse(origin).netloc)
                if o_host not in self.allowed:
                    return PlainTextResponse("Cross-site request blocked.", status_code=403)
            else:
                # Browsers always send Origin on cross-site form posts; tools like curl may omit it.
                if not request.headers.get("x-jobhunter-local"):
                    return PlainTextResponse("Missing origin.", status_code=403)
        sid = request.cookies.get(SESSION_COOKIE)
        new_cookie = False
        if not sid or len(sid) < 32:
            sid = secrets.token_hex(16)
            new_cookie = True
        request.state.session_id = sid
        request.state.csrf = csrf_token(self.secret, sid)
        if request.method in STATE_CHANGING and request.url.path.startswith("/do/"):
            # CSRF is verified in the route via form field; nothing to do here.
            pass
        response = await call_next(request)
        if new_cookie:
            response.set_cookie(SESSION_COOKIE, sid, httponly=True, samesite="strict", secure=False, path="/")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")  # no-referrer would make browsers send Origin: null on same-origin POSTs
        response.headers.setdefault("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        response.headers.setdefault("Cache-Control", "no-store")
        return response


def csrf_token(secret: bytes, session_id: str) -> str:
    return hmac.new(secret, session_id.encode(), hashlib.sha256).hexdigest()


def check_csrf(request: Request, token: str | None) -> bool:
    expected = getattr(request.state, "csrf", None)
    return bool(expected and token and hmac.compare_digest(expected, token))
