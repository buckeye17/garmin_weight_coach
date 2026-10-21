"""Small key/value store for user preferences and app state (the `setting` table)."""

from typing import Any

from sqlalchemy.orm import Session

from gwc.db.models import Setting
from gwc.db.session import session_scope

DEFAULTS: dict[str, Any] = {
    "sync_time": "09:01",
    "food_day_fallback_hour": 9,
    # Analysis (milestone 4). Goal is stored in kg/month; None means no goal set.
    "goal.loss_kg_per_month": None,
    "targets.tdee_method": "auto",  # auto | garmin | adaptive
    "analysis.trend_alpha": 0.1,
    "analysis.kcal_per_kg_lost": 22.0,
    "analysis.max_weekly_loss_pct": 1.0,
    "analysis.adaptive_window_days": 21,
}


def get_pref(key: str, default: Any = None, session: Session | None = None) -> Any:
    if session is not None:
        row = session.get(Setting, key)
    else:
        with session_scope() as s:
            row = s.get(Setting, key)
    if row is None:
        return default if default is not None else DEFAULTS.get(key)
    return row.value


def set_pref(key: str, value: Any, session: Session | None = None) -> None:
    def _set(s: Session) -> None:
        row = s.get(Setting, key)
        if row is None:
            s.add(Setting(key=key, value=value))
        else:
            row.value = value

    if session is not None:
        _set(session)
    else:
        with session_scope() as s:
            _set(s)


def delete_pref(key: str) -> None:
    with session_scope() as s:
        row = s.get(Setting, key)
        if row is not None:
            s.delete(row)
