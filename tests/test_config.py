from zoneinfo import ZoneInfo

from gwc.config import Settings


def test_defaults_point_into_data_dir(isolated_settings: Settings, tmp_path):
    s = isolated_settings
    assert s.data_dir == tmp_path / "data"
    assert s.db_path == tmp_path / "data" / "gwc.db"
    assert s.db_url.startswith("sqlite:///") and s.db_url.endswith("/data/gwc.db")
    assert s.llm_backend == "subscription"
    assert s.host == "127.0.0.1"
    assert s.port == 8060
    assert s.garmin_email is None


def test_ensure_dirs_creates_layout(isolated_settings: Settings):
    isolated_settings.ensure_dirs()
    for path in (
        isolated_settings.garmin_token_dir,
        isolated_settings.log_dir,
        isolated_settings.backup_dir,
    ):
        assert path.is_dir()


def test_tz_override(monkeypatch):
    monkeypatch.setenv("GWC_TZ", "America/Chicago")
    assert Settings(_env_file=None).tz == ZoneInfo("America/Chicago")


def test_tz_defaults_to_system_zone():
    assert isinstance(Settings(_env_file=None).tz, ZoneInfo)


def test_secrets_are_masked(monkeypatch):
    monkeypatch.setenv("GARMIN_PASSWORD", "hunter2")
    s = Settings(_env_file=None)
    assert "hunter2" not in repr(s)
    assert s.garmin_password.get_secret_value() == "hunter2"
