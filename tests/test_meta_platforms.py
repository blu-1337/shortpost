import copy
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from shortpost import config, db, library, runner, scheduler, tokens
from shortpost.auth import meta
from shortpost.http import ApiError, Http
from shortpost.naming import VideoInput, parse_stem

BASE = config.load_config(user_dir=Path("/nonexistent"))
INFO = SimpleNamespace(duration_s=35.0, width=1080, height=1920)


@pytest.fixture(autouse=True)
def appdata(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))


def make_http(routes, log):
    """routes: list of (substring, json) matched against 'METHOD url'; first match wins."""
    def handler(request: httpx.Request):
        request.read()
        key = f"{request.method} {request.url.path}"
        log.append((key, dict(request.headers), request.content))
        for sub, body in routes:
            if sub in key:
                return httpx.Response(body[0], json=body[1]) if isinstance(body, tuple) else httpx.Response(200, json=body)
        return httpx.Response(404, json={"error": {"message": "no route " + key}})
    return Http(httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)


def queue(tmp_path, platforms=("facebook", "instagram"), day=date(2026, 11, 4)):
    cfg = copy.deepcopy(BASE)
    cfg["general"]["db_path"] = str(tmp_path / "db.sqlite3")
    cfg["general"]["library_dir"] = str(tmp_path / "library")
    cfg["stories"]["enabled"] = False  # story tests switch it on
    f = tmp_path / "src" / "free-plugin.mp4"
    f.parent.mkdir(exist_ok=True)
    f.write_bytes(b"x" * 1000)
    conn = db.connect(cfg)
    rendered = {p: {"text": f"caption {p}", "comment": "🎁 link"} for p in platforms}
    jit = {p: 0 for p in platforms}
    meta_doc = {"core": {}, "platform": {}, "focus": {}, "source": "ai", "model": "m", "seed": 1, "prompts": {}}
    vid, lib = library.save_video(cfg, conn, VideoInput(f, parse_stem("free-plugin")), INFO, meta_doc, rendered, day, jit, "sha")
    return cfg, conn, vid, lib


NOW = datetime(2026, 11, 4, 18, 30, tzinfo=timezone.utc)  # after IG's 20:00 Bucharest = 18:00 UTC


def test_instagram_flow(tmp_path):
    cfg, conn, vid, lib = queue(tmp_path, ("instagram",))
    tokens.save("facebook", {"page_id": "PG", "ig_user_id": "17", "access_token": "IGTOKEN"})
    log = []
    http = make_http([
        ("/17/media_publish", {"id": "M1"}), ("/17/media", {"id": "C1"}), ("ig-api-upload", {"success": True}),
        ("/C1", {"status_code": "FINISHED"}), ("/M1", {"permalink": "https://instagram.com/reel/abc", "media_url": "https://cdn/x.mp4"}),
    ], log)
    out = runner.process(cfg, conn, http, NOW)
    assert out == ["instagram: published https://instagram.com/reel/abc"]
    up = next(h for k, h, c in log if "ig-api-upload" in k)
    assert up["authorization"] == "OAuth IGTOKEN" and up["offset"] == "0" and up["file_size"] == "1000"
    create = next(c for k, h, c in log if k.endswith("/17/media")).decode()
    assert "upload_type=resumable" in create and "caption=caption+instagram" in create
    post = db.posts_of(conn, vid)[0]
    assert post["state"] == "published" and json.loads(post["extra_json"])["media_url"] == "https://cdn/x.mp4"
    assert db.get_video(conn, vid)["state"] == "done"


def test_not_due_and_missed(tmp_path):
    cfg, conn, vid, lib = queue(tmp_path, ("instagram",))
    tokens.save("facebook", {"page_id": "PG", "ig_user_id": "17", "access_token": "T"})
    assert runner.process(cfg, conn, make_http([], []), NOW - timedelta(hours=3)) == []  # before 20:00 local
    out = runner.process(cfg, conn, make_http([], []), NOW + timedelta(hours=30))
    assert out == ["instagram: missed"]
    assert db.posts_of(conn, vid)[0]["state"] == "missed"


def test_no_token_keeps_pending(tmp_path):
    cfg, conn, vid, _ = queue(tmp_path, ("instagram",))
    out = runner.process(cfg, conn, make_http([], []), NOW)
    assert "auth facebook" in out[0]
    assert db.posts_of(conn, vid)[0]["state"] == "pending" and db.posts_of(conn, vid)[0]["attempts"] == 0


def test_failure_retries_with_backoff(tmp_path):
    cfg, conn, vid, _ = queue(tmp_path, ("instagram",))
    tokens.save("facebook", {"page_id": "PG", "ig_user_id": "17", "access_token": "T"})
    out = runner.process(cfg, conn, make_http([("/17/media", (400, {"error": {"message": "bad caption"}}))], []), NOW)
    assert "bad caption" in out[0]
    p = db.posts_of(conn, vid)[0]
    assert p["state"] == "failed" and p["attempts"] == 1 and p["next_attempt_at_utc"]
    assert runner.process(cfg, conn, make_http([], []), NOW) == []  # backoff not over yet
    later = datetime.fromisoformat(p["next_attempt_at_utc"]) + timedelta(minutes=1)
    http = make_http([("/17/media_publish", {"id": "M"}), ("/17/media", {"id": "C"}), ("ig-api-upload", {}),
                      ("/C", {"status_code": "FINISHED"}), ("/M", {"permalink": "u"})], [])
    assert runner.process(cfg, conn, http, later)[0].startswith("instagram: published")


def test_facebook_native_schedule_and_first_comment(tmp_path):
    cfg, conn, vid, lib = queue(tmp_path, ("facebook",))
    cfg["platforms"]["facebook"]["first_comment_link"] = True
    tokens.save("facebook", {"page_id": "PG", "access_token": "PGTOKEN"})
    log = []
    http = make_http([
        ("/PG/video_reels", {"video_id": "V1", "upload_url": "https://rupload.facebook.com/video-upload/v26.0/V1"}),
        ("video-upload", {"success": True}),
    ], log)
    now = datetime(2026, 11, 3, 20, 0, tzinfo=timezone.utc)
    out = runner.process(cfg, conn, http, now)
    assert out[0].startswith("facebook: scheduled")
    finish = [c.decode() for k, h, c in log if k.endswith("/PG/video_reels")][-1]
    assert "video_state=SCHEDULED" in finish and "scheduled_publish_time=" in finish and "upload_phase=finish" in finish
    up = next(h for k, h, c in log if "video-upload" in k)
    assert up["authorization"] == "OAuth PGTOKEN"
    p = db.posts_of(conn, vid)[0]
    assert p["state"] == "scheduled" and db.get_video(conn, vid)["state"] == "approved"
    # before go-live nothing happens; after go-live the first comment is posted once
    assert runner.process(cfg, conn, make_http([], []), now) == []
    live = datetime.fromisoformat(p["fire_at_utc"]) + timedelta(minutes=2)
    log2 = []
    http2 = make_http([("/V1/comments", {}), ("/PG_1/comments", {"id": "c"}),
                       ("/V1", {"post_id": "PG_1", "status": {"video_status": "ready"}})], log2)
    assert runner.process(cfg, conn, http2, live) == ["facebook: first comment posted"]
    assert any("PG_1/comments" in k for k, _, _ in log2)
    assert runner.process(cfg, conn, make_http([], []), live) == []
    assert db.get_video(conn, vid)["state"] == "done"


def test_compat_video_selected(tmp_path):
    cfg, conn, vid, lib = queue(tmp_path)
    assert runner.video_path(cfg, lib, "instagram").name == "video.mp4"
    (lib / "video_h264.mp4").write_bytes(b"h")
    assert runner.video_path(cfg, lib, "instagram").name == "video_h264.mp4"
    assert runner.video_path(cfg, lib, "youtube").name == "video.mp4"
    (lib / "video_facebook.mp4").write_bytes(b"f")
    assert runner.video_path(cfg, lib, "facebook").name == "video_facebook.mp4"


def test_auth_facebook_stores_page_and_instagram():
    log = []
    http = make_http([
        ("/oauth/access_token", {"access_token": "LL"}),
        ("/me/accounts", {"data": [{"id": "PG", "name": "Samastano", "access_token": "PAGETOK"}]}),
        ("/PG", {"instagram_business_account": {"id": "17", "username": "samastano_beats"}}),
    ], log)
    page = meta.auth_facebook(BASE, {"meta_app_id": "1", "meta_app_secret": "s"}, http, "SHORT")
    t = tokens.load("facebook")
    assert page["id"] == "PG" and t["access_token"] == "PAGETOK" and t["ig_user_id"] == "17" and t["ig_username"] == "samastano_beats"
    with pytest.raises(ValueError):
        meta.auth_facebook(BASE, {}, http, "x")


def test_auth_facebook_without_linked_instagram():
    http = make_http([
        ("/oauth/access_token", {"access_token": "LL"}),
        ("/me/accounts", {"data": [{"id": "PG", "name": "S", "access_token": "P"}]}),
        ("/PG", {}),
    ], [])
    meta.auth_facebook(BASE, {"meta_app_id": "1", "meta_app_secret": "s"}, http, "SHORT")
    assert "ig_user_id" not in tokens.load("facebook")


def test_http_retries_and_error_hides_query():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(500) if len(calls) < 3 else httpx.Response(400, json={"error": {"message": "nope"}})

    h = Http(httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)
    with pytest.raises(ApiError) as e:
        h.get("https://graph.facebook.com/v1/x?access_token=SECRET")
    assert len(calls) == 3 and "SECRET" not in str(e.value) and "nope" in str(e.value)


def _story_cfg(cfg, monkeypatch, tmp_path):
    cfg["stories"]["enabled"] = True
    fake = tmp_path / "story.mp4"
    fake.write_bytes(b"s" * 500)
    monkeypatch.setattr(runner, "story_video", lambda c, lib: fake)


def test_instagram_story_after_reel(tmp_path, monkeypatch):
    cfg, conn, vid, lib = queue(tmp_path, ("instagram",))
    _story_cfg(cfg, monkeypatch, tmp_path)
    tokens.save("facebook", {"page_id": "PG", "ig_user_id": "17", "access_token": "T"})
    reel = make_http([("/17/media_publish", {"id": "M1"}), ("/17/media", {"id": "C1"}), ("ig-api-upload", {}),
                      ("/C1", {"status_code": "FINISHED"}), ("/M1", {"permalink": "u", "media_url": "m"})], [])
    assert runner.process(cfg, conn, reel, NOW)[0].startswith("instagram: published")
    log = []
    story = make_http([("/17/media_publish", {"id": "S1"}), ("/17/media", {"id": "C2"}), ("ig-api-upload", {}),
                       ("/C2", {"status_code": "FINISHED"})], log)
    assert runner.process(cfg, conn, story, NOW) == ["instagram: story posted S1"]
    create = next(c for k, h, c in log if k.endswith("/17/media")).decode()
    assert "media_type=STORIES" in create
    assert json.loads(db.posts_of(conn, vid)[0]["extra_json"])["story_id"] == "S1"
    assert runner.process(cfg, conn, make_http([], []), NOW) == []  # only once


def test_story_failure_is_retried_then_given_up(tmp_path, monkeypatch):
    cfg, conn, vid, lib = queue(tmp_path, ("instagram",))
    _story_cfg(cfg, monkeypatch, tmp_path)
    tokens.save("facebook", {"page_id": "PG", "ig_user_id": "17", "access_token": "T"})
    reel = make_http([("/17/media_publish", {"id": "M1"}), ("/17/media", {"id": "C1"}), ("ig-api-upload", {}),
                      ("/C1", {"status_code": "FINISHED"}), ("/M1", {"permalink": "u"})], [])
    runner.process(cfg, conn, reel, NOW)
    bad = make_http([("/17/media", (400, {"error": {"message": "story rejected"}}))], [])
    for _ in range(3):
        assert "story failed" in runner.process(cfg, conn, bad, NOW)[0]
    assert runner.process(cfg, conn, bad, NOW) == []
    p = db.posts_of(conn, vid)[0]
    assert p["state"] == "published" and json.loads(p["extra_json"])["story_tries"] == 3  # the Reel itself stays published


def test_facebook_story_after_first_comment(tmp_path, monkeypatch):
    cfg, conn, vid, lib = queue(tmp_path, ("facebook",))
    _story_cfg(cfg, monkeypatch, tmp_path)
    tokens.save("facebook", {"page_id": "PG", "access_token": "PGTOKEN"})
    up = make_http([("/PG/video_reels", {"video_id": "V1", "upload_url": "https://rupload.facebook.com/video-upload/v26.0/V1"}),
                    ("video-upload", {})], [])
    runner.process(cfg, conn, up, datetime(2026, 11, 3, 20, 0, tzinfo=timezone.utc))
    live = datetime.fromisoformat(db.posts_of(conn, vid)[0]["fire_at_utc"]) + timedelta(minutes=2)
    log = []
    http = make_http([("/PG/video_stories", {"video_id": "ST1", "upload_url": "https://rupload.facebook.com/video-upload/v26.0/ST1"}),
                      ("video-upload", {}), ("/PG_1/comments", {"id": "c"}),
                      ("/V1", {"post_id": "PG_1", "status": {"video_status": "ready"}})], log)
    assert runner.process(cfg, conn, http, live) == ["facebook: first comment posted; facebook: story posted ST1"]
    assert "upload_phase=finish" in [c.decode() for k, h, c in log if k.endswith("/PG/video_stories")][-1]
