"""Environment-level configuration loaded from `.env`.

User preferences (units, goals, sync time, ...) live in the database, not here.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import tzlocal
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    garmin_email: str | None = Field(default=None, validation_alias="GARMIN_EMAIL")
    garmin_password: SecretStr | None = Field(default=None, validation_alias="GARMIN_PASSWORD")
    usda_api_key: SecretStr | None = Field(default=None, validation_alias="USDA_API_KEY")

    llm_backend: Literal["subscription", "api"] = Field(
        default="subscription", validation_alias="GWC_LLM_BACKEND"
    )
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")

    host: str = Field(default="127.0.0.1", validation_alias="GWC_HOST")
    port: int = Field(default=8060, validation_alias="GWC_PORT")
    tz_name: str | None = Field(default=None, validation_alias="GWC_TZ")
    data_dir: Path = Field(default=PROJECT_ROOT / "data", validation_alias="GWC_DATA_DIR")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "gwc.db"

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path.as_posix()}"

    @property
    def garmin_token_dir(self) -> Path:
        return self.data_dir / "garmin_tokens"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def tz(self) -> ZoneInfo:
        """Configured time zone, or the system's local zone when unset."""
        return ZoneInfo(self.tz_name) if self.tz_name else tzlocal.get_localzone()

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.garmin_token_dir, self.log_dir, self.backup_dir):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
