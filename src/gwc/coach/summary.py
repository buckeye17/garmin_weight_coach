"""Weekly summaries: generated after the weekly sync day's Garmin sync, and on demand.

A summary covers the food days Monday through Sunday of the week before it's
written; `week_start` is that Monday.
"""

import logging
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from gwc.analysis.food_day import current_food_day
from gwc.analysis.units import weight_unit
from gwc.coach import prompts
from gwc.coach.jobs import Job, get_jobs
from gwc.coach.llm import LLMRequest, get_backend
from gwc.coach.settings import feature_config, summary_enabled, summary_weekday
from gwc.coach.tools import data_tools
from gwc.db.models import CoachSummary
from gwc.db.session import session_scope
from gwc.db.types import utcnow
from gwc.prefs import get_pref
from gwc.scheduler import parse_sync_time

log = logging.getLogger(__name__)

# Gives the scheduled Garmin sync time to finish before the summary reads its data.
DELAY_AFTER_SYNC = timedelta(minutes=10)


def last_full_week(today: date) -> date:
    """Monday of the most recent Monday–Sunday week that has fully ended."""
    this_monday = today - timedelta(days=today.weekday())
    return this_monday - timedelta(days=7)


def list_summaries() -> list[CoachSummary]:
    with session_scope() as s:
        return list(s.scalars(select(CoachSummary).order_by(CoachSummary.week_start.desc())))


def has_summary(week_start: date) -> bool:
    with session_scope() as s:
        return (
            s.scalar(select(CoachSummary.id).where(CoachSummary.week_start == week_start))
            is not None
        )


def generate(week_start: date, tz: ZoneInfo, now: datetime, job: Job | None = None) -> str:
    week_end = week_start + timedelta(days=6)
    local = now.astimezone(tz)
    prompt = (
        f"[Local time {local:%A %Y-%m-%d %H:%M}. Weight unit: {weight_unit()}.]\n\n"
        f"Write my weekly summary for the food days {week_start:%a %b %d} through "
        f"{week_end:%a %b %d, %Y} ({week_start.isoformat()} to {week_end.isoformat()})."
    )
    req = LLMRequest(
        system=prompts.WEEKLY_SUMMARY,
        messages=[{"role": "user", "content": prompt}],
        config=feature_config("summary"),
        tools=data_tools(),
        on_text=job.on_text if job else None,
        on_turn=job.on_turn if job else None,
        on_activity=job.on_activity if job else None,
    )
    result = get_backend().run(req)
    content = result.text.strip()
    with session_scope() as s:
        row = s.scalars(select(CoachSummary).where(CoachSummary.week_start == week_start)).first()
        if row is None:
            row = CoachSummary(week_start=week_start, content=content)
            s.add(row)
        row.content = content
        row.model = result.model
    return content


def start_generate(week_start: date, tz: ZoneInfo, now: datetime) -> Job:
    return get_jobs().start(
        "summary", lambda job: generate(week_start, tz, now, job), key=f"summary:{week_start}"
    )


def scheduled_time(today: date, tz: ZoneInfo) -> datetime:
    """When this week's summary is due: the summary weekday, sync time + a short delay."""
    hour, minute = parse_sync_time(get_pref("sync_time"))
    day = today - timedelta(days=today.weekday()) + timedelta(days=summary_weekday())
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz) + DELAY_AFTER_SYNC


def due_week(tz: ZoneInfo, now: datetime) -> date | None:
    """The week to summarize now, if a scheduled summary is due and missing."""
    if not summary_enabled():
        return None
    local_today = now.astimezone(tz).date()
    if now < scheduled_time(local_today, tz):
        return None
    with session_scope() as s:
        today = current_food_day(s, tz, now)
    week = last_full_week(today)
    return None if has_summary(week) else week


def run_if_due(tz: ZoneInfo, now: datetime) -> Job | None:
    week = due_week(tz, now)
    if week is None:
        return None
    log.info("Generating weekly summary for week of %s", week)
    return start_generate(week, tz, now)


def _wait_for_sync(timeout: timedelta = timedelta(minutes=15)) -> None:
    from gwc.garmin.service import get_runner

    runner = get_runner()
    deadline = time.monotonic() + timeout.total_seconds()
    while runner.is_running() and time.monotonic() < deadline:
        time.sleep(10)


def check_and_run(tz: ZoneInfo) -> None:
    """Scheduler entry point: wait out a running sync, then summarize if due."""
    try:
        _wait_for_sync()
        run_if_due(tz, utcnow())
    except Exception:
        log.exception("Weekly summary check failed")


def register(scheduler, tz: ZoneInfo) -> None:
    """Check daily shortly after the sync time, and once soon after startup (catch-up)."""
    from apscheduler.triggers.cron import CronTrigger

    hour, minute = parse_sync_time(get_pref("sync_time"))
    at = datetime(2000, 1, 1, hour, minute) + DELAY_AFTER_SYNC
    scheduler.add_job(
        check_and_run,
        CronTrigger(hour=at.hour, minute=at.minute, timezone=tz),
        args=[tz],
        id="weekly-summary",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=12 * 3600,
        replace_existing=True,
    )
    scheduler.add_job(
        check_and_run,
        "date",
        run_date=utcnow() + timedelta(minutes=2),
        args=[tz],
        id="weekly-summary-catchup",
        replace_existing=True,
    )
