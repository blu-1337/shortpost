"""Config loading: repo config.toml deep-merged with %APPDATA%\\ShortPost\\config.toml; secrets separate."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def app_dir() -> Path:
    """%APPDATA%\\ShortPost (secrets, tokens, user config overrides)."""
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "ShortPost"


def deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _read(path: Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


def load_config(path: Path | None = None, user_dir: Path | None = None) -> dict:
    cfg = _read(path or REPO_ROOT / "config.toml")
    user = (user_dir or app_dir()) / "config.toml"
    if user.is_file():
        cfg = deep_merge(cfg, _read(user))
    return cfg


def load_secrets(user_dir: Path | None = None) -> dict:
    p = (user_dir or app_dir()) / "secrets.toml"
    return _read(p) if p.is_file() else {}


def save_secret(key: str, value: str, user_dir: Path | None = None) -> None:
    """Store/replace one string secret in a flat TOML file; values are escaped."""
    d = user_dir or app_dir()
    d.mkdir(parents=True, exist_ok=True)
    secrets = load_secrets(d)
    secrets[key] = value
    lines = []
    for k, v in sorted(secrets.items()):
        esc = str(v).replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'{k} = "{esc}"')
    (d / "secrets.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def resolve(cfg_path: str, base: Path | None = None) -> Path:
    p = Path(cfg_path)
    return p if p.is_absolute() else (base or REPO_ROOT) / p
