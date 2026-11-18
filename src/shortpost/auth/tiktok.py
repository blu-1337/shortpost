"""TikTok OAuth v2 with PKCE (CLAUDE.md 5.4, 13 step 6). Redirect and PKCE details are VERIFY items."""

from __future__ import annotations

import base64
import hashlib
import secrets as pysecrets
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

from .. import tokens
from ..http import Http

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
SCOPES = "user.info.basic,video.upload,video.publish"
REDIRECT = "http://localhost:8766/callback/"
FORM = {"Content-Type": "application/x-www-form-urlencoded"}
# Public ShortPost relay (D-021): used when you have no own tiktok_client_secret. It only adds the app's client secret to
# the token request and forwards it to TikTok; it stores nothing. Override with the secret `tiktok_relay_url`.
RELAY_URL = "https://shortpost-relay.shortpost.workers.dev"


def make_pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(pysecrets.token_bytes(48)).decode().rstrip("=")
    # the TikTok docs use the hex-encoded SHA256 (not base64url) as code_challenge (VERIFY live)
    return verifier, hashlib.sha256(verifier.encode()).hexdigest()


def auth_url(client_key: str, state: str, challenge: str, redirect: str = REDIRECT) -> str:
    return AUTH_URL + "?" + urlencode({"client_key": client_key, "scope": SCOPES, "response_type": "code",
                                       "redirect_uri": redirect, "state": state,
                                       "code_challenge": challenge, "code_challenge_method": "S256"})


def _store(rec: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    out = {"access_token": rec["access_token"], "refresh_token": rec["refresh_token"],
           "open_id": rec.get("open_id", ""), "scope": rec.get("scope", ""),
           "expires_at": (now + timedelta(seconds=int(rec.get("expires_in", 86400)))).isoformat(timespec="seconds"),
           "refresh_expires_at": (now + timedelta(seconds=int(rec.get("refresh_expires_in", 365 * 86400)))).isoformat(timespec="seconds")}
    tokens.save("tiktok", out)
    return out


def _check(rec: dict, what: str) -> None:
    if "access_token" not in rec:
        raise ValueError(f"tiktok {what} failed: {rec.get('error_description') or rec.get('error') or 'no token'}")


def relay_url(secrets: dict) -> str | None:
    """The relay is used only when no own client secret is configured."""
    if secrets.get("tiktok_client_secret"):
        return None
    return secrets.get("tiktok_relay_url") or RELAY_URL


def client_key(http: Http, secrets: dict) -> str:
    if secrets.get("tiktok_client_key"):
        return secrets["tiktok_client_key"]
    url = relay_url(secrets)
    key = (http.get(url) or {}).get("client_key") if url else None
    if not key:
        raise ValueError("no tiktok_client_key and the relay did not return one")
    return key


def exchange(http: Http, secrets: dict, code: str, verifier: str, redirect: str = REDIRECT) -> dict:
    relay = relay_url(secrets)
    if relay:
        rec = http.post(relay, json={"grant_type": "authorization_code", "code": code,
                                     "code_verifier": verifier, "redirect_uri": redirect})
        _check(rec, "token exchange")
        return _store(rec)
    rec = http.post(TOKEN_URL, headers=FORM, data={
        "client_key": secrets["tiktok_client_key"], "client_secret": secrets["tiktok_client_secret"],
        "code": code, "grant_type": "authorization_code", "redirect_uri": redirect, "code_verifier": verifier})
    _check(rec, "token exchange")
    return _store(rec)


def refresh(http: Http, secrets: dict, tok: dict) -> dict:
    relay = relay_url(secrets)
    if relay:
        rec = http.post(relay, json={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
        _check(rec, "refresh")
        rec.setdefault("refresh_token", tok["refresh_token"])
        return _store(rec)
    rec = http.post(TOKEN_URL, headers=FORM, data={
        "client_key": secrets["tiktok_client_key"], "client_secret": secrets["tiktok_client_secret"],
        "grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
    _check(rec, "refresh")
    rec.setdefault("refresh_token", tok["refresh_token"])
    return _store(rec)


def authorize(http: Http, secrets: dict, open_browser=None, timeout: int = 300) -> dict:
    """Interactive: print the URL, wait for the redirect on localhost:8766, exchange the code."""
    if secrets.get("tiktok_client_secret") and not secrets.get("tiktok_client_key"):
        raise ValueError("missing secret tiktok_client_key: run `shortpost set-secret tiktok_client_key`")
    key = client_key(http, secrets)
    verifier, challenge = make_pkce()
    state = pysecrets.token_urlsafe(16)
    got: dict = {}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            got.update({k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ShortPost: TikTok login received. You can close this tab.")

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 8766), H)
    srv.timeout = timeout
    url = auth_url(key, state, challenge)
    print("Open this URL, log in to TikTok and allow access:\n" + url)
    if open_browser:
        open_browser(url)
    t = threading.Thread(target=srv.handle_request)
    t.start()
    t.join(timeout)
    srv.server_close()
    if got.get("state") != state or "code" not in got:
        raise ValueError(f"no valid callback received ({got.get('error_description') or got.get('error') or 'timeout'})")
    return exchange(http, secrets, got["code"], verifier)
