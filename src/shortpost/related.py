"""Pick the long-form video a Short should link to as YouTube's "Related video" (CLAUDE.md D-015).

The YouTube API cannot set the field, so Studio browser mode does it. Steps:
1. `fetch_long_videos` reads the channel's long-form list from Studio (cached in data/channel_videos.json).
2. `rank` scores every long video against the Short's products, keywords and topic (deterministic, word based).
3. `choose` asks the local model to pick the best of the top candidates (or "none"); without a model answer the top
   deterministic match is used when it is strong enough. The AI can only return ids from the candidate list.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import browser, config
from .log import logger

STOP = set("a an and are as at be best by for free from how i in into is it its my of on or plugin plugins review short the this "
           "to use using vst with your you what why fl studio part trap hiphop beats beat type prod samastano video title description goes here".split())
CACHE_MAX_AGE = timedelta(days=1)
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["video_id", "reason"],
          "properties": {"video_id": {"type": "string"}, "reason": {"type": "string", "maxLength": 160}}}


def cache_path(cfg: dict) -> Path:
    return config.resolve(cfg["general"]["db_path"]).parent / "channel_videos.json"


def words(text: str) -> set[str]:
    """Lowercase word tokens (>=3 chars, no filler words); '#tags' and @handles count as words too."""
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) >= 3 and w not in STOP}


def scrape_long_videos(cfg: dict, limit: int = 80) -> list[dict]:
    """Studio's Videos tab (long-form): [{id, title, snippet}] newest first. Needs the ShortPost Chrome."""
    from playwright.sync_api import sync_playwright

    browser.launch(cfg)
    out: list[dict] = []
    with sync_playwright() as pw:
        _, ctx = browser.connect(pw, cfg)
        page = ctx.new_page()
        page.set_default_timeout(30000)
        try:
            page.goto(browser.STUDIO + "/videos/upload", wait_until="domcontentloaded")
            page.wait_for_selector("ytcp-video-row", timeout=60000)
            page.wait_for_timeout(2500)
            for _ in range(4):  # the list is lazy: scroll to the end a few times
                if page.locator("ytcp-video-row").count() >= limit:
                    break
                page.mouse.wheel(0, 20000)
                page.wait_for_timeout(1200)
            rows = page.locator("ytcp-video-row")
            for i in range(min(rows.count(), limit)):
                r = rows.nth(i)
                link = r.locator("a[href*='/video/']")
                href = link.first.get_attribute("href", timeout=2000) if link.count() else ""
                m = re.search(r"/video/([\w-]{11})", href or "")
                lines = [x.strip() for x in r.inner_text().split("\n") if x.strip()]
                if not m or len(lines) < 2:  # drafts and half-loaded rows have no link
                    continue
                out.append({"id": m.group(1), "title": lines[1][:150], "snippet": " ".join(lines[2:3])[:200]})
        finally:
            page.close()
    return out


def load_cache(cfg: dict) -> tuple[list[dict], datetime | None]:
    p = cache_path(cfg)
    if not p.is_file():
        return [], None
    doc = json.loads(p.read_text(encoding="utf-8"))
    return doc.get("videos", []), datetime.fromisoformat(doc["fetched_at"])


def long_videos(cfg: dict, refresh: bool = False, now: datetime | None = None) -> list[dict]:
    """Cached list; refreshed when older than a day and the ShortPost Chrome is already running (or refresh=True)."""
    now = now or datetime.now(timezone.utc)
    videos, at = load_cache(cfg)
    stale = at is None or now - at > CACHE_MAX_AGE
    if refresh or (stale and browser.is_up(cfg)):
        try:
            fresh = scrape_long_videos(cfg)
        except Exception as e:  # noqa: BLE001 - a Studio hiccup must not block adding a video
            logger.warning("could not read the channel's long videos: %s", e)
            return videos
        if fresh:
            cache_path(cfg).parent.mkdir(parents=True, exist_ok=True)
            cache_path(cfg).write_text(json.dumps({"fetched_at": now.isoformat(timespec="seconds"), "videos": fresh},
                                                   ensure_ascii=False, indent=1), encoding="utf-8")
            return fresh
    return videos


def rank(core: dict, topic: str, notes: str, videos: list[dict]) -> list[tuple[float, dict]]:
    """Score each video: product words x4 (title) / x2 (snippet), keyword and topic words x1.5 / x0.5, category x1."""
    prod = set().union(*[words(p) for p in core.get("products", [])] or [set()])
    kw = set().union(*[words(k) for k in core.get("keywords", [])] or [set()]) - prod
    base = (words(topic) | words(notes) | words(core.get("category", ""))) - prod - kw
    scored = []
    for v in videos:
        t, s = words(v["title"]), words(v.get("snippet", ""))
        score = 4 * len(prod & t) + 2 * len(prod & s) + 1.5 * len(kw & t) + 0.5 * len(kw & s) + 1.0 * len(base & t) + 0.25 * len(base & s)
        scored.append((score, v))
    scored.sort(key=lambda x: -x[0])
    return scored


def choose(cfg: dict, core: dict, topic: str, notes: str, videos: list[dict], llm=None, min_score: float = 4.0, top: int = 6) -> dict | None:
    """{'id','title','reason','score','by'} or None when nothing on the channel fits."""
    ranked = rank(core, topic, notes, videos)
    cands = [(s, v) for s, v in ranked[:top] if s > 0]
    if not cands:
        return None
    best_score, best = cands[0]
    if llm is not None:
        listing = "\n".join(f"- id {v['id']}: {v['title']}" + (f" | {v['snippet']}" if v.get("snippet") else "") for _, v in cands)
        user = (f"A new YouTube Short: topic '{topic}'. Plugins named: {', '.join(core.get('products', [])) or 'none'}. "
                f"Summary: {core.get('summary', '')}\n\nLong videos on the same channel:\n{listing}\n\n"
                "Pick the ONE long video a viewer of this Short would most likely want to watch next: the same plugin or product "
                "first, then the same technique or topic. If none of them is really about the same plugin or topic, answer video_id "
                "\"none\". Copy video_id exactly from the list.")
        system = "You match short videos to related long videos on one channel. Answer with JSON only."
        ans = llm.ask(system, user, SCHEMA, seed=1, last=False)
        if ans:
            vid = ans["video_id"].strip()
            if vid == "none":
                return None
            pick = next(((s, v) for s, v in cands if v["id"] == vid), None)
            if pick:
                return {"id": pick[1]["id"], "title": pick[1]["title"], "reason": ans["reason"], "score": pick[0], "by": "ai"}
            logger.warning("related: model answered an id outside the candidates (%s); using the word match", vid)
    if best_score >= min_score:
        return {"id": best["id"], "title": best["title"], "reason": "word match with the title", "score": best_score, "by": "words"}
    return None


def attach(cfg: dict, vi, meta: dict, rendered: dict, llm=None) -> str | None:
    """Fill rendered['youtube']['related'] (or remove it). Returns a warning text when YouTube is on but nothing was found."""
    y = rendered.get("youtube")
    if y is None or not cfg["platforms"]["youtube"].get("related_video", True):
        return None
    vids = long_videos(cfg)
    if not vids:
        y.pop("related", None)
        return "no long-video list yet: run `shortpost sync` (ShortPost Chrome must be open) to find the Related video"
    pick = choose(cfg, meta.get("core", {}), vi.parsed.topic, vi.notes, vids, llm)
    if pick:
        y["related"] = pick
        return None
    y.pop("related", None)
    return "no long video on the channel matches this topic: set Related video by hand if you want one"
