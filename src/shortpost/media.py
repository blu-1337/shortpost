"""ffprobe parsing, validation and the H.264 compatibility copy (CLAUDE.md 10.6)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

DURATION_CAPS = {"youtube": 180, "tiktok": 600, "instagram": 180, "facebook": 90, "threads": 300, "bluesky": 180, "pinterest": 900}


def find_tool(name: str) -> str | None:
    return shutil.which(name)


@dataclass
class VideoInfo:
    duration_s: float
    size_bytes: int
    width: int
    height: int
    vcodec: str
    pix_fmt: str
    fps: float
    bitrate: int
    acodec: str | None

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 0.0


def probe(path: Path) -> VideoInfo:
    exe = find_tool("ffprobe")
    if not exe:
        raise RuntimeError("ffprobe not found")
    out = subprocess.run(
        [exe, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
        capture_output=True, text=True, check=True, timeout=60,
    ).stdout
    return parse_probe(json.loads(out))


def parse_probe(data: dict) -> VideoInfo:
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    if v is None:
        raise ValueError("no video stream")
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = data.get("format", {})
    try:
        fps = float(Fraction(v.get("avg_frame_rate", "0/1")))
    except (ZeroDivisionError, ValueError):
        fps = 0.0
    duration = float(fmt.get("duration") or v.get("duration") or 0)
    return VideoInfo(
        duration_s=duration,
        size_bytes=int(fmt.get("size", 0)),
        width=int(v["width"]),
        height=int(v["height"]),
        vcodec=v.get("codec_name", ""),
        pix_fmt=v.get("pix_fmt", ""),
        fps=fps,
        bitrate=int(fmt.get("bit_rate") or 0),
        acodec=a.get("codec_name") if a else None,
    )


def needs_compat_copy(info: VideoInfo, cfg: dict) -> bool:
    """True unless the source is already safe H.264 8-bit (<=25 Mbit/s, <=100 MB)."""
    if not cfg["media"].get("compat_skip_if_h264_8bit", True):
        return True
    safe = info.vcodec == "h264" and info.pix_fmt == "yuv420p" and info.bitrate <= 25_000_000 and info.size_bytes <= 100_000_000
    return not safe


def validate(info: VideoInfo, cfg: dict) -> list[str]:
    """Warnings (never raises for soft issues)."""
    w = []
    if info.vcodec not in ("h264", "hevc"):
        w.append(f"unusual video codec {info.vcodec}")
    if not 0.5 <= info.aspect <= 0.6:
        w.append(f"aspect ratio {info.aspect:.2f} is not 9:16")
    if info.height < 1280:
        w.append(f"height {info.height} is below 1280 (1920 recommended)")
    if not info.acodec:
        w.append("no audio stream")
    elif info.acodec != "aac":
        w.append(f"audio is {info.acodec}, AAC recommended")
    for plat, cap in DURATION_CAPS.items():
        if cfg["platforms"].get(plat, {}).get("enabled") and info.duration_s > cap:
            w.append(f"{plat}: {info.duration_s:.0f}s is over the {cap}s cap")
    return w


def compat_command(src: Path, dst: Path, cfg: dict) -> list[str]:
    m = cfg["media"]
    return [
        "ffmpeg", "-y", "-i", str(src), "-c:v", "libx264", "-preset", "slow", "-crf", str(m["compat_crf"]),
        "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.2", "-maxrate", m["compat_maxrate"], "-bufsize", "24M",
        "-fpsmax", str(m["compat_max_fps"]), "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", str(dst),
    ]


BLACK_RE = re.compile(r"black_start:(\S+)\s+black_end:(\S+)")


def parse_leading_black(stderr: str) -> float:
    """Seconds of black at t=0 from ffmpeg blackdetect output (0.0 when the video does not start black)."""
    for m in BLACK_RE.finditer(stderr):
        if float(m.group(1)) <= 0.001:
            return float(m.group(2))
    return 0.0


def detect_leading_black(path: Path, cfg: dict) -> float:
    """Length of black at the very start (a black first frame becomes the platforms' automatic thumbnail).
    Returns 0.0 when off, when nothing is black, or when the black part is longer than `black_max_s` (a deliberate fade)."""
    m = cfg["media"]
    if not m.get("trim_black_start", True):
        return 0.0
    cmd = [find_tool("ffmpeg") or "ffmpeg", "-hide_banner", "-t", "2", "-i", str(path), "-vf",
           "blackdetect=d=0.001:pix_th=0.10", "-an", "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    black = parse_leading_black(r.stderr)
    return black if 0 < black <= float(m.get("black_max_s", 0.5)) else 0.0


def extract_frame(video: Path, out: Path, at_s: float = 0.0) -> None:
    """Write the frame at `at_s` seconds as a JPEG (used as a cover image)."""
    subprocess.run([find_tool("ffmpeg") or "ffmpeg", "-y", "-ss", f"{at_s:.3f}", "-i", str(video), "-frames:v", "1", "-q:v", "2", str(out)],
                   capture_output=True, check=True, timeout=120)


def trim_command(src: Path, dst: Path, start: float, info: VideoInfo) -> list[str]:
    """Cut `start` seconds off video and audio (re-encode: a copy can only cut on keyframes), same codec family."""
    if info.vcodec == "hevc":
        v = ["-c:v", "libx265", "-preset", "slow", "-crf", "14", "-pix_fmt", info.pix_fmt or "yuv420p10le", "-tag:v", "hvc1"]
    else:
        v = ["-c:v", "libx264", "-preset", "slow", "-crf", "14", "-pix_fmt", info.pix_fmt or "yuv420p"]
    return ["ffmpeg", "-y", "-i", str(src), "-ss", f"{start:.4f}", *v, "-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart", str(dst)]


def trim_leading_black(path: Path, cfg: dict) -> float:
    """Remove a black start from `path` in place. Returns the seconds removed (0.0 = untouched)."""
    start = detect_leading_black(path, cfg)
    if not start:
        return 0.0
    tmp = path.with_name(path.stem + ".trim" + path.suffix)
    cmd = trim_command(path, tmp, start, probe(path))
    cmd[0] = find_tool("ffmpeg") or "ffmpeg"
    try:
        subprocess.run(cmd[:1] + ["-nostats", "-loglevel", "error"] + cmd[1:], check=True)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
    return start


STORY_FONT = r"C\:/Windows/Fonts/arialbd.ttf"  # the drive colon must be escaped inside an ffmpeg filter


COVER_FONT = STORY_FONT


def opening_frame(path: Path, t: float = 0.6) -> bytes:
    """One JPEG of the opening (what the platforms' automatic cover tends to show)."""
    ff = find_tool("ffmpeg") or "ffmpeg"
    r = subprocess.run([ff, "-nostats", "-loglevel", "error", "-ss", f"{t}", "-i", str(path), "-frames:v", "1",
                        "-vf", "scale=540:-2", "-f", "image2pipe", "-vcodec", "mjpeg", "-"], capture_output=True, check=True)
    return r.stdout


def opening_has_title(cfg: dict, path: Path, client=None) -> bool | None:
    """Ask the local vision model whether the opening already carries an added title/caption/headline.
    True/False, or None when it cannot tell (model offline, bad answer): the caller then does NOT burn."""
    import base64

    import httpx

    ai = cfg["ai"]
    schema = {"type": "object", "required": ["has_title"], "properties": {"has_title": {"type": "boolean"}}}
    prompt = ("This is the first frame of a vertical video. Is there a title, headline, caption or other large text that the "
              "video editor ADDED on top of the footage (for example a hook sentence or a topic title)? Text that is part of "
              "software windows, plugin interfaces or menus does NOT count. Answer JSON {\"has_title\": true|false}.")
    body = {"model": ai["model"], "stream": False, "think": False, "format": schema, "keep_alive": "5m",
            "options": {"temperature": 0, "num_ctx": 4096},
            "messages": [{"role": "user", "content": prompt, "images": [base64.b64encode(opening_frame(path)).decode()]}]}
    try:
        c = client or httpx.Client(timeout=ai.get("timeout_s", 600))
        r = c.post(f"{ai['host']}/api/chat", json=body)
        r.raise_for_status()
        return bool(json.loads(r.json()["message"]["content"])["has_title"])
    except (httpx.HTTPError, KeyError, ValueError, TypeError, subprocess.SubprocessError, OSError):
        return None


def wrap_cover(text: str, width: int = 18) -> str:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    return chr(10).join(lines[:4])


def cover_command(src: Path, dst: Path, textfile: Path, seconds: float, info: VideoInfo) -> list[str]:
    """Burn the cover text over the first `seconds` (re-encode in the same codec family; audio is copied)."""
    tf = str(textfile).replace("\\", "/").replace(":", chr(92) + ":")
    vf = (f"drawtext=fontfile='{COVER_FONT}':textfile='{tf}':fontcolor=white:fontsize=72:line_spacing=14:"
          "box=1:boxcolor=0xbb0000@0.88:boxborderw=34:x=(w-text_w)/2:y=h*0.13:"
          f"enable='lt(t,{seconds})'")
    if info.vcodec == "hevc":
        v = ["-c:v", "libx265", "-preset", "slow", "-crf", "14", "-pix_fmt", info.pix_fmt or "yuv420p10le", "-tag:v", "hvc1"]
    else:
        v = ["-c:v", "libx264", "-preset", "slow", "-crf", "14", "-pix_fmt", info.pix_fmt or "yuv420p"]
    return ["ffmpeg", "-y", "-i", str(src), "-vf", vf, *v, "-c:a", "copy", "-movflags", "+faststart", str(dst)]


def burn_cover_text(path: Path, text: str, cfg: dict) -> None:
    """Replace `path` with a copy that shows `text` big over its first seconds."""
    import tempfile

    seconds = float(cfg["media"].get("cover_seconds", 1.8))
    tmp = path.with_name(path.stem + ".cover" + path.suffix)
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(wrap_cover(text))
        tf = Path(f.name)
    try:
        cmd = cover_command(path, tmp, tf, seconds, probe(path))
        cmd[0] = find_tool("ffmpeg") or "ffmpeg"
        subprocess.run(cmd[:1] + ["-nostats", "-loglevel", "error"] + cmd[1:], check=True)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
        tf.unlink(missing_ok=True)


def story_command(src: Path, dst: Path, cfg: dict) -> list[str]:
    """Story clip: <=59 s, H.264 8-bit, with 'Link in bio - <hub>' burned in (the APIs have no link sticker)."""
    hub = cfg["links"].get("hub_text", "links.medianopol.com")
    cfg = {**cfg, "stories": cfg.get("stories", {})}
    vf = (f"drawtext=fontfile='{STORY_FONT}':text='Link in bio - {hub}':fontcolor=white:fontsize=46:"
          "box=1:boxcolor=black@0.6:boxborderw=22:x=(w-text_w)/2:y=h*0.78")
    return ["ffmpeg", "-y", "-i", str(src), "-t", str(cfg["stories"].get("max_seconds", 59)), "-vf", vf,
            "-c:v", "libx264", "-preset", "medium", "-crf", "22", "-pix_fmt", "yuv420p", "-profile:v", "high",
            "-maxrate", "8M", "-bufsize", "16M", "-fpsmax", "30", "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
            "-movflags", "+faststart", str(dst)]


def make_story_copy(src: Path, dst: Path, cfg: dict) -> None:
    cmd = story_command(src, dst, cfg)
    cmd[0] = find_tool("ffmpeg") or "ffmpeg"
    subprocess.run(cmd[:1] + ["-nostats", "-loglevel", "error"] + cmd[1:], check=True)


def make_compat_copy(src: Path, dst: Path, cfg: dict) -> None:
    """Write the H.264 8-bit copy (slow preset: a minute or two for a 35 s 60 fps clip)."""
    cmd = compat_command(src, dst, cfg)
    cmd[0] = find_tool("ffmpeg") or "ffmpeg"
    subprocess.run(cmd[:1] + ["-nostats", "-loglevel", "error"] + cmd[1:], check=True)
