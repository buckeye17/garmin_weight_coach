from zoneinfo import ZoneInfo

import pytest

from fakes import FakeAuth, FakeClock, utc
from gwc.db.migrate import upgrade
from gwc.db.models import SyncKind, SyncRun, SyncStatus
from gwc.db.session import session_scope
from gwc.garmin.guard import SyncGuard
from gwc.garmin.sync import SyncService
from gwc.scheduler import most_recent_scheduled, needs_catchup, next_scheduled

NY = ZoneInfo("America/New_York")


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (utc(2026, 10, 20, 13, 1), utc(2026, 10, 20, 13, 1)),  # exactly 09:01 EDT
        (utc(2026, 10, 20, 13, 0), utc(2026, 10, 19, 13, 1)),  # 08:59:59 → yesterday
        (utc(2026, 10, 21, 3, 30), utc(2026, 10, 20, 13, 1)),  # 23:30 local
        (utc(2026, 11, 2, 15, 0), utc(2026, 11, 2, 14, 1)),  # after DST ends: 09:01 EST
    ],
)
def test_most_recent_scheduled(now, expected):
    assert most_recent_scheduled(now, "09:01", NY) == expected


def test_next_scheduled():
    assert next_scheduled(utc(2026, 10, 20, 12, 0), "09:01", NY) == utc(2026, 10, 20, 13, 1)


@pytest.fixture
def service(isolated_settings):
    upgrade()
    clock = FakeClock(utc(2026, 10, 20, 18, 0))  # 14:00 New York
    return SyncService(FakeAuth(), SyncGuard(NY, clock=clock), NY, clock=clock)


def _success_at(started):
    with session_scope() as s:
        s.add(SyncRun(kind=SyncKind.SCHEDULED, status=SyncStatus.SUCCESS, started_at=started))


def test_catchup_when_never_synced(service):
    assert needs_catchup(service, service.clock(), "09:01")


def test_no_catchup_after_todays_sync(service):
    _success_at(utc(2026, 10, 20, 13, 1))
    assert not needs_catchup(service, service.clock(), "09:01")


def test_catchup_when_todays_sync_missed(service):
    _success_at(utc(2026, 10, 19, 13, 1))
    assert needs_catchup(service, service.clock(), "09:01")


def test_failed_sync_does_not_count(service):
    with session_scope() as s:
        s.add(
            SyncRun(kind=SyncKind.SCHEDULED, status=SyncStatus.FAILED, started_at=service.clock())
        )
    assert needs_catchup(service, service.clock(), "09:01")


def test_no_catchup_when_not_connected(service):
    service.auth.connected = False
    assert not needs_catchup(service, service.clock(), "09:01")
