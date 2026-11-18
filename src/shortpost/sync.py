"""`shortpost sync`: record the days the YouTube channel already has a Short scheduled/published (CLAUDE.md 8.3, 10.1).

Browser mode reads Studio's Shorts list. Studio lists the date in the browser's local zone and without a time, so
a Short scheduled for 18:30 ET shows one calendar day later than its New York date when local time is already past
midnight. Rows that ShortPost itself uploaded are skipped (their New York slot is already in the DB), and for the
rest the listed date is used as-is.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from . import browser, db

log = logging.getLogger("shortpost.sync")
_DATE = re.compile(r"([A-Z][a-z]{2}) (\d{1,2}), (\d{4})")


def parse_row(text: str) -> tuple[date, str] | None:
    """(date, kind) from a Studio row's text; kind is 'scheduled' or 'published'. None for drafts and anything else."""
    kind = "scheduled" if "Scheduled" in text else "published" if "Published" in text else None
    m = _DATE.search(text)
    if not kind or not m:
        return None
    try:
        return datetime.strptime(" ".join(m.groups()), "%b %d %Y").date(), kind
    except ValueError:
        return None


def scrape_studio(cfg: dict) -> list[dict]:
    from playwright.sync_api import sync_playwright

    browser.launch(cfg)
    out = []
    with sync_playwright() as pw:
        _, ctx = browser.connect(pw, cfg)
        page = ctx.new_page()
        page.set_default_timeout(30000)
        try:
            page.goto(browser.STUDIO + "/videos/short", wait_until="domcontentloaded")
            page.wait_for_selector("ytcp-video-row", timeout=60000)
            page.wait_for_timeout(2000)
            rows = page.locator("ytcp-video-row")
            for i in range(min(rows.count(), 60)):
                r = rows.nth(i)
                href = r.locator("a[href*='/video/']").first.get_attribute("href") or ""
                m = re.search(r"/video/([\w-]{11})", href)
                out.append({"id": m.group(1) if m else "", "text": r.inner_text(), "title": r.inner_text().split("\n")[1][:120] if "\n" in r.inner_text() else ""})
        finally:
            page.close()
    return out


def run(cfg: dict, conn, rows: list[dict] | None = None, today: date | None = None) -> list[str]:
    """Store upcoming/today YouTube days in remote_occupied; returns report lines."""
    rows = scrape_studio(cfg) if rows is None else rows
    today = today or datetime.now(ZoneInfo(cfg["general"]["target_tz"])).date()
    ours = {r[0] for r in conn.execute("SELECT remote_id FROM posts WHERE platform='youtube' AND remote_id IS NOT NULL")}
    conn.execute("DELETE FROM remote_occupied WHERE platform='youtube'")
    lines = []
    for row in rows:
        parsed = parse_row(row["text"])
        if not parsed or parsed[0] < today:
            continue
        d, kind = parsed
        if row["id"] and row["id"] in ours:
            continue
        conn.execute("INSERT OR REPLACE INTO remote_occupied(date_ny, platform, remote_id, title) VALUES (?,?,?,?)",
                     (d.isoformat(), "youtube", row["id"] or row["title"], row["title"]))
        lines.append(f"{d.isoformat()}  {kind:<9} {row['title'][:70]}")
    conn.commit()
    return lines or ["no upcoming YouTube Shorts found"]
