import pytest


@pytest.fixture(autouse=True)
def _isolated_appdata(tmp_path_factory, monkeypatch):
    """Tests never see the real %APPDATA%\ShortPost secrets (no real Gemini or platform calls)."""
    monkeypatch.setenv("APPDATA", str(tmp_path_factory.mktemp("appdata")))
