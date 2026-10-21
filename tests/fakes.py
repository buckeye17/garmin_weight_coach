"""Synthetic Garmin stand-ins. Shapes mirror garminconnect payloads; values are made up."""

from datetime import UTC, date, datetime, timedelta
from typing import Any

from garminconnect import GarminConnectTooManyRequestsError


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def weigh_in(at: datetime, kg: float, sample: int, fat: float = 28.0) -> dict[str, Any]:
    return {
        "samplePk": sample,
        "timestampGMT": ms(at),
        "date": ms(at),
        "calendarDate": at.date().isoformat(),
        "weight": kg * 1000,
        "bmi": 29.1,
        "bodyFat": fat,
        "bodyWater": 52.0,
        "boneMass": 3900,
        "muscleMass": 38000,
    }


def summary(kcal: float = 2600, steps: int = 8000) -> dict[str, Any]:
    return {
        "totalKilocalories": kcal,
        "activeKilocalories": kcal - 1900,
        "bmrKilocalories": 1900,
        "totalSteps": steps,
        "restingHeartRate": 58,
        "averageStressLevel": -1,
        "bodyBatteryHighestValue": 80,
        "bodyBatteryLowestValue": 15,
    }


def sleep(wake_date: date, start: datetime, hours: float = 7.0, score: int = 78) -> dict[str, Any]:
    seconds = int(hours * 3600)
    return {
        "dailySleepDTO": {
            "id": ms(start),
            "calendarDate": wake_date.isoformat(),
            "sleepTimeSeconds": seconds,
            "sleepStartTimestampGMT": ms(start),
            "sleepEndTimestampGMT": ms(start + timedelta(seconds=seconds + 1200)),
            "deepSleepSeconds": seconds // 5,
            "lightSleepSeconds": seconds // 2,
            "remSleepSeconds": seconds // 5,
            "awakeSleepSeconds": 1200,
            "sleepScores": {"overall": {"value": score}},
        },
        "avgOvernightHrv": 42.0,
    }


def no_sleep(wake_date: date) -> dict[str, Any]:
    return {"dailySleepDTO": {"calendarDate": wake_date.isoformat(), "sleepTimeSeconds": None}}


def activity(activity_id: int, start: datetime, kcal: float = 350) -> dict[str, Any]:
    return {
        "activityId": activity_id,
        "activityName": "Morning Walk",
        "activityType": {"typeKey": "walking"},
        "startTimeGMT": start.strftime("%Y-%m-%d %H:%M:%S"),
        "duration": 3600.0,
        "distance": 5000.0,
        "calories": kcal,
        "averageHR": 101.4,
    }


class FakeGarmin:
    """Implements the `GarminApi` protocol over in-memory data."""

    def __init__(self) -> None:
        self.weigh_ins: list[dict[str, Any]] = []
        self.activities: list[dict[str, Any]] = []
        self.summaries: dict[str, dict[str, Any]] = {}
        self.sleeps: dict[str, dict[str, Any]] = {}
        self.device_sync: str | None = None
        self.calls: list[tuple[str, tuple]] = []
        self.rate_limit_after: int | None = None
        self.on_call = None

    def _call(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))
        if self.on_call:
            self.on_call(name)
        if self.rate_limit_after is not None and len(self.calls) > self.rate_limit_after:
            raise GarminConnectTooManyRequestsError("Rate limit exceeded: API Error 429")

    @staticmethod
    def _in_range(iso: str, start: str, end: str) -> bool:
        return start <= iso[:10] <= end

    def get_body_composition(self, startdate: str, enddate: str | None = None) -> dict:
        self._call("get_body_composition", startdate, enddate)
        end = enddate or startdate
        return {
            "dateWeightList": [
                w for w in self.weigh_ins if self._in_range(w["calendarDate"], startdate, end)
            ]
        }

    def get_activities_by_date(self, startdate: str, enddate: str | None = None) -> list:
        self._call("get_activities_by_date", startdate, enddate)
        end = enddate or startdate
        return [a for a in self.activities if self._in_range(a["startTimeGMT"], startdate, end)]

    def get_user_summary(self, cdate: str) -> dict:
        self._call("get_user_summary", cdate)
        if cdate not in self.summaries:
            from garminconnect import GarminConnectConnectionError

            raise GarminConnectConnectionError("No data received from server")
        return self.summaries[cdate]

    def get_sleep_data(self, cdate: str) -> dict:
        self._call("get_sleep_data", cdate)
        return self.sleeps.get(cdate) or no_sleep(date.fromisoformat(cdate))

    def get_sync_timestamp(self) -> str | None:
        self._call("get_sync_timestamp")
        return self.device_sync


class FakeAuth:
    def __init__(self, client: FakeGarmin | None = None, connected: bool = True) -> None:
        self.client = client or FakeGarmin()
        self.connected = connected
        self.invalidated = 0

    def has_tokens(self) -> bool:
        return self.connected

    def get_client(self) -> FakeGarmin:
        from gwc.garmin.client import AuthRequired

        if not self.connected:
            raise AuthRequired("Garmin is not connected")
        return self.client

    def invalidate_client(self) -> None:
        self.invalidated += 1


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)
