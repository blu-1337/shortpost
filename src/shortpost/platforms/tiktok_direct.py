"""TikTok Content Posting API, Direct Post (D-017).

Posts the video publicly with the caption, using the settings the user confirmed in the web UI
(privacy, comment/duet/stitch, commercial-content disclosure). TikTok has no scheduling field, so the
post happens when the runner fires it. Until the app passes TikTok's audit, TikTok forces private posts:
`tiktok_inbox.upload` then falls back to the inbox draft.
"""

from __future__ import annotations

import time
from pathlib import Path

from ..http import ApiError, Http
from .base import Result

API = "https://open.tiktokapis.com/v2"
SLEEP = time.sleep
FALLBACK_ERRORS = ("unaudited_client_can_only_post_to_private_accounts", "scope_not_authorized", "url_ownership_unverified")


class Fallback(Exception):
    """Direct Post is not available yet (unaudited app / missing scope): use the inbox draft instead."""


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"}


def creator_info(http: Http, token: str) -> dict:
    """Who would receive the post and what they may choose. Must be called each time the post page renders."""
    return (http.post(f"{API}/post/publish/creator_info/query/", headers=_headers(token)).get("data")) or {}


def validate_settings(s: dict, creator: dict | None = None) -> list[str]:
    """Rules from TikTok's content sharing guidelines. Returns problems (empty = ok)."""
    bad = []
    if not s.get("consented"):
        bad.append("the user has not confirmed the TikTok settings")
    priv = s.get("privacy")
    if not priv:
        bad.append("choose who can view the video")
    if creator and priv and priv not in creator.get("privacy_level_options", []):
        bad.append(f"'{priv}' is not allowed for this TikTok account")
    if s.get("disclose") and not (s.get("your_brand") or s.get("branded")):
        bad.append("content disclosure is on: tick 'Your brand' and/or 'Branded content'")
    if s.get("disclose") and s.get("branded") and priv == "SELF_ONLY":
        bad.append("branded content cannot be private")
    if creator:
        for k, flag in (("comment", "comment_disabled"), ("duet", "duet_disabled"), ("stitch", "stitch_disabled")):
            if s.get(k) and creator.get(flag):
                bad.append(f"{k} is disabled in this account's TikTok settings")
    return bad


def post_info(caption: str, s: dict, cover_ms: int = 0) -> dict:
    info = {"title": caption[:2200], "video_cover_timestamp_ms": int(cover_ms), "privacy_level": s["privacy"], "disable_comment": not s.get("comment"),
            "disable_duet": not s.get("duet"), "disable_stitch": not s.get("stitch")}
    if s.get("disclose"):
        info["brand_organic_toggle"] = bool(s.get("your_brand"))
        info["brand_content_toggle"] = bool(s.get("branded"))
    return info


def post(cfg: dict, video: Path, rendered: dict, token: str, http: Http) -> Result:
    from .tiktok_inbox import MB, _chunks, plan_chunks  # noqa: F401 - shared chunk logic

    s = rendered["direct"]
    creator = creator_info(http, token)
    bad = validate_settings(s, creator)
    if bad:
        raise ApiError("tiktok direct: " + "; ".join(bad))
    max_s = creator.get("max_video_post_duration_sec")
    size = video.stat().st_size
    chunk, total = plan_chunks(size)
    body = {"post_info": post_info(rendered["text"], s, cfg.get("media", {}).get("cover_ms", 0)),
            "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": total}}
    try:
        init = http.post(f"{API}/post/publish/video/init/", headers=_headers(token), json=body)
    except ApiError as e:
        if any(code in str(e) for code in FALLBACK_ERRORS):
            raise Fallback(str(e)) from None
        raise
    data = init.get("data") or {}
    pid, url = data.get("publish_id"), data.get("upload_url")
    if not pid or not url:
        raise ApiError(f"tiktok direct init: unexpected answer {init.get('error') or init}")
    start = 0
    for block in _chunks(video, chunk, total):
        end = start + len(block) - 1
        http.request("PUT", url, content=block, headers={"Content-Type": "video/mp4", "Content-Length": str(len(block)),
                                                         "Content-Range": f"bytes {start}-{end}/{size}"})
        start = end + 1
    # From here the post exists on TikTok: never raise (a retry would post it twice).
    status, post_id = "", None
    for _ in range(60):
        try:
            st = http.post(f"{API}/post/publish/status/fetch/", headers=_headers(token), json={"publish_id": pid}).get("data") or {}
        except ApiError:
            SLEEP(5)
            continue
        status = st.get("status", "")
        if status == "PUBLISH_COMPLETE":
            ids = st.get("publicaly_available_post_id") or []
            post_id = ids[0] if ids else None
            break
        if status == "FAILED":
            raise ApiError(f"tiktok direct failed: {st.get('fail_reason', 'unknown')}")
        SLEEP(5)
    extra = {"publish_id": pid, "direct": True, "privacy": s["privacy"]}
    if status != "PUBLISH_COMPLETE":
        extra["confirm_pending"] = status or "unknown"
    return Result("published", str(post_id or pid), "tiktok direct post", extra)
