import copy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from shortpost import config, db, library, scheduler
from shortpost.naming import VideoInput, parse_stem

BASE = config.load_config(user_dir=Path("/nonexistent"))
NOW = datetime(2026, 11, 3, 20, 0, tzinfo=timezone.utc)
INFO = SimpleNamespace(duration_s=35.0, width=1080, height=1920)


def make_cfg(tmp_path):
    cfg = copy.deepcopy(BASE)
    cfg["general"]["db_path"] = str(tmp_path / "db.sqlite3")
    cfg["general"]["library_dir"] = str(tmp_path / "library")
    return cfg


def add(cfg, conn, tmp_path, stem, day, sha):
    f = tmp_path / "src" / f"{stem}.mp4"
    f.parent.mkdir(exist_ok=True)
    f.write_bytes(b"x")
    vi = VideoInput(f, parse_stem(stem))
    rendered = {p: {"text": "t"} for p in ("youtube", "facebook", "instagram", "tiktok", "threads")}
    jit = scheduler.plan_jitters(cfg, list(rendered), 7)
    meta = {"core": {}, "platform": {}, "focus": {}, "source": "ai", "model": "m", "seed": 7, "prompts": {}}
    return library.save_video(cfg, conn, vi, INFO, meta, rendered, day, jit, sha)


def test_save_and_list(tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    conn = db.connect(cfg)
    vid, lib = add(cfg, conn, tmp_path, "free-plugin", date(2026, 11, 4), "a")
    assert (lib / "meta.json").is_file() and (lib / "video.mp4").is_file()
    assert (lib / "tiktok_caption.txt").read_text() == "t"  # copied, not moved: source is outside inbox
    assert (tmp_path / "src" / "free-plugin.mp4").exists()
    assert len(db.posts_of(conn, vid)) == 5
    assert db.occupied_dates(conn, cfg) == {date(2026, 11, 4)}
    assert library.show_video(cfg, conn, vid) == 0
    assert library.show_video(cfg, conn, 99) == 1
    assert library.list_calendar(cfg, conn, 14) == 0


def test_jitters_reproducible():
    a = scheduler.plan_jitters(BASE, ["youtube", "instagram"], 5)
    assert a == scheduler.plan_jitters(BASE, ["youtube", "instagram"], 5)
    assert abs(a["youtube"]) <= 7 and abs(a["instagram"]) <= 10


def test_second_video_next_day(tmp_path):
    cfg = make_cfg(tmp_path)
    conn = db.connect(cfg)
    add(cfg, conn, tmp_path, "one", date(2026, 11, 4), "a")
    assert scheduler.find_slot(cfg, NOW, db.occupied_dates(conn, cfg)) == date(2026, 11, 5)


def test_series_not_before(tmp_path):
    cfg = make_cfg(tmp_path)
    conn = db.connect(cfg)
    add(cfg, conn, tmp_path, "thing-part-1-of-3", date(2026, 11, 10), "a")
    key = parse_stem("thing-part-2-of-3").series_key
    nb = db.last_series_slot(conn, key, 2)
    assert nb == date(2026, 11, 10)
    assert scheduler.find_slot(cfg, NOW, db.occupied_dates(conn, cfg), None, nb) == date(2026, 11, 11)
    assert db.last_series_slot(conn, key, 1) is None


def test_cancel(tmp_path):
    cfg = make_cfg(tmp_path)
    conn = db.connect(cfg)
    vid, _ = add(cfg, conn, tmp_path, "one", date(2026, 11, 4), "a")
    conn.execute("UPDATE posts SET state='scheduled' WHERE platform='youtube'")
    msgs = library.cancel(conn, vid, "youtube")
    assert "already scheduled" in msgs[0]
    library.cancel(conn, vid)
    states = {p["platform"]: p["state"] for p in db.posts_of(conn, vid)}
    assert states["youtube"] == "scheduled" and states["instagram"] == "cancelled"
    conn.execute("UPDATE posts SET state='cancelled' WHERE platform='youtube'")
    library.cancel(conn, vid)
    assert db.get_video(conn, vid)["state"] == "cancelled"
    assert db.occupied_dates(conn, cfg) == set()


def test_reschedule(tmp_path):
    cfg = make_cfg(tmp_path)
    conn = db.connect(cfg)
    v1, _ = add(cfg, conn, tmp_path, "one", date(2026, 11, 4), "a")
    add(cfg, conn, tmp_path, "two", date(2026, 11, 5), "b")
    assert library.reschedule(cfg, conn, v1, date(2026, 11, 5), NOW).startswith("Refused")  # occupied
    assert library.reschedule(cfg, conn, v1, date(2026, 11, 3), NOW).startswith("Refused")  # too soon
    assert library.reschedule(cfg, conn, 99, date(2026, 11, 9), NOW).startswith("No video")
    assert library.reschedule(cfg, conn, v1, date(2026, 11, 9), NOW).startswith("Moved")
    yt = next(p for p in db.posts_of(conn, v1) if p["platform"] == "youtube")
    assert yt["fire_at_utc"] == scheduler.fire_time(cfg, "youtube", date(2026, 11, 9), yt["jitter_min"]).isoformat(timespec="seconds")
    conn.execute("UPDATE posts SET state='scheduled' WHERE video_id=?", (v1,))
    assert library.reschedule(cfg, conn, v1, date(2026, 11, 12), NOW).startswith("Refused")


def test_dst_mismatch_weeks():
    cfg = BASE
    # EU left DST on 2026-10-25, US still on DST until 11-01: ET->Bucharest offset is 6 h, so 18:30 ET = 00:30 local.
    for d in (date(2026, 10, 26), date(2026, 10, 31)):
        t = scheduler.fire_time(cfg, "facebook", d, 0).astimezone(scheduler.ZoneInfo("Europe/Bucharest"))
        assert (t.hour, t.minute) == (0, 30) and t.date() == d + timedelta(days=1)
    # Normal 7 h gap after 11-01 -> 01:30
    t = scheduler.fire_time(cfg, "facebook", date(2026, 11, 3), 0).astimezone(scheduler.ZoneInfo("Europe/Bucharest"))
    assert (t.hour, t.minute) == (1, 30)
    # Spring 2027: US DST from 03-14, EU from 03-28
    t = scheduler.fire_time(cfg, "facebook", date(2027, 3, 20), 0).astimezone(scheduler.ZoneInfo("Europe/Bucharest"))
    assert (t.hour, t.minute) == (0, 30)
    t = scheduler.fire_time(cfg, "facebook", date(2027, 3, 29), 0).astimezone(scheduler.ZoneInfo("Europe/Bucharest"))
    assert (t.hour, t.minute) == (1, 30)
