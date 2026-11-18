"""Bluesky video post through the `atproto` SDK (CLAUDE.md 5.6). Login uses a handle + app password from secrets."""

from __future__ import annotations

import re
from pathlib import Path

from .. import config
from ..http import ApiError, Http, NotConfigured
from .base import Result

NAME = "bluesky"
MAX_BYTES = 100 * 1000 * 1000  # ~100 MB cap (VERIFY via app.bsky.video.getUploadLimits)
TAG = re.compile(r"#([A-Za-z0-9_]+)")


def spans(rendered: dict) -> list[tuple[int, int, str, str]]:
    """Facet spans (start, end, kind, value) found in the rendered text: the link label and #tags."""
    text, out = rendered["text"], []
    label, url = rendered.get("link_label", ""), rendered.get("link_url", "")
    if label and url and (i := text.find(label)) >= 0:
        out.append((i, i + len(label), "link", url))
    for m in TAG.finditer(text):
        if not any(s <= m.start() < e for s, e, _, _ in out):
            out.append((m.start(), m.end(), "tag", m.group(1)))
    return sorted(out)


def build_text(rendered: dict):
    """TextBuilder with the link label as a link facet (not the raw URL) and tag facets."""
    from atproto import client_utils

    text, tb, pos = rendered["text"], client_utils.TextBuilder(), 0
    for s, e, kind, val in spans(rendered):
        tb.text(text[pos:s])
        tb.link(text[s:e], val) if kind == "link" else tb.tag(text[s:e], val)
        pos = e
    return tb.text(text[pos:])


def _client(secrets: dict):
    from atproto import Client

    for k in ("bluesky_handle", "bluesky_app_password"):
        if not secrets.get(k):
            raise NotConfigured(f"bluesky: run `shortpost set-secret {k}`")
    import httpx

    c = Client()
    c.request._client.timeout = httpx.Timeout(300, connect=20)  # the SDK default is 5 s, far too short for a video upload
    c.login(secrets["bluesky_handle"], secrets["bluesky_app_password"])
    return c


def upload(cfg: dict, video: Path, rendered: dict, fire_at, http: Http, native: bool = False,
           secrets: dict | None = None, client=None) -> Result:
    size = video.stat().st_size
    if size > MAX_BYTES:
        raise ApiError(f"bluesky: video is {size / 1e6:.0f} MB, over the ~100 MB cap")
    secrets = config.load_secrets() if secrets is None else secrets
    try:
        client = client or _client(secrets)
        data = video.read_bytes()
        try:
            resp = client.send_video(text=build_text(rendered), video=data,
                                     video_alt=rendered.get("alt_text") or None, langs=["en"])
        except Exception as first:  # noqa: BLE001 - video.bsky.app often times out; the PDS blob route is the fallback
            if type(first).__name__ != "InvokeTimeoutError":
                raise
            from atproto import models

            blob = client.upload_blob(data).blob
            embed = models.AppBskyEmbedVideo.Main(video=blob, alt=rendered.get("alt_text") or None)
            resp = client.send_post(text=build_text(rendered), embed=embed, langs=["en"])
    except NotConfigured:
        raise
    except Exception as e:  # SDK raises many types; surface a retryable ApiError without secrets
        raise ApiError(f"bluesky: {type(e).__name__}: {str(e)[:200]}") from None
    rkey = resp.uri.rsplit("/", 1)[-1]
    return Result("published", resp.uri, f"https://bsky.app/profile/{secrets.get('bluesky_handle', '')}/post/{rkey}", {})
