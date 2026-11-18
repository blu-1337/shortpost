"""`shortpost doctor`: environment check."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from . import config, media

OK, WARN, FAIL = "OK", "WARN", "FAIL"


@dataclass
class Check:
    name: str
    status: str
    detail: str


def check_ffmpeg() -> list[Check]:
    out = []
    for tool in ("ffmpeg", "ffprobe"):
        path = media.find_tool(tool)
        out.append(Check(tool, OK if path else FAIL, path or "not found (install.ps1 installs Gyan.FFmpeg)"))
    return out


def check_ollama(cfg: dict, client: httpx.Client | None = None) -> list[Check]:
    host = cfg["ai"]["host"]
    try:
        get = client.get if client else httpx.get
        tags = get(f"{host}/api/tags", timeout=5).json()
    except (httpx.HTTPError, ValueError) as e:
        return [Check("Ollama", FAIL, f"not reachable at {host} ({type(e).__name__})")]
    have = {m["name"] for m in tags.get("models", [])}
    out = [Check("Ollama", OK, f"{host}, {len(have)} models")]
    for model in dict.fromkeys([cfg["ai"]["model"], cfg["ai"]["fallback_model"]]):
        present = model in have or f"{model}:latest" in have
        out.append(Check(f"model {model}", OK if present else FAIL, "pulled" if present else f"run: ollama pull {model}"))
    return out


def check_gemini(cfg: dict) -> Check:
    from .ai import llm

    if "gemini" not in cfg["ai"].get("providers", ["ollama"]):
        return Check("Gemini", OK, "off (local model only)")
    if not llm.gemini_key():
        return Check("Gemini", WARN, "no gemini_api_key secret: using the local model only")
    return Check("Gemini", OK, f"key stored, model {cfg['ai'].get('gemini_model')} (local model is the fallback)")


def check_config(cfg: dict) -> Check:
    missing = [s for s in ("general", "schedule", "ai", "links", "platforms") if s not in cfg]
    if missing:
        return Check("config", FAIL, "missing sections: " + ", ".join(missing))
    enabled = [p for p, v in cfg["platforms"].items() if v.get("enabled")]
    return Check("config", OK, "enabled platforms: " + ", ".join(enabled))


def check_dirs(cfg: dict) -> Check:
    db_dir = config.resolve(cfg["general"]["db_path"]).parent
    try:
        db_dir.mkdir(parents=True, exist_ok=True)
        probe = db_dir / ".write-test"
        probe.write_text("x")
        probe.unlink()
    except OSError as e:
        return Check("data dir writable", FAIL, str(e))
    return Check("data dir writable", OK, str(db_dir))


def check_ntfy(secrets: dict) -> Check:
    if secrets.get("ntfy_topic"):
        return Check("ntfy topic", OK, "set")
    return Check("ntfy topic", WARN, "run: shortpost set-secret ntfy_topic --generate")


def check_youtube_mode(cfg: dict) -> Check:
    yt = cfg["platforms"]["youtube"]
    if yt["mode"] == "api" and not yt.get("audit_approved"):
        return Check("youtube mode", FAIL, "mode=api but audit_approved=false: API uploads would be locked Private")
    return Check("youtube mode", OK, yt["mode"])


def check_token(cfg: dict, platform: str) -> Check | None:
    """Stored token present and, for Instagram, not about to expire (60 days). None when the platform is disabled."""
    from datetime import datetime, timedelta, timezone

    from . import tokens

    if not cfg["platforms"].get(platform, {}).get("enabled"):
        return None
    name = f"{platform} token"
    if platform == "tiktok":
        tt = tokens.load("tiktok")
        if not tt:
            return Check(name, WARN, "run: shortpost auth tiktok")
        left = (datetime.fromisoformat(tt["refresh_expires_at"]) - datetime.now(timezone.utc)).days
        return Check(name, OK if left > 7 else WARN, f"refresh token valid {left} more days")
    if platform == "threads":
        th = tokens.load("threads")
        if not th:
            return Check(name, WARN, "run: shortpost auth threads")
        left = (datetime.fromisoformat(th["expires_at"]) - datetime.now(timezone.utc)).days
        return Check(name, OK if left > 7 else WARN, f"@{th.get('username', '')}, expires in {left} days (auto-refreshed on use)")
    if platform == "pinterest":
        pt = tokens.load("pinterest")
        if not pt:
            return Check(name, WARN, "run: shortpost auth pinterest (needs pinterest_app_id/secret secrets)")
        left = (datetime.fromisoformat(pt["refresh_expires_at"]) - datetime.now(timezone.utc)).days
        board = pt.get("board_name") or "NO BOARD"
        return Check(name, OK if left > 7 and pt.get("board_id") else WARN, f"board '{board}', refresh token valid {left} more days")
    if platform == "bluesky":
        s = config.load_secrets()
        ok = s.get("bluesky_handle") and s.get("bluesky_app_password")
        return Check(name, OK if ok else WARN, "handle + app password set" if ok else "run: set-secret bluesky_handle / bluesky_app_password")
    t = tokens.load("facebook")  # Instagram publishes with the Page token
    if not t:
        return Check(name, WARN, "run: shortpost auth facebook")
    if platform == "instagram":
        if not t.get("ig_user_id"):
            return Check(name, WARN, "no Instagram account linked; re-run auth facebook with instagram_basic + instagram_content_publish")
        return Check(name, OK, f"@{t.get('ig_username', '')} via the Page token")
    return Check(name, OK, "stored")


def check_task() -> Check:
    """The 'ShortPost Run' Task Scheduler entry (install\\register-task.ps1). WARN only: manual use works without it."""
    import subprocess

    try:
        r = subprocess.run(["schtasks", "/query", "/tn", "ShortPost Run"], capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return Check("task scheduler", WARN, "could not query schtasks")
    if r.returncode == 0:
        return Check("task scheduler", OK, "'ShortPost Run' registered")
    return Check("task scheduler", WARN, "not registered: powershell -ExecutionPolicy Bypass -File install\\register-task.ps1")


def run_checks(cfg: dict, secrets: dict) -> list[Check]:
    checks = [check_config(cfg), check_youtube_mode(cfg)]
    checks += check_ffmpeg()
    checks += check_ollama(cfg)
    checks.append(check_gemini(cfg))
    checks += [check_dirs(cfg), check_ntfy(secrets)]
    checks.append(check_task())
    checks += [c for c in (check_token(cfg, p) for p in ("facebook", "instagram", "tiktok", "threads", "bluesky", "pinterest")) if c]
    return checks


def print_table(checks: list[Check]) -> int:
    mark = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]"}
    for c in checks:
        print(f"{mark[c.status]} {c.name:<24} {c.detail}")
    return 1 if any(c.status == FAIL for c in checks) else 0
