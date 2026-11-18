"""The `run` loop for uploads (CLAUDE.md 11). Scanning inbox/ and ntfy arrive in later milestones."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config, db, platforms, scheduler
from .http import ApiError, Http, NotConfigured
from .log import logger

ORDER = ["instagram", "threads", "bluesky", "pinterest", "tiktok", "facebook", "youtube"]
RETRY_MIN = (15, 60, 240)


def video_path(cfg: dict, lib: Path, platform: str) -> Path | None:
    """Original for YouTube, the H.264 copy for compat platforms (when it exists), the <=90 s variant for long Facebook videos."""
    orig = next(lib.glob("video.*"), None)
    if platform == "facebook":
        var = next(lib.glob("video_facebook.*"), None)
        if var:
            return var
    if platform in cfg["media"]["compat_platforms"]:
        h264 = lib / "video_h264.mp4"
        if h264.is_file():
            return h264
    return orig


def _rendered(v) -> dict:
    return json.loads(Path(v["meta_path"]).read_text(encoding="utf-8"))["rendered"]


LOCK_STALE = timedelta(hours=2)  # same as the Task Scheduler execution limit
STABLE_SECONDS = 30  # a file untouched this long is done copying


@contextmanager
def run_lock(cfg: dict, now: datetime | None = None):
    """data/run.lock holds the PID. Yields False when another run is alive (lock younger than LOCK_STALE)."""
    path = config.resolve(cfg["general"]["db_path"]).parent / "run.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now(timezone.utc)
    if path.exists():
        age = now - datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        if age < LOCK_STALE:
            yield False
            return
        path.unlink(missing_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        yield False
        return
    with os.fdopen(fd, "w") as f:
        f.write(str(os.getpid()))
    try:
        yield True
    finally:
        path.unlink(missing_ok=True)


def inbox_candidates(cfg: dict, conn, now: datetime | None = None) -> list[Path]:
    """Videos in watch_dirs that are finished copying and not queued yet (sidecar and variant files are skipped)."""
    now = (now or datetime.now(timezone.utc)).timestamp()
    exts = tuple(e.lower() for e in cfg["general"]["video_exts"])
    out = []
    for d in cfg["general"]["watch_dirs"]:
        root = config.resolve(d)
        for f in sorted(root.glob("*")) if root.is_dir() else []:
            stem = f.stem.lower()
            if not f.is_file() or f.suffix.lower() not in exts or stem.endswith(("_facebook", "-fb", "_fb")):
                continue
            if now - f.stat().st_mtime < STABLE_SECONDS or db.video_by_sha1(conn, db.sha1_of(f)):
                continue
            out.append(f)
    return out


def notify_new_videos(cfg: dict, conn, files: list[Path], secrets: dict | None = None) -> int:
    """One ntfy push per new file (remembered in data/notified.json). Returns how many were announced."""
    from . import notify

    store = config.resolve(cfg["general"]["db_path"]).parent / "notified.json"
    seen = set(json.loads(store.read_text(encoding="utf-8"))) if store.is_file() else set()
    fresh = [(f, db.sha1_of(f)) for f in files]
    fresh = [(f, h) for f, h in fresh if h not in seen]
    if not fresh:
        return 0
    names = ", ".join(f.name for f, _ in fresh)
    notify.send(cfg, f"ShortPost: {len(fresh)} video(s) waiting", f"{names}\nRun: shortpost review", secrets)
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps(sorted(seen | {h for _, h in fresh})), encoding="utf-8")
    return len(fresh)


def run_once(cfg: dict, conn, http: Http | None = None, now: datetime | None = None) -> list[str]:
    """Background pass: upload what is due, then tell the phone about new inbox files. Never prompts."""
    lines = process(cfg, conn, http, now)
    files = inbox_candidates(cfg, conn, now)
    if files:
        n = notify_new_videos(cfg, conn, files)
        lines.append(f"{len(files)} new video(s) in the inbox" + (f" (notified {n})" if n else " (already notified)") + ": run `shortpost review`")
    return lines


def _instagram_media_url(conn, video_id: int) -> str:
    """Public video URL for Threads: the published Instagram media_url of the same video ('' until it exists)."""
    row = conn.execute("SELECT state, extra_json FROM posts WHERE video_id=? AND platform='instagram'", (video_id,)).fetchone()
    if row and row["state"] == "published":
        return json.loads(row["extra_json"] or "{}").get("media_url", "")
    return ""


def _instagram_permalink(conn, video_id: int) -> str:
    """The published Instagram Reel's page URL ('' until it exists): Threads links to it."""
    row = conn.execute("SELECT state, remote_url FROM posts WHERE video_id=? AND platform='instagram'", (video_id,)).fetchone()
    return row["remote_url"] if row and row["state"] == "published" and row["remote_url"] else ""


def _set(conn, pid: int, **cols) -> None:
    cols["updated_at"] = db.now_iso()
    conn.execute(f"UPDATE posts SET {', '.join(k + '=?' for k in cols)} WHERE id=?", [*cols.values(), pid])
    conn.commit()


def recover_interrupted(conn, now: datetime, hours: int = 3) -> list[str]:
    """A post stuck in 'uploading' (the process died mid-upload, e.g. a restart) would never be picked up again.
    TikTok inbox drafts are safe to resend (worst case a duplicate draft). Public platforms may already be live,
    so those become 'failed' with no automatic retry and a note to check the platform first."""
    out = []
    cut = (now - timedelta(hours=hours)).isoformat(timespec="seconds")
    for p in conn.execute("SELECT id, video_id, platform, mode FROM posts WHERE state='uploading' AND updated_at < ?", (cut,)).fetchall():
        if p["platform"] == "tiktok" and p["mode"] == "inbox":
            _set(conn, p["id"], state="pending", last_error="upload was interrupted; sending again", next_attempt_at_utc=None)
            out.append(f"tiktok #{p['video_id']}: interrupted upload reset, sending again")
        else:
            _set(conn, p["id"], state="failed", attempts=99, next_attempt_at_utc=None,
                 last_error="upload was interrupted: check the platform for a live post before retrying")
            out.append(f"{p['platform']} #{p['video_id']}: interrupted upload, check the platform before retrying")
        db.event(conn, p["video_id"], p["platform"], "interrupted", "found in 'uploading' after a restart or crash")
    return out


def _has_direct(p) -> bool:
    try:
        return bool((_rendered(p).get("tiktok") or {}).get("direct"))
    except (KeyError, ValueError, TypeError):
        return False


def _tiktok_held(conn, p, rendered_has_direct: bool):
    """One TikTok inbox draft at a time: while another video's draft is not marked as posted, this one waits."""
    if p["platform"] != "tiktok" or p["mode"] != "inbox" or rendered_has_direct:
        return None
    if json.loads(p["extra_json"] or "{}").get("resend"):
        return None  # the user asked for this one explicitly
    for r in conn.execute("SELECT video_id, extra_json FROM posts WHERE platform='tiktok' AND state='published' AND video_id != ?",
                          (p["video_id"],)).fetchall():
        e = json.loads(r["extra_json"] or "{}")
        if e.get("gate") and not e.get("user_done"):
            return r
    return None


def mark_tiktok_done(conn, video_id: int) -> bool:
    """The user finished (posted or deleted) this video's TikTok draft: the next video's draft may go."""
    row = conn.execute("SELECT id, extra_json FROM posts WHERE platform='tiktok' AND video_id=?", (video_id,)).fetchone()
    if not row:
        return False
    e = json.loads(row["extra_json"] or "{}")
    e["user_done"] = True
    _set(conn, row["id"], extra_json=json.dumps(e))
    db.event(conn, video_id, "tiktok", "user_done", "draft marked as posted")
    return True


def _fail(conn, p, err: str, now: datetime | None = None) -> None:
    n = p["attempts"] + 1
    nxt = None
    if n <= len(RETRY_MIN):
        nxt = ((now or datetime.now(timezone.utc)) + timedelta(minutes=RETRY_MIN[n - 1])).isoformat(timespec="seconds")
    _set(conn, p["id"], state="failed", attempts=n, last_error=err[:500], next_attempt_at_utc=nxt)
    db.event(conn, p["video_id"], p["platform"], "failed", err[:500])
    conn.commit()
    logger.warning("%s failed (attempt %d): %s", p["platform"], n, err)
    if nxt is None:  # out of retries: tell the phone
        from . import notify

        notify.send({}, f"ShortPost: {p['platform']} failed", err[:300])


def due(cfg: dict, p, v, now: datetime) -> str | None:
    """'upload' | 'missed' | None for one post."""
    if p["state"] == "failed":
        return "upload" if p["attempts"] <= len(RETRY_MIN) and p["next_attempt_at_utc"] and p["next_attempt_at_utc"] <= now.isoformat(timespec="seconds") else None
    if p["state"] != "pending":
        return None
    fire = datetime.fromisoformat(p["fire_at_utc"])
    if p["mode"] == "native":
        slot = datetime.fromisoformat(v["slot_date_ny"]).date()
        ahead = (slot - scheduler.today_in(cfg["general"]["target_tz"], now)).days
        return "upload" if ahead <= cfg["schedule"]["upload_ahead_days_max"] else None
    if fire > now:
        return None
    return "missed" if now - fire > timedelta(hours=cfg["schedule"]["max_catch_up_hours"]) else "upload"


def process(cfg: dict, conn, http: Http | None = None, now: datetime | None = None, only_native: bool = False) -> list[str]:
    """One pass over due posts. Returns human-readable lines."""
    http, now, out = http or Http(), now or datetime.now(timezone.utc), []
    out += recover_interrupted(conn, now)
    rows = conn.execute("SELECT p.*, v.lib_dir, v.meta_path, v.slot_date_ny FROM posts p JOIN videos v ON v.id=p.video_id "
                        "WHERE (v.state IN ('approved') AND p.state IN ('pending','failed','scheduled','published')) "
                        # a finished video may still owe its Story: look back one day only, so old posts never get one
                        "OR (v.state='done' AND p.state='published' AND p.platform IN ('instagram','facebook') AND p.fire_at_utc > ? "
                        "AND p.extra_json NOT LIKE '%story_id%')",
                        ((now - timedelta(days=1)).isoformat(timespec="seconds"),)).fetchall()
    rows.sort(key=lambda r: (r["fire_at_utc"][:10], ORDER.index(r["platform"]) if r["platform"] in ORDER else 99))
    for p in rows:
        conn.commit()  # never hold a write lock while a (slow) platform call runs: the UI and other runs must be able to write
        mod = platforms.get(p["platform"])
        if mod is None:
            continue
        if p["state"] in ("scheduled", "published"):
            line = _after_live(cfg, conn, p, mod, http, now)
            if line:
                out.append(line)
            continue
        if only_native and p["mode"] != "native":
            continue
        held = _tiktok_held(conn, p, rendered_has_direct=_has_direct(p))
        if held:
            out.append(f"tiktok: waiting, the draft of #{held['video_id']} in the TikTok Inbox is not marked as posted yet")
            continue
        action = due(cfg, p, p, now)
        if action == "missed" and p["platform"] == "tiktok" and p["mode"] == "inbox":
            action = "upload"  # it only waited for the previous draft to be finished: never 'missed' for that
        if action == "missed":
            _set(conn, p["id"], state="missed", last_error="PC was off or runner not running")
            db.event(conn, p["video_id"], p["platform"], "missed", "")
            out.append(f"{p['platform']}: missed")
            continue
        if action != "upload":
            continue
        vp = video_path(cfg, Path(p["lib_dir"]), p["platform"])
        try:
            rendered = _rendered(p)[p["platform"]]
            if p["platform"] == "threads":
                rendered = {**rendered, "_video_url": _instagram_media_url(conn, p["video_id"]),
                            "_link": _instagram_permalink(conn, p["video_id"])}
            if vp is None:
                raise ApiError("no video file in the library folder")
            _set(conn, p["id"], state="uploading")
            res = mod.upload(cfg, vp, rendered, datetime.fromisoformat(p["fire_at_utc"]), http, p["mode"] == "native")
        except NotConfigured as e:
            _set(conn, p["id"], state=p["state"])
            out.append(str(e))
            continue
        except Exception as e:  # noqa: BLE001 - one platform failing (e.g. a Studio error) must not stop the others
            _fail(conn, p, f"{type(e).__name__}: {e}", now)
            out.append(f"{p['platform']}: failed ({e})")
            continue
        _set(conn, p["id"], state=res.state, remote_id=res.remote_id, remote_url=res.remote_url,
             extra_json=json.dumps(res.extra), last_error=None, next_attempt_at_utc=None)
        db.event(conn, p["video_id"], p["platform"], res.state, res.remote_url)
        out.append(f"{p['platform']}: {res.state} {res.remote_url}")
    for vid in {r["video_id"] for r in rows}:
        db.refresh_video_state(conn, vid)
    conn.commit()
    return out


def _after_live(cfg, conn, p, mod, http, now) -> str | None:
    """Facebook first comment once the Reel is live."""
    if datetime.fromisoformat(p["fire_at_utc"]) > now:
        return None
    hook = getattr(mod, "after_live", None)
    extra = json.loads(p["extra_json"] or "{}")
    line = None
    if hook and not extra.get("first_comment_done"):
        try:
            if hook(cfg, p["remote_id"], _rendered(p)[p["platform"]], http):
                extra["first_comment_done"] = True
                _set(conn, p["id"], state="published", extra_json=json.dumps(extra))
                line = f"{p['platform']}: first comment posted"
        except (ApiError, NotConfigured) as e:
            logger.warning("%s first comment: %s", p["platform"], e)
    story = _story(cfg, conn, p, mod, http, extra)
    return "; ".join(x for x in (line, story) if x) or None


STORY_TRIES = 3


def story_video(cfg: dict, lib: Path) -> Path:
    """library/<video>/video_story.mp4, made on first use from the H.264 copy (or the original)."""
    from . import media

    dst = lib / "video_story.mp4"
    if not dst.is_file():
        src = lib / "video_h264.mp4"
        src = src if src.is_file() else next(lib.glob("video.*"))
        media.make_story_copy(src, dst, cfg)
    return dst


def _story(cfg, conn, p, mod, http, extra: dict) -> str | None:
    """Post the Story once the Reel is live (Instagram: published; Facebook: first comment step done). Never fails the post."""
    st = cfg.get("stories", {})
    plat = p["platform"]
    if not st.get("enabled") or plat not in st.get("platforms", []) or not hasattr(mod, "post_story") or extra.get("story_id"):
        return None
    if extra.get("story_tries", 0) >= STORY_TRIES:
        return None
    if plat == "facebook" and not extra.get("first_comment_done"):
        return None
    try:
        sid = mod.post_story(cfg, story_video(cfg, Path(p["lib_dir"])), http)
    except Exception as e:  # noqa: BLE001 - a story problem must never undo or retry the Reel itself
        extra["story_tries"] = extra.get("story_tries", 0) + 1
        extra["story_error"] = f"{type(e).__name__}: {e}"[:300]
        _set(conn, p["id"], extra_json=json.dumps(extra))
        logger.warning("%s story failed (try %d): %s", plat, extra["story_tries"], e)
        return f"{plat}: story failed ({e})"
    extra["story_id"] = sid
    extra.pop("story_error", None)
    _set(conn, p["id"], extra_json=json.dumps(extra))
    db.event(conn, p["video_id"], plat, "story", sid)
    return f"{plat}: story posted {sid}"
