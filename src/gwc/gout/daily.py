"""Purines eaten each food day, for the Trends chart.

Bands are total mg a day and rough, like the 48-hour cut-offs in triggers.py: a
low-purine diet for gout is commonly given as up to about 400 mg a day, and a
typical Western diet runs around 600–1,000 mg, so a day above 1,000 mg is more
than even an ordinary diet supplies.
"""

from dataclasses import dataclass
from datetime import date
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from gwc.analysis.food_day import FoodDays
from gwc.gout.profiles import Exposure, ProfileBook
from gwc.nutrition import log

DAILY_LOW_MAX = 400.0  # mg a day: a low-purine diet
DAILY_HIGH = 1000.0  # mg a day: above a typical diet


@dataclass(frozen=True)
class PurineDay:
    day: date
    exposure: Exposure  # estimated entries only
    unestimated: int  # entries with no estimate yet


def band(mg: float) -> str:
    if mg > DAILY_HIGH:
        return "High"
    return "Moderate" if mg > DAILY_LOW_MAX else "Low"


def daily_purines(s: Session, tz: ZoneInfo, start: date, end: date) -> list[PurineDay]:
    """One record per food day from `start` through `end` that has anything logged."""
    days = FoodDays.load(s, tz, start, end)
    book = ProfileBook(s)
    out = []
    for day, entries in sorted(log.entries_by_day(s, start, end, days).items()):
        total, missing = Exposure(), 0
        for e in entries:
            x = book.exposure(e)
            if x is None:
                missing += 1
            else:
                total += x
        out.append(PurineDay(day, total, missing))
    return out
