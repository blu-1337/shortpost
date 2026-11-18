"""Instagram Reels through the Facebook Page token on graph.facebook.com, resumable upload (CLAUDE.md 5.2).

The Meta app uses "Unified login", so there is no separate Instagram token: the Page token stored by
`shortpost auth facebook` also publishes to the Instagram professional account linked to that Page.
"""

from __future__ import annotations

from pathlib import Path

from .. import tokens
from ..http import ApiError, Http, NotConfigured
from .base import Result

NAME = "instagram"


def _tok() -> dict:
    t = tokens.load("facebook")
    if not t:
        raise NotConfigured("instagram: run `shortpost auth facebook`")
    if not t.get("ig_user_id"):
        raise NotConfigured("instagram: no Instagram account linked to the Page; re-run `shortpost auth facebook` with instagram_basic + instagram_content_publish")
    return t


def post_story(cfg: dict, video: Path, http: Http) -> str:
    """Publish the video as an Instagram Story (media_type=STORIES). Returns the story media id.
    The API has no link sticker, so the link is burned into the video by media.make_story_copy."""
    t, ver = _tok(), cfg["meta"]["graph_version"]
    uid, token = t["ig_user_id"], t["access_token"]
    base = f"https://graph.facebook.com/{ver}"
    c = http.post(f"{base}/{uid}/media", data={"media_type": "STORIES", "upload_type": "resumable", "access_token": token})
    return _finish(http, base, ver, uid, token, c["id"], video)["id"]


def upload(cfg: dict, video: Path, rendered: dict, fire_at, http: Http, native: bool = False) -> Result:
    t, ver = _tok(), cfg["meta"]["graph_version"]
    uid, token = t["ig_user_id"], t["access_token"]
    base = f"https://graph.facebook.com/{ver}"
    c = http.post(f"{base}/{uid}/media", data={
        "media_type": "REELS", "upload_type": "resumable", "caption": rendered["text"],
        "thumb_offset": str(int(cfg.get("media", {}).get("cover_ms", 0))),
        "share_to_feed": str(cfg["platforms"]["instagram"].get("share_to_feed", True)).lower(), "access_token": token})
    pub = _finish(http, base, ver, uid, token, c["id"], video)
    mid = pub["id"]
    info = http.get(f"{base}/{mid}", params={"fields": "permalink,media_url", "access_token": token})
    return Result("published", mid, info.get("permalink", ""), {"media_url": info.get("media_url", "")})


def _finish(http: Http, base: str, ver: str, uid: str, token: str, cid: str, video: Path) -> dict:
    """Upload the file into container `cid`, wait for FINISHED, publish it. Returns the media_publish answer."""
    size = video.stat().st_size
    http.post(f"https://rupload.facebook.com/ig-api-upload/{ver}/{cid}",
              headers={"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size)},
              content=video.read_bytes())   # NOT a streamed body: Meta answers a chunked/streamed upload with ProcessingFailedError (tested 2026-11-11)
    for _ in range(120):  # up to ~10 min
        st = http.get(f"{base}/{cid}", params={"fields": "status_code,status", "access_token": token})
        code = st.get("status_code")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise ApiError(f"instagram container {code}: {st.get('status')}")
        http.sleep(5)
    else:
        raise ApiError("instagram container did not finish in time")
    return http.post(f"{base}/{uid}/media_publish", data={"creation_id": cid, "access_token": token})
