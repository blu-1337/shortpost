"""Saving approved videos into library/ + DB, and the list/show/cancel/reschedule operations (local only until M3+)."""

from __future__ import annotations

import json
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

from . import config, db, scheduler
from .naming import VideoInput, slugify

ICON = {"published": "OK", "scheduled": "SCHED", "pending": "wait", "uploading": "...", "failed": "FAIL",
        "skipped": "skip", "cancelled": "x", "missed": "MISSED"}


def _in_inbox(path: Path) -> bool:
    try:
        path.resolve().relative_to((config.REPO_ROOT / "inbox").resolve())
        return True
    except ValueError:
        return False


def _place(src: Path, dst: Path) -> None:
    """Inbox files are moved; anything else (e.g. another project's renders) is copied, never moved."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if _in_inbox(src):
        shutil.move(str(src), str(dst))
    else:
        shutil.copy2(src, dst)


def save_video(cfg: dict, conn, vi: VideoInput, info, meta: dict, rendered: dict, day: date, jitters: dict[str, int],
               sha1: str) -> tuple[int, Path]:
    slug = slugify(vi.parsed.topic)[:60]
    if vi.parsed.series:
        slug += f"-part-{vi.parsed.series['part']}"
    lib = config.resolve(cfg["general"]["library_dir"]) / f"{day}__{slug}"
    suffix = vi.path.suffix.lower()
    _place(vi.path, lib / f"video{suffix}")
    for kind, p in vi.variants.items():
        _place(p, lib / f"video_{kind}{p.suffix.lower()}")
    side = vi.path.with_suffix(".txt")
    if vi.notes:
        (lib / "notes.txt").write_text(vi.notes, encoding="utf-8")
        if _in_inbox(side) and side.exists():
            side.unlink()
    doc = {"topic": vi.parsed.topic, "series": vi.parsed.series, "slot_date_ny": str(day), "source": vi.path.name,
           "ai": {k: meta[k] for k in ("core", "platform", "focus", "source", "model", "seed", "prompts") if k in meta},
           "rendered": rendered}
    (lib / "meta.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    if "tiktok" in rendered:
        (lib / "tiktok_caption.txt").write_text(rendered["tiktok"]["text"], encoding="utf-8")

    s = vi.parsed.series
    now = db.now_iso()
    try:
        return _insert_rows(cfg, conn, vi, info, rendered, day, jitters, sha1, lib, s, now)
    except Exception:
        # the database write failed (e.g. locked): put the inbox file back so nothing is orphaned in library/
        for dst, src in [(lib / f"video{suffix}", vi.path), *[(lib / f"video_{k}{p.suffix.lower()}", p) for k, p in vi.variants.items()]]:
            if _in_inbox(src) and dst.exists() and not src.exists():
                shutil.move(str(dst), str(src))
        shutil.rmtree(lib, ignore_errors=True)
        raise


def _insert_rows(cfg, conn, vi, info, rendered, day, jitters, sha1, lib, s, now):
    with conn:
        cur = conn.execute(
            "INSERT INTO videos(sha1, src_path, lib_dir, topic, notes, series_key, series_part, series_of, duration_s, width, height,"
            " slot_date_ny, state, meta_path, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sha1, str(vi.path), str(lib), vi.parsed.topic, vi.notes, vi.parsed.series_key, s["part"] if s else None,
             s["of"] if s else None, info.duration_s, info.width, info.height, str(day), "approved",
             str(lib / "meta.json"), now, now))
        vid = cur.lastrowid
        for plat in rendered:
            fire = scheduler.fire_time(cfg, plat, day, jitters[plat])
            conn.execute(
                "INSERT INTO posts(video_id, platform, mode, fire_at_utc, jitter_min, state, updated_at) VALUES (?,?,?,?,?,?,?)",
                (vid, plat, scheduler.platform_mode(cfg, plat), fire.isoformat(timespec="seconds"), jitters[plat], "pending", now))
        db.event(conn, vid, None, "approved", f"slot {day}")
    return vid, lib


# ---------------------------------------------------------------- list / show

def _fmt(cfg: dict, iso: str) -> str:
    from zoneinfo import ZoneInfo

    t = datetime.fromisoformat(iso)
    return f"{t.astimezone(ZoneInfo(cfg['general']['target_tz'])):%H:%M} ET / {t.astimezone(ZoneInfo(cfg['general']['local_tz'])):%a %H:%M}"


def list_calendar(cfg: dict, conn, days: int = 14, console: Console | None = None) -> int:
    c = console or Console()
    today = scheduler.today_in(cfg["general"]["target_tz"], datetime.now(timezone.utc))
    rows = conn.execute("SELECT * FROM videos WHERE slot_date_ny >= ? AND state != 'cancelled' ORDER BY slot_date_ny, id",
                        ((today - timedelta(days=2)).isoformat(),)).fetchall()
    rows = [r for r in rows if date.fromisoformat(r["slot_date_ny"]) <= today + timedelta(days=days)]
    if not rows:
        c.print("Nothing queued.")
        return 0
    t = Table(title=f"Queue (New York dates, next {days} days)")
    for col in ("id", "date (NY)", "topic", "state", "platforms"):
        t.add_column(col)
    for r in rows:
        parts = " ".join(f"{p['platform']}:{ICON.get(p['state'], p['state'])}" for p in db.posts_of(conn, r["id"]))
        part = f" (part {r['series_part']}/{r['series_of']})" if r["series_part"] else ""
        t.add_row(str(r["id"]), r["slot_date_ny"], r["topic"] + part, r["state"], parts)
    c.print(t)
    return 0


def show_video(cfg: dict, conn, vid: int, console: Console | None = None) -> int:
    c = console or Console()
    v = db.get_video(conn, vid)
    if not v:
        c.print(f"No video with id {vid}.")
        return 1
    c.print(f"[bold]#{v['id']}[/bold] {v['topic']}  slot {v['slot_date_ny']}  state {v['state']}")
    c.print(f"folder: {v['lib_dir']}")
    for p in db.posts_of(conn, vid):
        err = f"  error: {p['last_error']}" if p["last_error"] else ""
        c.print(f"  {p['platform']:<10} {p['mode']:<10} {p['state']:<10} {_fmt(cfg, p['fire_at_utc'])}  jitter {p['jitter_min']:+d}{err}")
    return 0


# ---------------------------------------------------------------- cancel / reschedule

REMOTE_STATES = ("scheduled", "published", "uploading")


def cancel(conn, vid: int, platform: str | None = None) -> list[str]:
    """Cancel pending/failed posts locally. Posts already on a platform are reported, not touched (needs M3+)."""
    msgs = []
    with conn:
        for p in db.posts_of(conn, vid):
            if platform and p["platform"] != platform:
                continue
            if p["state"] in REMOTE_STATES:
                msgs.append(f"{p['platform']}: already {p['state']} on the platform; remove it there manually (remote cancel arrives with its milestone)")
            elif p["state"] in ("pending", "failed"):
                conn.execute("UPDATE posts SET state='cancelled', updated_at=? WHERE id=?", (db.now_iso(), p["id"]))
                db.event(conn, vid, p["platform"], "cancelled", "")
                msgs.append(f"{p['platform']}: cancelled")
        db.refresh_video_state(conn, vid)
    return msgs


def reschedule(cfg: dict, conn, vid: int, new_day: date, now_utc: datetime | None = None) -> str:
    """Move a video to another free day. Refuses when any post is already on a platform (remote update not built yet)."""
    now_utc = now_utc or datetime.now(timezone.utc)
    v = db.get_video(conn, vid)
    if not v:
        return f"No video with id {vid}."
    posts = [p for p in db.posts_of(conn, vid) if p["state"] not in ("cancelled", "skipped")]
    if any(p["state"] in REMOTE_STATES for p in posts):
        return "Refused: some posts are already uploaded; their remote schedule cannot be changed yet."
    if new_day in db.occupied_dates(conn, cfg) - ({date.fromisoformat(v["slot_date_ny"])} if v["slot_date_ny"] else set()):
        return f"Refused: {new_day} already has a video."
    lead = timedelta(minutes=int(cfg["schedule"]["min_lead_minutes"]))
    for p in posts:
        if scheduler.fire_time(cfg, p["platform"], new_day, p["jitter_min"]) < now_utc + lead:
            return f"Refused: {p['platform']} would fire too soon on {new_day}."
    with conn:
        for p in posts:
            conn.execute("UPDATE posts SET fire_at_utc=?, updated_at=? WHERE id=?",
                         (scheduler.fire_time(cfg, p["platform"], new_day, p["jitter_min"]).isoformat(timespec="seconds"), db.now_iso(), p["id"]))
        conn.execute("UPDATE videos SET slot_date_ny=?, updated_at=? WHERE id=?", (str(new_day), db.now_iso(), vid))
        db.event(conn, vid, None, "rescheduled", f"{v['slot_date_ny']} -> {new_day}")
    return f"Moved #{vid} to {new_day}. meta.json keeps the old date in its folder name only."
