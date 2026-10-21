"""Daily nutrient targets: suggested from the monthly goal, with manual overrides.

- Deficit = goal × ρ / 30.44 days; calories = TDEE − deficit.
- Guardrails: the goal is capped at `max_weekly_loss_pct` of body weight per
  week, and calories never go below Garmin's resting (BMR) kcal.
- Protein 2.4 g/kg lean mass; fat max(25 % of kcal, 0.5 g/kg); carbs the rest;
  fiber 14 g per 1,000 kcal; sugar ≤ 10 % of kcal; sodium ≤ 2,300 mg.

Anything set in Settings overrides its suggestion, and overridden calories,
protein or fat feed into the macros derived from them. A food day's targets
are frozen into `daily_target` the first time they're computed, so later
syncs don't move them; changing the goal or overrides re-computes today.
"""

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from gwc.analysis.energy import DAYS_PER_MONTH
from gwc.analysis.food_day import current_food_day
from gwc.analysis.snapshot import Snapshot, build_snapshot
from gwc.db.models import DailyTarget
from gwc.db.session import session_scope
from gwc.nutrition.nutrients import NUTRIENT_KEYS
from gwc.prefs import get_pref, set_pref

MANUAL_TARGETS_PREF = "targets.manual"
GOAL_PREF = "goal.loss_kg_per_month"
TDEE_METHOD_PREF = "targets.tdee_method"

PROTEIN_G_PER_KG_LEAN = 2.4
FAT_MIN_KCAL_SHARE = 0.25
FAT_MIN_G_PER_KG = 0.5
FIBER_G_PER_1000_KCAL = 14.0
SUGAR_MAX_KCAL_SHARE = 0.10
SODIUM_MAX_MG = 2300.0

# Flags explaining how a plan was shaped; the UI turns them into sentences.
NO_GOAL = "no_goal"
WEEKLY_CAP = "weekly_cap"
RESTING_FLOOR = "resting_floor"
NO_TDEE = "no_tdee"
NO_WEIGHT = "no_weight"
ASSUMED_BODY_FAT = "assumed_body_fat"


@dataclass
class TargetPlan:
    values: dict[str, float | None]
    suggested: dict[str, float | None]
    overridden: list[str] = field(default_factory=list)
    goal_kg_per_month: float | None = None  # as set
    effective_kg_per_month: float | None = None  # after guardrails
    deficit_kcal: float | None = None
    tdee_method: str | None = None
    tdee_kcal: float | None = None
    floor_kcal: float | None = None
    flags: list[str] = field(default_factory=list)
    frozen: bool = False

    @property
    def complete(self) -> bool:
        return all(self.values.get(k) is not None for k in NUTRIENT_KEYS)

    def details(self) -> dict:
        return {
            "suggested": self.suggested,
            "overridden": self.overridden,
            "goal_kg_per_month": self.goal_kg_per_month,
            "effective_kg_per_month": self.effective_kg_per_month,
            "deficit_kcal": self.deficit_kcal,
            "floor_kcal": self.floor_kcal,
            "flags": self.flags,
        }


# --------------------------------------------------------------------------- prefs


def manual_targets(session: Session | None = None) -> dict[str, float | None]:
    stored = get_pref(MANUAL_TARGETS_PREF, {}, session=session) or {}
    return {k: stored.get(k) for k in NUTRIENT_KEYS}


def set_manual_targets(values: dict[str, float | None]) -> None:
    clean = {
        k: (float(v) if v not in (None, "") else None)
        for k, v in values.items()
        if k in NUTRIENT_KEYS
    }
    set_pref(MANUAL_TARGETS_PREF, clean)


def goal_kg_per_month(session: Session | None = None) -> float | None:
    value = get_pref(GOAL_PREF, session=session)
    return None if value is None else float(value)


def set_goal(kg_per_month: float | None) -> None:
    set_pref(GOAL_PREF, None if kg_per_month is None else float(kg_per_month))


# --------------------------------------------------------------------------- suggestion


def _r(value: float | None, step: float = 1) -> float | None:
    return None if value is None else round(value / step) * step


def suggest(
    *,
    goal_kg_per_month: float | None,
    tdee_kcal: float | None,
    tdee_method: str | None = None,
    resting_kcal: float | None,
    weight_kg: float | None,
    lean_kg: float | None,
    rho: float | None,
    max_weekly_loss_pct: float = 1.0,
    overrides: dict[str, float | None] | None = None,
) -> TargetPlan:
    over = {k: v for k, v in (overrides or {}).items() if v is not None}
    flags: list[str] = []
    goal = goal_kg_per_month or 0.0
    if goal_kg_per_month is None:
        flags.append(NO_GOAL)
    if weight_kg is not None:
        max_month = weight_kg * max_weekly_loss_pct / 100 * DAYS_PER_MONTH / 7
        if abs(goal) > max_month:
            goal = max_month if goal > 0 else -max_month
            flags.append(WEEKLY_CAP)
    else:
        flags.append(NO_WEIGHT)

    kcal = deficit = effective = None
    if tdee_kcal is None or rho is None:
        flags.append(NO_TDEE)
    else:
        kcal = _r(tdee_kcal - goal * rho / DAYS_PER_MONTH, 10)
        if resting_kcal is not None and kcal < resting_kcal:
            kcal = math.ceil(resting_kcal / 10) * 10
            flags.append(RESTING_FLOOR)
        deficit = tdee_kcal - kcal
        effective = deficit * DAYS_PER_MONTH / rho

    # Derived macros use the effective (possibly overridden) calories, protein and fat.
    k = over.get("kcal", kcal)
    protein = _r(PROTEIN_G_PER_KG_LEAN * lean_kg) if lean_kg is not None else None
    p = over.get("protein_g", protein)
    fat = None
    if k is not None and weight_kg is not None:
        fat = _r(max(FAT_MIN_KCAL_SHARE * k / 9, FAT_MIN_G_PER_KG * weight_kg))
    f = over.get("fat_g", fat)
    carbs = None
    if k is not None and p is not None and f is not None:
        carbs = _r(max(0.0, (k - 4 * p - 9 * f) / 4))
    suggested = {
        "kcal": kcal,
        "protein_g": protein,
        "carbs_g": carbs,
        "fat_g": fat,
        "fiber_g": _r(FIBER_G_PER_1000_KCAL * k / 1000) if k is not None else None,
        "sugar_g": _r(SUGAR_MAX_KCAL_SHARE * k / 4) if k is not None else None,
        "sodium_mg": SODIUM_MAX_MG,
    }
    values = {key: over.get(key, suggested[key]) for key in NUTRIENT_KEYS}
    return TargetPlan(
        values=values,
        suggested=suggested,
        overridden=[key for key in NUTRIENT_KEYS if key in over],
        goal_kg_per_month=goal_kg_per_month,
        effective_kg_per_month=effective,
        deficit_kcal=deficit,
        tdee_method=tdee_method,
        tdee_kcal=tdee_kcal,
        floor_kcal=resting_kcal,
        flags=flags,
    )


def plan_from_snapshot(s: Session, snap: Snapshot) -> TargetPlan:
    plan = suggest(
        goal_kg_per_month=goal_kg_per_month(s),
        tdee_kcal=snap.chosen.kcal,
        tdee_method=snap.chosen.method if snap.chosen.ready else None,
        resting_kcal=snap.garmin.resting_kcal,
        weight_kg=snap.weight_kg,
        lean_kg=snap.lean_kg,
        rho=snap.rho,
        max_weekly_loss_pct=float(get_pref("analysis.max_weekly_loss_pct", session=s)),
        overrides=manual_targets(s),
    )
    if snap.body_fat_assumed:
        plan.flags.append(ASSUMED_BODY_FAT)
    return plan


# --------------------------------------------------------------------------- per day


def _from_row(row: DailyTarget) -> TargetPlan:
    d = row.details or {}
    return TargetPlan(
        values={k: getattr(row, k) for k in NUTRIENT_KEYS},
        suggested=d.get("suggested") or {},
        overridden=d.get("overridden") or [],
        goal_kg_per_month=d.get("goal_kg_per_month"),
        effective_kg_per_month=d.get("effective_kg_per_month"),
        deficit_kcal=d.get("deficit_kcal"),
        tdee_method=row.tdee_method,
        tdee_kcal=row.tdee_kcal,
        floor_kcal=d.get("floor_kcal"),
        flags=d.get("flags") or [],
        frozen=True,
    )


def day_targets(
    s: Session, tz: ZoneInfo, now: datetime, day: date, snap: Snapshot | None = None
) -> TargetPlan:
    """Targets for food day `day`: frozen ones if recorded, else computed from current data.

    Today's are recorded the first time they're computed (when every value is known).
    """
    row = s.get(DailyTarget, day)
    if row is not None:
        return _from_row(row)
    snap = snap or build_snapshot(s, tz, now)
    plan = plan_from_snapshot(s, snap)
    if day == snap.today and plan.complete:
        s.add(
            DailyTarget(
                food_day=day,
                **{k: plan.values[k] for k in NUTRIENT_KEYS},
                tdee_method=plan.tdee_method or "manual",
                tdee_kcal=plan.tdee_kcal or 0.0,
                details=plan.details(),
            )
        )
        s.flush()
        plan.frozen = True
    return plan


def refreeze_today(tz: ZoneInfo, now: datetime) -> None:
    """Forget today's recorded targets so the next view re-computes them."""
    with session_scope() as s:
        row = s.get(DailyTarget, current_food_day(s, tz, now))
        if row is not None:
            s.delete(row)


def frozen_targets(s: Session, start: date, end: date) -> dict[date, float]:
    """Recorded calorie targets by food day, for charts."""
    rows = s.execute(
        select(DailyTarget.food_day, DailyTarget.kcal).where(
            DailyTarget.food_day >= start, DailyTarget.food_day <= end
        )
    ).all()
    return dict(rows)
