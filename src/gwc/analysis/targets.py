"""Daily nutrient targets.

For now targets are entered manually in Settings; milestone 4 adds suggested
targets from the monthly goal and expenditure estimates, with these values
acting as overrides.
"""

from gwc.nutrition.nutrients import NUTRIENT_KEYS
from gwc.prefs import get_pref, set_pref

MANUAL_TARGETS_PREF = "targets.manual"


def manual_targets() -> dict[str, float | None]:
    stored = get_pref(MANUAL_TARGETS_PREF, {}) or {}
    return {k: stored.get(k) for k in NUTRIENT_KEYS}


def set_manual_targets(values: dict[str, float | None]) -> None:
    clean = {
        k: (float(v) if v not in (None, "") else None)
        for k, v in values.items()
        if k in NUTRIENT_KEYS
    }
    set_pref(MANUAL_TARGETS_PREF, clean)


def current_targets() -> dict[str, float | None]:
    return manual_targets()
