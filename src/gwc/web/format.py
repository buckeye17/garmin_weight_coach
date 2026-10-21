from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


def fmt_when(dt: datetime | None, tz: ZoneInfo, now: datetime) -> str:
    """'Today 09:01', 'Yesterday 21:14', 'Tomorrow 09:01', or 'Mon Oct 19 09:01'."""
    if dt is None:
        return "—"
    local = dt.astimezone(tz)
    today = now.astimezone(tz).date()
    clock = f"{local:%H:%M}"
    delta = (local.date() - today).days
    if delta == 0:
        return f"Today {clock}"
    if delta == -1:
        return f"Yesterday {clock}"
    if delta == 1:
        return f"Tomorrow {clock}"
    return f"{local:%a %b} {local.day} {clock}"


def fmt_day(d: date) -> str:
    return f"{d:%a %b} {d.day}"


def fmt_duration(start: datetime | None, end: datetime | None) -> str:
    if start is None or end is None:
        return "—"
    seconds = int((end - start) / timedelta(seconds=1))
    return f"{seconds // 60}m {seconds % 60}s" if seconds >= 60 else f"{seconds}s"
