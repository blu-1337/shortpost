"""Facebook Reels via the Pages API (CLAUDE.md 5.3). Scheduling support is a VERIFY item."""

from __future__ import annotations

from pathlib import Path

from .. import tokens
from ..http import ApiError, Http, NotConfigured, file_stream
from .base import Result

NAME = "facebook"


def _tok() -> dict:
    t = tokens.load("facebook")
    if not t:
        raise NotConfigured("facebook: run `shortpost auth facebook`")
    return t


def upload(cfg: dict, video: Path, rendered: dict, fire_at, http: Http, native: bool) -> Result:
    t, ver = _tok(), cfg["meta"]["graph_version"]
    page, auth = t["page_id"], {"access_token": t["access_token"]}
    start = http.post(f"https://graph.facebook.com/{ver}/{page}/video_reels", data={**auth, "upload_phase": "start"})
    vid = start["video_id"]
    size = video.stat().st_size
    http.post(start.get("upload_url") or f"https://rupload.facebook.com/video-upload/{ver}/{vid}",
              headers={"Authorization": f"OAuth {t['access_token']}", "offset": "0", "file_size": str(size),
                       "Content-Length": str(size)},
              content=file_stream(video))
    fin = {**auth, "upload_phase": "finish", "video_id": vid, "description": rendered["text"]}
    if native:
        fin.update(video_state="SCHEDULED", scheduled_publish_time=str(int(fire_at.timestamp())))
    else:
        fin["video_state"] = "PUBLISHED"
    http.post(f"https://graph.facebook.com/{ver}/{page}/video_reels", data=fin)
    thumb_error = _set_cover(cfg, http, ver, vid, auth, video)
    return Result("scheduled" if native else "published", vid, f"https://www.facebook.com/reel/{vid}", {"first_comment_done": False, **({"cover_error": thumb_error} if thumb_error else {})})


def _set_cover(cfg: dict, http: Http, ver: str, vid: str, auth: dict, video: Path) -> str:
    """Best effort: upload the first frame as the preferred thumbnail. Never fails the post; returns an error text or ''."""
    import tempfile

    from .. import media

    try:
        with tempfile.TemporaryDirectory() as tmp:
            jpg = Path(tmp) / "cover.jpg"
            media.extract_frame(video, jpg, int(cfg.get("media", {}).get("cover_ms", 0)) / 1000)
            http.post(f"https://graph.facebook.com/{ver}/{vid}/thumbnails", data={**auth, "is_preferred": "true"},
                      files={"source": ("cover.jpg", jpg.read_bytes(), "image/jpeg")})
        return ""
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {str(e)[:150]}"


def post_story(cfg: dict, video: Path, http: Http) -> str:
    """Publish the video as a Page Story (video_stories: start, upload, finish). Returns the video id."""
    t, ver = _tok(), cfg["meta"]["graph_version"]
    page, auth = t["page_id"], {"access_token": t["access_token"]}
    base = f"https://graph.facebook.com/{ver}/{page}/video_stories"
    start = http.post(base, data={**auth, "upload_phase": "start"})
    vid = start["video_id"]
    size = video.stat().st_size
    http.post(start.get("upload_url") or f"https://rupload.facebook.com/video-upload/{ver}/{vid}",
              headers={"Authorization": f"OAuth {t['access_token']}", "offset": "0", "file_size": str(size),
                       "Content-Length": str(size)},
              content=file_stream(video))
    http.post(base, data={**auth, "upload_phase": "finish", "video_id": vid})
    return vid


def after_live(cfg: dict, remote_id: str, rendered: dict, http: Http) -> bool:
    """Post the link as first comment once the Reel is published. False = not live yet."""
    if not cfg["platforms"]["facebook"].get("first_comment_link", True) or not rendered.get("comment", "").strip():
        return True
    t, ver = _tok(), cfg["meta"]["graph_version"]
    info = http.get(f"https://graph.facebook.com/{ver}/{remote_id}", params={"fields": "status,post_id", "access_token": t["access_token"]})
    post_id = info.get("post_id")
    if not post_id or (info.get("status") or {}).get("video_status") not in ("ready", "published", None):
        return False
    data = {"message": rendered["comment"], "access_token": t["access_token"]}
    try:
        http.post(f"https://graph.facebook.com/{ver}/{post_id}/comments", data=data)
    except ApiError:
        # Reels' post_id is not always a commentable post (error #12); the video id is.
        http.post(f"https://graph.facebook.com/{ver}/{remote_id}/comments", data=data)
    return True
