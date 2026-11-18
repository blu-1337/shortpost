import json
from datetime import datetime, timezone
from pathlib import Path

import httpx

from shortpost import affiliates, config, render, scheduler
from shortpost.ai import metadata
from shortpost.ai.llm import LLM
from shortpost.naming import VideoInput, parse_stem

CFG = config.load_config(user_dir=Path("/nonexistent"))
CFG["platforms"]["bluesky"]["enabled"] = True
CFG["platforms"]["pinterest"]["enabled"] = True
CAT = affiliates.load_catalog()

GOOD_CORE = {
    "products": ["Vatrical"], "category": "enhancer plugin", "is_beat_video": False,
    "title_options": ["This Enhancer Adds Air To Dull Mixes", "Vatrical Enhancer: Worth Using Every Day?", "Make Vocals Pop With One Plugin"],
    "title": "This Enhancer Adds Air To Dull Mixes Fast", "hook": "Dull mix? Try this enhancer. See https://evil.example.com now",
    "summary": "Vatrical from FlameSound is an enhancer plugin. Mail me@x.com @someone", "keywords": ["a", "b", "c", "d", "e"],
    "hashtags": ["enhancer", "trap", "flamesound", "mixtips", "plugin", "audio"],
    "youtube_tags": ["vatrical", "enhancer plugin", "fl studio", "vst", "mixing", "plugin review", "flamesound", "free vst"],
    "alt_text": "A plugin demo",
}
GOOD_PLAT = {k: "text " * 3 for k in metadata.PLATFORM_FIELDS}


def fake_client(answers):
    """Returns the queued assistant contents in order, one per request."""
    it = iter(answers)

    def handler(request):
        return httpx.Response(200, json={"message": {"content": next(it)}, "prompt_eval_count": 1, "eval_count": 1})

    return httpx.Client(transport=httpx.MockTransport(handler))


def vi():
    return VideoInput(Path("x.mp4"), parse_stem("vatrical-from-flamesound-enhancer-vst-effects-plugin-part-1-of-3"))


def factory(per_model):
    return lambda cfg, model: LLM(cfg, model, per_model[model])


def test_valid_first_try():
    global GOOD_CORE
    c = fake_client([json.dumps(GOOD_CORE), json.dumps(GOOD_PLAT)])
    meta = metadata.generate(CFG, vi(), 35, llm_factory=factory({"gemma4:12b": c}), seed=5)
    assert meta["source"] == "ai" and meta["seed"] == 5
    assert "http" not in meta["core"]["hook"] and "@" not in meta["core"]["summary"]
    assert "flamesound" in meta["core"]["hashtags"]


def test_invalid_then_valid_retries():
    c = fake_client(["not json", json.dumps(GOOD_CORE), json.dumps(GOOD_PLAT)])
    assert metadata.generate(CFG, vi(), 35, llm_factory=factory({"gemma4:12b": c}))["source"] == "ai"


def test_fallback_model_then_template():
    bad = fake_client(["x"] * 2)
    good = fake_client([json.dumps(GOOD_CORE), json.dumps(GOOD_PLAT)])
    meta = metadata.generate(CFG, vi(), 35, llm_factory=factory({"gemma4:12b": bad, "qwen3.5:9b": good}))
    assert meta["source"] == "ai-fallback-model" and meta["model"] == "qwen3.5:9b"
    bad1, bad2 = fake_client(["x"] * 2), fake_client(["x"] * 2)
    meta = metadata.generate(CFG, vi(), 35, llm_factory=factory({"gemma4:12b": bad1, "qwen3.5:9b": bad2}))
    assert meta["source"] == "template"
    assert 0 < len(meta["core"]["title"]) <= 65


def test_clean_text():
    assert metadata.clean_text("Visit www.foo.com or https://a.b/c, mail a@b.com @me <b>") == "Visit or mail b"


def _render(core_over=None, notes="", v=None):
    core = {**metadata.sanitize_core(GOOD_CORE), **(core_over or {})}
    meta = {"core": core, "platform": metadata.sanitize_platform(GOOD_PLAT), "source": "ai", "seed": 1}
    v = v or vi()
    v.notes = notes
    return render.render_all(CFG, CAT, v, meta)


def test_limits_and_series_title():
    r = _render()
    assert r["youtube"]["title"].endswith(" (Part 1/3)") and 40 <= len(r["youtube"]["title"]) <= 65
    assert len(r["youtube"]["text"]) <= 5000
    tags = r["youtube"]["tags"]
    assert sum(len(t) + (2 if " " in t else 0) for t in tags) + len(tags) - 1 <= 500
    assert sum(len(t) + (2 if " " in t else 0) for t in tags) + len(tags) - 1 >= 470  # the field is filled
    assert render.graphemes(r["bluesky"]["text"]) <= 300
    assert len(r["threads"]["text"]) <= 500 and len(r["facebook"]["text"]) <= 250
    assert len(r["pinterest"]["title"]) <= 100 and len(r["pinterest"]["text"]) <= 500
    assert r["instagram"]["text"].count("#") <= 3 and r["tiktok"]["text"].count("#") <= 4


def test_no_affiliate_no_disclosure_and_fallback_link():
    r = _render()
    assert CFG["links"]["disclosure_long"] not in r["youtube"]["text"]
    assert "samastano.medianopol.com/free-stages-workbook-signup/" in r["facebook"]["comment"]
    assert "http" not in r["instagram"]["text"] and "http" not in r["tiktok"]["text"]
    assert "Part 2 tomorrow." in r["instagram"]["text"]


def test_affiliate_adds_disclosure_exactly():
    r = _render(notes="uses dogma")
    assert CFG["links"]["disclosure_long"] in r["youtube"]["text"]
    assert "https://ip.apmastering.com/plugins/dogma  (code samastano = 10% off)" in r["youtube"]["text"]
    assert "(affiliate link)" in r["threads"]["text"] and "http" not in r["instagram"]["text"]
    assert "code samastano" in r["instagram"]["text"]
    assert r["pinterest"]["link"] == "https://ip.apmastering.com/plugins/dogma"


def test_beat_only_tags_gated():
    assert "trap" not in render.hashtags(CFG, {"hashtags": ["trap", "x", "y"], "is_beat_video": False}, 4)
    assert "trap" in render.hashtags(CFG, {"hashtags": ["x"], "is_beat_video": True}, 4)
    assert len(render.hashtags(CFG, {"hashtags": ["x", "y"]}, 3)) == 3


def test_bluesky_never_drops_link_and_trims():
    long = {**GOOD_PLAT, "bluesky_text": "word " * 100}
    core = metadata.sanitize_core(GOOD_CORE)
    meta = {"core": core, "platform": long, "source": "ai", "seed": 1}
    r = render.render_all(CFG, CAT, vi(), meta)["bluesky"]
    assert render.graphemes(r["text"]) <= 300 and "FREE Finish More Beats" in r["text"]


def test_scheduler_et_to_bucharest_and_dst():
    cfg = CFG
    # 2026-11-04: US EST (-5), Bucharest EET (+2) -> 18:30 ET = 23:30 UTC = 01:30 next day local
    t = scheduler.fire_time(cfg, "youtube", datetime(2026, 11, 4).date(), 0)
    assert t == datetime(2026, 11, 4, 23, 30, tzinfo=timezone.utc)
    # EU switched back, US not yet: 2026-10-28 18:30 EDT = 22:30 UTC = 00:30 EET next day
    t = scheduler.fire_time(cfg, "youtube", datetime(2026, 10, 28).date(), 0)
    assert t == datetime(2026, 10, 28, 22, 30, tzinfo=timezone.utc)
    # local-time platforms: 20:00 Bucharest EET = 18:00 UTC
    t = scheduler.fire_time(cfg, "instagram", datetime(2026, 11, 4).date(), 0)
    assert t == datetime(2026, 11, 4, 18, 0, tzinfo=timezone.utc)


def test_find_slot_skips_occupied_and_past():
    now = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
    d = scheduler.find_slot(CFG, now)
    assert str(d) == "2026-11-03" or str(d) == "2026-11-04"
    d2 = scheduler.find_slot(CFG, now, occupied={d})
    assert d2 > d
    late = datetime(2026, 11, 3, 23, 0, tzinfo=timezone.utc)
    assert str(scheduler.find_slot(CFG, late)) == "2026-11-04"


def test_platforms_get_different_keywords_and_hashtags():
    core = metadata.sanitize_core({**GOOD_CORE, "keywords": [f"kw{i}" for i in range(8)],
                                   "hashtags": [f"tag{i}" for i in range(8)]})
    focus = metadata.assign_focus(core, ["youtube", "facebook", "instagram", "tiktok", "threads"])
    assert len({f["keyword"] for f in focus.values()}) == 5
    meta = {"core": core, "platform": metadata.sanitize_platform(GOOD_PLAT), "focus": focus, "source": "ai", "seed": 1}
    r = render.render_all(CFG, CAT, vi(), meta)
    topical = {p: [t for t in render.re.findall(r"#(\w+)", r[p]["text"]) if t.startswith("tag")] for p in ("youtube", "facebook", "instagram", "tiktok", "threads")}
    assert len({tuple(v) for v in topical.values()}) == 5
    assert render.coverage(r)["unique_hashtags"] >= 8


def test_focus_goes_to_model_prompt():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content)["messages"][-1]["content"])
        return httpx.Response(200, json={"message": {"content": json.dumps(GOOD_CORE if len(seen) == 1 else GOOD_PLAT)}})

    c = httpx.Client(transport=httpx.MockTransport(handler))
    metadata.generate(CFG, vi(), 35, llm_factory=lambda cfg, m: LLM(cfg, m, c))
    assert "focus_keywords" in seen[1] and "tiktok_caption" in seen[1]
