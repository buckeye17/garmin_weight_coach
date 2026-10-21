from logging.config import fileConfig

from alembic import context

from gwc.config import get_settings
from gwc.db.models import Base
from gwc.db.session import make_engine

config = context.config

if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    url = config.get_main_option("sqlalchemy.url")
    if url:
        return url
    settings = get_settings()
    settings.ensure_dirs()
    return settings.db_url


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = config.attributes.get("connection") or make_engine(_url())

    def _run(connection) -> None:
        # SQLite can't ALTER most things in place; batch mode recreates tables instead.
        context.configure(
            connection=connection, target_metadata=target_metadata, render_as_batch=True
        )
        with context.begin_transaction():
            context.run_migrations()

    if hasattr(connectable, "connect"):
        with connectable.connect() as connection:
            _run(connection)
    else:
        _run(connectable)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
