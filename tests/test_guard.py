from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest

from fakes import FakeClock, utc
from gwc.db.migrate import upgrade
from gwc.db.models import SyncKind, SyncRun, SyncStatus
from gwc.db.session import session_scope
from gwc.garmin.guard import BudgetExceeded, GuardConfig, LockedOut, SyncGuard

NY = ZoneInfo("America/New_York")


@pytest.fixture
def clock():
    return FakeClock(utc(2026, 10, 20, 18, 0))  # 14:00 in New York


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
def guard(isolated_settings, clock, sleeps):
    upgrade()
    return SyncGuard(NY, GuardConfig(), clock=clock, sleep=sleeps.append)


def _add_run(clock, kind=SyncKind.MANUAL, status=SyncStatus.SUCCESS, ago=timedelta(0)):
    with session_scope() as s:
        s.add(SyncRun(kind=kind, status=status, started_at=clock() - ago))


def test_fresh_state_allows_everything(guard):
    assert guard.check_sync(SyncKind.MANUAL, 20).allowed
    assert guard.check_sync(SyncKind.SCHEDULED, 20).allowed
    assert guard.check_login().allowed


def test_request_spacing_sleeps_between_requests(guard, sleeps):
    guard.before_request()
    guard.before_request()
    assert len(sleeps) == 1
    assert 0 < sleeps[0] <= 1.0


def test_request_budget(isolated_settings, clock):
    upgrade()
    guard = SyncGuard(NY, GuardConfig(max_requests_per_24h=3), clock=clock, sleep=lambda _: None)
    for _ in range(3):
        guard.before_request()
        guard.record_request("/x", 200)
    with pytest.raises(BudgetExceeded):
        guard.before_request()
    assert not guard.check_sync(SyncKind.SCHEDULED, 1).allowed
    clock.advance(hours=24, seconds=1)
    guard.before_request()


def test_budget_must_cover_estimated_sync(isolated_settings, clock):
    upgrade()
    guard = SyncGuard(NY, GuardConfig(max_requests_per_24h=30), clock=clock)
    for _ in range(20):
        guard.record_request("/x", 200)
    decision = guard.check_sync(SyncKind.SCHEDULED, 11)
    assert not decision.allowed
    assert "budget" in decision.reason


def test_manual_cooldown_counts_any_sync(guard, clock):
    _add_run(clock, kind=SyncKind.SCHEDULED, ago=timedelta(minutes=10))
    decision = guard.check_sync(SyncKind.MANUAL, 10)
    assert not decision.allowed
    assert decision.retry_at == clock() + timedelta(minutes=20)
    # Scheduled syncs aren't subject to the manual cooldown.
    assert guard.check_sync(SyncKind.SCHEDULED, 10).allowed
    clock.advance(minutes=20)
    assert guard.check_sync(SyncKind.MANUAL, 10).allowed


def test_manual_daily_limit_resets_at_local_midnight(guard, clock):
    for i in range(6):
        _add_run(clock, ago=timedelta(hours=1, minutes=i))
    decision = guard.check_sync(SyncKind.MANUAL, 10)
    assert not decision.allowed
    assert "manual" in decision.reason.lower()
    assert decision.retry_at == utc(2026, 10, 21, 4, 0)  # midnight New York (EDT)
    clock.now = utc(2026, 10, 21, 4, 1)
    assert guard.check_sync(SyncKind.MANUAL, 10).allowed


def test_running_sync_blocks(guard, clock):
    _add_run(clock, kind=SyncKind.SCHEDULED, status=SyncStatus.RUNNING, ago=timedelta(hours=2))
    assert not guard.check_sync(SyncKind.SCHEDULED, 10).allowed


def test_login_limit(guard, clock):
    for _ in range(3):
        guard.record_request("sso/login", None, is_login=True)
    assert not guard.check_login().allowed
    clock.advance(hours=24, seconds=1)
    assert guard.check_login().allowed


def test_lockout_escalates_then_resets(guard, clock):
    first = guard.register_rate_limit(is_login=False)
    assert first.until - clock() == timedelta(hours=1)
    assert not guard.check_sync(SyncKind.SCHEDULED, 1).allowed
    with pytest.raises(LockedOut):
        guard.before_request()

    clock.advance(hours=1, seconds=1)
    second = guard.register_rate_limit(is_login=False)
    assert second.until - clock() == timedelta(hours=4)
    clock.advance(hours=4, seconds=1)
    third = guard.register_rate_limit(is_login=False)
    assert third.until - clock() == timedelta(hours=24)
    clock.advance(hours=24, seconds=1)
    fourth = guard.register_rate_limit(is_login=False)
    assert fourth.until - clock() == timedelta(hours=24)  # capped at the last step

    clock.advance(days=3)
    fresh = guard.register_rate_limit(is_login=False)
    assert fresh.until - clock() == timedelta(hours=1)


def test_login_rate_limit_pauses_a_day(guard, clock):
    lockout = guard.register_rate_limit(is_login=True)
    assert lockout.until - clock() == timedelta(hours=24)
    assert not guard.check_login().allowed


def test_status_snapshot(guard, clock):
    guard.record_request("/x", 200)
    guard.record_request("sso/login", None, is_login=True)
    _add_run(clock)
    st = guard.status()
    assert (st.requests_24h, st.logins_24h, st.manual_today) == (2, 1, 1)
    assert st.lockout is None
