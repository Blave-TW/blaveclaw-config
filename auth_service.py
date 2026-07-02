#!/usr/bin/env python3
import hashlib
import hmac
import os
import platform
import secrets
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

OPENCLAW_HOME = os.environ.get("OPENCLAW_HOME") or (
    r"C:\openclaw" if platform.system() == "Windows" else "/root/.openclaw"
)
GATEWAY_SECRET = open(os.path.join(OPENCLAW_HOME, "gateway_secret")).read().strip()
SESSION_MAX_AGE = 28800  # 8 hours
valid_sessions = set()


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
            if session and session in valid_sessions:
                self.send_response(200)
                self.end_headers()
            else:
                self.send_response(401)
                self.end_headers()
            return

        redirect = params.get("redirect", [""])[0] or "/terminal/"
        if session and session in valid_sessions:
            self.send_response(302)
            self.send_header("Location", redirect)
            self.end_headers()
            return

        token = params.get("token", [""])[0]
        if token and _verify_token(token):
            sid = secrets.token_urlsafe(32)
            valid_sessions.add(sid)
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
