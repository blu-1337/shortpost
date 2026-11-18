import os
import time
from datetime import datetime, timedelta, timezone

from shortpost import db, log, notify, runner


def _cfg(tmp_path):
    return {"general": {"db_path": str(tmp_path / "data" / "x.sqlite3"), "watch_dirs": [str(tmp_path / "inbox")],
                        "video_exts": [".mp4", ".mov"]}}


def test_lock_exclusive_and_released(tmp_path):
    cfg = _cfg(tmp_path)
    with runner.run_lock(cfg) as a:
        assert a is True
        with runner.run_lock(cfg) as b:
            assert b is False
    with runner.run_lock(cfg) as c:
        assert c is True
    assert not (tmp_path / "data" / "run.lock").exists()


def test_stale_lock_is_replaced(tmp_path):
    cfg = _cfg(tmp_path)
    lock = tmp_path / "data" / "run.lock"
    lock.parent.mkdir()
    lock.write_text("1")
    old = time.time() - 3 * 3600
    os.utime(lock, (old, old))
    with runner.run_lock(cfg) as got:
        assert got is True


def test_inbox_candidates_skip_unstable_variant_and_queued(tmp_path):
    cfg = _cfg(tmp_path)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    old = time.time() - 120
    for name in ("a.mp4", "a_facebook.mp4", "b.mp4", "notes.txt"):
        f = inbox / name
        f.write_bytes(b"x" * 10 + name.encode())
        os.utime(f, (old, old))
    (inbox / "fresh.mp4").write_bytes(b"new")  # modified just now: still copying
    conn = db.connect({"general": {"db_path": cfg["general"]["db_path"]}})
    names = [f.name for f in runner.inbox_candidates(cfg, conn)]
    assert names == ["a.mp4", "b.mp4"]
    conn.execute("INSERT INTO videos (sha1, src_path, topic, state, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                 (db.sha1_of(inbox / "a.mp4"), "a", "a", "approved", "t", "t"))
    conn.commit()
    assert [f.name for f in runner.inbox_candidates(cfg, conn)] == ["b.mp4"]


def test_notify_once_per_file(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    f = tmp_path / "inbox" / "a.mp4"
    f.parent.mkdir()
    f.write_bytes(b"abc")
    sent = []
    monkeypatch.setattr(notify, "send", lambda cfg, title, body, secrets=None, **k: sent.append((title, body)) or True)
    assert runner.notify_new_videos(cfg, None, [f]) == 1
    assert runner.notify_new_videos(cfg, None, [f]) == 0
    assert len(sent) == 1 and "a.mp4" in sent[0][1] and sent[0][0].isascii()


def test_prune_logs(tmp_path):
    old, new = tmp_path / "shortpost-2026-01-01.log", tmp_path / "shortpost-2026-11-04.log"
    for f in (old, new):
        f.write_text("x")
    t = time.time() - 40 * 86400
    os.utime(old, (t, t))
    assert log.prune_logs(tmp_path, 30) == 1 and not old.exists() and new.exists()
