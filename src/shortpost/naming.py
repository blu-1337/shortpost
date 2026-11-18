"""Filename -> topic, series detection, sidecar notes, variant pairing (CLAUDE.md 10.2)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_SUFFIX = re.compile(r"(?:[_-]H\d+|[_-]facebook|[_-]fb|[_-]final|[_-]v\d+|[_-]\d{3,4}p)$", re.I)
_LEADING_DATE = re.compile(r"^(?:\d{4}-\d{2}-\d{2}|\d{8})[_-]?")
_SERIES = re.compile(r"(?:^|[-_ ])(?:part|pt)[-_ ]?(\d+)(?:[-_ ]?(?:of|from)[-_ ]?(\d+))?$", re.I)
_LINK_DIRECTIVE = re.compile(r"^\s*link\s*:\s*(\S+)\s*$", re.I | re.M)


@dataclass
class Parsed:
    topic: str
    series: dict | None = None  # {"part": n, "of": m | None}
    series_key: str | None = None


@dataclass
class VideoInput:
    path: Path
    parsed: Parsed
    notes: str = ""
    link_directive: str | None = None
    variants: dict = field(default_factory=dict)  # e.g. {"facebook": Path}


def _strip_suffixes(s: str) -> str:
    while True:
        new = _SUFFIX.sub("", s)
        if new == s:
            return s
        s = new


def _words(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[-_.]", " ", s)).strip()


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def parse_stem(stem: str, detect_series: bool = True) -> Parsed:
    s = _strip_suffixes(stem)
    s = _LEADING_DATE.sub("", s)
    series = None
    if detect_series:
        m = _SERIES.search(s)
        if m:
            series = {"part": int(m.group(1)), "of": int(m.group(2)) if m.group(2) else None}
            s = _strip_suffixes(s[: m.start()])
    topic = _words(s)
    return Parsed(topic=topic, series=series, series_key=slugify(topic) if series else None)


def find_variants(path: Path) -> dict:
    out = {}
    for suffix in ("_facebook", "-facebook", "_fb", "-fb"):
        cand = path.with_name(path.stem + suffix + path.suffix)
        if cand.is_file():
            out["facebook"] = cand
            break
    return out


def is_variant_file(path: Path) -> bool:
    """True for `<stem>_facebook.mp4` style files that belong to another video."""
    m = re.search(r"(?:[_-]facebook|[_-]fb)$", path.stem, re.I)
    if not m:
        return False
    base = path.with_name(path.stem[: m.start()] + path.suffix)
    return base.is_file()


def read_notes(path: Path) -> tuple[str, str | None]:
    """Sidecar `<stem>.txt` -> (notes text, link directive id or None)."""
    side = path.with_suffix(".txt")
    if not side.is_file():
        return "", None
    text = side.read_text(encoding="utf-8").strip()
    m = _LINK_DIRECTIVE.search(text)
    directive = m.group(1).lower() if m else None
    return text, directive


def load_video(path: Path, detect_series: bool = True) -> VideoInput:
    notes, directive = read_notes(path)
    return VideoInput(path, parse_stem(path.stem, detect_series), notes, directive, find_variants(path))
