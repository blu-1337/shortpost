from shortpost import config, doctor
from shortpost.cli import build_parser


def test_default_config_loads(tmp_path):
    cfg = config.load_config(user_dir=tmp_path)
    assert cfg["schedule"]["native_time"] == "18:30"
    assert cfg["platforms"]["youtube"]["mode"] == "browser"


def test_user_override_deep_merges(tmp_path):
    (tmp_path / "config.toml").write_text('[schedule]\nlocal_time = "21:00"\n', encoding="utf-8")
    cfg = config.load_config(user_dir=tmp_path)
    assert cfg["schedule"]["local_time"] == "21:00"
    assert cfg["schedule"]["native_time"] == "18:30"


def test_secret_roundtrip(tmp_path):
    config.save_secret("ntfy_topic", 'a"b', tmp_path)
    config.save_secret("other", "x", tmp_path)
    assert config.load_secrets(tmp_path) == {"ntfy_topic": 'a"b', "other": "x"}


def test_config_check_ok():
    assert doctor.check_config(config.load_config()).status == doctor.OK


def test_api_mode_without_audit_fails():
    cfg = config.load_config()
    cfg["platforms"]["youtube"]["mode"] = "api"
    assert doctor.check_youtube_mode(cfg).status == doctor.FAIL


def test_parser_has_all_commands():
    names = build_parser()._subparsers._group_actions[0].choices
    for n in ("doctor", "auth", "set-secret", "add", "review", "run", "list", "show", "regen", "edit", "reschedule", "cancel", "sync"):
        assert n in names
