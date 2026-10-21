"""Programmatic Alembic entry points used by the CLI and at app startup."""

from pathlib import Path

from alembic import command
from alembic.config import Config

ALEMBIC_DIR = Path(__file__).resolve().parent / "alembic"


def alembic_config(url: str | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", ALEMBIC_DIR.as_posix())
    if url:
        cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def upgrade(url: str | None = None, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)


def make_revision(message: str, url: str | None = None) -> None:
    command.revision(alembic_config(url), message=message, autogenerate=True)
