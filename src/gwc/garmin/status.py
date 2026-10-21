"""Read-only snapshot of Garmin connection and sync state for the UI."""

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select

from gwc.db.models import DailySummary, SyncKind, SyncRun, SyncStatus
from gwc.db.session import session_scope
from gwc.garmin.guard import Decision, GuardStatus
from gwc.garmin.service import get_auth, get_runner, get_sync_service
from gwc.garmin.sync import LAST_DEVICE_SYNC_PREF
from gwc.prefs import get_pref
from gwc.scheduler import next_scheduled


@dataclass(frozen=True)
class GarminOverview:
    connected: bool
    display_name: str | None
    has_credentials: bool
    mfa_pending: bool
    running: bool
    last_run: SyncRun | None
    last_success: SyncRun | None
    next_sync: datetime
    last_device_sync: datetime | None
    provisional_days: list[date]
    guard: GuardStatus
    manual: Decision
    recent_runs: list[SyncRun]


def get_overview(recent: int = 8) -> GarminOverview:
    auth = get_auth()
    service = get_sync_service()
    now = service.clock()
    with session_scope() as s:
        runs = list(s.scalars(select(SyncRun).order_by(SyncRun.started_at.desc()).limit(recent)))
        provisional = list(
            s.scalars(
                select(DailySummary.calendar_date)
                .where(DailySummary.is_provisional.is_(True))
                .order_by(DailySummary.calendar_date)
            )
        )
    raw_device_sync = get_pref(LAST_DEVICE_SYNC_PREF)
    running = get_runner().is_running() or any(r.status is SyncStatus.RUNNING for r in runs)
    return GarminOverview(
        connected=auth.has_tokens(),
        display_name=auth.display_name(),
        has_credentials=auth.has_credentials(),
        mfa_pending=auth.mfa_pending(),
        running=running,
        last_run=runs[0] if runs else None,
        last_success=service.last_success(),
        next_sync=next_scheduled(now, get_pref("sync_time"), service.tz),
        last_device_sync=datetime.fromisoformat(raw_device_sync) if raw_device_sync else None,
        # Today is always provisional; only earlier days are worth calling out.
        provisional_days=[d for d in provisional if d < service.today()],
        guard=service.guard.status(),
        manual=service.precheck(SyncKind.MANUAL),
        recent_runs=runs,
    )


def badge(ov: GarminOverview, fmt) -> tuple[str, str]:
    """Header badge text and color."""
    if ov.running:
        return "Syncing…", "blue"
    if ov.guard.lockout is not None:
        return f"Garmin paused until {fmt(ov.guard.lockout.until)}", "red"
    if not ov.connected:
        return "Garmin: not connected", "gray"
    if ov.last_run is not None and ov.last_run.status is SyncStatus.AUTH_REQUIRED:
        return "Reconnect Garmin", "orange"
    if ov.last_success is None:
        return "Garmin connected", "brand"
    text = f"Synced {fmt(ov.last_success.started_at)}"
    if ov.last_run is not None and ov.last_run.status is not SyncStatus.SUCCESS:
        return f"{text} · last sync failed", "orange"
    if ov.provisional_days:
        n = len(ov.provisional_days)
        return f"{text} · {n} provisional day{'s' if n > 1 else ''}", "yellow"
    return text, "brand"
