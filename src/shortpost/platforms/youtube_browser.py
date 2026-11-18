"""YouTube Shorts through the Studio upload dialog, in the dedicated Chrome (CLAUDE.md 9, D-003).

Schedules the Short natively (Visibility -> Schedule), so the PC need not be on at go-live. Studio shows the
schedule in the browser's local time zone, so the slot is converted to ``general.local_tz``.
"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import browser, config
from .base import Result

NAME = "youtube"
log = logging.getLogger("shortpost.youtube")

SEL = {  # verified against Studio on 2026-11-04; override with [platforms.youtube.selectors]
    "file": "input[type=file]",
    "title": "#title-textarea #textbox",
    "description": "#description-textarea #textbox",
    "not_for_kids": "tp-yt-paper-radio-button[name=VIDEO_MADE_FOR_KIDS_NOT_MFK]",
    "show_more": "#toggle-button",
    "tags": "#tags-container input",
    "altered_no": "tp-yt-paper-radio-button[name=VIDEO_HAS_ALTERED_CONTENT_NO]",
    "next": "#next-button",
    "schedule_expand": "#second-container-expand-button",
    "date_trigger": "#datepicker-trigger",
    "date_input": "ytcp-date-picker input",
    "time_input": "#time-of-day-container input",
    "done": "#done-button",
    "video_link": "a[href*='youtube.com/shorts/'], a[href*='youtu.be/']",
}


class StudioError(RuntimeError):
    pass


def _pause(page, lo=300, hi=1200) -> None:
    page.wait_for_timeout(random.randint(lo, hi))


def _fmt_date(dt: datetime) -> str:
    return f"{dt:%b} {dt.day}, {dt.year}"


def _fmt_time(dt: datetime) -> str:
    return f"{dt.hour % 12 or 12}:{dt:%M} {'AM' if dt.hour < 12 else 'PM'}"


def _type_into(page, sel: str, text: str) -> None:
    page.click(sel)
    page.keyboard.press("Control+a")
    page.keyboard.press("Delete")
    page.keyboard.type(text, delay=random.randint(15, 35))


def _debug_dump(page, cfg: dict, step: str) -> None:
    d = config.resolve(cfg["general"]["log_dir"]) / "screenshots"
    d.mkdir(parents=True, exist_ok=True)
    stem = d / f"{datetime.now():%Y%m%d-%H%M%S}_youtube_{step}"
    try:
        page.screenshot(path=str(stem) + ".png", timeout=10000)
        Path(str(stem) + ".html").write_text(page.content(), encoding="utf-8")
    except Exception as e:  # noqa: BLE001 - diagnostics must never mask the real error
        log.warning("could not save failure screenshot: %s", e)


def _set_file(page, selector: str, video: Path) -> None:
    """Attach the file. Playwright refuses >50 MB over CDP, so hand Chrome the local path through the DOM domain."""
    if video.stat().st_size < 45 * 1024 * 1024:
        page.set_input_files(selector, str(video))
        return
    cdp = page.context.new_cdp_session(page)
    try:
        root = cdp.send("DOM.getDocument", {"depth": 0})["root"]["nodeId"]
        node = cdp.send("DOM.querySelector", {"nodeId": root, "selector": selector})["nodeId"]
        cdp.send("DOM.setFileInputFiles", {"nodeId": node, "files": [str(video.resolve())]})
    finally:
        cdp.detach()


def upload(cfg: dict, video: Path, rendered: dict, fire_at: datetime, http=None, native: bool = True) -> Result:
    from playwright.sync_api import sync_playwright

    sel = {**SEL, **cfg["platforms"]["youtube"].get("selectors", {})}
    when = fire_at.astimezone(ZoneInfo(cfg["general"]["local_tz"]))
    browser.launch(cfg)
    step = "start"
    with sync_playwright() as pw:
        _, ctx = browser.connect(pw, cfg)
        page = ctx.new_page()
        page.set_default_timeout(30000)
        try:
            step = "open-upload"
            page.goto(browser.STUDIO + "/videos/upload?d=ud", wait_until="domcontentloaded")
            try:
                page.wait_for_selector(sel["file"], state="attached", timeout=60000)
            except Exception:
                if "accounts.google.com" in page.url:
                    raise StudioError("the ShortPost Chrome is signed out of Google: sign in at studio.youtube.com in that window, then Retry failed") from None
                raise
            _set_file(page, sel["file"], video)
            step = "details"
            page.wait_for_selector(sel["title"], timeout=90000)
            _pause(page, 1500, 2500)
            _type_into(page, sel["title"], rendered["title"])
            _pause(page)
            _type_into(page, sel["description"], rendered["text"])
            _pause(page)
            page.click(sel["not_for_kids"])
            page.click(sel["show_more"])
            _pause(page)
            step = "tags"
            if rendered.get("tags"):
                page.click(sel["tags"])
                page.keyboard.type(",".join(rendered["tags"]) + ",", delay=random.randint(10, 25))
            if page.locator(sel["altered_no"]).count():
                page.click(sel["altered_no"])
            step = "next"
            for _ in range(3):  # Details -> Video elements -> Checks -> Visibility
                _pause(page)
                page.click(sel["next"])
            step = "schedule"
            page.wait_for_selector(sel["schedule_expand"], timeout=60000)
            page.click(sel["schedule_expand"])
            _pause(page)
            page.click(sel["date_trigger"])
            _pause(page, 400, 800)
            dinp = page.locator(sel["date_input"]).first
            dinp.click()
            page.keyboard.press("Control+a")
            page.keyboard.type(_fmt_date(when), delay=30)
            page.keyboard.press("Enter")
            _pause(page)
            tinp = page.locator(sel["time_input"]).first
            tinp.click()
            page.keyboard.press("Control+a")
            page.keyboard.type(_fmt_time(when), delay=30)
            page.keyboard.press("Enter")
            _pause(page)
            got_d, got_t = page.inner_text(sel["date_trigger"]).strip(), tinp.input_value().replace(" ", " ").strip()
            if got_d != _fmt_date(when) or got_t != _fmt_time(when):
                raise StudioError(f"schedule fields show {got_d} {got_t}, wanted {_fmt_date(when)} {_fmt_time(when)}")
            link = page.locator(sel["video_link"]).first.get_attribute("href") or ""
            step = "confirm"
            try:  # Studio's checks take a few minutes; scheduling earlier only triggers a warning dialog
                page.get_by_text("Checks complete").first.wait_for(timeout=900000)
            except Exception:  # noqa: BLE001
                log.warning("youtube checks did not report complete; scheduling anyway")
            ok = page.get_by_text("Video scheduled")
            gotit = page.get_by_role("button", name="Got it")
            for _ in range(4):
                page.click(sel["done"])
                try:
                    ok.or_(gotit).first.wait_for(state="visible", timeout=60000)
                except Exception:  # noqa: BLE001
                    raise StudioError("Studio showed neither the confirmation nor a notice after Schedule")
                if ok.first.is_visible():
                    break
                gotit.first.click()
                _pause(page)
            else:
                raise StudioError("Studio never showed the 'Video scheduled' confirmation")
            link = page.locator(sel["video_link"]).last.get_attribute("href") or link
            _pause(page, 600, 1200)
            try:
                page.get_by_role("button", name="Close").last.click(timeout=8000)
            except Exception:  # noqa: BLE001 - the dialog may close itself
                pass
        except Exception as e:
            _debug_dump(page, cfg, step)
            raise StudioError(f"YouTube Studio failed at step '{step}': {type(e).__name__}: {str(e)[:200]}") from e
        finally:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass
    link = link.split("?")[0]
    vid = link.rstrip("/").rsplit("/", 1)[-1]
    log.info("youtube scheduled %s for %s", vid, when.isoformat())
    return Result("scheduled", vid, link, {"mode": "browser"})


def set_related(cfg: dict, video_id: str, title: str) -> None:
    """Set 'Related video' on a Short's edit page (the API cannot). Picks the picker card whose title matches."""
    from playwright.sync_api import sync_playwright

    browser.launch(cfg)
    step = "open"
    with sync_playwright() as pw:
        _, ctx = browser.connect(pw, cfg)
        page = ctx.new_page()
        page.set_default_timeout(30000)
        try:
            page.goto(f"https://studio.youtube.com/video/{video_id}/edit", wait_until="domcontentloaded")
            trigger = page.get_by_text("Related video", exact=True).first
            trigger.wait_for(timeout=60000)
            step = "picker"
            _pause(page, 600, 1200)
            trigger.click()
            cards = page.locator("ytcp-video-pick-dialog-contents ytcp-entity-card")
            cards.first.wait_for(timeout=30000)
            page.wait_for_timeout(1500)
            want = " ".join(title.split()).lower()
            hit = None
            for c in cards.all():
                label = " ".join((c.get_attribute("aria-label") or "").split()).lower()
                label = label.rsplit(", ", 1)[0] if label.endswith(("not selected", "selected")) else label
                if label == want or label.startswith(want[:60]):
                    hit = c
                    break
            if hit is None:
                raise StudioError(f"no video titled '{title[:60]}' in the Related video picker")
            step = "select"
            hit.scroll_into_view_if_needed()
            hit.click()
            _pause(page, 400, 900)
            page.get_by_role("button", name="Save").last.click()
            _pause(page, 800, 1500)
            step = "save"
            main_save = page.locator("#save")
            if main_save.count() and main_save.first.is_enabled():
                main_save.first.click()
                page.wait_for_timeout(3000)
        except Exception as e:
            _debug_dump(page, cfg, "related_" + step)
            raise StudioError(f"setting Related video failed at '{step}': {type(e).__name__}: {str(e)[:200]}") from e
        finally:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass


def delete_video(cfg: dict, video_id: str, commit: bool = True) -> None:
    """Delete a video in Studio (edit page -> Options -> Delete forever -> tick the box -> confirm).
    With commit=False it stops at the final confirm dialog and closes it (a safe dry run)."""
    from playwright.sync_api import sync_playwright

    browser.launch(cfg)
    step = "open"
    with sync_playwright() as pw:
        _, ctx = browser.connect(pw, cfg)
        page = ctx.new_page()
        page.set_default_timeout(30000)
        try:
            page.goto(f"https://studio.youtube.com/video/{video_id}/edit", wait_until="domcontentloaded")
            opts = page.get_by_role("button", name="Options").first
            gone = page.get_by_text("Oops, something went wrong")
            opts.or_(gone).first.wait_for(timeout=60000)
            if not opts.is_visible() and gone.is_visible():
                return                      # Studio has no such video any more: already deleted
            _pause(page, 600, 1200)
            step = "menu"
            opts.click()
            page.get_by_role("menuitem", name="Delete", exact=True).first.click()
            step = "confirm"
            box = page.locator("ytcp-checkbox-lit").first
            box.wait_for(timeout=15000)
            _pause(page, 400, 900)
            box.click()
            final = page.locator("ytcp-button#confirm-button").last
            if not commit:
                final.wait_for(timeout=10000)
                page.keyboard.press("Escape")
                return
            final.click()
            step = "done"
            page.wait_for_timeout(4000)
        except Exception as e:
            _debug_dump(page, cfg, "delete_" + step)
            raise StudioError(f"deleting the video failed at '{step}': {type(e).__name__}: {str(e)[:200]}") from e
        finally:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass
