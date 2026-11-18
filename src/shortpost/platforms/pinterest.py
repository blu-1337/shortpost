"""Pinterest video pin (API v5, CLAUDE.md 5.7). Stays disabled until Standard access is approved: Trial pins are
visible only to the owner and sandbox video pins are not allowed. Field names are VERIFY items."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import config, tokens
from ..http import ApiError, Http, NotConfigured
from .base import Result

NAME = "pinterest"
API = "https://api.pinterest.com/v5"
SLEEP = time.sleep


def _tok(http: Http, secrets: dict, now: datetime | None = None) -> dict:
    t = tokens.load("pinterest")
    if not t:
        raise NotConfigured("pinterest: run `shortpost auth pinterest`")
    if not t.get("board_id"):
        raise NotConfigured("pinterest: no board chosen; re-run `shortpost auth pinterest`")
    if datetime.fromisoformat(t["expires_at"]) - (now or datetime.now(timezone.utc)) < timedelta(days=1):
        from ..auth import pinterest as auth

        t = auth.refresh(http, secrets, t)
    return t


def upload(cfg: dict, video: Path, rendered: dict, fire_at, http: Http, native: bool = False,
           secrets: dict | None = None) -> Result:
    secrets = config.load_secrets() if secrets is None else secrets
    t = _tok(http, secrets)
    h = {"Authorization": f"Bearer {t['access_token']}"}
    m = http.post(f"{API}/media", headers=h, json={"media_type": "video"})
    mid, url, params = m.get("media_id"), m.get("upload_url"), m.get("upload_parameters") or {}
    if not mid or not url:
        raise ApiError(f"pinterest media register: unexpected answer {m}")
    with open(video, "rb") as f:  # S3-style multipart: the parameters first, the file last; no auth header here
        http.post(url, data=params, files={"file": (video.name, f, "video/mp4")})
    for _ in range(60):
        st = http.get(f"{API}/media/{mid}", headers=h).get("status", "")
        if st == "succeeded":
            break
        if st == "failed":
            raise ApiError("pinterest: media processing failed")
        SLEEP(5)
    else:
        raise ApiError("pinterest: media still processing after waiting")
    body = {"board_id": t["board_id"], "title": rendered["title"][:100], "description": rendered["text"][:500],
            "media_source": {"source_type": "video_id", "media_id": mid, "cover_image_key_frame_time": 1}}
    if rendered.get("link"):
        body["link"] = rendered["link"]
    if rendered.get("alt_text"):
        body["alt_text"] = rendered["alt_text"][:500]
    pin = http.post(f"{API}/pins", headers=h, json=body)
    if "id" not in pin:
        raise ApiError(f"pinterest pin: unexpected answer {pin}")
    return Result("published", pin["id"], f"https://www.pinterest.com/pin/{pin['id']}/", {})
