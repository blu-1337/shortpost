"""Threads token setup (CLAUDE.md 13 step 5): paste a token from the app dashboard, make it long-lived, store it."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .. import tokens
from ..http import ApiError, Http

BASE = "https://graph.threads.net"


def authorize(http: Http, secrets: dict, token: str, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    secret = secrets.get("threads_app_secret") or secrets.get("meta_app_secret")
    expires = 60 * 86400
    if secret:  # a short-lived token needs the exchange; a long-lived one makes it fail, so keep the pasted token then
        try:
            rec = http.get(f"{BASE}/access_token", params={"grant_type": "th_exchange_token", "client_secret": secret,
                                                           "access_token": token})
            token, expires = rec["access_token"], int(rec.get("expires_in", expires))
        except (ApiError, KeyError):
            pass
    me = http.get(f"{BASE}/v1.0/me", params={"fields": "id,username", "access_token": token})
    if "id" not in me:
        raise ValueError("threads: the token was not accepted by /me")
    rec = {"access_token": token, "user_id": me["id"], "username": me.get("username", ""),
           "expires_at": (now + timedelta(seconds=expires)).isoformat(timespec="seconds")}
    tokens.save("threads", rec)
    return rec
