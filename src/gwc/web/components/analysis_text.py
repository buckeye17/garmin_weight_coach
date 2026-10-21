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
    """One line: where today's calorie target came from."""
    kcal = plan.values.get("kcal")
    if kcal is None:
        return "No calorie target yet: it needs Garmin burn data or a manual value."
    if "kcal" in plan.overridden:
        return f"{kcal:,.0f} kcal, set manually in Settings."
    if plan.tdee_kcal is None or plan.deficit_kcal is None:
        return f"{kcal:,.0f} kcal."
    source = METHOD_LABELS.get(plan.tdee_method or "", "burn")
    if t.NO_GOAL in plan.flags:
        return f"{kcal:,.0f} kcal: maintenance at your {source} (no goal set)."
    sign = "−" if plan.deficit_kcal >= 0 else "+"
    return (
        f"{kcal:,.0f} kcal = {source} {plan.tdee_kcal:,.0f} {sign} "
        f"{abs(plan.deficit_kcal):,.0f}, about {fmt_rate(plan.effective_kg_per_month, unit)}."
    )


def flag_messages(plan: t.TargetPlan, unit: WeightUnit) -> list[str]:
    out = []
    flags = plan.flags
    if t.RESTING_FLOOR in flags and plan.floor_kcal is not None:
        out.append(
            f"Calories are held at your resting burn ({plan.floor_kcal:,.0f} kcal), so the "
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
    return f"{est.kcal:,.0f} ± {est.se or 0:,.0f} kcal/day. {est.note}"
