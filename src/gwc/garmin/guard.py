"""Self-imposed limits on Garmin traffic.

Garmin publishes no limits for the endpoints `garminconnect` uses, and repeated
logins have produced account-level 429 blocks lasting days. Every request goes
through `SyncGuard`, which spaces requests, enforces rolling budgets, and pauses
all traffic after Garmin rate-limits us. See docs/PLAN.md §4.4.
"""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from gwc.db.models import GarminLockout, GarminRequest, SyncKind, SyncRun, SyncStatus
from gwc.db.session import session_scope
from gwc.db.types import utcnow


@dataclass(frozen=True)
class GuardConfig:
    min_request_spacing_s: float = 1.0
    max_requests_per_24h: int = 500
    manual_cooldown: timedelta = timedelta(minutes=30)
    max_manual_per_day: int = 6
    max_logins_per_24h: int = 3
    lockout_steps: tuple[timedelta, ...] = (
        timedelta(hours=1),
        timedelta(hours=4),
        timedelta(hours=24),
    )
    login_lockout: timedelta = timedelta(hours=24)
    # A rate limit within this window after the previous lockout ended escalates to the next step.
    strike_window: timedelta = timedelta(hours=24)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str | None = None
    retry_at: datetime | None = None

    @classmethod
    def ok(cls) -> "Decision":
        return cls(True)


class BudgetExceeded(Exception):
    """Raised before a request that would exceed the rolling 24 h request budget."""


class LockedOut(Exception):
    def __init__(self, until: datetime) -> None:
        super().__init__(f"Garmin traffic paused until {until.isoformat()}")
        self.until = until


@dataclass(frozen=True)
class GuardStatus:
    requests_24h: int
    max_requests_24h: int
    logins_24h: int
    max_logins_24h: int
    manual_today: int
    max_manual_per_day: int
    lockout: GarminLockout | None


class SyncGuard:
    def __init__(
        self,
        tz: ZoneInfo,
        config: GuardConfig | None = None,
        clock: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.tz = tz
        self.config = config or GuardConfig()
        self.clock = clock
        self.sleep = sleep
        self._lock = threading.Lock()
        self._last_request_at: float | None = None
        self.current_run_id: int | None = None

    # ------------------------------------------------------------------ queries

    def active_lockout(self) -> GarminLockout | None:
        with session_scope() as s:
            return s.scalars(
                select(GarminLockout)
                .where(GarminLockout.until > self.clock())
                .order_by(GarminLockout.until.desc())
                .limit(1)
            ).first()

    def _count_requests(self, *, logins: bool) -> int:
        since = self.clock() - timedelta(hours=24)
        with session_scope() as s:
            q = select(func.count()).select_from(GarminRequest).where(GarminRequest.sent_at > since)
            if logins:
                q = q.where(GarminRequest.is_login.is_(True))
            return s.scalar(q) or 0

    def requests_last_24h(self) -> int:
        return self._count_requests(logins=False)

    def logins_last_24h(self) -> int:
        return self._count_requests(logins=True)

    def _local_day_start(self) -> datetime:
        local_now = self.clock().astimezone(self.tz)
        return local_now.replace(hour=0, minute=0, second=0, microsecond=0)

    def manual_syncs_today(self) -> int:
        with session_scope() as s:
            return (
                s.scalar(
                    select(func.count())
                    .select_from(SyncRun)
                    .where(SyncRun.kind == SyncKind.MANUAL)
                    .where(SyncRun.started_at >= self._local_day_start())
                )
                or 0
            )

    def last_sync_start(self) -> datetime | None:
        with session_scope() as s:
            return s.scalar(select(func.max(SyncRun.started_at)))

    def is_sync_running(self) -> bool:
        with session_scope() as s:
            return (
                s.scalar(
                    select(func.count())
                    .select_from(SyncRun)
                    .where(SyncRun.status == SyncStatus.RUNNING)
                )
                or 0
            ) > 0

    def status(self) -> GuardStatus:
        cfg = self.config
        return GuardStatus(
            requests_24h=self.requests_last_24h(),
            max_requests_24h=cfg.max_requests_per_24h,
            logins_24h=self.logins_last_24h(),
            max_logins_24h=cfg.max_logins_per_24h,
            manual_today=self.manual_syncs_today(),
            max_manual_per_day=cfg.max_manual_per_day,
            lockout=self.active_lockout(),
        )

    # ------------------------------------------------------------------ decisions

    def check_sync(self, kind: SyncKind, estimated_requests: int) -> Decision:
        lockout = self.active_lockout()
        if lockout is not None:
            return Decision(False, f"Garmin rate-limited us ({lockout.reason})", lockout.until)
        if self.is_sync_running():
            return Decision(False, "A sync is already running")
        remaining = self.config.max_requests_per_24h - self.requests_last_24h()
        if estimated_requests > remaining:
            return Decision(False, "Daily Garmin request budget used up")

        if kind is SyncKind.MANUAL:
            if self.manual_syncs_today() >= self.config.max_manual_per_day:
                tomorrow = self._local_day_start() + timedelta(days=1)
                return Decision(False, "Daily manual sync limit reached", tomorrow)
            last = self.last_sync_start()
            if last is not None:
                available_at = last + self.config.manual_cooldown
                if available_at > self.clock():
                    return Decision(False, "Cooling down after the last sync", available_at)
        return Decision.ok()

    def check_login(self) -> Decision:
        lockout = self.active_lockout()
        if lockout is not None:
            return Decision(False, f"Garmin rate-limited us ({lockout.reason})", lockout.until)
        if self.logins_last_24h() >= self.config.max_logins_per_24h:
            return Decision(False, "Too many Garmin login attempts in the last 24 hours")
        return Decision.ok()

    # ------------------------------------------------------------------ recording

    def before_request(self) -> None:
        """Block until the next request may be sent; raise if limits forbid it."""
        lockout = self.active_lockout()
        if lockout is not None:
            raise LockedOut(lockout.until)
        if self.requests_last_24h() >= self.config.max_requests_per_24h:
            raise BudgetExceeded("Garmin request budget for the last 24 hours is used up")
        with self._lock:
            if self._last_request_at is not None:
                wait = self.config.min_request_spacing_s - (
                    time.monotonic() - self._last_request_at
                )
                if wait > 0:
                    self.sleep(wait)
            self._last_request_at = time.monotonic()

    def record_request(
        self, endpoint: str, status_code: int | None, *, is_login: bool = False
    ) -> None:
        with session_scope() as s:
            s.add(
                GarminRequest(
                    sent_at=self.clock(),
                    endpoint=endpoint[:64],
                    is_login=is_login,
                    status_code=status_code,
                    sync_run_id=self.current_run_id,
                )
            )

    def register_rate_limit(self, *, is_login: bool) -> GarminLockout:
        """Pause all Garmin traffic, escalating if we were rate-limited recently."""
        now = self.clock()
        with session_scope() as s:
            previous = s.scalars(
                select(GarminLockout)
                .where(GarminLockout.until > now - self.config.strike_window)
                .order_by(GarminLockout.created_at.desc())
                .limit(1)
            ).first()
            steps = self.config.lockout_steps
            if is_login:
                strike, duration, reason = 1, self.config.login_lockout, "login returned 429"
            else:
                strike = (previous.strike + 1) if previous else 1
                duration = steps[min(strike, len(steps)) - 1]
                reason = "data request returned 429"
            lockout = GarminLockout(
                created_at=now, until=now + duration, strike=strike, reason=reason
            )
            s.add(lockout)
        return lockout
