"""YouTube dispatcher: ``platforms.youtube.mode`` picks Studio browser automation or (after the audit) the API."""

from __future__ import annotations

import logging

log = logging.getLogger("shortpost.youtube")


def upload(cfg, video, rendered, fire_at, http=None, native=True):
    mode = cfg["platforms"]["youtube"]["mode"]
    if mode == "browser":
        from . import youtube_browser

        res = youtube_browser.upload(cfg, video, rendered, fire_at, http, native)
        rel = rendered.get("related")
        if rel:  # the API cannot set "Related video"; a failure here must not undo the finished upload
            try:
                youtube_browser.set_related(cfg, res.remote_id, rel["title"])
                res.extra["related"] = rel["id"]
            except Exception as e:  # noqa: BLE001
                log.warning("related video not set: %s", e)
                res.extra["related_error"] = str(e)[:200]
        return res
    raise NotImplementedError("youtube api mode is not implemented yet (M4, after the audit)")
