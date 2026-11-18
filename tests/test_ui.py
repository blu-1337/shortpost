import copy
import json
import threading
import urllib.error
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from shortpost import config, pipeline
from shortpost.naming import parse_stem
from shortpost.ui import server

BASE = config.load_config(user_dir=Path("/nonexistent"))


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    cfg = copy.deepcopy(BASE)
    cfg["general"]["db_path"] = str(tmp_path / "db.sqlite3")
    cfg["general"]["library_dir"] = str(tmp_path / "library")
    cfg["general"]["watch_dirs"] = [str(tmp_path / "inbox")]
    app = server.App(cfg)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), None)
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = server.make_handler(app, port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield app, port
    httpd.shutdown()


def call(port, path, body=None, headers=None, host=None):
    h = dict(headers or {})
    if host:
        h["Host"] = host
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=None if body is None else json.dumps(body).encode(), headers=h,
                                 method="GET" if body is None else "POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_state_and_guards(srv):
    app, port = srv
    code, st = call(port, "/api/state")
    assert code == 200 and st["videos"] == [] and st["inbox"] == []
    assert call(port, "/api/state", host="evil.example.com")[0] == 403          # DNS rebinding
    assert call(port, "/api/draft/zzzz/discard", {})[0] == 403                  # POST needs X-ShortPost
    assert call(port, "/api/draft/zzzz/discard", {}, {"X-ShortPost": "1"})[0] == 200
    assert call(port, "/api/draft", {"path": "C:/Windows/notepad.exe"}, {"X-ShortPost": "1"})[0] == 400  # only inbox files


def test_fix_product_and_edit(srv):
    app, port = srv
    vi = SimpleNamespace(parsed=parse_stem("vatrical-plugin"), notes="", path=Path("x.mp4"))
    d = pipeline.Draft(Path("x.mp4"), vi, SimpleNamespace(duration_s=10, width=1080, height=1920, vcodec="h264", pix_fmt="yuv420p", size_bytes=1),
                       date(2026, 11, 8), "sha", False, [], [])
    d.meta = {"seed": 1, "core": {"products": ["Vatrical"]}}
    d.rendered = {"youtube": {"title": "How to use Vatrical", "text": "vatrical rocks #x", "tags": ["vatrical plugin"]},
                  "bluesky": {"text": "VATRICAL!"}}
    app.drafts["d1"] = d
    h = {"X-ShortPost": "1"}
    code, r = call(port, "/api/draft/d1/product", {"old": "Vatrical", "new": "Vatrica"}, h)
    assert code == 200 and r["replaced"] == 5
    assert d.rendered["youtube"]["title"] == "How to use Vatrica" and d.rendered["bluesky"]["text"] == "Vatrica!"
    assert d.meta["core"]["products"] == ["Vatrica"]
    assert call(port, "/api/draft/d1/edit", {"platform": "bluesky", "field": "text", "value": "hi"}, h)[0] == 200
    assert d.rendered["bluesky"]["text"] == "hi"
    assert call(port, "/api/draft/d1/edit", {"platform": "bluesky", "field": "link", "value": "x"}, h)[0] == 400


def test_second_draft_of_same_file_refused(srv, tmp_path):
    app, port = srv
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    f = inbox / "a-plugin.mp4"
    f.write_bytes(b"x" * 10)
    import os, time
    os.utime(f, (time.time() - 3600,) * 2)
    vi = SimpleNamespace(parsed=parse_stem("a-plugin"), notes="", path=f)
    app.drafts["d1"] = pipeline.Draft(f, vi, None, date(2026, 11, 8), "sha", False, [], [])
    code, r = call(port, "/api/draft", {"path": str(f)}, {"X-ShortPost": "1"})
    assert code == 400 and "already has a draft" in r["error"]


def test_sync_job_and_reslot(srv, monkeypatch):
    app, port = srv
    monkeypatch.setattr(pipeline, "sync_youtube", lambda cfg, conn, say=print: ["2026-11-09 x"])
    h = {"X-ShortPost": "1"}
    code, r = call(port, "/api/sync", {}, h)
    assert code == 200
    import time
    for _ in range(50):
        _, j = call(port, "/api/job/" + r["job"])
        if j["state"] != "running":
            break
        time.sleep(0.1)
    assert j["state"] == "done" and j["result"] == ["2026-11-09 x"] and app.synced


def test_tiktok_settings_validated(srv, monkeypatch):
    app, port = srv
    monkeypatch.setattr(app, "tiktok_creator", lambda: {"privacy_level_options": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"], "duet_disabled": True})
    vi = SimpleNamespace(parsed=parse_stem("x-plugin"), notes="", path=Path("x.mp4"))
    d = pipeline.Draft(Path("x.mp4"), vi, None, date(2026, 11, 8), "sha", False, [], [])
    d.rendered = {"tiktok": {"text": "t"}}
    app.drafts["d1"] = d
    h = {"X-ShortPost": "1"}
    ok = {"privacy": "PUBLIC_TO_EVERYONE", "comment": True, "consented": True}
    assert call(port, "/api/draft/d1/tiktok", {**ok, "consented": False}, h)[0] == 400       # needs consent
    assert call(port, "/api/draft/d1/tiktok", {**ok, "privacy": None}, h)[0] == 400          # no default privacy
    assert call(port, "/api/draft/d1/tiktok", {**ok, "duet": True}, h)[0] == 400             # account disables duet
    assert call(port, "/api/draft/d1/tiktok", {**ok, "disclose": True}, h)[0] == 400         # disclosure with nothing ticked
    assert call(port, "/api/draft/d1/tiktok", ok, h)[0] == 200
    assert d.rendered["tiktok"]["direct"]["privacy"] == "PUBLIC_TO_EVERYONE" and d.rendered["tiktok"]["direct"]["duet"] is False
    assert call(port, "/api/draft/d1/tiktok", {"clear": True}, h)[0] == 200 and "direct" not in d.rendered["tiktok"]


def test_remove_plan_and_remove(srv, monkeypatch):
    from shortpost import db, remove
    app, port = srv
    conn = app.conn()
    now = db.now_iso()
    with conn:
        conn.execute("INSERT INTO videos(id,sha1,src_path,topic,slot_date_ny,state,created_at,updated_at) VALUES (9,'abc','x','t','2026-12-01','approved',?,?)", (now, now))
        for i, (plat, st) in enumerate([("facebook", "scheduled"), ("instagram", "published"), ("bluesky", "pending"), ("youtube", "uploading")]):
            conn.execute("INSERT INTO posts(video_id,platform,mode,fire_at_utc,jitter_min,state,remote_id,remote_url,updated_at) VALUES (9,?,?,?,0,?,?,?,?)",
                         (plat, "native", now, st, f"r{i}", f"https://x/{i}", now))
    h = {"X-ShortPost": "1"}
    plan = call(port, "/api/video/9/removeplan", {}, h)[1]["lines"]
    assert any("facebook: DELETE" in l for l in plan) and any("instagram: cannot be deleted" in l for l in plan)
    deleted = []
    monkeypatch.setattr(remove, "_fb_delete", lambda cfg, http, rid: deleted.append(rid))
    msgs = remove.remove_video(app.cfg, conn, 9)
    assert deleted == ["r0"]
    states = {p["platform"]: p["state"] for p in db.posts_of(conn, 9)}
    assert states == {"facebook": "cancelled", "instagram": "cancelled", "bluesky": "cancelled", "youtube": "uploading"}
    assert any("upload still running" in m for m in msgs)
    assert db.get_video(conn, 9)["state"] != "cancelled"            # youtube still pending: video stays


def test_failed_delete_keeps_post(srv, monkeypatch):
    from shortpost import db, remove
    app, port = srv
    conn = app.conn()
    now = db.now_iso()
    with conn:
        conn.execute("INSERT INTO videos(id,sha1,src_path,topic,slot_date_ny,state,created_at,updated_at) VALUES (8,'def','x','t','2026-12-02','approved',?,?)", (now, now))
        conn.execute("INSERT INTO posts(video_id,platform,mode,fire_at_utc,jitter_min,state,remote_id,updated_at) VALUES (8,'facebook','native',?,0,'scheduled','r',?)", (now, now))

    def boom(*a):
        raise RuntimeError("nope")
    monkeypatch.setattr(remove, "_fb_delete", boom)
    msgs = remove.remove_video(app.cfg, conn, 8)
    assert "FAILED" in msgs[0] and db.posts_of(conn, 8)[0]["state"] == "scheduled"
    monkeypatch.setattr(remove, "_fb_delete", lambda *a: None)
    remove.remove_video(app.cfg, conn, 8)
    v = db.get_video(conn, 8)
    assert v["state"] == "cancelled" and v["sha1"].endswith(":removed:8")   # day freed, file can be added again


def test_commit_post_now_sets_due_times(srv, monkeypatch):
    from datetime import date
    from shortpost import db, library, runner
    app, _ = srv
    conn = app.conn()
    now = db.now_iso()
    with conn:
        conn.execute("INSERT INTO videos(id,sha1,src_path,topic,slot_date_ny,state,created_at,updated_at) VALUES (20,'n','x','t','2099-01-01','approved',?,?)", (now, now))
        for plat, mode in (("youtube", "native"), ("facebook", "native"), ("instagram", "local_time")):
            conn.execute("INSERT INTO posts(video_id,platform,mode,fire_at_utc,jitter_min,state,updated_at) VALUES (20,?,?,'2099-01-01T00:00:00+00:00',0,'pending',?)", (plat, mode, now))
    monkeypatch.setattr(library, "save_video", lambda *a, **k: (20, Path("lib")))
    ran = []
    monkeypatch.setattr(runner, "process", lambda cfg, conn, **k: ran.append(k) or [])
    d = pipeline.Draft(Path("x.mp4"), None, None, date(2099, 1, 1), "n", False, [], [], meta={"seed": 1}, rendered={"youtube": {}, "facebook": {}, "instagram": {}})
    pipeline.commit(app.cfg, conn, d, lambda s: None, post_now=True)
    rows = {r["platform"]: r for r in db.posts_of(conn, 20)}
    assert rows["facebook"]["mode"] == "local_time"
    assert rows["instagram"]["fire_at_utc"] < "2099"
    assert rows["youtube"]["mode"] == "native" and rows["youtube"]["fire_at_utc"] < "2099"
    assert ran == [{}]


def test_serial_jobs_run_one_after_another(srv):
    import time
    app, _ = srv
    order = []

    def mk(name, delay):
        def fn(say):
            order.append(name + ":start"); time.sleep(delay); order.append(name + ":end")
        return fn

    a = app.start_job("A", mk("a", 0.3), serial=app.approve_lock)
    time.sleep(0.05)
    b = app.start_job("B", mk("b", 0.0), serial=app.approve_lock)
    for _ in range(100):
        if all(j["state"] != "running" for j in app.jobs.values()):
            break
        time.sleep(0.05)
    assert order == ["a:start", "a:end", "b:start", "b:end"]
    assert "queued" in app.jobs[b]["log"][0]


def _mk_draft(app, tmp_path, monkeypatch):
    f = tmp_path / "inbox" / "a-plugin.mp4"
    f.parent.mkdir(exist_ok=True)
    f.write_bytes(b"x" * 10)
    vi = SimpleNamespace(parsed=parse_stem("a-plugin"), notes="", path=f)
    info = SimpleNamespace(duration_s=10, width=1080, height=1920, vcodec="h264", pix_fmt="yuv420p", size_bytes=1)
    d = pipeline.Draft(f, vi, info, date(2026, 11, 20), "sha-a", False, ["w"], [], only={"youtube"}, skip={"pinterest"})
    d.meta = {"seed": 1, "core": {"products": ["A"]}}
    d.rendered = {"youtube": {"title": "T ünï", "text": "hello", "tags": ["a"]}}
    app.drafts["d1"] = d
    from shortpost import media, naming
    monkeypatch.setattr(media, "probe", lambda p: info)
    monkeypatch.setattr(naming, "load_video", lambda p, detect=True: vi)
    return d, f


def test_preapprove_survives_restart(srv, tmp_path, monkeypatch):
    app, port = srv
    d, f = _mk_draft(app, tmp_path, monkeypatch)
    h = {"X-ShortPost": "1"}
    assert call(port, "/api/draft/d1/edit", {"platform": "youtube", "field": "text", "value": "edited"}, h)[0] == 200
    assert call(port, "/api/draft/d1/preapprove", {}, h)[0] == 200
    assert (app.draft_dir() / "d1.json").is_file()
    app2 = server.App(app.cfg)                       # "restart"
    d2 = app2.drafts["d1"]
    assert d2.pre and d2.rendered["youtube"]["text"] == "edited" and d2.day == date(2026, 11, 20)
    assert d2.only == {"youtube"} and d2.skip == {"pinterest"} and d2.sha1 == "sha-a" and d2.warnings == ["w"]
    st = app2.queue()
    assert st["drafts"][0]["pre"] is True and st["notes"] == []
    app2.discard("d1")
    assert not (app.draft_dir() / "d1.json").exists() and "d1" not in app2.drafts


def test_missing_video_or_corrupt_file_kept_and_reported(srv, tmp_path, monkeypatch):
    app, port = srv
    d, f = _mk_draft(app, tmp_path, monkeypatch)
    app.save_draft("d1")
    (app.draft_dir() / "bad.json").write_text("{not json", encoding="utf-8")
    f.unlink()
    app2 = server.App(app.cfg)
    assert app2.drafts == {} and len(app2.load_notes) == 2
    assert (app.draft_dir() / "d1.json").exists() and (app.draft_dir() / "bad.json").exists()


def test_approve_deletes_saved_file_and_restores_on_failure(srv, tmp_path, monkeypatch):
    app, port = srv
    d, f = _mk_draft(app, tmp_path, monkeypatch)
    app.save_draft("d1")
    import time
    monkeypatch.setattr(pipeline, "commit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    jid = app.approve("d1")
    for _ in range(100):
        if app.jobs[jid]["state"] != "running":
            break
        time.sleep(0.05)
    assert app.jobs[jid]["state"] == "error" and "d1" in app.drafts and (app.draft_dir() / "d1.json").exists()
    monkeypatch.setattr(pipeline, "commit", lambda *a, **k: (7, tmp_path))
    jid = app.approve("d1")
    for _ in range(100):
        if app.jobs[jid]["state"] != "running":
            break
        time.sleep(0.05)
    assert app.jobs[jid]["result"] == 7 and "d1" not in app.drafts and not (app.draft_dir() / "d1.json").exists()


def test_handles_sets():
    from shortpost import handles

    spec = handles.spec_of({"youtube": "@manleylabs @pluginalliance, dirkulrich21\n@manleylabs", "facebook": "", "instagram": "@a"})
    assert spec["youtube"] == ["@manleylabs", "@pluginalliance", "@dirkulrich21"] and spec["facebook"] == []
    r = {"youtube": {"text": "Hello\n\n#a #b"}, "facebook": {"text": "Hi"}, "instagram": {"text": "x" * 2199}}
    rep = handles.apply(r, spec)
    assert r["youtube"]["text"].endswith("\n\nWith @manleylabs @pluginalliance @dirkulrich21")
    assert "facebook" not in rep and "!instagram" in rep and r["instagram"]["text"] == "x" * 2199
    assert handles.apply(r, spec).get("youtube") is None  # already there
    items = handles.upsert([], {"name": "Set", "youtube": "Google"})
    items = handles.upsert(items, {"name": "set", "youtube": "@Google @Other"})
    assert len(items) == 1 and items[0]["youtube"] == ["@Google", "@Other"]


def test_handles_tiktok_and_duplicate_error():
    from shortpost import handles

    r = {"tiktok": {"text": "cap #a"}, "youtube": {"text": "d"}}
    spec = handles.spec_of({"tiktok": "@x @y", "youtube": "@x"})
    assert not handles.already_there(r, spec)
    handles.apply(r, spec)
    assert r["tiktok"]["text"].endswith("With @x @y")
    assert handles.already_there(r, spec)
    assert not handles.already_there(r, handles.spec_of({"tiktok": "@x @z"}))


def test_video_file_served_with_range(srv, tmp_path):
    app, port = srv
    lib = tmp_path / "library" / "x"
    lib.mkdir(parents=True)
    (lib / "video.mp4").write_bytes(bytes(range(100)))
    conn = app.conn()
    conn.execute("INSERT INTO videos (sha1, src_path, lib_dir, topic, state, created_at, updated_at) VALUES ('s','p',?, 't', 'approved', '', '')", (str(lib),))
    conn.commit()
    vid = conn.execute("SELECT id FROM videos").fetchone()["id"]
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/video/{vid}/file", headers={"Range": "bytes=10-19"})
    with urllib.request.urlopen(req) as r:
        assert r.status == 206 and r.read() == bytes(range(10, 20)) and r.headers["Content-Range"] == "bytes 10-19/100"
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/video/{vid}/file") as r:
        assert r.status == 200 and len(r.read()) == 100
    assert call(port, "/api/video/9999/file")[0] == 404
