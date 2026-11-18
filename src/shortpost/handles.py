"""Saved @handle sets (YouTube / Facebook / Instagram) that can be appended to a draft's text (D-020).

A set = a name plus any number of handles per platform. Sets live in data/handles.json and are offered in a dropdown.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

PLATFORMS = ("youtube", "facebook", "instagram", "tiktok")
LIMITS = {"youtube": 5000, "facebook": 2000, "instagram": 2200, "tiktok": 2200}
PREFIX = "With "


def parse(value) -> list[str]:
    """'@a @b, @c' (or a list) -> ['@a', '@b', '@c'], deduplicated, each with one leading @."""
    text = " ".join(value) if isinstance(value, list) else (value or "")
    out: list[str] = []
    for h in re.findall(r"@?[\w.]+", text):
        h = "@" + h.lstrip("@")
        if h != "@" and h.lower() not in [x.lower() for x in out]:
            out.append(h)
    return out


def load(path: Path) -> list[dict]:
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    for i in items:  # older single-handle records
        for p in PLATFORMS:
            i[p] = parse(i.get(p, []))
    return items


def save(path: Path, items: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def spec_of(b: dict) -> dict:
    return {p: parse(b.get(p, "")) for p in PLATFORMS}


def upsert(items: list[dict], b: dict) -> list[dict]:
    """Save a set; a set with the same name (any case) is replaced."""
    spec = spec_of(b)
    if not any(spec.values()):
        raise ValueError("enter at least one handle")
    name = (b.get("name") or "").strip() or next(h for p in PLATFORMS for h in spec[p][:1])
    old = next((i for i in items if i["name"].lower() == name.lower()), None)
    rec = {"id": old["id"] if old else uuid.uuid4().hex[:8], "name": name, **spec}
    return [i for i in items if i is not old] + [rec]


def remove(items: list[dict], hid: str) -> list[dict]:
    return [i for i in items if i["id"] != hid]


def already_there(rendered: dict, spec: dict) -> bool:
    """True when every handle given is already in the text of every platform that has handles (nothing new to add)."""
    for p in PLATFORMS:
        r = rendered.get(p)
        if r and spec.get(p) and any(not re.search(re.escape(h) + r"(?!\w)", r.get("text", ""), re.I) for h in spec[p]):
            return False
    return True


def apply(rendered: dict, spec: dict) -> dict[str, list[str]]:
    """Append the handles to the end of the text of each platform that has some.

    -> {platform: [handles added]}. Handles already in the text are skipped; a platform whose limit would be
    exceeded is left unchanged (reported under '!platform')."""
    report: dict[str, list[str]] = {}
    for p in PLATFORMS:
        r = rendered.get(p)
        if not r:
            continue
        text = r.get("text", "")
        new = [h for h in spec.get(p, []) if not re.search(re.escape(h) + r"(?!\w)", text, re.I)]
        if not new:
            continue
        out = f"{text.rstrip()}\n\n{PREFIX}{' '.join(new)}"
        if len(out) > LIMITS[p]:
            report["!" + p] = new
            continue
        r["text"] = out
        report[p] = new
    return report
