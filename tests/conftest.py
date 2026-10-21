import pytest

from gwc import config
from gwc.db import session


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """Point every test at a throwaway data dir and ignore the real .env."""
    for var in ("GARMIN_EMAIL", "GARMIN_PASSWORD", "USDA_API_KEY", "ANTHROPIC_API_KEY", "GWC_TZ"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GWC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setitem(config.Settings.model_config, "env_file", None)
    config.get_settings.cache_clear()
    session.get_engine.cache_clear()
    session._session_factory.cache_clear()
    yield config.get_settings()
    config.get_settings.cache_clear()
    session.get_engine.cache_clear()
    session._session_factory.cache_clear()
