import copy
import shutil
import subprocess
from pathlib import Path

import pytest

from shortpost import config, media

CFG = config.load_config(user_dir=Path("/nonexistent"))

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg missing")


def test_parse_leading_black():
    assert media.parse_leading_black("[x] black_start:0 black_end:0.0166833 black_duration:0.0166833") == pytest.approx(0.0166833)
    assert media.parse_leading_black("black_start:3.2 black_end:3.4 black_duration:0.2") == 0.0   # black in the middle
    assert media.parse_leading_black("nothing here") == 0.0


def _clip(path: Path, black_s: float):
    """0.5 s clip: `black_s` of black, then a bright test pattern, with audio."""
    vf = f"color=c=black:s=240x426:r=30:d={black_s}[b];testsrc=s=240x426:r=30:d=1[t];[b][t]concat=n=2:v=1:a=0"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-filter_complex", vf, "-f", "lavfi", "-i", "sine=d=2", "-shortest",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)], check=True)


@needs_ffmpeg
def test_trim_removes_one_black_frame(tmp_path):
    src = tmp_path / "v.mp4"
    _clip(src, 1 / 30)
    assert media.detect_leading_black(src, CFG) == pytest.approx(1 / 30, abs=0.01)
    assert media.trim_leading_black(src, CFG) > 0
    assert media.detect_leading_black(src, CFG) == 0.0
    assert not (tmp_path / "v.trim.mp4").exists()


@needs_ffmpeg
def test_long_black_is_left_alone_and_switch_works(tmp_path):
    src = tmp_path / "v.mp4"
    _clip(src, 1.0)                                    # deliberate 1 s fade-in: longer than black_max_s
    assert media.detect_leading_black(src, CFG) == 0.0
    short = tmp_path / "s.mp4"
    _clip(short, 1 / 30)
    off = copy.deepcopy(CFG)
    off["media"]["trim_black_start"] = False
    assert media.detect_leading_black(short, off) == 0.0
    assert media.trim_leading_black(short, off) == 0.0
