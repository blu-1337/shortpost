"""Real Chrome with a dedicated profile + CDP attach (CLAUDE.md 9). Never touches the user's everyday profile."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import httpx

CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
CHANNEL_ID = "UC8Y5eseUJdNOQt1UlEuB_Ug"
STUDIO = f"https://studio.youtube.com/channel/{CHANNEL_ID}"


def profile_dir(cfg: dict) -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ShortPost" / "chrome"
    return base / cfg["platforms"]["youtube"].get("chrome_profile", "youtube")


def cdp_url(cfg: dict) -> str:
    return f"http://127.0.0.1:{cfg['platforms']['youtube'].get('cdp_port', 9333)}"


def is_up(cfg: dict) -> bool:
    try:
        return httpx.get(cdp_url(cfg) + "/json/version", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def launch(cfg: dict, url: str = STUDIO) -> None:
    """Start Chrome (if not already listening on the CDP port) with the dedicated profile."""
    if is_up(cfg):
        return
    prof = profile_dir(cfg)
    prof.mkdir(parents=True, exist_ok=True)
    port = cfg["platforms"]["youtube"].get("cdp_port", 9333)
    subprocess.Popen([str(CHROME), f"--user-data-dir={prof}", f"--remote-debugging-port={port}",
                      "--no-first-run", "--no-default-browser-check", url])
    for _ in range(30):
        if is_up(cfg):
            return
        time.sleep(1)
    raise RuntimeError("Chrome did not open the debugging port")


def connect(pw, cfg: dict):
    """Attach Playwright to the running Chrome; returns (browser, context)."""
    last = None
    for _ in range(4):  # the first attach after a killed client sometimes fails
        try:
            browser = pw.chromium.connect_over_cdp(cdp_url(cfg))
            return browser, browser.contexts[0]
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2)
    raise RuntimeError(f"could not attach to Chrome: {last}")
