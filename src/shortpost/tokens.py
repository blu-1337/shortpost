"""Token files in %APPDATA%\\ShortPost\\tokens\\<name>.json (never in the repo, never logged)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config


def _path(name: str, user_dir: Path | None = None) -> Path:
    return (user_dir or config.app_dir()) / "tokens" / f"{name}.json"


def load(name: str, user_dir: Path | None = None) -> dict | None:
    p = _path(name, user_dir)
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


def save(name: str, data: dict, user_dir: Path | None = None) -> None:
    p = _path(name, user_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {**data, "obtained_at": data.get("obtained_at") or datetime.now(timezone.utc).isoformat(timespec="seconds")}
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def age(tok: dict, now: datetime | None = None) -> timedelta:
    return (now or datetime.now(timezone.utc)) - datetime.fromisoformat(tok["obtained_at"])
