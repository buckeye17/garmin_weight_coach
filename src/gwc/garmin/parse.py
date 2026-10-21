"""Turn raw Garmin JSON into column dicts for the ORM models.

Field names follow garminconnect's typed models (garminconnect/typed.py) and
Garmin's weight-service payloads. Parsers are defensive: missing fields become
None, and raw payloads are stored separately so they can be re-parsed later.
"""

from datetime import UTC, date, datetime
from typing import Any


def ms_to_utc(ms: int | float | None) -> datetime | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def gmt_str_to_utc(value: str | None) -> datetime | None:
    """Parse Garmin's offset-less GMT strings ("2026-10-20 13:45:00" or ISO with 'T')."""
    if not value:
        return None
    text = value.replace("T", " ").split(".")[0].rstrip("Z")
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)


def _grams_to_kg(value: float | None) -> float | None:
    return None if value is None else value / 1000


def parse_body_comp(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    rows = []
    for item in (payload or {}).get("dateWeightList") or []:
        measured_at = ms_to_utc(item.get("timestampGMT")) or ms_to_utc(item.get("date"))
        weight_kg = _grams_to_kg(item.get("weight"))
        if measured_at is None or weight_kg is None:
            continue
        rows.append(
            {
                "measured_at": measured_at,
                "garmin_sample_id": item.get("samplePk"),
                "weight_kg": weight_kg,
                "body_fat_pct": item.get("bodyFat"),
                "muscle_mass_kg": _grams_to_kg(item.get("muscleMass")),
                "bone_mass_kg": _grams_to_kg(item.get("boneMass")),
                "body_water_pct": item.get("bodyWater"),
                "bmi": item.get("bmi"),
            }
        )
    return rows


def parse_daily_summary(cdate: date, payload: dict[str, Any] | None) -> dict[str, Any]:
    p = payload or {}
    return {
        "calendar_date": cdate,
        "total_kcal": p.get("totalKilocalories"),
        "active_kcal": p.get("activeKilocalories"),
        "resting_kcal": p.get("bmrKilocalories"),
        "steps": p.get("totalSteps"),
        "resting_hr": p.get("restingHeartRate"),
        "avg_stress": _non_negative(p.get("averageStressLevel")),
        "body_battery_high": p.get("bodyBatteryHighestValue"),
        "body_battery_low": p.get("bodyBatteryLowestValue"),
    }


def _non_negative(value: int | None) -> int | None:
    # Garmin uses -1/-2 for "not enough data".
    return value if value is not None and value >= 0 else None


def parse_sleep(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """Main overnight sleep, or None when Garmin has no sleep for that date."""
    p = payload or {}
    dto = p.get("dailySleepDTO") or {}
    start = ms_to_utc(dto.get("sleepStartTimestampGMT"))
    end = ms_to_utc(dto.get("sleepEndTimestampGMT"))
    cal = dto.get("calendarDate")
    if start is None or end is None or not cal or not dto.get("sleepTimeSeconds"):
        return None
    scores = dto.get("sleepScores") or {}
    overall = scores.get("overall") or {}
    return {
        "garmin_sleep_id": dto.get("id"),
        "calendar_date": date.fromisoformat(cal),
        "start_at": start,
        "end_at": end,
        "is_nap": False,
        "duration_s": dto.get("sleepTimeSeconds"),
        "deep_s": dto.get("deepSleepSeconds"),
        "light_s": dto.get("lightSleepSeconds"),
        "rem_s": dto.get("remSleepSeconds"),
        "awake_s": dto.get("awakeSleepSeconds"),
        "sleep_score": overall.get("value"),
        "avg_overnight_hrv": p.get("avgOvernightHrv") or dto.get("avgSleepHRV"),
    }


def parse_activities(payload: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    rows = []
    for a in payload or []:
        activity_id = a.get("activityId")
        start = gmt_str_to_utc(a.get("startTimeGMT"))
        if activity_id is None or start is None:
            continue
        avg_hr = a.get("averageHR")
        rows.append(
            {
                "garmin_activity_id": activity_id,
                "activity_type": ((a.get("activityType") or {}).get("typeKey") or "other")[:64],
                "name": (a.get("activityName") or None),
                "start_at": start,
                "duration_s": a.get("duration"),
                "distance_m": a.get("distance"),
                "kcal": a.get("calories"),
                "avg_hr": round(avg_hr) if avg_hr is not None else None,
            }
        )
    return rows


def parse_sync_timestamp(payload: Any) -> datetime | None:
    if isinstance(payload, dict):
        payload = payload.get("syncTimestamp") or payload.get("lastSyncTimestampGMT")
    if isinstance(payload, int | float):
        return ms_to_utc(payload)
    if isinstance(payload, str):
        try:
            return gmt_str_to_utc(payload)
        except ValueError:
            return None
    return None
