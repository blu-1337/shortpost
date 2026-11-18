import base64
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import pytest

from shortpost import tokens
from shortpost.auth import pinterest as auth
from shortpost.http import ApiError, Http, NotConfigured
from shortpost.platforms import pinterest as pin

SECRETS = {"pinterest_app_id": "ID", "pinterest_app_secret": "SEC"}


def _http(handler):
    return Http(httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)


def _tok(**kw):
    return {"access_token": "AT", "refresh_token": "RT", "board_id": "B1", "board_name": "Board",
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=20)).isoformat(),
            "refresh_expires_at": (datetime.now(timezone.utc) + timedelta(days=300)).isoformat(), **kw}


def test_auth_url_and_exchange_uses_basic_auth(monkeypatch):
    u = auth.auth_url("ID", "st")
    assert "client_id=ID" in u and "pins%3Awrite" in u or "pins:write" in u
    saved = {}
    monkeypatch.setattr(tokens, "save", lambda n, d, user_dir=None: saved.update(d))

    def handler(req):
        assert req.headers["Authorization"] == "Basic " + base64.b64encode(b"ID:SEC").decode()
        assert parse_qs(req.content.decode())["grant_type"] == ["authorization_code"]
        return httpx.Response(200, json={"access_token": "A", "refresh_token": "R", "expires_in": 100, "refresh_token_expires_in": 200})

    rec = auth.exchange(_http(handler), SECRETS, "CODE")
    assert rec["access_token"] == "A" and saved["refresh_token"] == "R"


def test_refresh_keeps_board(monkeypatch):
    monkeypatch.setattr(tokens, "save", lambda n, d, user_dir=None: None)
    h = _http(lambda r: httpx.Response(200, json={"access_token": "NEW", "expires_in": 100}))
    out = auth.refresh(h, SECRETS, _tok())
    assert out["access_token"] == "NEW" and out["board_id"] == "B1" and out["refresh_token"] == "RT"


def test_pick_board_by_name_then_create(monkeypatch):
    saved = {}
    monkeypatch.setattr(tokens, "save", lambda n, d, user_dir=None: saved.update(d))

    def has(req):
        return httpx.Response(200, json={"items": [{"id": "9", "name": "Samastano - Plugin Reviews"}]})

    assert auth.pick_board(_http(has), _tok(), "samastano - plugin reviews")["id"] == "9" and saved["board_id"] == "9"

    def none(req):
        if req.method == "GET":
            return httpx.Response(200, json={"items": []})
        return httpx.Response(201, json={"id": "NEW", "name": "X"})

    assert auth.pick_board(_http(none), _tok(), "X")["id"] == "NEW"


def test_upload_flow(monkeypatch, tmp_path):
    monkeypatch.setattr(tokens, "load", lambda n, user_dir=None: _tok())
    monkeypatch.setattr(pin, "SLEEP", lambda s: None)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * 50)
    seen = {"polls": 0}

    def handler(req):
        p = req.url.path
        if p == "/v5/media" and req.method == "POST":
            return httpx.Response(201, json={"media_id": "M1", "upload_url": "https://s3.example/up", "upload_parameters": {"key": "k"}})
        if req.url.host == "s3.example":
            assert "Authorization" not in req.headers and b'name="key"' in req.content and b"xxxx" in req.content
            return httpx.Response(204)
        if p == "/v5/media/M1":
            seen["polls"] += 1
            return httpx.Response(200, json={"status": "processing" if seen["polls"] < 2 else "succeeded"})
        if p == "/v5/pins":
            import json

            b = json.loads(req.content)
            assert b["board_id"] == "B1" and b["media_source"]["media_id"] == "M1" and b["link"] == "https://dest"
            return httpx.Response(201, json={"id": "P1"})
        raise AssertionError(p)

    res = pin.upload({}, video, {"title": "T", "text": "D", "link": "https://dest", "alt_text": "a"}, None, _http(handler), secrets=SECRETS)
    assert res.remote_id == "P1" and res.remote_url.endswith("/pin/P1/") and seen["polls"] == 2


def test_upload_media_failed(monkeypatch, tmp_path):
    monkeypatch.setattr(tokens, "load", lambda n, user_dir=None: _tok())
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")

    def handler(req):
        if req.url.path == "/v5/media" and req.method == "POST":
            return httpx.Response(201, json={"media_id": "M", "upload_url": "https://s3.example/u", "upload_parameters": {}})
        if req.url.host == "s3.example":
            return httpx.Response(204)
        return httpx.Response(200, json={"status": "failed"})

    with pytest.raises(ApiError, match="failed"):
        pin.upload({}, video, {"title": "T", "text": "D"}, None, _http(handler), secrets=SECRETS)


def test_needs_auth_and_board(monkeypatch, tmp_path):
    monkeypatch.setattr(tokens, "load", lambda n, user_dir=None: None)
    with pytest.raises(NotConfigured, match="auth pinterest"):
        pin.upload({}, tmp_path / "v", {"title": "t", "text": "d"}, None, _http(lambda r: httpx.Response(200)), secrets=SECRETS)
    monkeypatch.setattr(tokens, "load", lambda n, user_dir=None: _tok(board_id=""))
    with pytest.raises(NotConfigured, match="board"):
        pin.upload({}, tmp_path / "v", {"title": "t", "text": "d"}, None, _http(lambda r: httpx.Response(200)), secrets=SECRETS)
