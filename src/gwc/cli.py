import argparse
import logging
import os

from gwc.config import get_settings

log = logging.getLogger("gwc")


def _configure_logging() -> None:
    settings = get_settings()
    settings.ensure_dirs()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(settings.log_dir / "gwc.log", encoding="utf-8"),
        ],
    )
    logging.getLogger("alembic").setLevel(logging.WARNING)


def cmd_serve(args: argparse.Namespace) -> None:
    from gwc.db.migrate import upgrade
    from gwc.web.app import create_app

    settings = get_settings()
    upgrade()
    app = create_app()
    host = args.host or settings.host
    port = args.port or settings.port

    # The debug reloader runs the app in a child process; only that one schedules syncs.
    if not args.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        _start_scheduler()
        _warn_if_garmin_disconnected(host, port)

    if args.debug:
        app.run(host=host, port=port, debug=True)
        return

    from waitress import serve

    log.info("Serving on http://%s:%s", host, port)
    serve(app.server, host=host, port=port, threads=8)


def _start_scheduler() -> None:
    from gwc.coach import summary
    from gwc.garmin.service import get_runner, get_sync_service
    from gwc.scheduler import SyncScheduler

    scheduler = SyncScheduler(get_sync_service(), get_runner())
    scheduler.start()
    summary.register(scheduler.scheduler, scheduler.service.tz)


def _warn_if_garmin_disconnected(host: str, port: int) -> None:
    from gwc.garmin.service import get_auth

    if not get_auth().has_tokens():
        log.warning(
            "Garmin is not connected, so nothing will sync. Open http://%s:%s/settings "
            "and click Connect (the app never logs in on its own).",
            host,
            port,
        )


def cmd_db_upgrade(_args: argparse.Namespace) -> None:
    from gwc.db.migrate import upgrade

    upgrade()


def cmd_db_revision(args: argparse.Namespace) -> None:
    from gwc.db.migrate import make_revision

    make_revision(args.message)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gwc", description="Garmin Weight Coach")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the web app")
    serve.add_argument("--host", help="bind address (default from GWC_HOST)")
    serve.add_argument("--port", type=int, help="port (default from GWC_PORT)")
    serve.add_argument("--debug", action="store_true", help="Dash dev server with hot reload")
    serve.set_defaults(func=cmd_serve)

    db = sub.add_parser("db", help="database migrations")
    db_sub = db.add_subparsers(dest="db_command", required=True)
    db_sub.add_parser("upgrade", help="apply migrations").set_defaults(func=cmd_db_upgrade)
    rev = db_sub.add_parser("revision", help="autogenerate a migration from model changes")
    rev.add_argument("-m", "--message", required=True)
    rev.set_defaults(func=cmd_db_revision)

    return parser


def main(argv: list[str] | None = None) -> None:
    _configure_logging()
    args = build_parser().parse_args(argv)
    args.func(args)
