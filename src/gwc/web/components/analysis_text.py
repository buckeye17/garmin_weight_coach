"""Plain-English explanations of target plans and burn estimates."""

from gwc.analysis import targets as t
from gwc.analysis.tdee import TdeeEstimate
from gwc.analysis.units import WeightUnit, fmt_weight

METHOD_LABELS = {"garmin": "Garmin burn", "adaptive": "trend-based burn"}


def fmt_rate(kg_per_month: float | None, unit: WeightUnit) -> str:
    if kg_per_month is None:
        return "—"
    return f"{fmt_weight(kg_per_month, unit)}/month"


def fmt_kcal(value: float | None) -> str:
    return "—" if value is None else f"{value:,.0f} kcal"


def plan_summary(plan: t.TargetPlan, unit: WeightUnit) -> str:
    """Where a day's calorie target came from, including its workouts and steps."""
    parts = [_base_summary(plan, unit), workout_summary(plan), step_summary(plan)]
    if plan.values.get("kcal") is not None and (plan.workout_kcal or plan.step_credit_kcal):
        parts.append(f"Total {plan.values['kcal']:,.0f} kcal.")
    return " ".join(p for p in parts if p)


def _base_summary(plan: t.TargetPlan, unit: WeightUnit) -> str:
    kcal = (plan.base_values or plan.values).get("kcal")
    if kcal is None:
        return "No calorie target yet: it needs Garmin burn data or a manual value."
    if "kcal" in plan.overridden:
        return f"{kcal:,.0f} kcal, set manually in Settings."
    if plan.tdee_kcal is None or plan.deficit_kcal is None:
        return f"{kcal:,.0f} kcal."
    source = METHOD_LABELS.get(plan.tdee_method or "", "burn")
    left_out = []
    if plan.eat_back_pct is not None and plan.workout_avg_kcal:
        left_out.append("workouts")
    if plan.step_pct is not None and plan.step_avg_kcal:
        left_out.append("extra steps")
    if left_out:
        source += " without " + " or ".join(left_out)
    if t.NO_GOAL in plan.flags:
        return f"{kcal:,.0f} kcal: maintenance at your {source} (no goal set)."
    sign = "−" if plan.deficit_kcal >= 0 else "+"
    return (
        f"{kcal:,.0f} kcal = {source} {plan.tdee_kcal:,.0f} {sign} "
        f"{abs(plan.deficit_kcal):,.0f}, about {fmt_rate(plan.effective_kg_per_month, unit)}."
    )


def workout_summary(plan: t.TargetPlan) -> str:
    """E.g. "+380 kcal for Run (60 min): 75% of 504 kcal above resting." """
    if not plan.workout_kcal or plan.values.get("kcal") is None:
        return ""
    ws = plan.workouts
    if len(ws) == 1:
        mins = f" ({ws[0].duration_s / 60:,.0f} min)" if ws[0].duration_s else ""
        what = f"{ws[0].name}{mins}"
    else:
        what = f"{len(ws)} workouts"
    net = sum(w.net_kcal for w in ws)
    return (
        f"+{plan.workout_kcal:,.0f} kcal for {what}: {plan.eat_back_pct:,.0f}% of "
        f"{net:,.0f} kcal above resting."
    )


def step_summary(plan: t.TargetPlan) -> str:
    """E.g. "+40 kcal for extra steps: 50% of 85 kcal above your usual 335." """
    if not plan.step_credit_kcal or plan.values.get("kcal") is None:
        return ""
    above = (plan.step_kcal_today or 0.0) - (plan.typical_step_kcal or 0.0)
    return (
        f"+{plan.step_credit_kcal:,.0f} kcal for extra steps: {plan.step_pct:,.0f}% of "
        f"{above:,.0f} kcal above your usual {plan.typical_step_kcal:,.0f} active kcal "
        "outside workouts."
    )


def flag_messages(plan: t.TargetPlan, unit: WeightUnit) -> list[str]:
    out = []
    flags = plan.flags
    if t.RESTING_FLOOR in flags and plan.floor_kcal is not None:
        out.append(
            f"Calories are held at the floor of {plan.floor_kcal:,.0f} kcal (75% of your "
            f"resting burn, and never under {t.MIN_KCAL:,.0f}), so the "
            f"goal of {fmt_rate(plan.goal_kg_per_month, unit)} is slowed to "
            f"{fmt_rate(plan.effective_kg_per_month, unit)}."
        )
    if t.WEEKLY_CAP in flags:
        out.append("The goal is capped at the maximum weekly loss set in Settings.")
    if t.ASSUMED_BODY_FAT in flags:
        out.append("Your scale hasn't reported body fat, so 30% is assumed for protein and ρ.")
    if t.NO_WEIGHT in flags:
        out.append("No weigh-ins yet: protein and fat targets need your weight.")
    return out


def tdee_line(est: TdeeEstimate) -> str:
    if not est.ready or est.kcal is None:
        return est.note
    line = f"{est.kcal:,.0f} ± {est.se or 0:,.0f} kcal/day. {est.note}"
    extras = []
    if est.exercise_kcal:
        extras.append(f"workouts {est.exercise_kcal:,.0f}")
    if est.step_kcal:
        extras.append(f"steps above your usual {est.step_kcal:,.0f}")
    if extras:
        line += (
            f" Of that, {' and '.join(extras)} kcal/day; targets start from "
            f"{est.base_kcal:,.0f} and add each day's own."
        )
    return line
