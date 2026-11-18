import copy
import json
from pathlib import Path

import httpx

from shortpost import config
from shortpost.ai import llm, metadata
from shortpost.ai.llm import GeminiLLM

CFG = config.load_config(user_dir=Path("/nonexistent"))
SCHEMA = {"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}}}


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def ok(text):
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": text}]}}], "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 3}})


def test_gemini_valid_answer_key_in_header_not_url():
    seen = {}

    def h(req):
        seen["url"], seen["key"] = str(req.url), req.headers.get("x-goog-api-key")
        return ok('{"a": "x"}')

    g = GeminiLLM(CFG, client=client(h), key="SECRET")
    assert g.ask("sys", "user", SCHEMA, 1) == {"a": "x"}
    assert "SECRET" not in seen["url"] and seen["key"] == "SECRET" and g.stats.prompt_tokens == 5


def test_gemini_invalid_then_valid_retries():
    answers = iter(['{"b": 1}', '{"a": "y"}'])
    g = GeminiLLM(CFG, client=client(lambda r: ok(next(answers))), key="k")
    assert g.ask("s", "u", SCHEMA, 1) == {"a": "y"} and g.stats.schema_failures == 1


def test_gemini_quota_error_returns_none_and_pauses(monkeypatch):
    monkeypatch.setattr(llm, "_gemini_blocked_until", 0.0)
    calls = []

    def h(req):
        calls.append(1)
        return httpx.Response(429, json={"error": "quota"})

    g = GeminiLLM(CFG, client=client(h), key="k")
    assert g.ask("s", "u", SCHEMA, 1) is None
    assert g.ask("s", "u", SCHEMA, 1) is None and len(calls) == 1      # second call skipped while paused
    monkeypatch.setattr(llm, "_gemini_blocked_until", 0.0)


def test_generate_tries_gemini_first_then_local(monkeypatch):
    monkeypatch.setattr(llm, "gemini_key", lambda: "k")
    monkeypatch.setattr(metadata, "gemini_enabled", lambda cfg: True)
    order = []

    class Fake:
        def __init__(self, model):
            self.model = model

        def ask(self, *a, **k):
            order.append(self.model)
            return None

    from test_metadata_render import vi
    meta = metadata.generate(copy.deepcopy(CFG), vi(), 35, llm_factory=lambda c, m: Fake(m), seed=1)
    assert order[0].startswith("gemini:") and order[1:] == [CFG["ai"]["model"], CFG["ai"]["fallback_model"]]
    assert meta["source"] == "template"


def test_platform_copy_falls_to_local_model_when_gemini_dies_between_calls(monkeypatch):
    monkeypatch.setattr(metadata, "gemini_enabled", lambda cfg: True)
    from test_metadata_render import GOOD_CORE, GOOD_PLAT, vi
    calls = []

    class Fake:
        def __init__(self, model):
            self.model, self.n = model, 0

        def ask(self, sys_, user, schema, seed, last=False):
            calls.append((self.model, last))
            if self.model.startswith("gemini:"):
                return GOOD_CORE if not last else None      # core ok, platform copy fails (quota)
            return GOOD_PLAT

    meta = metadata.generate(copy.deepcopy(CFG), vi(), 35, llm_factory=lambda c, m: Fake(m), seed=1)
    assert meta["model"].startswith("gemini:") and meta["source"] == "ai"
    assert (CFG["ai"]["model"], True) in calls
    assert meta["platform"] == metadata.sanitize_platform(GOOD_PLAT)


def test_wrap_cover_and_title_check_fallback():
    from shortpost import media
    assert media.wrap_cover("Your EQ curve is lying").split(chr(10)) == ["Your EQ curve is", "lying"]
    assert media.wrap_cover("") == ""
    assert len(media.wrap_cover("a " * 40).split(chr(10))) <= 4
