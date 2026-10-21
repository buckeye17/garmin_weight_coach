"""Daily Garmin sync at the configured time, plus catch-up when a run was missed.

APScheduler's misfire grace covers the PC sleeping through the sync time: the
job runs when the machine wakes. Startup catch-up covers the app not running.
"""

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from gwc.db.models import SyncKind
from gwc.garmin.sync import SyncRunner, SyncService
from gwc.prefs import get_pref

log = logging.getLogger(__name__)

JOB_ID = "garmin-daily-sync"
MISFIRE_GRACE = timedelta(hours=12)


def parse_sync_time(value: str) -> tuple[int, int]:
    hour, minute = value.split(":")
    return int(hour), int(minute)


def most_recent_scheduled(now: datetime, sync_time: str, tz: ZoneInfo) -> datetime:
    """The latest scheduled sync time at or before `now`."""
    hour, minute = parse_sync_time(sync_time)
    local = now.astimezone(tz)
    scheduled = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if scheduled > local:
        scheduled -= timedelta(days=1)
    return scheduled


def next_scheduled(now: datetime, sync_time: str, tz: ZoneInfo) -> datetime:
    return most_recent_scheduled(now, sync_time, tz) + timedelta(days=1)


def needs_catchup(service: SyncService, now: datetime, sync_time: str) -> bool:
    if not service.auth.has_tokens():
        return False
    last = service.last_success()
    return last is None or last.started_at < most_recent_scheduled(now, sync_time, service.tz)


class SyncScheduler:
    def __init__(self, service: SyncService, runner: SyncRunner) -> None:
        self.service = service
        self.runner = runner
        self.scheduler = BackgroundScheduler(timezone=service.tz)

    @property
    def sync_time(self) -> str:
        return get_pref("sync_time")

    def start(self) -> None:
        self.service.recover_interrupted()
        hour, minute = parse_sync_time(self.sync_time)
        self.scheduler.add_job(
            self._scheduled,
            CronTrigger(hour=hour, minute=minute, timezone=self.service.tz),
            id=JOB_ID,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=int(MISFIRE_GRACE.total_seconds()),
            replace_existing=True,
        )
        self.scheduler.start()
        log.info("Daily Garmin sync scheduled at %s", self.sync_time)
        self.catch_up()

    def shutdown(self) -> None:
        self.scheduler.shutdown(wait=False)

    def catch_up(self) -> None:
        if needs_catchup(self.service, self.service.clock(), self.sync_time):
            result = self.runner.start(SyncKind.CATCHUP)
            log.info("Catch-up sync: %s", result.message)

    def _scheduled(self) -> None:
        result = self.runner.start(SyncKind.SCHEDULED)
        log.info("Scheduled sync: %s", result.message)
