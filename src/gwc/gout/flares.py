"""Gout flares: when one started, where, and how bad each day of it was.

Every day of a flare has symptoms (1–5); a day that wasn't rated is assumed to be
as bad as the last day that was. A flare stays open (shown on Today, asking how
bad today is) until it's marked over, which sets its last day with symptoms.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from gwc.db.models import GoutDay, GoutFlare

# Label and what it feels like, so ratings stay comparable from one flare to the next.
SEVERITY: dict[int, tuple[str, str]] = {
    1: ("Twinge", "You notice the joint: a slight ache or warmth, no swelling. Using it is fine."),
    2: ("Mild", "Sore and a little swollen or warm, but you can walk on it and use it normally."),
    3: (
        "Moderate",
        "Red, swollen and clearly painful. You limp or avoid using it; shoes may not fit.",
    ),
    4: ("Severe", "Very painful. Hard to put weight on or use, and it may wake you at night."),
    5: ("Worst", "Can't use it at all. Even a bedsheet touching it hurts."),
}
SEVERITY_COLORS = {1: "yellow", 2: "orange", 3: "orange", 4: "red", 5: "red"}
JOINTS = [
    "Big toe",
    "Top of foot",
    "Ankle",
    "Heel",
    "Knee",
    "Wrist",
    "Finger",
    "Elbow",
]
MEDICATIONS = ["Colchicine", "Ibuprofen", "Naproxen", "Indomethacin", "Prednisone", "Ice"]
MAX_SEVERITY = 5


def _clean_list(values: list[str] | None, limit: int = 64) -> list[str]:
    out: list[str] = []
    for v in values or []:
        v = " ".join(str(v).split())[:limit]
        if v and v.casefold() not in {o.casefold() for o in out}:
            out.append(v)
    return out


def _check_severity(severity: int | None) -> int:
    if severity is None or not 1 <= int(severity) <= MAX_SEVERITY:
        raise ValueError("Pick how bad it is, 1 to 5")
    return int(severity)


def start_day(flare: GoutFlare) -> date:
    return flare.started_at.astimezone(ZoneInfo(flare.tz)).date()


def load_flare(s: Session, flare_id: int) -> GoutFlare | None:
    return s.scalars(
        select(GoutFlare).where(GoutFlare.id == flare_id).options(selectinload(GoutFlare.days))
    ).first()


def list_flares(s: Session) -> list[GoutFlare]:
    """Newest first."""
    return list(
        s.scalars(
            select(GoutFlare)
            .options(selectinload(GoutFlare.days))
            .order_by(GoutFlare.started_at.desc())
        )
    )


def active_flare(s: Session) -> GoutFlare | None:
    return s.scalars(
        select(GoutFlare)
        .where(GoutFlare.ended_on.is_(None))
        .options(selectinload(GoutFlare.days))
        .order_by(GoutFlare.started_at.desc())
    ).first()


def save_flare(
    s: Session,
    flare_id: int | None,
    *,
    started_at: datetime,
    tz: str,
    joints: list[str] | None,
    note: str | None,
    severity: int | None = None,
    medications: list[str] | None = None,
) -> GoutFlare:
    """Create a flare (with its first day's severity) or edit when and where it was."""
    if flare_id is None:
        severity = _check_severity(severity)
        flare = GoutFlare(started_at=started_at, tz=tz, joints=[])
        s.add(flare)
    else:
        flare = load_flare(s, flare_id)
        if flare is None:
            raise ValueError("Flare not found")
    flare.started_at = started_at
    flare.tz = tz
    flare.joints = _clean_list(joints)
    flare.note = (note or "").strip() or None
    first = start_day(flare)
    if flare.ended_on is not None and flare.ended_on < first:
        flare.ended_on = first
    # Days before a moved start no longer belong to this flare.
    flare.days = [d for d in flare.days if d.day >= first]
    s.flush()
    if flare_id is None:
        set_day(s, flare.id, first, severity, medications, None)
    return flare


def set_day(
    s: Session,
    flare_id: int,
    day: date,
    severity: int | None,
    medications: list[str] | None,
    note: str | None,
) -> GoutDay:
    """Record how bad one day of a flare was (1–5; a day without symptoms ends the flare)."""
    severity = _check_severity(severity)
    flare = load_flare(s, flare_id)
    if flare is None:
        raise ValueError("Flare not found")
    if day < start_day(flare):
        raise ValueError("That day is before the flare started")
    if flare.ended_on is not None and day > flare.ended_on:
        raise ValueError("That day is after the flare ended")
    row = next((d for d in flare.days if d.day == day), None)
    if row is None:
        row = GoutDay(day=day, severity=severity, medications=[])
        flare.days.append(row)
    row.severity = severity
    row.medications = _clean_list(medications)
    row.note = (note or "").strip() or None
    s.flush()
    return row


def end_flare(s: Session, flare_id: int, ended_on: date | None) -> None:
    """Mark a flare over as of `ended_on` (None reopens it)."""
    flare = load_flare(s, flare_id)
    if flare is None:
        raise ValueError("Flare not found")
    if ended_on is not None:
        ended_on = max(ended_on, start_day(flare))
        flare.days = [d for d in flare.days if d.day <= ended_on]
    flare.ended_on = ended_on


def delete_flare(s: Session, flare_id: int) -> None:
    flare = s.get(GoutFlare, flare_id)
    if flare is not None:
        s.delete(flare)


def flare_dates(flare: GoutFlare, today: date) -> list[date]:
    """Every day from the start through the end (or today, while it's still going)."""
    first = start_day(flare)
    last = flare.ended_on or max(today, first)
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def severity_on(flare: GoutFlare, day: date) -> tuple[int, bool] | None:
    """How bad `day` was, and whether that's assumed: an unrated day counts the same
    as the last rated day before it."""
    last = None
    for d in sorted(flare.days, key=lambda d: d.day):
        if d.day == day:
            return d.severity, False
        if d.day > day:
            break
        last = d
    return (last.severity, True) if last is not None else None


def default_end(flare: GoutFlare, today: date) -> date:
    """The last day with symptoms when it's marked over today: today if today was
    rated, otherwise yesterday (unrated days count as still going)."""
    rated_today = any(d.day == today for d in flare.days)
    return max(start_day(flare), today if rated_today else today - timedelta(days=1))


def peak(flare: GoutFlare) -> int | None:
    return max((d.severity for d in flare.days), default=None)


def day_number(flare: GoutFlare, day: date) -> int:
    return (day - start_day(flare)).days + 1
