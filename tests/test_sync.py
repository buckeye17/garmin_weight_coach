from datetime import date, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from fakes import FakeAuth, FakeClock, FakeGarmin, activity, sleep, summary, utc, weigh_in
from gwc.db.migrate import upgrade
from gwc.db.models import (
    Activity,
    BodyComp,
    DailySummary,
    GarminRaw,
    Sleep,
    SyncKind,
    SyncRun,
    SyncStatus,
)
from gwc.db.session import session_scope
from gwc.garmin.guard import GuardConfig, SyncGuard
from gwc.garmin.sync import SyncService

NY = ZoneInfo("America/New_York")
TODAY = date(2026, 10, 20)


@pytest.fixture
def clock():
    return FakeClock(utc(2026, 10, 20, 13, 1))  # 09:01 New York


@pytest.fixture
def garmin():
    g = FakeGarmin()
    for i in range(35):
        d = TODAY - timedelta(days=i)
        g.summaries[d.isoformat()] = summary(kcal=2500 + i)
        # Night-owl sleep: falls asleep ~04:15 local (08:15 UTC) on the wake date.
        g.sleeps[d.isoformat()] = sleep(d, utc(d.year, d.month, d.day, 8, 15))
    g.weigh_ins = [
        weigh_in(utc(2026, 10, 19, 17, 0), 95.0, sample=1),
        weigh_in(utc(2026, 10, 18, 17, 0), 95.4, sample=2),
        weigh_in(utc(2026, 9, 25, 17, 0), 97.0, sample=3),
    ]
    g.activities = [activity(500, utc(2026, 10, 19, 21, 0))]
    g.device_sync = "2026-10-20T12:30:00.0"
    return g


@pytest.fixture
def service(isolated_settings, clock, garmin):
    upgrade()
    guard = SyncGuard(NY, GuardConfig(), clock=clock, sleep=lambda _: None)
    return SyncService(FakeAuth(garmin), guard, NY, clock=clock)


def _count(model) -> int:
    with session_scope() as s:
        return s.scalar(select(func.count()).select_from(model))


def _runs() -> list[SyncRun]:
    with session_scope() as s:
        return list(s.scalars(select(SyncRun).order_by(SyncRun.id)))


def test_first_sync_backfills_30_days(service, garmin):
    result = service.run(SyncKind.SCHEDULED)
    assert result.started and result.status is SyncStatus.SUCCESS
    run = _runs()[0]
    assert (run.window_start, run.window_end) == (TODAY - timedelta(days=30), TODAY)
    assert _count(DailySummary) == 31
    assert _count(Sleep) == 31
    assert _count(BodyComp) == 3
    assert _count(Activity) == 1
    # Newest day fetched first, so a sync cut short keeps the most useful data.
    day_calls = [args[0] for name, args in garmin.calls if name == "get_user_summary"]
    assert day_calls[0] == TODAY.isoformat()


def test_later_syncs_refetch_four_days(service, garmin):
    service.run(SyncKind.BACKFILL)
    garmin.calls.clear()
    service.run(SyncKind.SCHEDULED)
    run = _runs()[-1]
    assert (run.window_start, run.window_end) == (TODAY - timedelta(days=3), TODAY)
    assert sum(1 for name, _ in garmin.calls if name == "get_user_summary") == 4


def test_sync_is_idempotent_and_updates_values(service, garmin):
    service.run(SyncKind.BACKFILL)
    garmin.summaries[TODAY.isoformat()] = summary(kcal=3100)
    service.run(SyncKind.SCHEDULED)
    assert _count(DailySummary) == 31
    assert _count(Sleep) == 31
    assert _count(BodyComp) == 3
    with session_scope() as s:
        today = s.scalars(select(DailySummary).where(DailySummary.calendar_date == TODAY)).one()
        assert today.total_kcal == 3100
        assert s.scalar(select(func.count()).select_from(GarminRaw)) > 0


def test_deleted_weigh_in_is_removed_only_inside_window(service, garmin):
    service.run(SyncKind.BACKFILL)
    garmin.weigh_ins = [w for w in garmin.weigh_ins if w["samplePk"] != 1]
    # Garmin no longer returns the out-of-window sample either, but it must survive.
    garmin.weigh_ins = [w for w in garmin.weigh_ins if w["samplePk"] != 3]
    service.run(SyncKind.SCHEDULED)
    with session_scope() as s:
        samples = set(s.scalars(select(BodyComp.garmin_sample_id)))
    assert samples == {2, 3}


def test_edited_sleep_replaces_main_sleep(service, garmin):
    service.run(SyncKind.BACKFILL)
    garmin.sleeps[TODAY.isoformat()] = sleep(TODAY, utc(2026, 10, 20, 9, 30), hours=6)
    service.run(SyncKind.SCHEDULED)
    with session_scope() as s:
        rows = list(s.scalars(select(Sleep).where(Sleep.calendar_date == TODAY)))
    assert len(rows) == 1
    assert rows[0].start_at == utc(2026, 10, 20, 9, 30)


def test_days_without_summary_are_skipped(service, garmin):
    del garmin.summaries[(TODAY - timedelta(days=2)).isoformat()]
    result = service.run(SyncKind.BACKFILL)
    assert result.status is SyncStatus.SUCCESS
    assert _count(DailySummary) == 30


def test_provisional_days_follow_device_upload(service, garmin):
    # Watch last uploaded at 08:30 New York today: yesterday is complete, today isn't.
    service.run(SyncKind.BACKFILL)
    with session_scope() as s:
        flags = dict(
            s.execute(select(DailySummary.calendar_date, DailySummary.is_provisional)).all()
        )
    assert flags[TODAY] is True
    assert flags[TODAY - timedelta(days=1)] is False

    # If the watch hasn't uploaded since 22:00 two days ago, yesterday is provisional too.
    garmin.device_sync = "2026-10-19T02:00:00.0"
    service.run(SyncKind.SCHEDULED)
    with session_scope() as s:
        flags = dict(
            s.execute(select(DailySummary.calendar_date, DailySummary.is_provisional)).all()
        )
    assert flags[TODAY - timedelta(days=1)] is True
    assert flags[TODAY - timedelta(days=2)] is True
    assert flags[TODAY - timedelta(days=3)] is False


def test_rate_limit_mid_sync_keeps_partial_data_and_locks_out(service, garmin, clock):
    garmin.rate_limit_after = 6  # body comp, activities, then two days of summary + sleep
    result = service.run(SyncKind.BACKFILL)
    assert result.status is SyncStatus.RATE_LIMITED
    assert _count(DailySummary) == 2
    lockout = service.guard.active_lockout()
    assert lockout is not None and lockout.until - clock() == timedelta(hours=1)
    # Nothing else may run until the lockout ends.
    assert not service.run(SyncKind.SCHEDULED).started
    clock.advance(hours=1, seconds=1)
    garmin.rate_limit_after = None
    assert service.run(SyncKind.SCHEDULED).status is SyncStatus.SUCCESS


def test_not_connected_does_not_create_a_run(service):
    service.auth.connected = False
    result = service.run(SyncKind.SCHEDULED)
    assert not result.started
    assert "Connect Garmin" in result.message
    assert _runs() == []


def test_rejected_tokens_mark_auth_required(service):
    from gwc.garmin.client import AuthRequired

    def boom():
        raise AuthRequired("tokens expired")

    service.auth.get_client = boom
    result = service.run(SyncKind.SCHEDULED)
    assert result.status is SyncStatus.AUTH_REQUIRED


def test_manual_sync_respects_cooldown(service, clock):
    assert service.run(SyncKind.MANUAL).status is SyncStatus.SUCCESS
    clock.advance(minutes=5)
    result = service.run(SyncKind.MANUAL)
    assert not result.started
    assert "Cooling down" in result.message


def test_requests_are_counted_per_run(service, garmin):
    garmin.on_call = lambda name: service.guard.record_request(name, 200)
    service.run(SyncKind.SCHEDULED)
    assert _runs()[0].requests_made == len(garmin.calls)


def test_recover_interrupted(service, clock):
    with session_scope() as s:
        s.add(SyncRun(kind=SyncKind.SCHEDULED, status=SyncStatus.RUNNING, started_at=clock()))
    assert service.recover_interrupted() == 1
    assert _runs()[0].status is SyncStatus.FAILED
    assert service.run(SyncKind.SCHEDULED).started
