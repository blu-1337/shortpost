"""Slot finding and fire times (CLAUDE.md 10.1). M1: pure functions without the DB; M2 adds occupied days from SQLite."""

from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

NATIVE = ("youtube", "facebook")


def _hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def platform_mode(cfg: dict, platform: str) -> str:
    """native | local_time | inbox. YouTube's config mode (browser|api) is a method; both schedule natively."""
    if platform == "youtube":
        return "native"
    return cfg["platforms"][platform]["mode"]


def fire_time(cfg: dict, platform: str, day: date, jitter_min: int) -> datetime:
    """UTC go-live time of ``platform`` for slot day ``day`` (a New York calendar date)."""
    s = cfg["schedule"]
    if platform_mode(cfg, platform) == "native":
        tz, t = ZoneInfo(cfg["general"]["target_tz"]), _hm(s["native_time"])
    else:
        tz, t = ZoneInfo(cfg["general"]["local_tz"]), _hm(s["local_time"])
    local = datetime.combine(day, t, tzinfo=tz) + timedelta(minutes=jitter_min)
    return local.astimezone(timezone.utc)


def jitter_range(cfg: dict, platform: str) -> int:
    s = cfg["schedule"]
    return int(s["native_jitter_minutes"] if platform_mode(cfg, platform) == "native" else s["local_jitter_minutes"])


def draw_jitter(cfg: dict, platform: str, rng: random.Random | None = None) -> int:
    r = (rng or random).randint
    j = jitter_range(cfg, platform)
    return r(-j, j)


def today_in(tz_name: str, now_utc: datetime) -> date:
    return now_utc.astimezone(ZoneInfo(tz_name)).date()


def enabled_platforms(cfg: dict) -> list[str]:
    return [p for p, v in cfg["platforms"].items() if v.get("enabled")]


def find_slot(cfg: dict, now_utc: datetime, occupied: set[date] | None = None, requested: date | None = None,
              not_before: date | None = None) -> date:
    occupied = occupied or set()
    day = requested or today_in(cfg["general"]["target_tz"], now_utc)
    if not_before and day <= not_before:
        day = not_before + timedelta(days=1)
    lead = timedelta(minutes=int(cfg["schedule"]["min_lead_minutes"]))
    plats = enabled_platforms(cfg)
    for _ in range(3650):
        earliest = min(fire_time(cfg, p, day, -jitter_range(cfg, p)) for p in plats)
        if day not in occupied and earliest >= now_utc + lead:
            return day
        day += timedelta(days=1)
    raise RuntimeError("no free slot found")


def plan_jitters(cfg: dict, platforms: list[str], seed: int) -> dict[str, int]:
    """Reproducible per-platform jitter: the preview and the saved posts use the same values."""
    rng = random.Random(seed)
    return {p: draw_jitter(cfg, p, rng) for p in platforms}
