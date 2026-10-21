import pytest

from gwc import config
from gwc.coach import jobs, llm
from gwc.db import session
from gwc.garmin import service
from gwc.nutrition import service as nutrition_service


def _clear_caches():
    config.get_settings.cache_clear()
    session.get_engine.cache_clear()
    session._session_factory.cache_clear()
    for factory in (
        service.get_guard,
        service.get_auth,
        service.get_sync_service,
        service.get_runner,
    ):
        factory.cache_clear()
    jobs.get_jobs.cache_clear()
    nutrition_service.get_usda.cache_clear()
    llm.set_backend_override(None)


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """Point every test at a throwaway data dir and ignore the real .env."""
    for var in ("GARMIN_EMAIL", "GARMIN_PASSWORD", "USDA_API_KEY", "ANTHROPIC_API_KEY", "GWC_TZ"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GWC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setitem(config.Settings.model_config, "env_file", None)
    _clear_caches()
    yield config.get_settings()
    _clear_caches()
