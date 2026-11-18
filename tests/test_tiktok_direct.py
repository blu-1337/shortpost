import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from shortpost import tokens
from shortpost.http import ApiError, Http
from shortpost.platforms import tiktok_direct as td
from shortpost.platforms import tiktok_inbox as ti

CFG = {"schedule": {"native_time": "18:30"}, "notify": {"ntfy_server": "https://ntfy.sh"},
       "platforms": {"tiktok": {"push_caption": False, "direct_post": True}}}
CREATOR = {"creator_nickname": "S", "privacy_level_options": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"], "comment_disabled": False,
           "duet_disabled": True, "stitch_disabled": False, "max_video_post_duration_sec": 600}
GOOD = {"consented": True, "privacy": "PUBLIC_TO_EVERYONE", "comment": True, "duet": False, "stitch": False, "disclose": True,
        "your_brand": True, "branded": False}


def test_validate_rules():
    assert td.validate_settings(GOOD, CREATOR) == []
    assert td.validate_settings({**GOOD, "consented": False}, CREATOR)
    assert td.validate_settings({**GOOD, "privacy": None}, CREATOR)
    assert td.validate_settings({**GOOD, "privacy": "FOLLOWER_OF_CREATOR"}, CREATOR)       # not offered
    assert td.validate_settings({**GOOD, "your_brand": False}, CREATOR)                      # disclosure on, nothing ticked
    assert td.validate_settings({**GOOD, "branded": True, "privacy": "SELF_ONLY"}, CREATOR)  # branded cannot be private
    assert td.validate_settings({**GOOD, "duet": True}, CREATOR)                             # duet disabled by the account


def test_post_info_defaults_off():
    i = td.post_info("cap", GOOD)
    assert i["disable_duet"] and i["disable_stitch"] and not i["disable_comment"]
    assert i["brand_organic_toggle"] is True and i["brand_content_toggle"] is False
    assert "brand_organic_toggle" not in td.post_info("cap", {**GOOD, "disclose": False})


def _setup(monkeypatch, tmp_path):
    monkeypatch.setattr(tokens, "load", lambda name, user_dir=None: {
        "access_token": "AT", "expires_at": (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()})
    monkeypatch.setattr(td, "SLEEP", lambda s: None)
    v = tmp_path / "video_h264.mp4"
    v.write_bytes(b"x" * 1000)
    return v


def test_direct_post_flow(tmp_path, monkeypatch):
    video = _setup(monkeypatch, tmp_path)
    seen = {}

    def handler(req):
        p = req.url.path
        if p.endswith("creator_info/query/"):
            return httpx.Response(200, json={"data": CREATOR})
        if p.endswith("video/init/"):
            seen["body"] = json.loads(req.content)
            return httpx.Response(200, json={"data": {"publish_id": "p1", "upload_url": "https://up.example/u"}})
        if req.method == "PUT":
            return httpx.Response(201)
        return httpx.Response(200, json={"data": {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": [123]}})

    http = Http(httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)
    res = ti.upload(CFG, video, {"text": "cap #a", "direct": GOOD}, None, http, False, secrets={})
    assert res.state == "published" and res.remote_id == "123" and res.extra["direct"]
    assert seen["body"]["post_info"]["privacy_level"] == "PUBLIC_TO_EVERYONE"
    assert seen["body"]["post_info"]["title"] == "cap #a"


def test_unaudited_falls_back_to_inbox(tmp_path, monkeypatch):
    video = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(ti, "SLEEP", lambda s: None)
    monkeypatch.setattr(ti.notify, "send", lambda *a, **k: True)
    paths = []

    def handler(req):
        p = req.url.path
        paths.append(p)
        if p.endswith("creator_info/query/"):
            return httpx.Response(200, json={"data": CREATOR})
        if p.endswith("inbox/video/init/"):
            return httpx.Response(200, json={"data": {"publish_id": "p2", "upload_url": "https://up.example/u"}})
        if p.endswith("video/init/"):
            return httpx.Response(403, json={"error": {"code": "unaudited_client_can_only_post_to_private_accounts"}})
        if req.method == "PUT":
            return httpx.Response(201)
        return httpx.Response(200, json={"data": {"status": "SEND_TO_USER_INBOX"}})

    http = Http(httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None, retries=1)
    res = ti.upload(CFG, video, {"text": "cap", "direct": GOOD}, None, http, False, secrets={})
    assert res.extra.get("needs_user_finish") and any(p.endswith("inbox/video/init/") for p in paths)


def test_invalid_settings_block(tmp_path, monkeypatch):
    video = _setup(monkeypatch, tmp_path)
    http = Http(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": CREATOR}))), sleep=lambda s: None)
    with pytest.raises(ApiError):
        ti.upload(CFG, video, {"text": "c", "direct": {**GOOD, "consented": False}}, None, http, False, secrets={})
