from pathlib import Path

import pytest

from shortpost import affiliates, config, media
from shortpost.naming import find_variants, is_variant_file, parse_stem, read_notes

SAMPLE = "vatrical-from-flamesound-enhancer-vst-effects-plugin-part-1-of-3"


def test_sample_topic_and_series():
    p = parse_stem(SAMPLE)
    assert p.topic == "vatrical from flamesound enhancer vst effects plugin"
    assert p.series == {"part": 1, "of": 3}
    assert p.series_key == "vatrical-from-flamesound-enhancer-vst-effects-plugin"


@pytest.mark.parametrize("stem,topic", [
    ("free-soothe2-plugin_dagon_H1", "free soothe2 plugin dagon"),
    ("kirchhoff-eq_best-eq-for-808s_H1_facebook", "kirchhoff eq best eq for 808s"),
    ("2026-11-03_cool-plugin_final", "cool plugin"),
    ("20261103-cool-plugin_v2_1080p", "cool plugin"),
])
def test_suffix_and_date_stripping(stem, topic):
    p = parse_stem(stem)
    assert p.topic == topic and p.series is None


def test_part_without_total_and_no_false_positive():
    assert parse_stem("cool-plugin-pt2").series == {"part": 2, "of": None}
    assert parse_stem("depart-2").series is None


def test_variants_and_notes(tmp_path):
    v = tmp_path / "a.mp4"
    v.write_bytes(b"x")
    (tmp_path / "a_facebook.mp4").write_bytes(b"x")
    (tmp_path / "a.txt").write_text("free plugin\nlink: none\n", encoding="utf-8")
    assert find_variants(v)["facebook"].name == "a_facebook.mp4"
    assert is_variant_file(tmp_path / "a_facebook.mp4") and not is_variant_file(v)
    assert read_notes(v) == ("free plugin\nlink: none", "none")


CAT = affiliates.load_catalog()


def ids(platform, text, beat=False, directive=None):
    return [e.id for e in affiliates.select(CAT, platform, text, beat, directive)]


def test_dogma_beats_fl_studio():
    assert ids("facebook", "dogma limiter in fl studio") == ["dogma"]
    assert ids("youtube", "dogma limiter in fl studio") == ["dogma", "fl-studio"]


def test_fallback_and_beat_video():
    assert ids("facebook", "vatrical enhancer") == ["workbook-free"]
    assert ids("facebook", "vatrical enhancer", beat=True) == ["beatstars"]
    assert ids("youtube", "vatrical enhancer") == []


def test_directives():
    assert ids("facebook", "dogma", directive="none") == []
    assert ids("facebook", "dogma", directive="dark-streets") == ["dark-streets"]


def test_whole_word_matching():
    assert ids("facebook", "undogmatic stuff") == ["workbook-free"]


def test_urls_exact():
    assert {e.id: e.url for e in CAT}["wa-production"] == "https://www.waproduction.com/#znprv2qadovvj"
    assert {e.id: e.url for e in CAT}["stranis"].endswith("?ref=SAMASTANO")


def test_pinterest_destination():
    ch = "https://www.youtube.com/@samastano"
    assert affiliates.pinterest_destination(CAT, "vatrical", False, ch) == ch
    assert "beatstars" in affiliates.pinterest_destination(CAT, "x", True, ch)
    assert "apmastering" in affiliates.pinterest_destination(CAT, "dogma", False, ch)


SAMPLE_PROBE = {
    "streams": [
        {"codec_type": "video", "codec_name": "hevc", "pix_fmt": "yuv420p10le", "width": 1080, "height": 1920,
         "avg_frame_rate": "60000/1001"},
        {"codec_type": "audio", "codec_name": "aac"},
    ],
    "format": {"duration": "35.4", "size": "93728865", "bit_rate": "21200000"},
}


def test_probe_sample_needs_compat():
    cfg = config.load_config()
    info = media.parse_probe(SAMPLE_PROBE)
    assert info.vcodec == "hevc" and round(info.fps, 2) == 59.94
    assert media.needs_compat_copy(info, cfg)
    assert media.validate(info, cfg) == []


def test_h264_8bit_skips_compat():
    cfg = config.load_config()
    data = {**SAMPLE_PROBE, "streams": [{**SAMPLE_PROBE["streams"][0], "codec_name": "h264", "pix_fmt": "yuv420p"}, SAMPLE_PROBE["streams"][1]],
            "format": {"duration": "35", "size": "20000000", "bit_rate": "8000000"}}
    assert not media.needs_compat_copy(media.parse_probe(data), cfg)


def test_real_sample_if_present():
    p = Path(__file__).parents[1] / "sample-video" / (SAMPLE + ".mp4")
    if not p.exists():
        pytest.skip("sample missing")
    info = media.probe(p)
    assert (info.width, info.height) == (1080, 1920) and info.vcodec == "hevc"
