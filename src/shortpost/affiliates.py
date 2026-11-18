"""Deterministic link selection from affiliates.toml (CLAUDE.md 8.5). The AI never picks or writes URLs."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .config import REPO_ROOT


@dataclass
class Entry:
    id: str
    label: str
    url: str
    kind: str  # "link" (affiliate, needs disclosure) | "promo" (own product)
    platforms: list[str]
    keywords: list[str] = field(default_factory=list)
    code: str = ""
    discount: str = ""
    priority: int = 0
    use: str = ""

    @property
    def is_affiliate(self) -> bool:
        return self.kind == "link"

    @property
    def code_text(self) -> str:
        if self.code and self.discount:
            return f"(code {self.code} = {self.discount})"
        if self.discount:
            return f"({self.discount})"
        return ""


def load_catalog(path: Path | None = None) -> list[Entry]:
    with (path or REPO_ROOT / "affiliates.toml").open("rb") as f:
        raw = tomllib.load(f)
    out = [Entry(kind="link", **d) for d in raw.get("link", [])]
    out += [Entry(kind="promo", **d) for d in raw.get("promo", [])]
    return out


def _matches(entry: Entry, text: str) -> bool:
    for kw in entry.keywords:
        if re.search(rf"(?<![a-z0-9]){re.escape(kw.lower())}(?![a-z0-9])", text, re.I):
            return True
    return False


def _find(catalog: list[Entry], id_: str) -> Entry | None:
    return next((e for e in catalog if e.id == id_), None)


def select(catalog: list[Entry], platform: str, text: str, is_beat: bool, directive: str | None = None) -> list[Entry]:
    """Entries to show on ``platform``. YouTube: up to 2 deals (+ a keyword promo). Others: exactly one or none."""
    if directive == "none":
        return []
    if directive:
        forced = _find(catalog, directive)
        return [forced] if forced else []
    here = [e for e in catalog if platform in e.platforms]
    links = sorted((e for e in here if e.kind == "link" and _matches(e, text)), key=lambda e: -e.priority)
    kw_promos = [e for e in here if e.kind == "promo" and e.use == "keyword" and _matches(e, text)]
    if platform == "youtube":
        return links[:2] + kw_promos[:1]
    if links:
        return links[:1]
    if kw_promos:
        return kw_promos[:1]
    want = "beat_video" if is_beat else "fallback"
    return [e for e in here if e.kind == "promo" and e.use == want][:1]


def pinterest_destination(catalog: list[Entry], text: str, is_beat: bool, channel: str, directive: str | None = None) -> str:
    """Rule 5: matched deal, else the beat promo for beat videos, else the channel."""
    picked = select(catalog, "pinterest", text, is_beat, directive)
    if picked:
        return picked[0].url
    if is_beat:
        beat = next((e for e in catalog if e.use == "beat_video"), None)
        if beat:
            return beat.url
    return channel
