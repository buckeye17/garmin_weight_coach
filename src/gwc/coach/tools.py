"""Read-only data tools exposed to Claude.

Weights come back in the user's display unit (with the unit named), energy in
kcal, nutrients in g / mg. Dates are food days (a food day ends when the user
falls asleep, not at midnight). Nothing here writes to the database.
"""

from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select

from gwc.analysis.days import load_days
from gwc.analysis.food_day import FoodDays, current_food_day
from gwc.analysis.projection import scenarios
from gwc.analysis.snapshot import build_snapshot
from gwc.analysis.targets import day_targets, goal_kg_per_month
from gwc.analysis.units import kg_to, weight_unit
from gwc.coach.llm import ToolSpec
from gwc.config import get_settings
from gwc.db.models import Activity, BodyComp, ManualActivity, Sleep
from gwc.db.session import session_scope
from gwc.db.types import utcnow
from gwc.nutrition import foods, log
from gwc.nutrition.nutrients import NUTRIENT_KEYS
from gwc.nutrition.service import get_usda
from gwc.nutrition.usda import UsdaError, UsdaHit

MAX_DAYS = 120


def _r(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(value, digits)


def _w(value_kg: float | None) -> float | None:
    return None if value_kg is None else round(kg_to(value_kg, weight_unit()), 1)


def _days(args: dict[str, Any], default: int = 14) -> int:
    return max(1, min(MAX_DAYS, int(args.get("days") or default)))


def _date(value: str | None, fallback: date) -> date:
    return date.fromisoformat(value[:10]) if value else fallback


def _context() -> tuple[Any, datetime]:
    return get_settings().tz, utcnow()


DAYS_SCHEMA = {
    "type": "object",
    "properties": {
        "days": {
            "type": "integer",
            "description": f"How many days back from today (1–{MAX_DAYS}, default 14).",
        }
    },
    "required": [],
}


# --------------------------------------------------------------------------- handlers


def get_overview(_args: dict[str, Any]) -> dict[str, Any]:
    tz, now = _context()
    unit = weight_unit()
    with session_scope() as s:
        snap = build_snapshot(s, tz, now)
        plan = day_targets(s, tz, now, snap.today, snap=snap)
        goal = goal_kg_per_month(s)
    est = {
        e.method: {
            "kcal_per_day": _r(e.kcal, 0),
            "std_error": _r(e.se, 0),
            "ready": e.ready,
            "note": e.note,
            "workouts_kcal_per_day": _r(e.exercise_kcal, 0),
            "usual_step_kcal_per_day": _r(e.typical_step_kcal, 0),
            "steps_above_usual_kcal_per_day": _r(e.step_kcal, 0),
            "without_workouts_or_extra_steps_kcal_per_day": _r(e.base_kcal, 0),
        }
        for e in (snap.garmin, snap.adaptive)
    }
    return {
        "today_food_day": snap.today.isoformat(),
        "local_time": now.astimezone(tz).strftime("%Y-%m-%d %H:%M"),
        "weight_unit": unit,
        "trend_weight": _w(snap.weight_kg),
        "body_fat_pct": _r(snap.current.fat_trend_pct) if snap.current else None,
        "body_fat_assumed": snap.body_fat_assumed,
        "goal_loss_per_month": _w(goal),
        "targets_today": {k: _r(v, 0) for k, v in plan.values.items()},
        "target_flags": plan.flags,
        "burn_estimates": est,
        "burn_used_for_targets": snap.chosen.method,
        "resting_kcal": _r(snap.garmin.resting_kcal, 0),
        "kcal_per_kg_change": _r(snap.rho, 0),
        "last_7_food_days_complete": len(snap.intakes7),
        "mean_intake_last_7_complete": (
            _r(sum(snap.intakes7) / len(snap.intakes7), 0) if snap.intakes7 else None
        ),
    }


def get_weight_trend(args: dict[str, Any]) -> dict[str, Any]:
    tz, now = _context()
    with session_scope() as s:
        snap = build_snapshot(s, tz, now)
    since = snap.today - timedelta(days=_days(args, 30))
    return {
        "weight_unit": weight_unit(),
        "days": [
            {
                "date": p.day.isoformat(),
                "weigh_in": _w(p.weight_kg),
                "trend": _w(p.trend_kg),
                "body_fat_pct": _r(p.body_fat_pct),
                "body_fat_trend_pct": _r(p.fat_trend_pct),
            }
            for p in snap.trend
            if p.day > since
        ],
    }


def get_body_comp(args: dict[str, Any]) -> dict[str, Any]:
    tz, now = _context()
    since = now - timedelta(days=_days(args, 30))
    with session_scope() as s:
        rows = s.scalars(
            select(BodyComp).where(BodyComp.measured_at >= since).order_by(BodyComp.measured_at)
        ).all()
        return {
            "weight_unit": weight_unit(),
            "weigh_ins": [
                {
                    "at": r.measured_at.astimezone(tz).strftime("%Y-%m-%d %H:%M"),
                    "weight": _w(r.weight_kg),
                    "body_fat_pct": _r(r.body_fat_pct),
                    "muscle_mass": _w(r.muscle_mass_kg),
                    "bone_mass": _w(r.bone_mass_kg),
                    "body_water_pct": _r(r.body_water_pct),
                    "bmi": _r(r.bmi),
                }
                for r in rows
            ],
        }


def get_daily_nutrition(args: dict[str, Any]) -> dict[str, Any]:
    tz, now = _context()
    with session_scope() as s:
        today = current_food_day(s, tz, now)
        end = min(_date(args.get("end_date"), today), today)
        start = _date(args.get("start_date"), end - timedelta(days=6))
        start = max(start, end - timedelta(days=MAX_DAYS))
        records = load_days(s, tz, start, end)
        targets = {d.day: day_targets(s, tz, now, d.day).values for d in records if d.day == today}
    out = []
    for d in records:
        row: dict[str, Any] = {
            "food_day": d.day.isoformat(),
            "status": (
                "in progress"
                if d.day == today
                else "complete"
                if d.complete
                else "partial"
                if d.entries
                else "not logged"
            ),
            "entries": d.entries,
            "intake": {k: _r(v, 0) for k, v in d.intake.items()} if d.intake else None,
            "garmin_total_burn": _r(d.garmin_total, 0),
            "garmin_burn_provisional": d.garmin_provisional,
        }
        if d.day in targets:
            row["targets"] = {k: _r(v, 0) for k, v in targets[d.day].items()}
        out.append(row)
    return {"days": out}


def get_food_entries(args: dict[str, Any]) -> dict[str, Any]:
    tz, now = _context()
    with session_scope() as s:
        today = current_food_day(s, tz, now)
        day = _date(args.get("food_day"), today)
        fd = FoodDays.load(s, tz, day - timedelta(days=1), day + timedelta(days=1))
        entries = log.entries_for_day(s, day, fd)
        start, (end, from_sleep) = fd.start_of(day), fd.end_of(day)
        return {
            "food_day": day.isoformat(),
            "starts": start.astimezone(tz).strftime("%Y-%m-%d %H:%M"),
            "ends": end.astimezone(tz).strftime("%Y-%m-%d %H:%M"),
            "ends_at_sleep": from_sleep,
            "marked_incomplete": log.is_incomplete(s, day),
            "entries": [
                {
                    "time": e.eaten_at.astimezone(tz).strftime("%Y-%m-%d %H:%M"),
                    "food": e.description,
                    "amount": e.unit_label or f"{e.grams:.0f} g",
                    **{k: _r(getattr(e, k), 0) for k in NUTRIENT_KEYS},
                }
                for e in entries
            ],
            "totals": {k: _r(v, 0) for k, v in log.totals(entries).items()},
        }


def get_targets(args: dict[str, Any]) -> dict[str, Any]:
    tz, now = _context()
    with session_scope() as s:
        today = current_food_day(s, tz, now)
        day = _date(args.get("food_day"), today)
        plan = day_targets(s, tz, now, day)
    return {
        "food_day": day.isoformat(),
        "targets": {k: _r(v, 0) for k, v in plan.values.items()},
        "suggested": {k: _r(v, 0) for k, v in plan.suggested.items()},
        "overridden": plan.overridden,
        "burn_method": plan.tdee_method,
        "burn_kcal": _r(plan.tdee_kcal, 0),
        "burn_excludes_workouts": plan.eat_back_pct is not None,
        "burn_excludes_extra_steps": plan.step_pct is not None,
        "targets_before_workouts_and_steps": {k: _r(v, 0) for k, v in plan.base_values.items()},
        "workout_kcal_added": _r(plan.workout_kcal, 0),
        "workout_eat_back_pct": plan.eat_back_pct,
        "workouts": [w.as_dict() for w in plan.workouts],
        "step_kcal_added": _r(plan.step_credit_kcal, 0),
        "steps_eat_back_pct": plan.step_pct,
        "active_kcal_outside_workouts": _r(plan.step_kcal_today, 0),
        "usual_active_kcal_outside_workouts": _r(plan.typical_step_kcal, 0),
        "deficit_kcal": _r(plan.deficit_kcal, 0),
        "goal_loss_per_month": _w(plan.goal_kg_per_month),
        "achievable_loss_per_month": _w(plan.effective_kg_per_month),
        "weight_unit": weight_unit(),
        "flags": plan.flags,
        "recorded_for_day": plan.frozen,
    }


def get_garmin_daily(args: dict[str, Any]) -> dict[str, Any]:
    tz, now = _context()
    with session_scope() as s:
        today = current_food_day(s, tz, now)
        records = load_days(s, tz, today - timedelta(days=_days(args) - 1), today)
    return {
        "days": [
            {
                "date": d.day.isoformat(),
                "total_kcal": _r(d.garmin_total, 0),
                "resting_kcal": _r(d.garmin_resting, 0),
                "active_kcal": _r(d.garmin_active, 0),
                "steps": d.steps,
                "provisional": d.garmin_provisional,
            }
            for d in records
            if d.garmin_total is not None or d.steps is not None
        ]
    }


def get_sleep(args: dict[str, Any]) -> dict[str, Any]:
    tz, now = _context()
    since = now.astimezone(tz).date() - timedelta(days=_days(args))
    with session_scope() as s:
        rows = s.scalars(
            select(Sleep).where(Sleep.calendar_date > since).order_by(Sleep.start_at)
        ).all()
        return {
            "sleeps": [
                {
                    "wake_date": r.calendar_date.isoformat(),
                    "nap": r.is_nap,
                    "asleep": r.start_at.astimezone(tz).strftime("%Y-%m-%d %H:%M"),
                    "awake": r.end_at.astimezone(tz).strftime("%Y-%m-%d %H:%M"),
                    "hours": _r((r.duration_s or 0) / 3600),
                    "deep_h": _r((r.deep_s or 0) / 3600),
                    "rem_h": _r((r.rem_s or 0) / 3600),
                    "score": r.sleep_score,
                    "hrv": _r(r.avg_overnight_hrv),
                }
                for r in rows
            ]
        }


def get_activities(args: dict[str, Any]) -> dict[str, Any]:
    tz, now = _context()
    since = now - timedelta(days=_days(args, 30))
    with session_scope() as s:
        rows = s.scalars(
            select(Activity).where(Activity.start_at >= since).order_by(Activity.start_at)
        ).all()
        manual = s.scalars(
            select(ManualActivity)
            .where(ManualActivity.food_day >= since.astimezone(tz).date())
            .order_by(ManualActivity.food_day, ManualActivity.id)
        ).all()
        return {
            "activities": [
                {
                    "start": a.start_at.astimezone(tz).strftime("%Y-%m-%d %H:%M"),
                    "type": a.activity_type,
                    "name": a.name,
                    "minutes": _r((a.duration_s or 0) / 60, 0),
                    "distance_km": _r((a.distance_m or 0) / 1000, 2),
                    "kcal": _r(a.kcal, 0),
                    "avg_hr": a.avg_hr,
                }
                for a in rows
            ],
            "entered_in_app": [
                {
                    "food_day": m.food_day.isoformat(),
                    "name": m.name,
                    "kcal_above_resting": _r(m.kcal, 0),
                }
                for m in manual
            ],
        }


def get_projection(_args: dict[str, Any]) -> dict[str, Any]:
    tz, now = _context()
    with session_scope() as s:
        snap = build_snapshot(s, tz, now)
        plan = day_targets(s, tz, now, snap.today, snap=snap)
    projections = scenarios(snap, plan.typical_kcal)
    return {
        "weight_unit": weight_unit(),
        "start_trend_weight": _w(snap.weight_kg),
        "horizon_days": 28,
        "scenarios": [
            {
                "name": p.label,
                "intake_kcal": _r(p.intake_kcal, 0),
                "burn_kcal": _r(p.tdee_kcal, 0),
                "weight_in_4_weeks": _w(p.expected[-1]),
                "range_10_to_90": [_w(p.p10[-1]), _w(p.p90[-1])],
            }
            for p in projections
        ],
        "note": "'As eaten' scenarios need at least 3 complete food days in the last 7.",
    }


# --------------------------------------------------------------------------- food lookup


class FoodLookup:
    """search_my_foods / search_usda for food parsing; remembers USDA hits it returned."""

    def __init__(self) -> None:
        self.usda_hits: dict[int, UsdaHit] = {}

    def search_my_foods(self, args: dict[str, Any]) -> dict[str, Any]:
        with session_scope() as s:
            found = foods.search_foods(s, args.get("query") or "", limit=10)
            return {
                "foods": [
                    {
                        "food_id": f.id,
                        "name": f.name,
                        "brand": f.brand,
                        "kind": f.source.value,
                        "source_url": f.source_url,
                        "favorite": f.is_favorite,
                        "per_100g": {k: _r(getattr(f, k)) for k in NUTRIENT_KEYS},
                        "servings": [{"label": sv.label, "grams": sv.grams} for sv in f.servings],
                    }
                    for f in found
                ]
            }

    def search_usda(self, args: dict[str, Any]) -> dict[str, Any]:
        try:
            hits = get_usda().search(
                args.get("query") or "", branded=bool(args.get("branded")), page_size=8
            )
        except UsdaError as e:
            return {"error": str(e)}
        out = []
        for h in hits:
            self.usda_hits[h.fdc_id] = h
            out.append(
                {
                    "fdc_id": h.fdc_id,
                    "name": h.name,
                    "brand": h.brand,
                    "data_type": h.data_type,
                    "per_100g": {k: _r(v) for k, v in h.per100.items()},
                    "servings": [{"label": sv.label, "grams": sv.grams} for sv in h.servings],
                }
            )
        return {"results": out}

    def specs(self) -> list[ToolSpec]:
        query_schema = {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "A few words, e.g. 'egg'"}},
            "required": ["query"],
        }
        return [
            ToolSpec(
                "search_my_foods",
                "Search the user's saved foods, recipes, previously used USDA foods and "
                "restaurant items saved from earlier lookups (brand = the chain). Check this "
                "first: these are what the user actually eats. Returns nutrients per 100 g "
                "and named servings with grams.",
                query_schema,
                self.search_my_foods,
                lambda a: f"Checking your foods for “{a.get('query', '')}”…",
            ),
            ToolSpec(
                "search_usda",
                "Search USDA FoodData Central. Set branded=true for packaged products. Returns "
                "nutrients per 100 g and portion sizes in grams when known.",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "branded": {"type": "boolean", "description": "Search branded foods"},
                    },
                    "required": ["query"],
                },
                self.search_usda,
                lambda a: f"Searching USDA for “{a.get('query', '')}”…",
            ),
        ]


# --------------------------------------------------------------------------- specs


def data_tools() -> list[ToolSpec]:
    day_schema = {
        "type": "object",
        "properties": {"food_day": {"type": "string", "description": "YYYY-MM-DD; default today"}},
        "required": [],
    }
    return [
        ToolSpec(
            "get_overview",
            "Start here. Today's food day and local time, display units, trend weight, goal, "
            "today's targets, burn (TDEE) estimates, and how many of the last 7 days are "
            "fully logged.",
            {"type": "object", "properties": {}, "required": []},
            get_overview,
            lambda _a: "Looking at your overview…",
        ),
        ToolSpec(
            "get_weight_trend",
            "Daily weigh-ins with the smoothed trend weight and body fat %.",
            DAYS_SCHEMA,
            get_weight_trend,
            lambda _a: "Reading your weight trend…",
        ),
        ToolSpec(
            "get_body_comp",
            "Every scale reading: weight, body fat, muscle, bone, water, BMI.",
            DAYS_SCHEMA,
            get_body_comp,
            lambda _a: "Reading body composition…",
        ),
        ToolSpec(
            "get_daily_nutrition",
            "Per-food-day intake totals (kcal, protein, carbs, fat, fiber, sugar, sodium), "
            "logging status (complete / partial / not logged) and Garmin's burn. Defaults to "
            "the last 7 days.",
            {
                "type": "object",
                "properties": {
                    "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "end_date": {"type": "string", "description": "YYYY-MM-DD; default today"},
                },
                "required": [],
            },
            get_daily_nutrition,
            lambda _a: "Reading your food log…",
        ),
        ToolSpec(
            "get_food_entries",
            "Every food logged on one food day, with times and nutrients.",
            day_schema,
            get_food_entries,
            lambda a: f"Reading entries for {a.get('food_day') or 'today'}…",
        ),
        ToolSpec(
            "get_targets",
            "Nutrient targets for a food day: final values, the suggestions, which are manual "
            "overrides, the burn and deficit behind calories, guardrail flags, and the "
            "calories added for that day's Garmin workouts and above-usual steps.",
            day_schema,
            get_targets,
            lambda _a: "Checking your targets…",
        ),
        ToolSpec(
            "get_garmin_daily",
            "Garmin's daily totals: total, resting and active kcal, and steps.",
            DAYS_SCHEMA,
            get_garmin_daily,
            lambda _a: "Reading Garmin daily totals…",
        ),
        ToolSpec(
            "get_sleep",
            "Sleeps with start/end, duration, stages, score and HRV.",
            DAYS_SCHEMA,
            get_sleep,
            lambda _a: "Reading your sleep…",
        ),
        ToolSpec(
            "get_activities",
            "Recorded Garmin activities with type, duration, distance, kcal and heart rate, "
            "plus workouts the user entered in the app with their guess at kcal above resting.",
            DAYS_SCHEMA,
            get_activities,
            lambda _a: "Reading your activities…",
        ),
        ToolSpec(
            "get_projection",
            "The app's 4-week weight projection: 'at your target' and 'as eaten' scenarios "
            "with 10–90% ranges.",
            {"type": "object", "properties": {}, "required": []},
            get_projection,
            lambda _a: "Running the projection…",
        ),
    ]
