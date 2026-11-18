"""SQLite schema and queries (CLAUDE.md 8.7)."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
  id INTEGER PRIMARY KEY,
  sha1 TEXT UNIQUE NOT NULL,
  src_path TEXT NOT NULL,
  lib_dir TEXT,
  topic TEXT NOT NULL,
  notes TEXT,
  series_key TEXT, series_part INT, series_of INT,
  duration_s REAL, width INT, height INT,
  slot_date_ny TEXT,
  state TEXT NOT NULL,
  meta_path TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
  id INTEGER PRIMARY KEY,
  video_id INTEGER NOT NULL REFERENCES videos(id),
  platform TEXT NOT NULL,
  mode TEXT NOT NULL,
  fire_at_utc TEXT NOT NULL,
  jitter_min INTEGER NOT NULL,
  state TEXT NOT NULL,
  remote_id TEXT, remote_url TEXT,
  extra_json TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at_utc TEXT,
  last_error TEXT,
  updated_at TEXT NOT NULL,
  UNIQUE(video_id, platform)
);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, at_utc TEXT, video_id INT, platform TEXT, kind TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS remote_occupied (date_ny TEXT, platform TEXT, remote_id TEXT, title TEXT, PRIMARY KEY(date_ny, platform, remote_id));
"""

POST_TERMINAL = ("published", "skipped", "cancelled", "missed")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(cfg: dict) -> sqlite3.Connection:
    path = config.resolve(cfg["general"]["db_path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=60)  # the UI, the 15-minute task and `shortpost` can all write: wait instead of failing after 5 s
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")
    try:
        conn.execute("PRAGMA journal_mode=WAL")  # readers no longer block the writer and vice versa
    except sqlite3.OperationalError:
        pass  # another process holds the file right now; it keeps its current mode and the next connect retries
    conn.executescript(SCHEMA)
    return conn


def sha1_of(path: Path) -> str:
    """sha1(size + first 8 MB + last 8 MB): cheap dedupe for big files."""
    size = path.stat().st_size
    h = hashlib.sha1(str(size).encode())
    with path.open("rb") as f:
        h.update(f.read(8 << 20))
        if size > 16 << 20:
            f.seek(size - (8 << 20))
            h.update(f.read(8 << 20))
    return h.hexdigest()


def event(conn, video_id: int | None, platform: str | None, kind: str, message: str) -> None:
    conn.execute("INSERT INTO events(at_utc, video_id, platform, kind, message) VALUES (?,?,?,?,?)",
                 (now_iso(), video_id, platform, kind, message))


def video_by_sha1(conn, sha1: str):
    return conn.execute("SELECT * FROM videos WHERE sha1=?", (sha1,)).fetchone()


def get_video(conn, vid: int):
    return conn.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone()


def posts_of(conn, vid: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM posts WHERE video_id=? ORDER BY fire_at_utc, platform", (vid,)).fetchall()


def occupied_dates(conn, cfg: dict) -> set[date]:
    rows = conn.execute("SELECT slot_date_ny FROM videos WHERE slot_date_ny IS NOT NULL AND state NOT IN ('cancelled','error')").fetchall()
    days = {date.fromisoformat(r[0]) for r in rows}
    if cfg["schedule"].get("respect_youtube_schedule", True):
        days |= {date.fromisoformat(r[0]) for r in conn.execute("SELECT date_ny FROM remote_occupied")}
    return days


def last_series_slot(conn, series_key: str, before_part: int | None = None) -> date | None:
    """Latest slot of an earlier part of the same series (part N+1 must go after part N)."""
    q = "SELECT MAX(slot_date_ny) FROM videos WHERE series_key=? AND state NOT IN ('cancelled','error')"
    args: list = [series_key]
    if before_part is not None:
        q += " AND series_part < ?"
        args.append(before_part)
    r = conn.execute(q, args).fetchone()[0]
    return date.fromisoformat(r) if r else None


def refresh_video_state(conn, vid: int) -> None:
    """A video is cancelled when all its posts are cancelled, done when all are terminal."""
    states = [r["state"] for r in posts_of(conn, vid)]
    if not states:
        return
    new = None
    if all(s == "cancelled" for s in states):
        new = "cancelled"
    elif all(s in POST_TERMINAL for s in states):
        new = "done"
    if new:
        conn.execute("UPDATE videos SET state=?, updated_at=? WHERE id=?", (new, now_iso(), vid))
