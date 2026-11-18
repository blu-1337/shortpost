from datetime import date

from shortpost import db, sync


def _cfg(tmp_path):
    return {"general": {"db_path": str(tmp_path / "t.sqlite3"), "target_tz": "America/New_York"}}


def test_parse_row():
    assert sync.parse_row("0:39 | title | Scheduled | Nov 7, 2026 | Scheduled | 0") == (date(2026, 11, 7), "scheduled")
    assert sync.parse_row("x Public Nov 3, 2026 Published 45") == (date(2026, 11, 3), "published")
    assert sync.parse_row("0:11 title Draft Edit draft") is None


def test_run_skips_past_drafts_and_our_own(tmp_path):
    cfg = _cfg(tmp_path)
    conn = db.connect(cfg)
    conn.execute("INSERT INTO videos(sha1,src_path,topic,state,created_at,updated_at) VALUES('a','p','t','approved','x','x')")
    conn.execute("INSERT INTO posts(video_id,platform,mode,fire_at_utc,jitter_min,state,remote_id,updated_at) VALUES(1,'youtube','native','x',0,'scheduled','OURS_______',  'x')")
    rows = [
        {"id": "AAAAAAAAAAA", "title": "manual", "text": "Scheduled Nov 7, 2026 Scheduled"},
        {"id": "OURS_______", "title": "ours", "text": "Scheduled Nov 12, 2026 Scheduled"},
        {"id": "BBBBBBBBBBB", "title": "old", "text": "Public Nov 3, 2026 Published"},
        {"id": "CCCCCCCCCCC", "title": "draft", "text": "Draft Edit draft"},
    ]
    sync.run(cfg, conn, rows, today=date(2026, 11, 4))
    assert [r[0] for r in conn.execute("SELECT date_ny FROM remote_occupied")] == ["2026-11-07"]
    assert date(2026, 11, 7) in db.occupied_dates(conn, {"schedule": {"respect_youtube_schedule": True}})
