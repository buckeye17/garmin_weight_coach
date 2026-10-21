"""Food days: a food day ends when you fall asleep, not at midnight.

Food day D ends at the start of the main Garmin sleep labeled with wake date
D+1. Without one (all-nighter, watch off, not synced yet) it ends at the
fallback hour (default 09:00) on D+1. Boundaries are computed on the fly from
`sleep`, so entries move to the right day once sleep data arrives.
"""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from gwc.db.models import Sleep
from gwc.prefs import get_pref

# A sleep only counts as the end of day D if it starts in this local window,
# which keeps a mislabeled or very odd sleep from swallowing a whole day.
EARLIEST_SLEEP = time(12, 0)  # on D
LATEST_SLEEP = time(18, 0)  # on D+1


class FoodDays:
    def __init__(self, tz: ZoneInfo, fallback_hour: int, sleep_starts: dict[date, datetime]):
        self.tz = tz
        self.fallback_hour = fallback_hour
        self.sleep_starts = sleep_starts  # wake date -> main sleep start (UTC)

    @classmethod
    def load(cls, s: Session, tz: ZoneInfo, start: date, end: date) -> "FoodDays":
        """Load sleeps needed for food days `start - 1` through `end + 1`."""
        rows = s.execute(
            select(Sleep.calendar_date, Sleep.start_at).where(
                Sleep.is_nap.is_(False),
                Sleep.calendar_date >= start - timedelta(days=1),
                Sleep.calendar_date <= end + timedelta(days=2),
            )
        ).all()
        fallback = int(get_pref("food_day_fallback_hour", session=s))
        return cls(tz, fallback, {d: start_at for d, start_at in rows})

    def _local(self, d: date, t: time) -> datetime:
        return datetime.combine(d, t, tzinfo=self.tz).astimezone(UTC)

    def end_of(self, d: date) -> tuple[datetime, bool]:
        """When food day `d` ends (UTC), and whether that came from Garmin sleep."""
        nxt = d + timedelta(days=1)
        start = self.sleep_starts.get(nxt)
        if start is not None and self._local(d, EARLIEST_SLEEP) <= start <= self._local(
            nxt, LATEST_SLEEP
        ):
            return start, True
        return self._local(nxt, time(self.fallback_hour)), False

    def start_of(self, d: date) -> datetime:
        return self.end_of(d - timedelta(days=1))[0]

    def bounds(self, d: date) -> tuple[datetime, datetime]:
        return self.start_of(d), self.end_of(d)[0]

    def day_of(self, t: datetime) -> date:
        local = t.astimezone(self.tz).date()
        if t < self.end_of(local - timedelta(days=1))[0]:
            return local - timedelta(days=1)
        if t >= self.end_of(local)[0]:
            return local + timedelta(days=1)
        return local


def food_days_around(s: Session, tz: ZoneInfo, d: date, days: int = 2) -> FoodDays:
    return FoodDays.load(s, tz, d - timedelta(days=days), d + timedelta(days=days))


def current_food_day(s: Session, tz: ZoneInfo, now: datetime) -> date:
    local = now.astimezone(tz).date()
    return food_days_around(s, tz, local).day_of(now)
