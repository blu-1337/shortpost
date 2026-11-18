"""Platform uploaders. Each module exposes ``upload(cfg, video, rendered, fire_at, http, native) -> Result``."""

from importlib import import_module

IMPLEMENTED = ("instagram", "facebook", "youtube", "tiktok", "threads", "bluesky", "pinterest")  # grows with each milestone; the run order is set in runner.ORDER


MODULE = {"tiktok": "tiktok_inbox"}  # platform name -> module name when they differ


def get(name: str):
    return import_module(f"{__name__}.{MODULE.get(name, name)}") if name in IMPLEMENTED else None
