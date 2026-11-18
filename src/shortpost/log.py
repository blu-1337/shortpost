"""Stdlib logging: console plus an optional UTF-8 file."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("shortpost")


def setup_logging(log_file: Path | None = None, verbose: bool = False) -> None:
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)


def prune_logs(log_dir: Path, keep_days: int = 30) -> int:
    """Delete shortpost-*.log files older than keep_days. Returns how many were removed."""
    import time

    cutoff, n = time.time() - keep_days * 86400, 0
    for f in log_dir.glob("shortpost-*.log") if log_dir.is_dir() else []:
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                n += 1
        except OSError:
            pass
    return n


def mask(secret: str) -> str:
    """Mask a token for logs: abcd…wxyz."""
    if len(secret) <= 8:
        return "…"
    return f"{secret[:4]}…{secret[-4:]}"
