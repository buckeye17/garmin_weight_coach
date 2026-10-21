"""Daily copies of the SQLite database in `data/backups/`.

One file per local day (`gwc-YYYY-MM-DD.db`): a later sync the same day
refreshes that day's copy. The newest `KEEP` copies are kept.
"""

import logging
import sqlite3
from datetime import date
from pathlib import Path

from gwc.config import get_settings

log = logging.getLogger(__name__)

KEEP = 14
PREFIX = "gwc-"
SUFFIX = ".db"


def backup_path(day: date, backup_dir: Path | None = None) -> Path:
    return (backup_dir or get_settings().backup_dir) / f"{PREFIX}{day.isoformat()}{SUFFIX}"


def list_backups(backup_dir: Path | None = None) -> list[Path]:
    """Backups oldest first (the ISO date in the name sorts chronologically)."""
    backup_dir = backup_dir or get_settings().backup_dir
    return sorted(backup_dir.glob(f"{PREFIX}????-??-??{SUFFIX}"))


def backup_database(day: date, keep: int = KEEP) -> Path:
    """Copy the live DB with SQLite's online backup API, then prune old copies.

    The copy is written to a temp file and renamed, so an interrupted backup
    never replaces a good one.
    """
    settings = get_settings()
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_path(day, settings.backup_dir)
    tmp = target.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)

    src = sqlite3.connect(settings.db_path)
    try:
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    tmp.replace(target)

    for old in list_backups(settings.backup_dir)[:-keep]:
        old.unlink(missing_ok=True)
    log.info("Database backed up to %s", target)
    return target
