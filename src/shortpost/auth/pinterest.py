"""Pinterest OAuth (API v5, CLAUDE.md 5.7, 13 step 8): authorization code on a localhost callback, refresh, board pick."""

from __future__ import annotations

import secrets as pysecrets
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

from .. import tokens
from ..http import ApiError, Http

AUTH_URL = "https://www.pinterest.com/oauth/"
API = "https://api.pinterest.com/v5"
SCOPES = "boards:read,boards:write,pins:read,pins:write,user_accounts:read"
PORT = 8767
REDIRECT = f"http://localhost:{PORT}/callback/"


def _basic(secrets: dict) -> tuple[str, str]:
    for k in ("pinterest_app_id", "pinterest_app_secret"):
        if not secrets.get(k):
            raise ValueError(f"missing secret {k}: run `shortpost set-secret {k}`")
    return secrets["pinterest_app_id"], secrets["pinterest_app_secret"]


def auth_url(app_id: str, state: str, redirect: str = REDIRECT) -> str:
    return AUTH_URL + "?" + urlencode({"client_id": app_id, "redirect_uri": redirect, "response_type": "code",
                                       "scope": SCOPES, "state": state})


def _store(rec: dict, old: dict | None = None, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    if "access_token" not in rec:
        raise ValueError(f"pinterest token request failed: {rec.get('message') or rec}")
    out = {**(old or {}), "access_token": rec["access_token"],
           "refresh_token": rec.get("refresh_token") or (old or {}).get("refresh_token", ""),
           "expires_at": (now + timedelta(seconds=int(rec.get("expires_in", 2592000)))).isoformat(timespec="seconds"),
           "refresh_expires_at": (now + timedelta(seconds=int(rec.get("refresh_token_expires_in", 365 * 86400)))).isoformat(timespec="seconds"),
           "scope": rec.get("scope", (old or {}).get("scope", ""))}
    tokens.save("pinterest", out)
    return out


def exchange(http: Http, secrets: dict, code: str, redirect: str = REDIRECT) -> dict:
    rec = http.post(f"{API}/oauth/token", auth=_basic(secrets),
                    data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect})
    return _store(rec)


def refresh(http: Http, secrets: dict, tok: dict) -> dict:
    rec = http.post(f"{API}/oauth/token", auth=_basic(secrets),
                    data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
    return _store(rec, tok)


def pick_board(http: Http, tok: dict, wanted: str, choose=None) -> dict:
    """Find the configured board by name; else ask `choose(boards)`; else create it. Stores board_id in the token file."""
    h = {"Authorization": f"Bearer {tok['access_token']}"}
    boards = http.get(f"{API}/boards", headers=h, params={"page_size": 100}).get("items", [])
    board = next((b for b in boards if b.get("name", "").lower() == wanted.lower()), None)
    if board is None and boards and choose:
        board = choose(boards)
    if board is None:
        board = http.post(f"{API}/boards", headers=h, json={"name": wanted, "privacy": "PUBLIC"})
    if "id" not in board:
        raise ApiError("pinterest: could not find or create the board")
    tokens.save("pinterest", {**tok, "board_id": board["id"], "board_name": board.get("name", wanted)})
    return board


def authorize(http: Http, secrets: dict, open_browser=None, timeout: int = 300) -> dict:
    app_id, _ = _basic(secrets)
    state, got = pysecrets.token_urlsafe(16), {}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            got.update({k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ShortPost: Pinterest login received. You can close this tab.")

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", PORT), H)
    srv.timeout = timeout
    url = auth_url(app_id, state)
    print("Open this URL, log in to Pinterest and allow access:\n" + url)
    if open_browser:
        open_browser(url)
    t = threading.Thread(target=srv.handle_request)
    t.start()
    t.join(timeout)
    srv.server_close()
    if got.get("state") != state or "code" not in got:
        raise ValueError(f"no valid callback received ({got.get('error') or 'timeout'})")
    return exchange(http, secrets, got["code"])
