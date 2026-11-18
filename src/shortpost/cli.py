"""argparse CLI. Handlers import lazily and return an int exit code."""

from __future__ import annotations

import argparse
import getpass
import json
import secrets as pysecrets
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

from . import config
from .log import prune_logs, setup_logging

PLATFORMS = ["youtube", "youtube-browser", "facebook", "instagram", "threads", "tiktok", "bluesky", "pinterest"]


def _stub(milestone: str):
    def handler(args) -> int:
        print(f"'{args.command}' is not implemented yet (planned in {milestone}).")
        return 2

    return handler


def _cmd_doctor(args) -> int:
    from . import doctor

    cfg = config.load_config(Path(args.config) if args.config else None)
    return doctor.print_table(doctor.run_checks(cfg, config.load_secrets()))


def _cmd_set_secret(args) -> int:
    if args.generate:
        value = f"samastano-{pysecrets.token_urlsafe(12)}"
        config.save_secret(args.key, value)
        # The ntfy topic must be typed into the phone app, so it is shown once.
        print(f"Stored {args.key} = {value}" if args.key == "ntfy_topic" else f"Stored {args.key}.")
        return 0
    value = getpass.getpass(f"{args.key}: ")
    if not value:
        print("Empty value, nothing stored.")
        return 1
    config.save_secret(args.key, value)
    print(f"Stored {args.key}.")
    return 0


def _cmd_add(args) -> int:
    """Probe, generate, render, preview; on approval save to library + DB (uploads arrive in M3+)."""
    from datetime import datetime, timezone

    from . import affiliates, browser, db, library, media, naming, preview, render, scheduler, sync
    from .ai import metadata

    cfg = config.load_config(Path(args.config) if args.config else None)
    path = Path(args.file)
    if not path.is_file():
        print(f"file not found: {path}")
        return 1
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
    catalog = affiliates.load_catalog()
    only = set(args.only.split(",")) if args.only else None
    skip = set(args.skip.split(",")) if args.skip else set()
    conn = db.connect(cfg)
    if not args.dry_run and cfg["platforms"]["youtube"]["mode"] == "browser" and browser.is_up(cfg):
        try:
            sync.run(cfg, conn)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"YouTube sync failed ({type(e).__name__}); days already scheduled there may be double-booked")
    elif not args.dry_run:
        warnings.append("YouTube days not synced (the ShortPost Chrome is not running): run `shortpost sync`")
    sha1 = db.sha1_of(path)
    dup = db.video_by_sha1(conn, sha1)
    if dup and not args.dry_run:
        print(f"Already queued as #{dup['id']} (slot {dup['slot_date_ny']}, state {dup['state']}).")
        return 1
    now = datetime.now(timezone.utc)
    not_before = None
    s = vi.parsed.series
    if s and cfg["series"].get("consecutive_days", True):
        not_before = db.last_series_slot(conn, vi.parsed.series_key, s["part"])
        if s["part"] > 1 and not_before is None:
            warnings.append(f"part {s['part']} added but no earlier part of this series is queued")
    day = scheduler.find_slot(cfg, now, db.occupied_dates(conn, cfg), args.date, not_before)
    if args.date and day != args.date:
        warnings.append(f"{args.date} is taken or too soon; using {day}")
    hint = ""
    while True:
        deals = [e.label for e in affiliates.select(catalog, "youtube", vi.parsed.topic + " " + vi.notes, False, vi.link_directive)]
        meta = metadata.generate(cfg, vi, info.duration_s, deals, hint)
        rendered = render.render_all(cfg, catalog, vi, meta, only)
        for p in skip:
            rendered.pop(p, None)
        rel_warn = _attach_related(cfg, vi, meta, rendered)
        if rel_warn and rel_warn not in warnings:
            warnings.append(rel_warn)
        preview.show(cfg, vi, info, meta, rendered, day, warnings)
        if args.dry_run or args.yes or cfg["general"].get("auto_approve") or not sys.stdin.isatty():
            break
        choice = input("[Y]es / [R]egenerate / [C]ancel: ").strip().lower()
        if choice.startswith("r"):
            hint = input("Optional hint for the AI: ").strip()
            continue
        if choice.startswith("y"):
            break
        print("Cancelled.")
        return 1
    if args.dry_run:
        print("\n(dry run: nothing was saved, copied or uploaded)")
        return 0
    if not (args.yes or cfg["general"].get("auto_approve") or sys.stdin.isatty()):
        print("\nNot approved (no terminal and no --yes); nothing saved.")
        return 1
    jitters = scheduler.plan_jitters(cfg, list(rendered), meta["seed"])
    vid, lib = library.save_video(cfg, conn, vi, info, meta, rendered, day, jitters, sha1)
    print(f"\nSaved as #{vid} in {lib} (slot {day}).")
    if black_s:
        print("Trimming the black first frame(s)...")
        try:
            cut = media.trim_leading_black(next(lib.glob("video.*")), cfg)
            print(f"  removed {cut * 1000:.0f} ms of black from the start")
        except (OSError, subprocess.SubprocessError) as e:
            print(f"  trim failed, the original is kept: {e}")
    if compat:
        dst = lib / "video_h264.mp4"
        print("Making the H.264 compatibility copy (this takes a minute)...")
        try:
            media.make_compat_copy(next(lib.glob("video.*")), dst, cfg)
            print(f"  wrote {dst.name} ({dst.stat().st_size / 1e6:.1f} MB)")
        except (OSError, subprocess.CalledProcessError) as e:
            print(f"  compatibility copy failed: {e}")
    from . import runner

    print("Uploading to native-schedule platforms (others post at their local time via `shortpost run`)...")
    for line in runner.process(cfg, conn, only_native=True):
        print("  " + line)
    return 0


def _cmd_auth(args) -> int:
    from .auth import meta
    from .http import ApiError, Http

    cfg = config.load_config(Path(args.config) if args.config else None)
    http = Http()
    try:
        if args.platform == "facebook":
            tok = getpass.getpass("Facebook user token from Graph API Explorer (hidden): ").strip()

            def choose(pages):
                for i, pg in enumerate(pages, 1):
                    print(f"  {i}. {pg.get('name')} ({pg['id']})")
                return pages[int(input("Which Page? ").strip()) - 1]

            page = meta.auth_facebook(cfg, config.load_secrets(), http, tok, choose)
            print(f"Stored the Page token for {page.get('name')} ({page['id']}).")
            ig = page.get("instagram")
            print(f"Instagram: @{ig.get('username')} ({ig['id']})" if ig else
                  "Instagram: no linked account found; the token needs instagram_basic + instagram_content_publish.")
        elif args.platform == "youtube-browser":
            from playwright.sync_api import sync_playwright

            from . import browser

            browser.launch(cfg)
            print("Chrome opened with the dedicated ShortPost profile. Log in to the Samastano Google account there (including 2FA).")
            input("Press Enter here when YouTube Studio shows the channel dashboard... ")
            with sync_playwright() as pw:
                _, ctx = browser.connect(pw, cfg)
                page = ctx.new_page()
                page.goto(browser.STUDIO, wait_until="domcontentloaded")
                page.wait_for_timeout(4000)
                ok = browser.CHANNEL_ID in page.url and "studio.youtube.com" in page.url
                print(f"Studio check: {'OK' if ok else 'NOT logged in / wrong channel'} ({page.url.split('?')[0]})")
                page.close()
            return 0 if ok else 1
        elif args.platform == "tiktok":
            import webbrowser

            from .auth import tiktok

            tok = tiktok.authorize(http, config.load_secrets(), webbrowser.open)
            print(f"Stored the TikTok token (scope {tok['scope']}).")
        elif args.platform == "threads":
            from .auth import threads

            tok = getpass.getpass("Threads user token from the app dashboard (hidden): ").strip()
            rec = threads.authorize(http, config.load_secrets(), tok)
            print(f"Stored the Threads token for @{rec['username']} ({rec['user_id']}), valid until {rec['expires_at'][:10]}.")
        elif args.platform == "pinterest":
            import webbrowser

            from .auth import pinterest

            secrets = config.load_secrets()
            tok = pinterest.authorize(http, secrets, webbrowser.open)

            def choose(boards):
                for i, b in enumerate(boards, 1):
                    print(f"  {i}. {b.get('name')}")
                n = input("Board number (Enter = create the configured one): ").strip()
                return boards[int(n) - 1] if n else None

            board = pinterest.pick_board(http, tok, cfg["platforms"]["pinterest"]["board"], choose)
            print(f"Stored the Pinterest token; board '{board.get('name')}'. Keep pinterest.enabled=false until Standard access is approved.")
        elif args.platform == "bluesky":
            from .platforms import bluesky

            bluesky._client(config.load_secrets())
            print("Bluesky login OK (handle + app password from secrets).")
        elif args.platform == "instagram":
            print("Instagram uses the Facebook Page token: run `shortpost auth facebook` with instagram_basic + instagram_content_publish.")
            return 2
        else:
            print(f"'auth {args.platform}' is not implemented yet (planned in a later milestone).")
            return 2
    except (ApiError, ValueError, IndexError) as e:
        print(f"auth failed: {e}")
        return 1
    return 0


def _cmd_run(args) -> int:
    from . import db, runner
    from .auth import meta
    from .http import ApiError, Http

    cfg = config.load_config(Path(args.config) if args.config else None)
    http, conn = Http(), db.connect(cfg)
    if args.dry_run:
        print("(dry run: nothing uploaded)")
        return 0
    with runner.run_lock(cfg) as got:
        if not got:
            print("Another run is active; exiting.")
            return 0
        lines = runner.run_once(cfg, conn, http)
    print("\n".join(lines) or "Nothing due.")
    return 0


def _cmd_review(args) -> int:
    """Walk through inbox videos that are not queued yet, with the normal preview and confirm flow of `add`."""
    from . import db, runner

    cfg = config.load_config(Path(args.config) if args.config else None)
    files = runner.inbox_candidates(cfg, db.connect(cfg))
    if not files:
        print("Nothing waiting in the inbox.")
        return 0
    rc = 0
    for f in files:
        print(f"\n=== {f.name} ===")
        ns = argparse.Namespace(file=str(f), date=None, yes=False, only=None, skip=None, dry_run=args.dry_run,
                                config=args.config, verbose=False, command="add")
        rc = max(rc, _cmd_add(ns))
    return rc


def _attach_related(cfg: dict, vi, meta: dict, rendered: dict) -> str | None:
    """Related video for the YouTube Short (D-015); the local model picks among the best word matches."""
    from . import related
    from .ai.llm import ChainLLM

    if "youtube" not in rendered:
        return None
    try:
        return related.attach(cfg, vi, meta, rendered, ChainLLM(cfg))
    except Exception as e:  # noqa: BLE001 - never block adding a video because of this
        return f"related video lookup failed: {type(e).__name__}: {str(e)[:120]}"


def _cmd_ui(args) -> int:
    from .ui import server

    cfg = config.load_config(Path(args.config) if args.config else None)
    return server.serve(cfg, args.port, not args.no_browser)


def _cmd_sync(args) -> int:
    from . import db, sync

    cfg = config.load_config(Path(args.config) if args.config else None)
    conn = db.connect(cfg)
    if cfg["platforms"]["youtube"]["mode"] != "browser":
        print("sync supports YouTube browser mode only for now.")
        return 2
    try:
        lines = sync.run(cfg, conn)
    except Exception as e:  # noqa: BLE001 - Studio/Chrome problems should not traceback at the user
        print(f"sync failed: {type(e).__name__}: {str(e)[:200]}")
        return 1
    print("Days occupied on YouTube (listed date, local time zone):")
    print("\n".join("  " + l for l in lines))
    from . import related

    vids = related.long_videos(cfg, refresh=True)
    print(f"Long videos available as Related video: {len(vids)}")
    return 0


def _with_db(args):
    from . import db

    cfg = config.load_config(Path(args.config) if args.config else None)
    return cfg, db.connect(cfg)


def _cmd_list(args) -> int:
    from . import library

    cfg, conn = _with_db(args)
    return library.list_calendar(cfg, conn, args.days)


def _cmd_show(args) -> int:
    from . import library

    cfg, conn = _with_db(args)
    return library.show_video(cfg, conn, args.id)


def _cmd_cancel(args) -> int:
    from . import library

    _, conn = _with_db(args)
    msgs = library.cancel(conn, args.id, args.platform)
    print("\n".join(msgs) or "Nothing to cancel.")
    return 0


def _cmd_reschedule(args) -> int:
    from . import library

    cfg, conn = _with_db(args)
    msg = library.reschedule(cfg, conn, args.id, args.date)
    print(msg)
    return 1 if msg.startswith(("Refused", "No video")) else 0


def _editable(conn, vid: int, platform: str | None):
    """(video row, platforms whose post has not been uploaded yet, platforms already uploaded)."""
    v = conn.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone()
    if v is None:
        return None, [], []
    posts = conn.execute("SELECT platform, state FROM posts WHERE video_id=?", (vid,)).fetchall()
    open_ = [p["platform"] for p in posts if p["state"] in ("pending", "failed") and (platform in (None, p["platform"]))]
    done = [p["platform"] for p in posts if p["state"] not in ("pending", "failed", "cancelled", "skipped")]
    return v, open_, done


def _cmd_edit(args) -> int:
    """Open meta.json in Notepad, then check it is still valid JSON. Posts already uploaded are not changed remotely."""
    cfg, conn = _with_db(args)
    v, open_, done = _editable(conn, args.id, None)
    if v is None:
        print(f"No video #{args.id}.")
        return 1
    meta = Path(v["meta_path"])
    backup = meta.read_text(encoding="utf-8")
    subprocess.run(["notepad", str(meta)], check=False)
    try:
        doc = json.loads(meta.read_text(encoding="utf-8"))
        for plat in doc["rendered"]:
            doc["rendered"][plat]["text"]
    except (ValueError, KeyError, TypeError) as e:
        meta.write_text(backup, encoding="utf-8")
        print(f"meta.json is not valid ({e}); your edit was reverted.")
        return 1
    if doc["rendered"].get("tiktok"):
        (meta.parent / "tiktok_caption.txt").write_text(doc["rendered"]["tiktok"]["text"], encoding="utf-8")
    print(f"Saved. Will use the new text for: {', '.join(open_) or 'nothing (all uploaded)'}.")
    if done:
        print(f"Warning: already uploaded/scheduled, NOT updated remotely: {', '.join(done)}.")
    return 0


def _cmd_regen(args) -> int:
    """Generate new AI text for the platforms of a video that are not uploaded yet, preview it, save on approval."""
    from . import affiliates, db, media, naming, preview, render, scheduler
    from .ai import metadata

    cfg, conn = _with_db(args)
    v, open_, done = _editable(conn, args.id, args.platform)
    if v is None:
        print(f"No video #{args.id}.")
        return 1
    if not open_:
        print("Nothing to regenerate: every selected platform is already uploaded or scheduled.")
        return 1
    meta_path = Path(v["meta_path"])
    doc = json.loads(meta_path.read_text(encoding="utf-8"))
    series = doc.get("series")
    parsed = naming.Parsed(topic=doc["topic"], series=series, series_key=v["series_key"])
    vi = naming.VideoInput(path=meta_path.parent / "video.mp4", parsed=parsed, notes=v["notes"] or "")
    catalog = affiliates.load_catalog()
    hint = ""
    while True:
        deals = [e.label for e in affiliates.select(catalog, "youtube", vi.parsed.topic + " " + vi.notes, False, vi.link_directive)]
        meta = metadata.generate(cfg, vi, v["duration_s"] or 0, deals, hint)
        rendered = render.render_all(cfg, catalog, vi, meta, set(open_))
        _attach_related(cfg, vi, meta, rendered)
        info = type("I", (), {"duration_s": v["duration_s"] or 0, "width": v["width"], "height": v["height"], "vcodec": "-", "pix_fmt": "-", "size_bytes": 0})()
        day = datetime.fromisoformat(v["slot_date_ny"]).date()
        preview.show(cfg, vi, info, meta, rendered, day, [f"regenerating only: {', '.join(open_)}"])
        if not sys.stdin.isatty():
            print("No terminal: nothing saved.")
            return 1
        choice = input("[Y]es save / [R]egenerate / [C]ancel: ").strip().lower()
        if choice.startswith("r"):
            hint = input("Optional hint for the AI: ").strip()
            continue
        if choice.startswith("y"):
            break
        print("Cancelled.")
        return 1
    for plat in open_:
        if plat in rendered:
            doc["rendered"][plat] = rendered[plat]
    doc["ai"] = {k: meta[k] for k in ("core", "platform", "focus", "source", "model", "seed", "prompts") if k in meta}
    meta_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    if "tiktok" in open_ and "tiktok" in doc["rendered"]:
        (meta_path.parent / "tiktok_caption.txt").write_text(doc["rendered"]["tiktok"]["text"], encoding="utf-8")
    print(f"Saved new text for: {', '.join(open_)}." + (f" Not changed (already uploaded): {', '.join(done)}." if done else ""))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="shortpost", description="Local multi-platform Shorts poster")
    p.add_argument("--config", help="path to a config.toml")
    p.add_argument("--dry-run", action="store_true", help="no uploads or post writes")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    def cmd(name, help_, handler, milestone=None):
        sp = sub.add_parser(name, help=help_)
        # Accept the global flags after the subcommand too (SUPPRESS keeps a value given before it).
        sp.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        sp.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        sp.add_argument("--config", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        sp.set_defaults(func=handler or _stub(milestone))
        return sp

    cmd("doctor", "check the environment", _cmd_doctor)
    cmd("auth", "one-time login for a platform", _cmd_auth).add_argument("platform", choices=PLATFORMS)
    ss = cmd("set-secret", "store a secret in %APPDATA%\\ShortPost\\secrets.toml", _cmd_set_secret)
    ss.add_argument("key")
    ss.add_argument("--generate", action="store_true", help="generate a random value (ntfy_topic)")
    a = cmd("add", "add a video: metadata, preview, schedule", _cmd_add)
    a.add_argument("file")
    a.add_argument("--date", type=date.fromisoformat)
    a.add_argument("--yes", action="store_true")
    a.add_argument("--only")
    a.add_argument("--skip")
    cmd("review", "review videos waiting in the inbox", _cmd_review)
    r = cmd("run", "background loop (Task Scheduler)", _cmd_run)
    r.add_argument("--once", action="store_true")
    cmd("list", "calendar of queued posts", _cmd_list).add_argument("--days", type=int, default=14)
    cmd("show", "details for one video", _cmd_show).add_argument("id", type=int)
    g = cmd("regen", "regenerate AI text", _cmd_regen)
    g.add_argument("id", type=int)
    g.add_argument("--platform")
    cmd("edit", "edit meta.json", _cmd_edit).add_argument("id", type=int)
    rs = cmd("reschedule", "move to another day", _cmd_reschedule)
    rs.add_argument("id", type=int)
    rs.add_argument("date", type=date.fromisoformat)
    c = cmd("cancel", "cancel pending posts", _cmd_cancel)
    c.add_argument("id", type=int)
    c.add_argument("--platform")
    cmd("sync", "pull occupied days from YouTube", _cmd_sync)
    u = cmd("ui", "local web UI on 127.0.0.1 (review, queue, failures, log)", _cmd_ui)
    u.add_argument("--port", type=int, default=8780)
    u.add_argument("--no-browser", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # captions contain emoji; piped Windows consoles default to cp1252
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    try:
        cfg = config.load_config(Path(args.config) if args.config else None)
        log_dir = config.resolve(cfg["general"]["log_dir"])
        setup_logging(log_dir / f"shortpost-{date.today().isoformat()}.log", args.verbose)
        prune_logs(log_dir)
    except (OSError, KeyError) as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
