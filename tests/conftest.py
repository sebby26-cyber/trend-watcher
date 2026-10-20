"""Keep user-installed packs out of tests unless a test explicitly selects them."""
import pytest


@pytest.fixture(autouse=True)
def isolated_pack_search(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    monkeypatch.setenv("TREND_WATCH_PACKS_DIR", "")
