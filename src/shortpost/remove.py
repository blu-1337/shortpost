"""Remove a video: cancel what has not posted yet and delete it from the platforms that allow deleting.

What each platform can do (checked 2026-11-09, see CLAUDE.md 6):
  facebook   Graph API  DELETE /{video_id}                      (verified live)
  bluesky    atproto    delete_post(uri)                         (not verified live)
  threads    Threads API DELETE /{media_id}                      (not verified live)
  youtube    Studio in the ShortPost Chrome: Options -> Delete forever
  instagram  the API cannot delete media -> delete by hand in the app
  tiktok     the API cannot delete posts -> delete by hand in the app
"""

from __future__ import annotations

import re
from typing import Callable

from . import db, tokens
from .http import ApiError, Http, NotConfigured

MANUAL = ("instagram", "tiktok")      # no delete API
ON_PLATFORM = ("scheduled", "published")


def describe(conn, vid: int) -> list[str]:
    """One line per post saying what Remove will do (shown in the confirm box before anything happens)."""
    out = []
    for p in db.posts_of(conn, vid):
        s, n = p["state"], p["platform"]
        if s in ("cancelled", "skipped"):
            continue
        if s in ON_PLATFORM and n in MANUAL:
            out.append(f"{n}: cannot be deleted by the API, you must delete it in the app" + (f" ({p['remote_url']})" if p["remote_url"] else ""))
        elif s in ON_PLATFORM:
            out.append(f"{n}: DELETE it from {n} (it is {s})")
        elif s == "uploading":
            out.append(f"{n}: upload in progress, wait until it finishes, then remove")
        else:
            out.append(f"{n}: cancel ({s})")
    return out


def _fb_delete(cfg: dict, http: Http, remote_id: str) -> None:
    t = tokens.load("facebook")
    if not t:
        raise NotConfigured("facebook: no token")
    try:
        http.request("DELETE", f"https://graph.facebook.com/{cfg['meta']['graph_version']}/{remote_id}", params={"access_token": t["access_token"]})
    except ApiError as e:
        if "does not exist" not in str(e):      # already gone (deleted by hand) counts as deleted
            raise


def _threads_delete(http: Http, remote_id: str) -> None:
    t = tokens.load("threads")
    if not t:
        raise NotConfigured("threads: no token")
    http.request("DELETE", f"https://graph.threads.net/v1.0/{remote_id}", params={"access_token": t["access_token"]})


def _bluesky_delete(remote_id: str) -> None:
    from . import config
    from .platforms import bluesky

    bluesky._client(config.load_secrets()).delete_post(remote_id)


def _youtube_delete(cfg: dict, remote_url: str, commit: bool = True) -> None:
    from .platforms import youtube_browser

    m = re.search(r"(?:shorts/|v=|video/)([\w-]{11})", remote_url or "")
    if not m:
        raise ApiError("youtube: no video id on record")
    youtube_browser.delete_video(cfg, m.group(1), commit=commit)


def remove_video(cfg: dict, conn, vid: int, say: Callable[[str], None] = print, http: Http | None = None, youtube_commit: bool = True) -> list[str]:
    """Cancel/delete every post of the video. A post that could not be deleted stays as it was and is reported."""
    http = http or Http()
    msgs: list[str] = []
    for p in db.posts_of(conn, vid):
        n, s = p["platform"], p["state"]
        if s in ("cancelled", "skipped"):
            continue
        done = False
        try:
            if s in ("pending", "failed", "missed"):
                msgs.append(f"{n}: cancelled")
                done = True
            elif s == "uploading":
                msgs.append(f"{n}: upload still running, NOT removed (run Remove again when it has finished)")
            elif n in MANUAL:
                msgs.append(f"{n}: delete it by hand in the app" + (f": {p['remote_url']}" if p["remote_url"] else "") + " (marked cancelled here)")
                done = True
            elif n == "facebook":
                say("Deleting the Facebook Reel...")
                _fb_delete(cfg, http, p["remote_id"])
                msgs.append("facebook: deleted")
                done = True
            elif n == "bluesky":
                say("Deleting the Bluesky post...")
                _bluesky_delete(p["remote_id"])
                msgs.append("bluesky: deleted")
                done = True
            elif n == "threads":
                say("Deleting the Threads post...")
                _threads_delete(http, p["remote_id"])
                msgs.append("threads: deleted")
                done = True
            elif n == "youtube":
                say("Deleting the video in YouTube Studio (takes a minute)...")
                _youtube_delete(cfg, p["remote_url"], youtube_commit)
                msgs.append("youtube: deleted" if youtube_commit else "youtube: dry run, reached the final confirm")
                done = youtube_commit
            else:
                msgs.append(f"{n}: no way to delete, remove it by hand")
        except Exception as e:  # noqa: BLE001 - one platform failing must not stop the others
            msgs.append(f"{n}: FAILED to delete ({type(e).__name__}: {str(e)[:150]}); still on the platform")
        if done:
            with conn:
                conn.execute("UPDATE posts SET state='cancelled', updated_at=? WHERE id=?", (db.now_iso(), p["id"]))
                db.event(conn, vid, n, "removed", msgs[-1])
    with conn:
        db.refresh_video_state(conn, vid)
        v = db.get_video(conn, vid)
        if v and v["state"] == "cancelled" and not str(v["sha1"]).endswith(f":removed:{vid}"):
            # free the hash so the same file can be added again later
            conn.execute("UPDATE videos SET sha1=? WHERE id=?", (f"{v['sha1']}:removed:{vid}", vid))
    return msgs
