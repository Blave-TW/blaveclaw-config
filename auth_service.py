#!/usr/bin/env python3
import hashlib
import hmac
import os
import platform
import secrets
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# BLAVECLAW_HOME, not OPENCLAW_HOME — the openclaw product itself reads
# OPENCLAW_HOME as a home-directory override, so reusing that name breaks it.
BLAVECLAW_HOME = os.environ.get("BLAVECLAW_HOME") or (
    r"C:\openclaw" if platform.system() == "Windows" else "/root/.openclaw"
)
GATEWAY_SECRET = open(os.path.join(BLAVECLAW_HOME, "gateway_secret")).read().strip()
SESSION_MAX_AGE = 28800  # 8 hours
valid_sessions = {}  # sid -> expire_ts (epoch seconds)


def _sanitize_redirect(raw):
    # Whitelist: only relative paths under /terminal/, no CRLF/scheme injection.
    if raw.startswith("/terminal/") and "\r" not in raw and "\n" not in raw and "://" not in raw:
        return raw
    return "/terminal/"


def _is_valid_session(sid):
    if not sid or sid not in valid_sessions:
        return False
    if valid_sessions[sid] < time.time():
        del valid_sessions[sid]
        return False
    return True


def _prune_expired_sessions():
    now = time.time()
    for sid in [s for s, exp in valid_sessions.items() if exp < now]:
        del valid_sessions[sid]


def _verify_token(token):
    try:
        payload, sig = token.rsplit(".", 1)
        _, expire_ts = payload.split(":", 1)
        if int(expire_ts) < int(time.time()):
            return False
        expected = hmac.new(
            GATEWAY_SECRET.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, sig)
    except Exception:
        return False


def _get_session_cookie(cookie_header):
    for part in cookie_header.split(";"):
        part = part.strip()
        if part.startswith("ide_session="):
            return part[len("ide_session="):]
    return None


class AuthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        session = _get_session_cookie(self.headers.get("Cookie", ""))

        if parsed.path == "/validate":
            if _is_valid_session(session):
                self.send_response(200)
                self.end_headers()
            else:
                self.send_response(401)
                self.end_headers()
            return

        redirect = _sanitize_redirect(params.get("redirect", [""])[0] or "/terminal/")
        if _is_valid_session(session):
            self.send_response(302)
            self.send_header("Location", redirect)
            self.end_headers()
            return

        token = params.get("token", [""])[0]
        if token and _verify_token(token):
            _prune_expired_sessions()
            sid = secrets.token_urlsafe(32)
            valid_sessions[sid] = time.time() + SESSION_MAX_AGE
            self.send_response(302)
            self.send_header("Location", redirect)
            self.send_header(
                "Set-Cookie",
                f"ide_session={sid}; Max-Age={SESSION_MAX_AGE}; HttpOnly; Path=/"
            )
            self.end_headers()
            return

        self.send_response(302)
        self.send_header("Location", "https://blave.org/blaveclaw")
        self.end_headers()

    def log_message(self, *args):
        pass


HTTPServer(("127.0.0.1", 8081), AuthHandler).serve_forever()
