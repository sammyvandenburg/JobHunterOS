"""A small fake of Google's OAuth and Gmail endpoints, served for real on localhost.

Hunter talks to it exactly as it would talk to Google (redirects, token exchange with PKCE,
refresh, profile lookup, messages/send, revoke), so both the TestClient tests and the browser
journey exercise the real code path. Nothing here is reachable from outside this machine.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
from email import message_from_bytes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

CLIENT_ID = "test-client.apps.googleusercontent.com"
CLIENT_SECRET = "test-desktop-secret"


class FakeGoogle:
    def __init__(self):
        self.address = "student@gmail.example"
        self.grant_scope: str | None = None  # None: grant exactly what was asked
        self.refresh_ok = True
        self.send_status = 200
        self.auth_requests: list[dict] = []
        self.token_requests: list[dict] = []
        self.sent: list[dict] = []
        self.revoked: list[str] = []
        self._codes: dict[str, dict] = {}
        self._refresh: dict[str, str] = {}
        self.access_tokens: set[str] = set()
        self._n = 0
        self._lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _send(self, status: int, body: dict | None = None, location: str | None = None):
                self.send_response(status)
                if location:
                    self.send_header("Location", location)
                data = json.dumps(body or {}).encode()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _bearer_ok(self) -> bool:
                auth = self.headers.get("Authorization", "")
                return auth.startswith("Bearer ") and auth[7:] in fake.access_tokens

            def do_GET(self):
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                if u.path == "/o/oauth2/v2/auth":
                    fake.auth_requests.append(q)
                    if q.get("client_id") != CLIENT_ID or q.get("response_type") != "code":
                        return self._send(400, {"error": "invalid_client"})
                    code = fake._next("code")
                    fake._codes[code] = {"challenge": q.get("code_challenge"), "method": q.get("code_challenge_method"), "redirect_uri": q.get("redirect_uri"), "scope": q.get("scope", "")}
                    return self._send(302, {}, location=f"{q['redirect_uri']}?{urlencode({'code': code, 'state': q.get('state', '')})}")
                if u.path == "/gmail/v1/users/me/profile":
                    if not self._bearer_ok():
                        return self._send(401, {"error": "unauthorized"})
                    return self._send(200, {"emailAddress": fake.address})
                return self._send(404, {"error": "not found"})

            def do_POST(self):
                u = urlparse(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                ctype = self.headers.get("Content-Type", "")
                form = {k: v[0] for k, v in parse_qs(raw.decode()).items()} if "x-www-form-urlencoded" in ctype else {}
                if u.path == "/token":
                    return self._token(form)
                if u.path == "/revoke":
                    q = {k: v[0] for k, v in parse_qs(u.query).items()}
                    tok = q.get("token") or form.get("token")
                    fake.revoked.append(tok)
                    fake._refresh.pop(tok, None)
                    return self._send(200, {})
                if u.path == "/gmail/v1/users/me/messages/send":
                    if not self._bearer_ok():
                        return self._send(401, {"error": {"code": 401, "message": "Invalid Credentials"}})
                    if fake.send_status != 200:
                        return self._send(fake.send_status, {"error": {"code": fake.send_status, "message": "simulated"}})
                    body = json.loads(raw.decode() or "{}")
                    msg = message_from_bytes(base64.urlsafe_b64decode(body["raw"] + "=="))
                    text = ""
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            text = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8")
                            break
                    rec = {"to": msg["To"], "from": msg["From"], "subject": msg["Subject"], "text": text, "id": fake._next("m")}
                    fake.sent.append(rec)
                    return self._send(200, {"id": rec["id"], "labelIds": ["SENT"]})
                return self._send(404, {"error": "not found"})

            def _token(self, form: dict):
                grant = form.get("grant_type")
                entry = {"grant_type": grant, **{k: form.get(k) for k in ("client_id", "redirect_uri")}, "pkce_ok": None}
                fake.token_requests.append(entry)
                if form.get("client_id") != CLIENT_ID or form.get("client_secret") != CLIENT_SECRET:
                    return self._send(401, {"error": "invalid_client"})
                if grant == "authorization_code":
                    c = fake._codes.pop(form.get("code", ""), None)
                    if not c or c["redirect_uri"] != form.get("redirect_uri"):
                        return self._send(400, {"error": "invalid_grant"})
                    verifier = form.get("code_verifier", "")
                    expect = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
                    entry["pkce_ok"] = c["method"] == "S256" and expect == c["challenge"]
                    if not entry["pkce_ok"]:
                        return self._send(400, {"error": "invalid_grant", "error_description": "code_verifier mismatch"})
                    scope = fake.grant_scope if fake.grant_scope is not None else c["scope"]
                    at, rt = fake._next("at"), fake._next("rt")
                    fake.access_tokens.add(at)
                    fake._refresh[rt] = scope
                    return self._send(200, {"access_token": at, "refresh_token": rt, "expires_in": 3599, "token_type": "Bearer", "scope": scope})
                if grant == "refresh_token":
                    rt = form.get("refresh_token", "")
                    if not fake.refresh_ok or rt not in fake._refresh:
                        return self._send(400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
                    at = fake._next("at")
                    fake.access_tokens.add(at)
                    return self._send(200, {"access_token": at, "expires_in": 3599, "token_type": "Bearer", "scope": fake._refresh[rt]})
                return self._send(400, {"error": "unsupported_grant_type"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def _next(self, prefix: str) -> str:
        with self._lock:
            self._n += 1
            return f"{prefix}-{self._n}"

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def env(self) -> dict[str, str]:
        """Environment that points Hunter at this fake instead of Google."""
        return {
            "JOBHUNTER_GOOGLE_AUTH_URL": f"{self.base}/o/oauth2/v2/auth",
            "JOBHUNTER_GOOGLE_TOKEN_URL": f"{self.base}/token",
            "JOBHUNTER_GOOGLE_REVOKE_URL": f"{self.base}/revoke",
            "JOBHUNTER_GMAIL_API": f"{self.base}/gmail/v1/users/me",
            "JOBHUNTER_GOOGLE_CLIENT_ID": CLIENT_ID,
            "JOBHUNTER_GOOGLE_CLIENT_SECRET": CLIENT_SECRET,
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
