from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from shortpost.platforms import youtube, youtube_browser as yb


def test_date_time_format_matches_studio():
    d = datetime(2026, 11, 12, 1, 30, tzinfo=ZoneInfo("Europe/Bucharest"))
    assert yb._fmt_date(d) == "Nov 12, 2026"
    assert yb._fmt_time(d) == "1:30 AM"
    assert yb._fmt_time(d.replace(hour=0)) == "12:30 AM"
    assert yb._fmt_time(d.replace(hour=12)) == "12:30 PM"
    assert yb._fmt_time(d.replace(hour=23, minute=5)) == "11:05 PM"


def test_ny_slot_converts_to_local_wall_clock():
    from datetime import timezone

    fire = datetime(2026, 11, 11, 23, 30, tzinfo=timezone.utc)  # 18:30 ET
    local = fire.astimezone(ZoneInfo("Europe/Bucharest"))
    assert (yb._fmt_date(local), yb._fmt_time(local)) == ("Nov 12, 2026", "1:30 AM")


def test_api_mode_not_implemented():
    cfg = {"platforms": {"youtube": {"mode": "api"}}}
    with pytest.raises(NotImplementedError):
        youtube.upload(cfg, None, {}, None)
