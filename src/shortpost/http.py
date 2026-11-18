"""httpx wrapper: timeout, retry on 5xx/429 with backoff, errors without tokens (CLAUDE.md 14)."""

from __future__ import annotations

import time
from urllib.parse import urlsplit

import httpx

from .log import logger


class ApiError(RuntimeError):
    pass


class NotConfigured(RuntimeError):
    """A platform has no token yet: the post stays pending instead of failing."""


class Http:
    def __init__(self, client: httpx.Client | None = None, sleep=time.sleep, retries: int = 3):
        self.client = client or httpx.Client(timeout=httpx.Timeout(120, connect=20))
        self.sleep, self.retries = sleep, retries

    def request(self, method: str, url: str, **kw) -> dict:
        where = urlsplit(url)._replace(query="").geturl()
        for attempt in range(self.retries + 1):
            try:
                r = self.client.request(method, url, **kw)
            except httpx.TransportError as e:
                if attempt == self.retries:
                    raise ApiError(f"{method} {where}: {type(e).__name__}") from None
                self.sleep(2 ** attempt)
                continue
            if (r.status_code == 429 or r.status_code >= 500) and attempt < self.retries:
                self.sleep(2 ** attempt)
                continue
            if r.status_code >= 400:
                try:
                    err = r.json().get("error", r.text)
                    msg = err.get("message", err) if isinstance(err, dict) else err
                    if isinstance(err, dict) and err.get("code") and err["code"] != "ok":
                        msg = f"{err['code']}: {msg}"  # TikTok/others put the machine-readable reason in `code`
                except ValueError:
                    msg = r.text[:300]
                raise ApiError(f"{method} {where} -> {r.status_code}: {msg}")
            logger.debug("%s %s -> %s", method, where, r.status_code)
            try:
                return r.json() if r.content else {}
            except ValueError:
                return {}
        raise ApiError(f"{method} {where}: retries exhausted")

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)


def file_stream(path, chunk: int = 4 << 20):
    with open(path, "rb") as f:
        while block := f.read(chunk):
            yield block
