from datetime import date

from fakes import activity, no_sleep, sleep, summary, utc, weigh_in
from gwc.garmin import parse


def test_body_comp_converts_grams():
    rows = parse.parse_body_comp(
        {"dateWeightList": [weigh_in(utc(2026, 10, 20, 17, 30), 95.2, sample=11)]}
    )
    assert rows == [
        {
            "measured_at": utc(2026, 10, 20, 17, 30),
            "garmin_sample_id": 11,
            "weight_kg": 95.2,
            "body_fat_pct": 28.0,
            "muscle_mass_kg": 38.0,
            "bone_mass_kg": 3.9,
            "body_water_pct": 52.0,
            "bmi": 29.1,
        }
    ]


def test_body_comp_skips_incomplete_entries():
    assert parse.parse_body_comp({"dateWeightList": [{"samplePk": 1}]}) == []
    assert parse.parse_body_comp(None) == []
    assert parse.parse_body_comp({"dateWeightList": None}) == []


def test_daily_summary_drops_sentinel_stress():
    row = parse.parse_daily_summary(date(2026, 10, 20), summary(kcal=2700))
    assert row["total_kcal"] == 2700
    assert row["resting_kcal"] == 1900
    assert row["active_kcal"] == 800
    assert row["avg_stress"] is None


def test_sleep_main_session():
    row = parse.parse_sleep(sleep(date(2026, 10, 21), utc(2026, 10, 21, 8, 15), hours=7))
    assert row["calendar_date"] == date(2026, 10, 21)
    assert row["start_at"] == utc(2026, 10, 21, 8, 15)
    assert row["duration_s"] == 7 * 3600
    assert row["sleep_score"] == 78
    assert row["avg_overnight_hrv"] == 42.0
    assert row["is_nap"] is False


def test_sleep_missing_returns_none():
    assert parse.parse_sleep(no_sleep(date(2026, 10, 21))) is None
    assert parse.parse_sleep({}) is None
    assert parse.parse_sleep(None) is None


def test_activities():
    rows = parse.parse_activities([activity(99, utc(2026, 10, 20, 22, 5)), {"activityId": None}])
    assert len(rows) == 1
    assert rows[0]["garmin_activity_id"] == 99
    assert rows[0]["start_at"] == utc(2026, 10, 20, 22, 5)
    assert rows[0]["activity_type"] == "walking"
    assert rows[0]["avg_hr"] == 101


def test_sync_timestamp_formats():
    expected = utc(2026, 10, 20, 13, 45)
    assert parse.parse_sync_timestamp("2026-10-20T13:45:00.0") == expected
    assert parse.parse_sync_timestamp("2026-10-20 13:45:00") == expected
    assert parse.parse_sync_timestamp(int(expected.timestamp() * 1000)) == expected
    assert parse.parse_sync_timestamp({"syncTimestamp": "2026-10-20T13:45:00.0"}) == expected
    assert parse.parse_sync_timestamp(None) is None
    assert parse.parse_sync_timestamp("garbage") is None
