"""Threads API video post (CLAUDE.md 5.5). The video must be at a public URL, so it reuses Instagram's `media_url`.

The runner puts that URL into ``rendered["_video_url"]`` once the Instagram post is published.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import tokens
from ..http import ApiError, Http, NotConfigured
from .base import Result

NAME = "threads"
BASE = "https://graph.threads.net/v1.0"
REFRESH_URL = "https://graph.threads.net/refresh_access_token"
REFRESH_AFTER = timedelta(days=7)
MAX_AGE = timedelta(days=55)  # long-lived tokens last 60 days; past this a refresh may be too late


def _tok() -> dict:
    t = tokens.load("threads")
    if not t or not t.get("user_id"):
        raise NotConfigured("threads: run `shortpost auth threads`")
    return t


def refresh_if_needed(http: Http, t: dict, now: datetime | None = None) -> dict:
    """Refresh a long-lived token older than 7 days (it can be refreshed until it expires)."""
    now = now or datetime.now(timezone.utc)
    if tokens.age(t, now) < REFRESH_AFTER:
        return t
    rec = http.get(REFRESH_URL, params={"grant_type": "th_refresh_token", "access_token": t["access_token"]})
    if "access_token" not in rec:
        raise ApiError("threads token refresh failed; run `shortpost auth threads` again")
    t = {**t, "access_token": rec["access_token"], "obtained_at": now.isoformat(timespec="seconds"),
         "expires_at": (now + timedelta(seconds=int(rec.get("expires_in", 60 * 86400)))).isoformat(timespec="seconds")}
    tokens.save("threads", t)
    return t


def upload(cfg: dict, video: Path, rendered: dict, fire_at, http: Http, native: bool = False) -> Result:
    """post_type "link" (default): a text post that links to the Instagram Reel. "video": native video
    from a public URL. Threads rejects Meta's own CDN URLs (verified 2026-11-07), so "video" needs `_video_url` from elsewhere."""
    video_mode = cfg.get("platforms", {}).get("threads", {}).get("post_type", "link") == "video"
    url = rendered.get("_video_url" if video_mode else "_link")
    if not url:
        raise NotConfigured("threads: waiting for the Instagram post (its page URL is attached to the Threads post)")
    t = refresh_if_needed(http, _tok())
    uid, token = t["user_id"], t["access_token"]
    if video_mode:
        data = {"media_type": "VIDEO", "video_url": url, "text": rendered["text"], "access_token": token}
    else:
        # link_attachment with an Instagram Reel URL is rejected at publish time (error_subcode 4279047, verified
        # 2026-11-07), so the Reel link goes into the text itself (links in Threads text are clickable).
        text = rendered["text"]
        extra = f"\n🎬 Watch the Reel: {url}"
        data = {"media_type": "TEXT", "text": text + extra if len(text) + len(extra) <= 500 else text, "access_token": token}
    c = http.post(f"{BASE}/{uid}/threads", data=data)
    cid = c["id"]
    http.sleep(30 if video_mode else 10)  # Meta recommends ~30 s before publishing a video
    for _ in range(60):
        st = http.get(f"{BASE}/{cid}", params={"fields": "status,error_message", "access_token": token})
        code = st.get("status")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise ApiError(f"threads container {code}: {st.get('error_message', '')}")
        http.sleep(5)
    else:
        raise ApiError("threads container did not finish in time")
    pub = http.post(f"{BASE}/{uid}/threads_publish", data={"creation_id": cid, "access_token": token})
    mid = pub["id"]
    info = http.get(f"{BASE}/{mid}", params={"fields": "permalink", "access_token": token})
    return Result("published", mid, info.get("permalink", ""), {})
