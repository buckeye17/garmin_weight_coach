"""Sick days and the recovery days after them, left out of projections.

Being ill breaks what the estimates assume about a day: appetite drops, you
move less, a fever raises resting burn, and weigh-ins swing with water
(dehydration, an emptier gut, glycogen) before rebounding. Appetite often runs
high for a few days afterwards. None of that says much about the weeks ahead,
so the days you mark sick, and the `RECOVERY_PREF` days after each stretch of
them, are "off" days:

- The "as eaten" intake and Garmin's burn average skip them, reaching back for
  the same number of ordinary days (snapshot.py, tdee.py).
- The trend-based burn skips their intake and weigh-ins, and fits the weight
  slope with a fresh starting level after each stretch, so the dip, the
  rebound and whatever was really lost or gained while ill don't read as
  ordinary burn (tdee.py).
- The weight trend ignores their weigh-ins and carries on along its slope
  (trend.py).
"""

from datetime import date, timedelta

from sqlalchemy.orm import Session

from gwc.nutrition import log
from gwc.prefs import get_pref

SICK = "sick"
RECOVERING = "recovering"
RECOVERY_PREF = "analysis.sick_recovery_days"
MAX_RECOVERY_DAYS = 14


def recovery_days(s: Session) -> int:
    return max(0, min(MAX_RECOVERY_DAYS, int(get_pref(RECOVERY_PREF, session=s))))


def statuses(sick: set[date], recovery: int) -> dict[date, str]:
    """`SICK` for each sick day, `RECOVERING` for the `recovery` days after each stretch."""
    out = dict.fromkeys(sick, SICK)
    for d in sick:
        for k in range(1, recovery + 1):
            out.setdefault(d + timedelta(days=k), RECOVERING)
    return out


def load_statuses(s: Session, start: date | None, end: date) -> dict[date, str]:
    """Off days from `start` (or the first one recorded) through `end`."""
    n = recovery_days(s)
    sick = log.sick_days(s, None if start is None else start - timedelta(days=n), end)
    return {
        d: v for d, v in statuses(sick, n).items() if (start is None or d >= start) and d <= end
    }


def recovery_day(s: Session, day: date) -> tuple[int, int] | None:
    """(k, n) when `day` is the k-th of n recovery days after being sick, else None."""
    n = recovery_days(s)
    if n == 0 or log.is_sick(s, day):
        return None
    sick = log.sick_days(s, day - timedelta(days=n), day - timedelta(days=1))
    if not sick:
        return None
    return (day - max(sick)).days, n
