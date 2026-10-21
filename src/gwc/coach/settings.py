"""Coach preferences: backend, and model / effort per feature (stored in the `setting` table)."""

from dataclasses import dataclass
from typing import Literal

from gwc.config import get_settings
from gwc.prefs import get_pref, set_pref

Feature = Literal["chat", "parse", "summary"]
FEATURES: dict[str, str] = {
    "chat": "Coach chat",
    "parse": "Food logging",
    "summary": "Weekly summary",
}
EFFORTS = ["low", "medium", "high", "xhigh", "max"]

# Aliases the subscription backend understands; the API backend needs full IDs.
MODEL_ALIASES: dict[str, str] = {
    "opus": "claude-opus-5-5",
    "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-5-5",
    "fable": "claude-fable-5-1",
}
MODEL_LABELS: dict[str, str] = {
    "opus": "Opus (Opus 5.5)",
    "sonnet": "Sonnet (Sonnet 5.5)",
    "haiku": "Haiku (Haiku 5.5)",
    "fable": "Fable (Fable 5.1, may use usage credits)",
}
DEFAULT_EFFORT: dict[str, str] = {"chat": "medium", "parse": "medium", "summary": "high"}

BACKEND_PREF = "coach.backend"
FALLBACK_PREF = "coach.fallback_model"
SUMMARY_ENABLED_PREF = "coach.summary_enabled"
SUMMARY_WEEKDAY_PREF = "coach.summary_weekday"  # 0 = Monday
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _model_pref(feature: str) -> str:
    return f"coach.model.{feature}"


def _effort_pref(feature: str) -> str:
    return f"coach.effort.{feature}"


@dataclass(frozen=True)
class FeatureConfig:
    feature: str
    model: str  # alias or full ID
    effort: str
    fallback_model: str | None


def backend_name() -> str:
    return get_pref(BACKEND_PREF) or get_settings().llm_backend


def feature_config(feature: str) -> FeatureConfig:
    return FeatureConfig(
        feature=feature,
        model=(get_pref(_model_pref(feature)) or "opus").strip(),
        effort=get_pref(_effort_pref(feature)) or DEFAULT_EFFORT[feature],
        fallback_model=get_pref(FALLBACK_PREF) or None,
    )


def save_coach_settings(
    *,
    backend: str,
    models: dict[str, str],
    efforts: dict[str, str],
    fallback_model: str | None,
    summary_enabled: bool,
    summary_weekday: int,
) -> None:
    set_pref(BACKEND_PREF, backend if backend in ("subscription", "api") else "subscription")
    for feature in FEATURES:
        model = (models.get(feature) or "opus").strip()
        set_pref(_model_pref(feature), model)
        effort = efforts.get(feature)
        set_pref(_effort_pref(feature), effort if effort in EFFORTS else DEFAULT_EFFORT[feature])
    set_pref(FALLBACK_PREF, (fallback_model or "").strip() or None)
    set_pref(SUMMARY_ENABLED_PREF, bool(summary_enabled))
    set_pref(SUMMARY_WEEKDAY_PREF, max(0, min(6, int(summary_weekday))))


def summary_enabled() -> bool:
    value = get_pref(SUMMARY_ENABLED_PREF)
    return True if value is None else bool(value)


def summary_weekday() -> int:
    value = get_pref(SUMMARY_WEEKDAY_PREF)
    return 0 if value is None else int(value)


def full_model_id(model: str) -> str:
    return MODEL_ALIASES.get(model, model)
