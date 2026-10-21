"""Fetch Garmin data for a window of days and upsert it.

Daily/catch-up/manual syncs re-fetch today plus the last 3 days (Garmin
finalizes totals and sleep late); the first sync backfills 30 days. Days are
fetched newest first and committed one at a time, so a sync cut short by a
rate limit still keeps what it got.
"""

import logging
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from garminconnect import GarminConnectAuthenticationError, GarminConnectNotFoundError
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from gwc.db.models import (
    Activity,
    BodyComp,
    DailySummary,
    GarminRaw,
    GarminRequest,
    Sleep,
    SyncKind,
    SyncRun,
    SyncStatus,
)
from gwc.db.session import session_scope
from gwc.db.types import utcnow
from gwc.garmin import parse
from gwc.garmin.client import AuthRequired, GarminApi, is_rate_limit_error
from gwc.garmin.guard import BudgetExceeded, Decision, LockedOut, SyncGuard
from gwc.prefs import get_pref, set_pref

log = logging.getLogger(__name__)

BACKFILL_DAYS = 30
RESYNC_DAYS = 3
LAST_DEVICE_SYNC_PREF = "garmin.last_device_sync_at"


@dataclass(frozen=True)
class SyncResult:
    started: bool
    message: str
    status: SyncStatus | None = None
    run_id: int | None = None


def local_midnight_utc(d: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(d, time(), tzinfo=tz).astimezone(UTC)


def _days_desc(start: date, end: date) -> Iterator[date]:
    d = end
    while d >= start:
        yield d
        d -= timedelta(days=1)


def _cause_chain(exc: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        yield cur
        cur = cur.__cause__ or cur.__context__


def _find_cause(exc: BaseException, types: tuple[type[BaseException], ...]) -> BaseException | None:
    return next((c for c in _cause_chain(exc) if isinstance(c, types)), None)


def _is_missing_data(exc: BaseException) -> bool:
    return isinstance(exc, GarminConnectNotFoundError) or "no data received" in str(exc).lower()


class SyncService:
    def __init__(
        self,
        auth: Any,
        guard: SyncGuard,
        tz: ZoneInfo,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.auth = auth
        self.guard = guard
        self.tz = tz
        self.clock = clock
        self._run_lock = threading.Lock()

    # ------------------------------------------------------------------ planning

    def today(self) -> date:
        return self.clock().astimezone(self.tz).date()

    def has_backfilled(self) -> bool:
        with session_scope() as s:
            return (
                s.scalar(
                    select(func.count())
                    .select_from(SyncRun)
                    .where(SyncRun.kind == SyncKind.BACKFILL, SyncRun.status == SyncStatus.SUCCESS)
                )
                or 0
            ) > 0

    def window_for(self, kind: SyncKind) -> tuple[date, date]:
        end = self.today()
        days = (
            BACKFILL_DAYS if kind is SyncKind.BACKFILL or not self.has_backfilled() else RESYNC_DAYS
        )
        return end - timedelta(days=days), end

    @staticmethod
    def estimate_requests(start: date, end: date) -> int:
        days = (end - start).days + 1
        # summary + sleep per day; body comp, activities (1+ pages), device sync, profile load
        return days * 2 + 6

    def precheck(self, kind: SyncKind) -> Decision:
        if not self.auth.has_tokens():
            return Decision(False, "Connect Garmin first")
        start, end = self.window_for(kind)
        return self.guard.check_sync(kind, self.estimate_requests(start, end))

    def last_success(self) -> SyncRun | None:
        with session_scope() as s:
            return s.scalars(
                select(SyncRun)
                .where(SyncRun.status == SyncStatus.SUCCESS)
                .order_by(SyncRun.started_at.desc())
                .limit(1)
            ).first()

    def recover_interrupted(self) -> int:
        """Mark runs left RUNNING by a crash or shutdown as failed."""
        with session_scope() as s:
            result = s.execute(
                update(SyncRun)
                .where(SyncRun.status == SyncStatus.RUNNING)
                .values(status=SyncStatus.FAILED, finished_at=self.clock(), error="interrupted")
            )
            return result.rowcount or 0

    # ------------------------------------------------------------------ running

    def run(self, kind: SyncKind) -> SyncResult:
        if not self._run_lock.acquire(blocking=False):
            return SyncResult(False, "A sync is already running")
        try:
            return self._run_locked(kind)
        finally:
            self._run_lock.release()

    def _run_locked(self, kind: SyncKind) -> SyncResult:
        decision = self.precheck(kind)
        if not decision.allowed:
            return SyncResult(False, decision.reason or "Sync not allowed")

        start, end = self.window_for(kind)
        with session_scope() as s:
            run = SyncRun(kind=kind, started_at=self.clock(), window_start=start, window_end=end)
            s.add(run)
            s.flush()
            run_id = run.id
        self.guard.current_run_id = run_id
        log.info("Garmin %s sync %s..%s started (run %s)", kind.value, start, end, run_id)

        status, error = SyncStatus.SUCCESS, None
        try:
            client = self.auth.get_client()
            self._sync_window(client, start, end)
        except AuthRequired as e:
            status, error = SyncStatus.AUTH_REQUIRED, str(e)
        except Exception as e:  # noqa: BLE001 - classified below, recorded on the run
            status, error = self._classify_failure(e)
        finally:
            self.guard.current_run_id = None

        with session_scope() as s:
            run = s.get(SyncRun, run_id)
            run.status = status
            run.error = error
            run.finished_at = self.clock()
            run.requests_made = (
                s.scalar(
                    select(func.count())
                    .select_from(GarminRequest)
                    .where(GarminRequest.sync_run_id == run_id)
                )
                or 0
            )
        log.info("Garmin sync run %s finished: %s %s", run_id, status.value, error or "")
        message = "Sync complete" if status is SyncStatus.SUCCESS else (error or status.value)
        return SyncResult(True, message, status, run_id)

    def _classify_failure(self, exc: Exception) -> tuple[SyncStatus, str]:
        if any(is_rate_limit_error(c) for c in _cause_chain(exc)):
            lockout = self.guard.register_rate_limit(is_login=False)
            until = lockout.until.astimezone(self.tz)
            return SyncStatus.RATE_LIMITED, f"Garmin rate-limited us; paused until {until:%a %H:%M}"
        guarded = _find_cause(exc, (LockedOut, BudgetExceeded))
        if guarded is not None:
            return SyncStatus.FAILED, str(guarded)
        if _find_cause(exc, (GarminConnectAuthenticationError, AuthRequired)) is not None:
            self.auth.invalidate_client()
            return (
                SyncStatus.AUTH_REQUIRED,
                "Garmin rejected the saved login; reconnect in Settings",
            )
        log.exception("Garmin sync failed")
        return SyncStatus.FAILED, str(exc).splitlines()[0][:300] if str(exc) else type(exc).__name__

    # ------------------------------------------------------------------ fetching

    def _sync_window(self, client: GarminApi, start: date, end: date) -> None:
        s_str, e_str = start.isoformat(), end.isoformat()
        window_start = local_midnight_utc(start, self.tz)
        window_end = local_midnight_utc(end + timedelta(days=1), self.tz)

        body = client.get_body_composition(s_str, e_str)
        with session_scope() as s:
            _store_raw(s, "body_comp", f"{s_str}..{e_str}", body)
            _replace_in_window(
                s,
                BodyComp,
                BodyComp.measured_at,
                "measured_at",
                parse.parse_body_comp(body),
                window_start,
                window_end,
            )

        activities = client.get_activities_by_date(s_str, e_str)
        with session_scope() as s:
            _store_raw(s, "activities", f"{s_str}..{e_str}", activities)
            _replace_in_window(
                s,
                Activity,
                Activity.start_at,
                "garmin_activity_id",
                parse.parse_activities(activities),
                window_start,
                window_end,
            )

        for day in _days_desc(start, end):
            d_str = day.isoformat()
            try:
                summary = client.get_user_summary(d_str)
            except Exception as e:
                if not _is_missing_data(e):
                    raise
                summary = None
            sleep = client.get_sleep_data(d_str)
            with session_scope() as s:
                if summary is not None:
                    _store_raw(s, "user_summary", d_str, summary)
                    _upsert(
                        s, DailySummary, ["calendar_date"], parse.parse_daily_summary(day, summary)
                    )
                _store_raw(s, "sleep", d_str, sleep)
                _replace_main_sleep(s, day, parse.parse_sleep(sleep))

        device_sync = parse.parse_sync_timestamp(client.get_sync_timestamp())
        with session_scope() as s:
            if device_sync is not None:
                set_pref(LAST_DEVICE_SYNC_PREF, device_sync.isoformat(), session=s)
            self._refresh_provisional(s)

    def _refresh_provisional(self, s: Session) -> None:
        """A calendar day is provisional until the watch has uploaded after it ended."""
        raw = get_pref(LAST_DEVICE_SYNC_PREF, session=s)
        last_upload = datetime.fromisoformat(raw) if raw else None
        for row in s.scalars(select(DailySummary)):
            day_end = local_midnight_utc(row.calendar_date + timedelta(days=1), self.tz)
            row.is_provisional = last_upload is None or last_upload < day_end


def _store_raw(s: Session, endpoint: str, key: str, payload: Any) -> None:
    stmt = insert(GarminRaw).values(
        endpoint=endpoint, key=key, payload=payload, fetched_at=utcnow()
    )
    s.execute(
        stmt.on_conflict_do_update(
            index_elements=["endpoint", "key"],
            set_={"payload": stmt.excluded.payload, "fetched_at": stmt.excluded.fetched_at},
        )
    )


def _upsert(s: Session, model: type, keys: list[str], values: dict[str, Any]) -> None:
    now = utcnow()
    stmt = insert(model).values(**values, created_at=now, updated_at=now)
    updates = {k: stmt.excluded[k] for k in values if k not in keys}
    updates["updated_at"] = now
    s.execute(stmt.on_conflict_do_update(index_elements=keys, set_=updates))


def _replace_in_window(
    s: Session,
    model: type,
    time_col: Any,
    key: str,
    rows: list[dict[str, Any]],
    window_start: datetime,
    window_end: datetime,
) -> None:
    """Mirror Garmin for the window: upsert what it returned, drop what it no longer has."""
    keep = [r[key] for r in rows]
    key_col = getattr(model, key)
    s.execute(
        delete(model)
        .where(time_col >= window_start, time_col < window_end)
        .where(key_col.not_in(keep))
    )
    for row in rows:
        _upsert(s, model, [key], row)


def _replace_main_sleep(s: Session, calendar_date: date, row: dict[str, Any] | None) -> None:
    s.execute(delete(Sleep).where(Sleep.calendar_date == calendar_date, Sleep.is_nap.is_(False)))
    if row is not None:
        # The same sleep can't be labeled with another date, but guard against Garmin relabeling.
        s.execute(delete(Sleep).where(Sleep.start_at == row["start_at"]))
        s.add(Sleep(**row))


class SyncRunner:
    """Runs syncs on a background thread so the UI stays responsive."""

    def __init__(self, service: SyncService) -> None:
        self.service = service
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, kind: SyncKind) -> SyncResult:
        with self._lock:
            if self.is_running():
                return SyncResult(False, "A sync is already running")
            decision = self.service.precheck(kind)
            if not decision.allowed:
                return SyncResult(False, decision.reason or "Sync not allowed")
            self._thread = threading.Thread(
                target=self._run, args=(kind,), name=f"garmin-sync-{kind.value}", daemon=True
            )
            self._thread.start()
            return SyncResult(True, "Sync started")

    def _run(self, kind: SyncKind) -> None:
        try:
            self.service.run(kind)
        except Exception:
            log.exception("Background Garmin sync crashed")
