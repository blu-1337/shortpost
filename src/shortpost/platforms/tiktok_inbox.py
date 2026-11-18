"""TikTok Content Posting API, upload-to-inbox mode (CLAUDE.md 5.4, D-002).

The API cannot set the caption in this mode, so the caption goes to the phone via ntfy.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import config, notify, tokens
from ..http import ApiError, Http, NotConfigured
from ..log import logger
from .base import Result

NAME = "tiktok"
API = "https://open.tiktokapis.com/v2"
MB = 1 << 20
SLEEP = time.sleep


def plan_chunks(size: int) -> tuple[int, int]:
    """(chunk_size, total_chunk_count). Chunks 5-64 MB, last may be up to 128 MB (VERIFY); small files go in one."""
    if size <= 64 * MB:
        return size, 1
    chunk = 64 * MB
    return chunk, size // chunk  # the last chunk absorbs the remainder


def refresh_if_needed(http: Http, secrets: dict, now: datetime | None = None) -> dict:
    t = tokens.load("tiktok")
    if not t:
        raise NotConfigured("tiktok: run `shortpost auth tiktok`")
    now = now or datetime.now(timezone.utc)
    if datetime.fromisoformat(t["expires_at"]) - now > timedelta(hours=1):
        return t
    from ..auth import tiktok as auth

    return auth.refresh(http, secrets, t)


def _chunks(path: Path, chunk: int, total: int):
    with open(path, "rb") as f:
        for i in range(total):
            yield f.read(chunk) if i < total - 1 else f.read()


def push_title(cfg: dict, video: Path | None = None) -> str:
    slug = video.parent.name.split("__", 1)[-1][:40] if video else ""
    return f"TikTok draft: {slug} - post, or schedule {cfg['schedule']['native_time']} ET (01:30 local)" if slug else         f"TikTok draft ready - post, or schedule {cfg['schedule']['native_time']} ET (01:30 local)"


def upload(cfg: dict, video: Path, rendered: dict, fire_at, http: Http, native: bool = False,
           secrets: dict | None = None) -> Result:
    secrets = config.load_secrets() if secrets is None else secrets
    t = refresh_if_needed(http, secrets)
    if rendered.get("direct") and cfg["platforms"]["tiktok"].get("direct_post", True):
        from . import tiktok_direct

        try:
            return tiktok_direct.post(cfg, video, rendered, t["access_token"], http)
        except tiktok_direct.Fallback as e:
            import logging

            logging.getLogger("shortpost").warning("tiktok direct post not available yet, using the inbox draft: %s", str(e)[:150])
    auth = {"Authorization": f"Bearer {t['access_token']}", "Content-Type": "application/json; charset=UTF-8"}
    size = video.stat().st_size
    chunk, total = plan_chunks(size)
    init = http.post(f"{API}/post/publish/inbox/video/init/", headers=auth,
                     json={"source_info": {"source": "FILE_UPLOAD", "video_size": size,
                                           "chunk_size": chunk, "total_chunk_count": total}})
    data = init.get("data") or {}
    pid, url = data.get("publish_id"), data.get("upload_url")
    if not pid or not url:
        raise ApiError(f"tiktok init: unexpected answer {init.get('error') or init}")
    start = 0
    for block in _chunks(video, chunk, total):
        end = start + len(block) - 1
        http.request("PUT", url, content=block, headers={
            "Content-Type": "video/mp4", "Content-Length": str(len(block)),
            "Content-Range": f"bytes {start}-{end}/{size}"})
        start = end + 1
    status = ""
    for _ in range(200):
        st = http.post(f"{API}/post/publish/status/fetch/", headers=auth, json={"publish_id": pid}).get("data") or {}
        status = st.get("status", "")
        if status in ("SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"):
            break
        if status == "FAILED":
            raise ApiError(f"tiktok failed: {st.get('fail_reason', 'unknown')}")
        SLEEP(3)
    else:
        raise ApiError(f"tiktok: still '{status}' after waiting")
    caption = rendered["text"]
    (video.parent / "tiktok_caption.txt").write_text(caption, encoding="utf-8")
    pushed = False
    if cfg["platforms"]["tiktok"].get("push_caption", True):
        # ONE push: the tip is in the title, the body is the caption alone so it copies clean
        pushed = notify.send(cfg, push_title(cfg, video), caption, secrets)
        logger.info("tiktok ntfy push %s", "sent" if pushed else "NOT sent (no topic or ntfy unreachable)")
    return Result("published", pid, "tiktok inbox", {"needs_user_finish": True, "gate": True, "publish_id": pid, "push_sent": bool(pushed)})
