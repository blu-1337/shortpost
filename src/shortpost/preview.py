"""Rich preview of the rendered posts (CLAUDE.md 10.5)."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from . import render, scheduler

LABELS = {"youtube": "YouTube Shorts", "facebook": "Facebook Reels", "instagram": "Instagram Reels", "tiktok": "TikTok (draft + ntfy)",
          "threads": "Threads", "bluesky": "Bluesky", "pinterest": "Pinterest"}


def plan_times(cfg: dict, rendered: dict, day: date, seed: int) -> dict[str, datetime]:
    """Go-live UTC time per platform with a reproducible jitter."""
    jit = scheduler.plan_jitters(cfg, list(rendered), seed)
    return {p: scheduler.fire_time(cfg, p, day, jit[p]) for p in rendered}


def fmt_time(cfg: dict, utc: datetime) -> str:
    et = utc.astimezone(ZoneInfo(cfg["general"]["target_tz"]))
    loc = utc.astimezone(ZoneInfo(cfg["general"]["local_tz"]))
    return f"{et:%a %Y-%m-%d %H:%M} ET | {loc:%a %H:%M} local"


def show(cfg: dict, vi, info, meta: dict, rendered: dict, day: date, warnings: list[str], console: Console | None = None) -> None:
    c = console or Console()
    times = plan_times(cfg, rendered, day, meta["seed"])
    head = Table.grid(padding=(0, 2))
    head.add_row("Video", vi.path.name)
    head.add_row("Topic", vi.parsed.topic + (f"   series: part {vi.parsed.series['part']} of {vi.parsed.series['of']}" if vi.parsed.series else ""))
    head.add_row("Media", f"{info.duration_s:.1f}s  {info.width}x{info.height}  {info.vcodec}/{info.pix_fmt}  {info.size_bytes / 1e6:.1f} MB")
    head.add_row("Slot day", f"{day} (New York date)")
    src = meta["source"] + (f" ({meta['model']})" if meta.get("model") else "")
    head.add_row("AI", f"{src}  seed {meta['seed']}")
    c.print(Panel(head, title="ShortPost preview"))
    if meta["source"] == "template":
        c.print("[yellow]AI fallback: template-only text, please edit or regenerate[/yellow]")
    for w in warnings:
        c.print(f"[yellow]warning:[/yellow] {w}")
    prods = [x for x in meta.get("core", {}).get("products", []) if x]
    if prods:
        c.print(f"[bold yellow]Check the spelling of:[/bold yellow] [bold]{escape(', '.join(prods))}[/bold] "
                "[dim](taken from the filename; every post uses it exactly)[/dim]")
    cov = render.coverage(rendered)
    c.print(f"[dim]Reach: {cov['unique_hashtags']} distinct hashtags across platforms ({', '.join(cov['hashtags'])})[/dim]")
    for plat, r in rendered.items():
        body = []
        fk = meta.get("focus", {}).get(plat, {}).get("keyword")
        if fk:
            body.append(f"[dim]focus keyword: {escape(fk)}[/dim]")
        if r.get("title"):
            body.append(f"[bold]{r['title']}[/bold]  ({len(r['title'])} chars)")
        body.append(escape(r["text"]))
        if r.get("tags"):
            body.append("[dim]tags: " + ", ".join(r["tags"]) + "[/dim]")
        if r.get("related"):
            rel = r["related"]
            body.append(f"[bold]Related video:[/bold] {escape(rel['title'])} [dim]({rel['by']}: {escape(rel['reason'])})[/dim]")
        if r.get("comment"):
            body.append("[dim]first comment: " + r["comment"] + "[/dim]")
        if r.get("link"):
            body.append("[dim]pin link: " + r["link"] + "[/dim]")
        for w in r.get("warnings", []):
            body.append("[yellow]" + w + "[/yellow]")
        c.print(Panel("\n".join(body), title=f"{LABELS[plat]}  |  {fmt_time(cfg, times[plat])}", title_align="left"))
