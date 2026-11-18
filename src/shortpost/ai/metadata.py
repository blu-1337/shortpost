"""Two-call AI metadata generation with repair, fallback model and a template-only fallback (CLAUDE.md 10.3)."""

from __future__ import annotations

import json
import random
import re
from typing import Callable

from ..log import logger
from ..naming import VideoInput
from .llm import GEMINI_PREFIX, LLM, gemini_enabled, load_prompt, make_llm

CORE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["products", "category", "is_beat_video", "title_options", "title", "hook", "summary", "keywords",
                 "hashtags", "youtube_tags", "alt_text"],
    "properties": {
        "products": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
        "category": {"type": "string"},
        "is_beat_video": {"type": "boolean"},
        "title_options": {"type": "array", "items": {"type": "string", "maxLength": 70}, "minItems": 3, "maxItems": 3},
        "title": {"type": "string", "maxLength": 70},
        "hook": {"type": "string", "maxLength": 110},
        "summary": {"type": "string", "maxLength": 300},
        "keywords": {"type": "array", "items": {"type": "string"}, "minItems": 5, "maxItems": 12},
        "hashtags": {"type": "array", "items": {"type": "string", "pattern": "^[a-z0-9]+$"}, "minItems": 6, "maxItems": 10},
        "youtube_tags": {"type": "array", "items": {"type": "string", "maxLength": 40}, "minItems": 8, "maxItems": 15},
        "alt_text": {"type": "string", "maxLength": 200},
        "cover_line": {"type": "string", "maxLength": 40},  # optional: 3-6 word text burned into the first frames
    },
}

PLATFORM_FIELDS = {
    "tiktok_caption": 250, "instagram_caption": 350, "facebook_description": 200, "threads_text": 350,
    "bluesky_text": 200, "pinterest_title": 100, "pinterest_description": 400,
}
PLATFORM_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": list(PLATFORM_FIELDS),
    "properties": {k: {"type": "string", "maxLength": n} for k, n in PLATFORM_FIELDS.items()},
}

_URL = re.compile(r"(?:https?://|www\.)\S+|\b[\w-]+\.(?:com|net|org|io|app|co|ly)\b\S*", re.I)
_EMAIL = re.compile(r"\S+@\S+\.\S+")
_HANDLE = re.compile(r"(?<!\w)@\w+")
_BEAT_WORDS = re.compile(r"\b(beats?|type beat|instrumental|beat ?tape)\b", re.I)


def clean_text(s: str) -> str:
    """Strip URLs, emails, @handles and angle brackets from model output."""
    s = _EMAIL.sub("", s)
    s = _URL.sub("", s)
    s = _HANDLE.sub("", s)
    s = s.replace("<", "").replace(">", "")
    return re.sub(r"[ \t]+", " ", s).strip()


def clean_tag(t: str) -> str:
    return re.sub(r"[^a-z0-9]", "", t.lower().lstrip("#"))


def sanitize_core(core: dict) -> dict:
    out = dict(core)
    for k in ("category", "title", "hook", "summary", "alt_text", "cover_line"):
        out[k] = clean_text(str(out.get(k, "")))
    out["title_options"] = [clean_text(t) for t in out.get("title_options", [])]
    out["products"] = [clean_text(p) for p in out.get("products", [])]
    out["keywords"] = [clean_text(k) for k in out.get("keywords", [])]
    out["youtube_tags"] = [clean_text(t) for t in out.get("youtube_tags", [])]
    out["hashtags"] = list(dict.fromkeys(t for t in (clean_tag(h) for h in out.get("hashtags", [])) if t))
    return out


def sanitize_platform(p: dict) -> dict:
    return {k: clean_text(str(v)).replace("#", "") for k, v in p.items()}


def title_case(s: str) -> str:
    small = {"a", "an", "the", "for", "of", "from", "and", "in", "on", "to", "with"}
    words = s.split()
    return " ".join(w if (i and w.lower() in small) else w[:1].upper() + w[1:] for i, w in enumerate(words))


def trim_words(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    cut = s[:n].rsplit(" ", 1)[0]
    return (cut or s[:n]).rstrip(" ,;:-")


def template_core(vi: VideoInput) -> dict:
    """Template-only fallback built from the topic. Never blocks."""
    topic = vi.parsed.topic
    title = trim_words(title_case(topic), 65)
    words = [w for w in re.split(r"\W+", topic.lower()) if len(w) > 2]
    is_beat = bool(_BEAT_WORDS.search(topic + " " + vi.notes))
    return {
        "products": [], "category": "plugin", "is_beat_video": is_beat,
        "title_options": [title] * 3, "title": title, "hook": trim_words(title, 110),
        "summary": trim_words(f"A quick look at {topic}.", 300),
        "keywords": (words + ["fl studio", "vst plugin", "mixing", "music production", "beatmaking"])[:12],
        "hashtags": list(dict.fromkeys(words))[:6],
        "youtube_tags": list(dict.fromkeys([topic] + words + ["fl studio", "vst plugin", "mixing", "plugin review", "beatmaker", "music production"]))[:12],
        "alt_text": trim_words(f"Short video about {topic}", 200),
    }


def template_platform(core: dict, focus: dict | None = None) -> dict:
    hook, summ = core["hook"], core["summary"]
    kw = lambda p: (focus or {}).get(p, {}).get("keyword", "")  # noqa: E731
    def mix(p, n):
        k = kw(p)
        extra = f" ({k})" if k and k.lower() not in (hook + summ).lower() else ""
        return trim_words(f"{hook}{extra} {summ}", n)
    return {
        "tiktok_caption": mix("tiktok", 250),
        "instagram_caption": mix("instagram", 350),
        "facebook_description": mix("facebook", 200),
        "threads_text": mix("threads", 350),
        "bluesky_text": trim_words(hook, 200),
        "pinterest_title": trim_words(core["title"], 100),
        "pinterest_description": mix("pinterest", 400),
    }


PLATFORM_ORDER = ["youtube", "facebook", "instagram", "tiktok", "threads", "bluesky", "pinterest"]


def assign_focus(core: dict, platforms: list[str]) -> dict[str, dict]:
    """Give each platform its own focus keyword and topical-hashtag rotation so the posts cover different terms.

    Deterministic: platform i gets keyword i (wrapping) and rotates the topical hashtag pool by i.
    """
    pool = list(dict.fromkeys([k for k in core.get("keywords", []) if k] + core.get("products", [])))
    out = {}
    ordered = [p for p in PLATFORM_ORDER if p in platforms]
    for i, p in enumerate(ordered):
        out[p] = {"keyword": pool[i % len(pool)] if pool else "", "rot": i}
    return out


def _user_core(vi: VideoInput, duration_s: float, deals: list[str], hint: str) -> str:
    lines = [f"Topic (from the filename): {vi.parsed.topic}", f"Length: {duration_s:.0f} seconds"]
    if vi.notes:
        lines.append(f"Creator notes: {vi.notes}")
    if vi.parsed.series:
        s = vi.parsed.series
        of = f" of {s['of']}" if s["of"] else ""
        lines.append(f"This is part {s['part']}{of} of a short series. A part suffix is added to the title by the program, "
                     f"so keep the title at most 55 characters.")
    if deals:
        lines.append("Matched deals (names only, do not write links): " + ", ".join(deals))
    if hint:
        lines.append(f"Extra instruction from the creator: {hint}")
    return "\n".join(lines)


def generate(cfg: dict, vi: VideoInput, duration_s: float, deals: list[str] | None = None, hint: str = "",
             seed: int | None = None, llm_factory: Callable[[dict, str], LLM] | None = None) -> dict:
    """Returns {core, platform, source, model, seed, prompts}. source: ai | ai-fallback-model | template."""
    ai = cfg["ai"]
    seed = seed or int(ai.get("seed") or 0) or random.randint(1, 2**31 - 1)
    make = llm_factory or make_llm
    core_sys, core_v, core_h = load_prompt("core", CORE_SCHEMA)
    plat_sys, plat_v, plat_h = load_prompt("platform", PLATFORM_SCHEMA)
    prompts = {"core": f"v{core_v}:{core_h}", "platform": f"v{plat_v}:{plat_h}"}
    logger.info("prompts core %s platform %s", prompts["core"], prompts["platform"])

    models = list(dict.fromkeys([ai["model"], ai.get("fallback_model") or ai["model"]]))
    if gemini_enabled(cfg):
        models.insert(0, GEMINI_PREFIX + ai["gemini_model"])  # free tier first; any failure drops to the local models
    for idx, model in enumerate(models):
        llm = make(cfg, model)
        core = llm.ask(core_sys, _user_core(vi, duration_s, deals or [], hint), CORE_SCHEMA, seed)
        if core is None:
            logger.warning("core metadata failed with %s", model)
            continue
        core = sanitize_core(core)
        focus = assign_focus(core, _enabled(cfg))
        plat = llm.ask(plat_sys, json.dumps({"core": core, "focus_keywords": _focus_map(focus)}, ensure_ascii=False),
                       PLATFORM_SCHEMA, seed, last=True)
        if plat is None:
            logger.warning("platform copy failed with %s, trying the next model for it", model)
            for later in models[idx + 1:]:  # e.g. Gemini quota hit between the two calls: the local model writes the platform copy
                plat = make(cfg, later).ask(plat_sys, json.dumps({"core": core, "focus_keywords": _focus_map(focus)}, ensure_ascii=False),
                                            PLATFORM_SCHEMA, seed, last=True)
                if plat is not None:
                    break
                logger.warning("platform copy failed with %s", later)
        if plat is None:
            logger.warning("no model wrote the platform copy, using templates for it")
            plat = template_platform(core, focus)
        else:
            plat = sanitize_platform(plat)
        return {"core": core, "platform": plat, "focus": focus, "source": "ai" if idx == 0 else "ai-fallback-model",
                "model": model, "seed": seed, "prompts": prompts}

    core = template_core(vi)
    focus = assign_focus(core, _enabled(cfg))
    return {"core": core, "platform": template_platform(core, focus), "focus": focus, "source": "template",
            "model": None, "seed": seed, "prompts": prompts}


def _enabled(cfg: dict) -> list[str]:
    return [p for p, v in cfg["platforms"].items() if v.get("enabled")]


def _focus_map(focus: dict) -> dict:
    """Which keyword each written field must be built around."""
    field_of = {"tiktok": "tiktok_caption", "instagram": "instagram_caption", "facebook": "facebook_description",
                "threads": "threads_text", "bluesky": "bluesky_text", "pinterest": "pinterest_description"}
    return {field_of[p]: f["keyword"] for p, f in focus.items() if p in field_of and f["keyword"]}
