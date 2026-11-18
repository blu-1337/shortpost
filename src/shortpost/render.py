"""Render per-platform text from AI output + deterministic links/hashtags (CLAUDE.md 7, 10.3-10.4).

What ends up in ``rendered[platform]`` is exactly what gets posted.
"""

from __future__ import annotations

import re

import regex

from . import affiliates
from .affiliates import Entry
from .ai.metadata import trim_words
from .config import REPO_ROOT
from .naming import VideoInput

TEMPLATES = REPO_ROOT / "templates"


def graphemes(s: str) -> int:
    return len(regex.findall(r"\X", s))


def fill(name: str, **vals: str) -> str:
    """Fill a template; empty placeholders leave no blank-line gaps."""
    text = (TEMPLATES / name).read_text(encoding="utf-8")
    for k, v in vals.items():
        text = text.replace("{" + k + "}", v)
    text = "\n".join(line.rstrip() for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def hashtags(cfg: dict, core: dict, n: int, rot: int = 0) -> list[str]:
    """Brand tags first (leaving one slot for a topical tag), then topical ones; beat-only tags gated."""
    brand = list(cfg["hashtags"]["brand"])
    beat_only = list(cfg["hashtags"]["beat_only"])
    is_beat = core.get("is_beat_video", False)
    topical = [t for t in core.get("hashtags", []) if t not in brand and (is_beat or t not in beat_only)]
    if is_beat:
        topical = beat_only + [t for t in topical if t not in beat_only]
    if topical and rot:
        k = rot % len(topical)
        topical = topical[k:] + topical[:k]  # each platform starts at a different topical tag
    if n <= 0:
        return []
    if n == 1:
        pool = topical or brand
        return pool[:1]
    take_brand = min(len(brand), n - 1)
    out = brand[:take_brand]
    out += [t for t in topical if t not in out][: n - len(out)]
    if len(out) < n:
        out += [b for b in brand if b not in out][: n - len(out)]
    return out


def _tags(tags: list[str]) -> str:
    return " ".join("#" + t for t in tags)


def link_line(cfg: dict, e: Entry | None) -> str:
    if e is None:
        return ""
    if e.is_affiliate:
        return " ".join(x for x in (f"🛒 {e.label}: {e.url}", e.code_text, cfg["links"]["disclosure_short"]) if x)
    return f"🎁 {e.label}: {e.url}"


def youtube_line(e: Entry) -> str:
    if e.is_affiliate:
        extra = f"  {e.code_text}" if e.code_text else ""
        return f"🛒 {e.label} → {e.url}{extra}"
    return f"🎁 {e.label} → {e.url}"


def series_suffix(cfg: dict, vi: VideoInput) -> str:
    s = vi.parsed.series
    if not s or not cfg["series"].get("detect", True):
        return ""
    if s["of"]:
        return cfg["series"]["title_suffix"].format(n=s["part"], of=s["of"])
    return f" (Part {s['part']})"


def series_line(cfg: dict, vi: VideoInput) -> str:
    s = vi.parsed.series
    if s and s["of"] and s["of"] > s["part"] and cfg["series"].get("consecutive_days"):
        return f"Part {s['part'] + 1} tomorrow."
    return ""


def pick_title(core: dict, budget: int, lo: int) -> str:
    cands = [core.get("title", "")] + list(core.get("title_options", []))
    cands = [c.strip() for c in cands if c.strip()]
    for c in cands:
        if lo <= len(c) <= budget:
            return c
    base = cands[0] if cands else "Untitled Short"
    return trim_words(base, budget)


def youtube_tags(core: dict, cfg: dict) -> list[str]:
    """Fill the whole tags field (tags_total_chars, 500 by default), most important tags first.

    Order: the AI's own tags, its search keywords, product-based phrases, then evergreen channel tags.
    Candidates that do not fit are skipped (not break), so short tags fill the last gap."""
    y = cfg["platforms"]["youtube"]
    budget, cap = y["tags_total_chars"], y.get("tags_max", 60)
    products = [p for p in core.get("products", []) if p]
    category = core.get("category", "")
    cand = list(core.get("youtube_tags", [])) + list(core.get("keywords", []))
    for p in products:
        cand += [p, f"{p} plugin", f"{p} vst", f"{p} review", f"{p} tutorial", f"how to use {p}",
                 f"{p} fl studio", f"{p} settings", f"{p} demo"]
    if category:
        cand += [category, f"best {category}", f"{category} plugin", f"{category} vst"]
    cand += list(y.get("tags_extra", []))
    if core.get("is_beat_video"):
        cand += list(cfg["hashtags"].get("beat_only", [])) + list(y.get("tags_extra_beats", []))
    out, total, seen = [], 0, set()
    for t in cand:
        t = re.sub(r"\s+", " ", str(t)).strip().strip("#")
        if not t or "<" in t or ">" in t or len(t) > 40 or t.lower() in seen:
            continue
        cost = len(t) + (2 if " " in t else 0) + (1 if out else 0)
        if total + cost > budget:
            continue
        if len(out) >= cap:
            break
        out.append(t)
        seen.add(t.lower())
        total += cost
    return out


def render_all(cfg: dict, catalog: list[Entry], vi: VideoInput, meta: dict, only: set[str] | None = None) -> dict:
    """-> {platform: {"text": ..., plus extras, "warnings": [...]}} for every enabled platform."""
    core, copy = meta["core"], meta["platform"]
    L, P = cfg["links"], cfg["platforms"]
    is_beat = core.get("is_beat_video", False)
    match_text = " ".join([vi.parsed.topic, vi.notes] + core.get("products", []))
    directive = vi.link_directive
    sfx = series_suffix(cfg, vi)
    sline = series_line(cfg, vi)

    focus = meta.get("focus", {})

    def rot(plat):
        return focus.get(plat, {}).get("rot", 0)

    def pick(plat):
        return affiliates.select(catalog, plat, match_text, is_beat, directive)

    out: dict[str, dict] = {}
    enabled = {p for p, v in P.items() if v.get("enabled")}
    if only:
        enabled &= only

    if "youtube" in enabled:
        y = P["youtube"]
        picked = pick("youtube")
        title = pick_title(core, y["title_max"] - len(sfx), max(y["title_min"] - len(sfx), 1)) + sfx
        title = title.replace("<", "").replace(">", "")
        tags = _tags(hashtags(cfg, core, y["hashtags"], rot("youtube")) + (["shorts"] if y.get("add_shorts_hashtag") else []))
        desc = fill(
            "youtube_description.txt", hook=core["hook"], summary=core["summary"],
            affiliate_block="\n".join(youtube_line(e) for e in picked), hub_text=L["hub_text"], channel=L["channel"],
            disclosure=L["disclosure_long"] if any(e.is_affiliate for e in picked) else "", hashtags=tags,
        )
        w = []
        if not y["title_min"] <= len(title) <= y["title_max"]:
            w.append(f"title is {len(title)} chars (target {y['title_min']}-{y['title_max']})")
        out["youtube"] = {"title": title, "text": desc[: y["description_max"]], "tags": youtube_tags(core, cfg),
                          "links": [e.id for e in picked], "warnings": w}

    if "facebook" in enabled:
        f = P["facebook"]
        tags = _tags(hashtags(cfg, core, f["hashtags"], rot("facebook")))
        body = " ".join(x for x in (copy["facebook_description"], sline) if x)
        body = trim_words(body, max(f["description_max"] - len(tags) - 2, 40))
        picked = pick("facebook")
        out["facebook"] = {"text": fill("facebook.txt", description=body, hashtags=tags),
                           "comment": fill("facebook_comment.txt", link_line=link_line(cfg, picked[0] if picked else None)),
                           "links": [e.id for e in picked], "warnings": []}

    for plat, key in (("instagram", "instagram_caption"), ("tiktok", "tiktok_caption")):
        if plat not in enabled:
            continue
        c = P[plat]
        tags = _tags(hashtags(cfg, core, c["hashtags"], rot(plat)))
        caption = copy[key]
        picked = pick(plat)
        if picked and picked[0].is_affiliate and picked[0].code_text:
            e = picked[0]
            off = e.discount.replace(" through this link", "")
            how = f"with code {e.code}, link in bio" if e.code else "via link in bio"
            caption += f" {off} {e.label} {how} {L['disclosure_short']}"
        if sline:
            caption += " " + sline
        cap_limit = c["caption_max"] if plat == "instagram" else c["caption_target"]
        caption = trim_words(caption, cap_limit)
        text = fill(f"{plat}.txt", caption=caption, link_in_bio_cta=L["link_in_bio_cta"], hashtags=tags)
        w = []
        if plat == "tiktok":
            w.append("TikTok caption goes to your phone via ntfy (API cannot set it)")
        out[plat] = {"text": text[: c["caption_max"]], "links": [e.id for e in picked], "warnings": w}

    if "threads" in enabled:
        t = P["threads"]
        picked = pick("threads")
        tag = _tags(hashtags(cfg, core, t["hashtags"], rot("threads")))
        line = link_line(cfg, picked[0] if picked else None)
        body = " ".join(x for x in (copy["threads_text"], sline) if x)
        room = t["text_max"] - len(line) - len(tag) - 6
        out["threads"] = {"text": fill("threads.txt", text=trim_words(body, max(room, 40)), link_line=line, hashtag=tag),
                          "links": [e.id for e in picked], "warnings": []}

    if "bluesky" in enabled:
        b = P["bluesky"]
        limit = b["text_max_graphemes"]
        picked = pick("bluesky")
        e = picked[0] if picked else None
        tags = hashtags(cfg, core, b["hashtags"], rot("bluesky"))
        label = ""
        if e:
            label = f"🛒 {e.label}" if e.is_affiliate else f"🎁 {e.label}"
            if e.is_affiliate:
                label = " ".join(x for x in (label, e.code_text, L["disclosure_short"]) if x)
        body = copy["bluesky_text"]

        def build(body_, tags_):
            return fill("bluesky.txt", text=body_, link_line=label, hashtags=_tags(tags_))

        text = build(body, tags)
        while graphemes(text) > limit and tags:
            tags = tags[:-1]
            text = build(body, tags)
        if graphemes(text) > limit:
            room = limit - graphemes(build("", tags)) - 2
            text = build(trim_words(body, max(room, 20)), tags)
        out["bluesky"] = {"text": text, "link_label": label, "link_url": e.url if e else "", "tags": tags,
                          "alt_text": core.get("alt_text", ""), "links": [e.id] if e else [], "warnings": []}

    if "pinterest" in enabled:
        p = P["pinterest"]
        picked = pick("pinterest")
        affil = any(e.is_affiliate for e in picked)
        title = trim_words(copy["pinterest_title"], p["title_max"] - len(sfx)) + sfx
        desc = fill("pinterest.txt", description=copy["pinterest_description"],
                    disclosure=L["disclosure_long"] if affil else "", hashtags=_tags(hashtags(cfg, core, p["hashtags"], rot("pinterest"))))
        dest = affiliates.pinterest_destination(catalog, match_text, is_beat, L["channel"], directive)
        out["pinterest"] = {"title": title, "text": trim_words(desc, p["description_max"]), "link": dest,
                            "alt_text": core.get("alt_text", ""), "links": [e.id for e in picked], "warnings": []}
    return out


def coverage(rendered: dict) -> dict:
    """How many distinct hashtags and focus words the posts cover together (more = wider search reach)."""
    tags = set()
    for r in rendered.values():
        tags |= {t.lower() for t in re.findall(r"#(\w+)", r["text"])}
    return {"unique_hashtags": len(tags), "hashtags": sorted(tags)}
