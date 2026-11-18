import json
from datetime import datetime, timedelta, timezone

import httpx

from shortpost import notify, tokens
from shortpost.auth import tiktok as auth
from shortpost.http import Http
from shortpost.platforms import tiktok_inbox as ti

CFG = {"schedule": {"native_time": "18:30"}, "notify": {"ntfy_server": "https://ntfy.sh"},
       "platforms": {"tiktok": {"push_caption": True}}}


def test_plan_chunks():
    assert ti.plan_chunks(10 << 20) == (10 << 20, 1)
    assert ti.plan_chunks(64 << 20) == (64 << 20, 1)
    assert ti.plan_chunks(150 << 20) == (64 << 20, 2)


def test_pkce_and_url():
    v, c = auth.make_pkce()
    assert len(c) == 64 and v
    u = auth.auth_url("KEY", "st", c)
    assert "client_key=KEY" in u and "video.upload" in u and "code_challenge_method=S256" in u


def test_notify_ascii_title_and_no_topic():
    seen = {}

    def handler(req):
        seen["title"], seen["body"], seen["url"] = req.headers["Title"], req.content.decode(), str(req.url)
        return httpx.Response(200)

    c = httpx.Client(transport=httpx.MockTransport(handler))
    assert notify.send(CFG, "TikTok draft ready ✅", "hi é", {"ntfy_topic": "t"}, c)
    assert seen["title"].isascii() and seen["url"].endswith("/t") and seen["body"] == "hi é"
    assert notify.send(CFG, "x", "y", {}, c) is False


def test_upload_flow(tmp_path, monkeypatch):
    monkeypatch.setattr(tokens, "load", lambda name, user_dir=None: {
        "access_token": "AT", "expires_at": (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()})
    monkeypatch.setattr(ti, "SLEEP", lambda s: None)
    pushed = []
    monkeypatch.setattr(ti.notify, "send", lambda cfg, title, body, secrets=None, **k: pushed.append((title, body)) or True)
    video = tmp_path / "video_h264.mp4"
    video.write_bytes(b"x" * 1000)
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path))
        if req.url.path.endswith("/init/"):
            body = json.loads(req.content)["source_info"]
            assert body["video_size"] == 1000 and body["total_chunk_count"] == 1
            assert req.headers["Authorization"] == "Bearer AT"
            return httpx.Response(200, json={"data": {"publish_id": "p1", "upload_url": "https://up.example/u"}})
        if req.method == "PUT":
            assert req.headers["Content-Range"] == "bytes 0-999/1000"
            return httpx.Response(201)
        return httpx.Response(200, json={"data": {"status": "SEND_TO_USER_INBOX"}})

    http = Http(httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)
    res = ti.upload(CFG, video, {"text": "cap #a"}, None, http, False, secrets={})
    assert res.state == "published" and res.extra["needs_user_finish"]
    assert (tmp_path / "tiktok_caption.txt").read_text(encoding="utf-8") == "cap #a"
    assert len(pushed) == 1 and pushed[0][0].startswith("TikTok draft") and "18:30 ET" in pushed[0][0]
    assert pushed[0][1] == "cap #a"


def test_upload_failed_status(tmp_path, monkeypatch):
    import pytest

    from shortpost.http import ApiError

    monkeypatch.setattr(tokens, "load", lambda name, user_dir=None: {
        "access_token": "AT", "expires_at": (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()})
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * 10)

    def handler(req):
        if req.url.path.endswith("/init/"):
            return httpx.Response(200, json={"data": {"publish_id": "p", "upload_url": "https://up.example/u"}})
        if req.method == "PUT":
            return httpx.Response(201)
        return httpx.Response(200, json={"data": {"status": "FAILED", "fail_reason": "file_format_check_failed"}})

    http = Http(httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)
    with pytest.raises(ApiError, match="file_format_check_failed"):
        ti.upload(CFG, video, {"text": "c"}, None, http, False, secrets={})


def test_one_tiktok_draft_at_a_time(tmp_path):
    import json
    import sqlite3

    from shortpost import runner

    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("CREATE TABLE posts (id INTEGER PRIMARY KEY, video_id INT, platform TEXT, state TEXT, extra_json TEXT, updated_at TEXT);"
                    "CREATE TABLE events (id INTEGER PRIMARY KEY, at_utc TEXT, video_id INT, platform TEXT, kind TEXT, message TEXT);")
    c.execute("INSERT INTO posts VALUES (1, 1, 'tiktok', 'published', ?, '')", (json.dumps({"gate": True}),))
    nxt = {"platform": "tiktok", "mode": "inbox", "video_id": 2, "extra_json": None}
    assert runner._tiktok_held(c, nxt, False)["video_id"] == 1
    assert runner._tiktok_held(c, nxt, True) is None  # direct post: no gate
    assert runner._tiktok_held(c, {**nxt, "platform": "bluesky"}, False) is None
    assert runner._tiktok_held(c, {**nxt, "extra_json": json.dumps({"resend": True})}, False) is None  # explicit resend ignores the gate
    assert runner.mark_tiktok_done(c, 1)
    assert runner._tiktok_held(c, nxt, False) is None


def test_recover_interrupted_uploads():
    import sqlite3
    from datetime import datetime, timedelta, timezone

    from shortpost import runner

    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("CREATE TABLE posts (id INTEGER PRIMARY KEY, video_id INT, platform TEXT, mode TEXT, state TEXT, last_error TEXT, attempts INT, next_attempt_at_utc TEXT, extra_json TEXT, updated_at TEXT);"
                    "CREATE TABLE events (id INTEGER PRIMARY KEY, at_utc TEXT, video_id INT, platform TEXT, kind TEXT, message TEXT);")
    old = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(timespec="seconds")
    new = datetime.now(timezone.utc).isoformat(timespec="seconds")
    c.execute("INSERT INTO posts VALUES (1, 9, 'tiktok', 'inbox', 'uploading', NULL, 0, NULL, NULL, ?)", (old,))
    c.execute("INSERT INTO posts VALUES (2, 9, 'instagram', 'local_time', 'uploading', NULL, 0, NULL, NULL, ?)", (old,))
    c.execute("INSERT INTO posts VALUES (3, 9, 'bluesky', 'local_time', 'uploading', NULL, 0, NULL, NULL, ?)", (new,))
    out = runner.recover_interrupted(c, datetime.now(timezone.utc))
    st = {r["id"]: r["state"] for r in c.execute("SELECT id, state FROM posts")}
    assert st == {1: "pending", 2: "failed", 3: "uploading"} and len(out) == 2
