"""The add-a-video pipeline shared by `shortpost add` (terminal) and `shortpost ui` (browser).

prepare()  probe, slot, AI text, render -> Draft (nothing saved, nothing moved)
generate() new AI text for an existing Draft
commit()   save to library + DB, H.264 copy, upload to native-schedule platforms
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

from . import affiliates, browser, db, library, media, naming, render, scheduler, sync


class Duplicate(Exception):
    def __init__(self, row):
        super().__init__(f"Already queued as #{row['id']} (slot {row['slot_date_ny']}, state {row['state']}).")
        self.row = row


@dataclass
class Draft:
    path: Path
    vi: object
    info: object
    day: date
    sha1: str
    compat: bool
    warnings: list[str]
    catalog: list
    only: set | None = None
    skip: set = field(default_factory=set)
    meta: dict = field(default_factory=dict)
    rendered: dict = field(default_factory=dict)
    black_s: float = 0.0
    pre: bool = False  # pre-approved: text is final, waiting for the user's "Schedule post" / "Post now"


def attach_related(cfg: dict, vi, meta: dict, rendered: dict) -> str | None:
    """Related video for the YouTube Short (D-015); the local model picks among the best word matches."""
    from . import related
    from .ai.llm import ChainLLM

    if "youtube" not in rendered:
        return None
    try:
        return related.attach(cfg, vi, meta, rendered, ChainLLM(cfg))
    except Exception as e:  # noqa: BLE001 - never block adding a video because of this
        return f"related video lookup failed: {type(e).__name__}: {str(e)[:120]}"


def prepare(cfg: dict, conn, path: Path, requested: date | None = None, only: set | None = None, skip: set | None = None,
            dry_run: bool = False, now: datetime | None = None) -> Draft:
    vi = naming.load_video(path, cfg["series"].get("detect", True))
    info = media.probe(path)
    warnings = media.validate(info, cfg)
    compat = media.needs_compat_copy(info, cfg)
    if compat:
        warnings.append(f"{info.vcodec}/{info.pix_fmt} source: an H.264 compatibility copy will be made for {', '.join(cfg['media']['compat_platforms'])}")
    try:
        black_s = media.detect_leading_black(path, cfg)
    except (OSError, subprocess.SubprocessError):
        black_s = 0.0
    if black_s:
        warnings.append(f"the video starts with {black_s * 1000:.0f} ms of black (the automatic thumbnail would be black): it will be trimmed when you approve")
    if not dry_run and cfg["platforms"]["youtube"]["mode"] == "browser" and browser.is_up(cfg):
        try:
            sync.run(cfg, conn)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"YouTube sync failed ({type(e).__name__}); days already scheduled there may be double-booked")
    elif not dry_run:
        warnings.append("YouTube days not synced (the ShortPost Chrome is not running): run `shortpost sync` or use the Sync YouTube button in the UI")
    sha1 = db.sha1_of(path)
    dup = db.video_by_sha1(conn, sha1)
    if dup and not dry_run:
        raise Duplicate(dup)
    now = now or datetime.now(timezone.utc)
    not_before = None
    s = vi.parsed.series
    if s and cfg["series"].get("consecutive_days", True):
        not_before = db.last_series_slot(conn, vi.parsed.series_key, s["part"])
        if s["part"] > 1 and not_before is None:
            warnings.append(f"part {s['part']} added but no earlier part of this series is queued")
    day = scheduler.find_slot(cfg, now, db.occupied_dates(conn, cfg), requested, not_before)
    if requested and day != requested:
        warnings.append(f"{requested} is taken or too soon; using {day}")
    return Draft(path, vi, info, day, sha1, compat, warnings, affiliates.load_catalog(), only, skip or set(), black_s=black_s)


def reslot(cfg: dict, conn, d: Draft, now: datetime | None = None, synced: bool = False) -> Draft:
    """Pick the day again (after a sync or a series fix) and refresh the slot-related warnings."""
    d.warnings = [w for w in d.warnings if not w.startswith(("YouTube days not synced", "part ", "YouTube sync failed")) and " is taken or too soon" not in w]
    if not synced and cfg["platforms"]["youtube"]["mode"] == "browser":
        d.warnings.append("YouTube days not synced: use the Sync YouTube button")
    s = d.vi.parsed.series
    not_before = None
    if s and cfg["series"].get("consecutive_days", True):
        not_before = db.last_series_slot(conn, d.vi.parsed.series_key, s["part"])
        if s["part"] > 1 and not_before is None:
            d.warnings.append(f"part {s['part']} added but no earlier part of this series is queued")
    d.day = scheduler.find_slot(cfg, now or datetime.now(timezone.utc), db.occupied_dates(conn, cfg), None, not_before)
    return d


def sync_youtube(cfg: dict, conn, say: Callable[[str], None] = print) -> list[str]:
    """Start the ShortPost Chrome if needed, then read the channel's scheduled/published Shorts into the DB."""
    import time

    if cfg["platforms"]["youtube"]["mode"] != "browser":
        raise RuntimeError("sync supports YouTube browser mode only")
    if not browser.is_up(cfg):
        say("Starting the ShortPost Chrome (log in there if Studio asks)...")
        browser.launch(cfg)
        for _ in range(20):
            time.sleep(1)
            if browser.is_up(cfg):
                break
        else:
            raise RuntimeError("the ShortPost Chrome did not start")
    say("Reading YouTube Studio...")
    return sync.run(cfg, conn)


def generate(cfg: dict, d: Draft, hint: str = "") -> Draft:
    """(Re)write the AI text and render every platform. Keeps non-related warnings, refreshes the related-video one."""
    from .ai import metadata

    deals = [e.label for e in affiliates.select(d.catalog, "youtube", d.vi.parsed.topic + " " + d.vi.notes, False, d.vi.link_directive)]
    d.meta = metadata.generate(cfg, d.vi, d.info.duration_s, deals, hint)
    d.rendered = render.render_all(cfg, d.catalog, d.vi, d.meta, d.only)
    for p in d.skip:
        d.rendered.pop(p, None)
    warn = attach_related(cfg, d.vi, d.meta, d.rendered)
    d.warnings = [w for w in d.warnings if not w.startswith(("no long ", "related video", "cover line"))]
    line = d.meta.get("core", {}).get("cover_line", "")
    if line and cfg["media"].get("cover_text", True):
        d.warnings.append(f"cover line {line!r}: burned into the first {cfg['media'].get('cover_seconds', 1.8)} s on approval, unless the opening already has a title")
    if warn and warn not in d.warnings:
        d.warnings.append(warn)
    return d


def commit(cfg: dict, conn, d: Draft, say: Callable[[str], None] = print, native_upload: bool = True, post_now: bool = False) -> tuple[int, Path]:
    """Save the approved draft and (optionally) upload native-schedule platforms right away."""
    if post_now:
        today = scheduler.today_in(cfg["general"]["target_tz"], datetime.now(timezone.utc))
        if today not in db.occupied_dates(conn, cfg):
            d.day = today
    jitters = scheduler.plan_jitters(cfg, list(d.rendered), d.meta["seed"])
    vid, lib = library.save_video(cfg, conn, d.vi, d.info, d.meta, d.rendered, d.day, jitters, d.sha1)
    say(f"Saved as #{vid} in {lib} (slot {d.day}).")
    if d.black_s:
        say("Trimming the black first frame(s)...")
        try:
            cut = media.trim_leading_black(next(lib.glob("video.*")), cfg)
            say(f"  removed {cut * 1000:.0f} ms of black from the start")
        except (OSError, subprocess.SubprocessError) as e:
            say(f"  trim failed, the original is kept: {e}")
    if cfg["media"].get("cover_text", True):
        line = (d.meta.get("core") or {}).get("cover_line", "").strip()
        if line:
            video = next(lib.glob("video.*"))
            say("Checking whether the opening already has a title (local vision model)...")
            has = media.opening_has_title(cfg, video)
            if has is None:
                say("  could not tell, so no cover text is burned in")
            elif has:
                say("  the opening already has a title: left as it is")
            else:
                try:
                    media.burn_cover_text(video, line, cfg)
                    say(f"  burned in the cover line: {line!r}")
                except (OSError, subprocess.SubprocessError) as e:
                    say(f"  cover text failed, the original is kept: {e}")
    if d.compat:
        dst = lib / "video_h264.mp4"
        say("Making the H.264 compatibility copy (this takes a minute)...")
        try:
            media.make_compat_copy(next(lib.glob("video.*")), dst, cfg)
            say(f"  wrote {dst.name} ({dst.stat().st_size / 1e6:.1f} MB)")
        except (OSError, subprocess.CalledProcessError) as e:
            say(f"  compatibility copy failed: {e}")
    if post_now:
        from datetime import timedelta
        from . import runner

        now = datetime.now(timezone.utc)
        yt_at = (now + timedelta(minutes=30)).isoformat(timespec="seconds")
        with conn:
            # Facebook publishes at once (mode flips to a direct post); YouTube Studio can only schedule, so it goes ~30 min ahead;
            # every local-time/inbox platform is due right now.
            conn.execute("UPDATE posts SET mode='local_time', fire_at_utc=? WHERE video_id=? AND platform='facebook'", (now.isoformat(timespec="seconds"), vid))
            conn.execute("UPDATE posts SET fire_at_utc=? WHERE video_id=? AND platform='youtube'", (yt_at, vid))
            conn.execute("UPDATE posts SET fire_at_utc=? WHERE video_id=? AND mode!='native' AND platform!='facebook'", (now.isoformat(timespec="seconds"), vid))
            db.event(conn, vid, None, "post_now", "approved with Post now")
        say("Posting now (YouTube Studio can only schedule, so the Short goes live ~30 minutes from now)...")
        for line in runner.process(cfg, conn):
            say("  " + line)
    elif native_upload:
        from . import runner

        say("Uploading to native-schedule platforms (others post at their local time via `shortpost run`)...")
        for line in runner.process(cfg, conn, only_native=True):
            say("  " + line)
    return vid, lib
