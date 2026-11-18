"""ntfy phone pushes (CLAUDE.md D-006). Never raises: a failed push must not break a run."""

from __future__ import annotations

import httpx

from . import config
from .log import logger


def send(cfg: dict, title: str, body: str, secrets: dict | None = None, client: httpx.Client | None = None,
         priority: int = 3) -> bool:
    """POST to <ntfy_server>/<topic>. The title header must be ASCII (HTTP header)."""
    secrets = config.load_secrets() if secrets is None else secrets
    topic = secrets.get("ntfy_topic")
    if not topic:
        logger.info("ntfy: no topic set, skipped: %s", title)
        return False
    server = cfg.get("notify", {}).get("ntfy_server", "https://ntfy.sh").rstrip("/")
    try:
        r = (client or httpx).post(f"{server}/{topic}", content=body.encode("utf-8"), timeout=15,
                                   headers={"Title": title.encode("ascii", "replace").decode(), "Priority": str(priority)})
        r.raise_for_status()
        return True
    except httpx.HTTPError as e:
        logger.warning("ntfy failed: %s", type(e).__name__)
        return False
