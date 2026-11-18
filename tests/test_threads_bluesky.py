import json
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from shortpost import runner, tokens
from shortpost.auth import threads as threads_auth
from shortpost.http import ApiError, Http, NotConfigured
from shortpost.platforms import bluesky, threads

CFG = {"meta": {"graph_version": "v26.0"}}


def _tok(age_days=1):
    return {"access_token": "AT", "user_id": "U1", "username": "me",
            "obtained_at": (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat(timespec="seconds"),
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=50)).isoformat(timespec="seconds")}


def _http(handler):
    return Http(httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)


def test_threads_waits_for_instagram_url(monkeypatch, tmp_path):
    monkeypatch.setattr(tokens, "load", lambda n, user_dir=None: _tok())
    with pytest.raises(NotConfigured, match="Instagram"):
        threads.upload(CFG, tmp_path / "v.mp4", {"text": "hi"}, None, _http(lambda r: httpx.Response(200)))


def test_threads_flow(monkeypatch, tmp_path):
    monkeypatch.setattr(tokens, "load", lambda n, user_dir=None: _tok())
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path))
        if req.url.path.endswith("/threads"):
            body = req.content.decode()
            assert "instagram.com%2Freel%2Fx" in body and "media_type=TEXT" in body and "link_attachment" not in body
            return httpx.Response(200, json={"id": "C1"})
        if req.url.path.endswith("/C1"):
            return httpx.Response(200, json={"status": "FINISHED"})
        if req.url.path.endswith("/threads_publish"):
            return httpx.Response(200, json={"id": "M1"})
        return httpx.Response(200, json={"permalink": "https://www.threads.com/@me/post/x"})

    res = threads.upload(CFG, tmp_path / "v.mp4", {"text": "hi", "_link": "https://instagram.com/reel/x"}, None, _http(handler))
    assert res.state == "published" and res.remote_id == "M1" and res.remote_url.endswith("/post/x")


def test_threads_video_mode(monkeypatch, tmp_path):
    import copy
    monkeypatch.setattr(tokens, "load", lambda n, user_dir=None: _tok())
    cfg = {**copy.deepcopy(CFG), "platforms": {"threads": {"post_type": "video"}}}
    bodies = []

    def handler(req):
        if req.url.path.endswith("/threads"):
            bodies.append(req.content.decode())
            return httpx.Response(200, json={"id": "C1"})
        if req.url.path.endswith("/C1"):
            return httpx.Response(200, json={"status": "FINISHED"})
        return httpx.Response(200, json={"id": "M1", "permalink": "p"})

    threads.upload(cfg, tmp_path / "v.mp4", {"text": "hi", "_video_url": "https://cdn.example/v.mp4"}, None, _http(handler))
    assert "media_type=VIDEO" in bodies[0] and "video_url=" in bodies[0]


def test_threads_container_error(monkeypatch, tmp_path):
    monkeypatch.setattr(tokens, "load", lambda n, user_dir=None: _tok())

    def handler(req):
        if req.url.path.endswith("/threads"):
            return httpx.Response(200, json={"id": "C1"})
        return httpx.Response(200, json={"status": "ERROR", "error_message": "bad video"})

    with pytest.raises(ApiError, match="bad video"):
        threads.upload(CFG, tmp_path / "v.mp4", {"text": "hi", "_link": "u"}, None, _http(handler))


def test_threads_refresh_when_old(monkeypatch):
    saved = {}
    monkeypatch.setattr(tokens, "save", lambda n, d, user_dir=None: saved.update(d))
    h = _http(lambda r: httpx.Response(200, json={"access_token": "NEW", "expires_in": 5184000}))
    assert threads.refresh_if_needed(h, _tok(age_days=1))["access_token"] == "AT"
    assert threads.refresh_if_needed(h, _tok(age_days=8))["access_token"] == "NEW" and saved["access_token"] == "NEW"


def test_threads_auth_exchange_and_fallback(monkeypatch):
    saved = {}
    monkeypatch.setattr(tokens, "save", lambda n, d, user_dir=None: saved.update(d))

    def handler(req):
        if req.url.path.endswith("/access_token"):
            return httpx.Response(200, json={"access_token": "LONG", "expires_in": 5184000})
        return httpx.Response(200, json={"id": "U9", "username": "samastano"})

    rec = threads_auth.authorize(_http(handler), {"meta_app_secret": "s"}, "short")
    assert rec["access_token"] == "LONG" and saved["user_id"] == "U9"

    def failing_exchange(req):
        if req.url.path.endswith("/access_token"):
            return httpx.Response(400, json={"error": {"message": "already long"}})
        return httpx.Response(200, json={"id": "U9", "username": "samastano"})

    assert threads_auth.authorize(_http(failing_exchange), {"meta_app_secret": "s"}, "kept")["access_token"] == "kept"


def test_bluesky_spans_and_text():
    r = {"text": "Great plugin\n\n🛒 DOGMA (affiliate link)\n#flstudio #vst",
         "link_label": "🛒 DOGMA (affiliate link)", "link_url": "https://ip.apmastering.com/plugins/dogma"}
    kinds = [(k, v) for _, _, k, v in bluesky.spans(r)]
    assert kinds == [("link", "https://ip.apmastering.com/plugins/dogma"), ("tag", "flstudio"), ("tag", "vst")]
    tb = bluesky.build_text(r)
    assert tb.build_text() == r["text"]
    assert len(tb.build_facets()) == 3


def test_bluesky_upload_with_fake_client(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * 100)
    calls = {}

    class Fake:
        def send_video(self, **kw):
            calls.update(kw)
            return SimpleNamespace(uri="at://did:plc:abc/app.bsky.feed.post/3kxyz")

    res = bluesky.upload(CFG, video, {"text": "hi #a", "alt_text": "alt"}, None, Http(), secrets={"bluesky_handle": "me.bsky.social"},
                         client=Fake())
    assert res.remote_url == "https://bsky.app/profile/me.bsky.social/post/3kxyz"
    assert calls["video"] == b"x" * 100 and calls["video_alt"] == "alt"


def test_bluesky_needs_secrets_and_size_cap(tmp_path, monkeypatch):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    with pytest.raises(NotConfigured):
        bluesky.upload(CFG, video, {"text": "hi"}, None, Http(), secrets={})
    monkeypatch.setattr(bluesky, "MAX_BYTES", 0)
    with pytest.raises(ApiError, match="100 MB"):
        bluesky.upload(CFG, video, {"text": "hi"}, None, Http(), secrets={})


def test_runner_instagram_media_url():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE posts (video_id INT, platform TEXT, state TEXT, extra_json TEXT)")
    assert runner._instagram_media_url(conn, 1) == ""
    conn.execute("INSERT INTO posts VALUES (1,'instagram','pending',NULL)")
    assert runner._instagram_media_url(conn, 1) == ""
    conn.execute("UPDATE posts SET state='published', extra_json=?", (json.dumps({"media_url": "https://cdn/x.mp4"}),))
    assert runner._instagram_media_url(conn, 1) == "https://cdn/x.mp4"
