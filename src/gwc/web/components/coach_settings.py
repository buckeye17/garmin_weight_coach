"""Settings → Coach: backend, model and effort per feature, fallback, weekly summary."""

import time

import dash_mantine_components as dmc
from dash import ALL, Input, Output, State, callback, dcc
from dash_iconify import DashIconify

from gwc.coach.llm import LLMError, LLMRequest, get_backend
from gwc.coach.settings import (
    EFFORTS,
    FEATURES,
    MODEL_ALIASES,
    MODEL_LABELS,
    WEEKDAYS,
    backend_name,
    feature_config,
    save_coach_settings,
    summary_enabled,
    summary_weekday,
)
from gwc.config import get_settings

CUSTOM = "custom"
MODEL_DATA = [{"value": k, "label": v} for k, v in MODEL_LABELS.items()] + [
    {"value": CUSTOM, "label": "Custom model ID…"}
]
FALLBACK_DATA = [{"value": "", "label": "None"}] + [
    {"value": k, "label": v.split(" (")[0]} for k, v in MODEL_LABELS.items()
]


def _feature_row(feature: str, label: str) -> dmc.Stack:
    return dmc.Stack(
        [
            dmc.Text(label, size="sm", fw=600),
            dmc.Group(
                [
                    dmc.Select(
                        id={"type": "coach-model", "feature": feature},
                        data=MODEL_DATA,
                        allowDeselect=False,
                        flex=2,
                        miw=0,
                    ),
                    dmc.Select(
                        id={"type": "coach-effort", "feature": feature},
                        data=[{"value": e, "label": f"Effort: {e}"} for e in EFFORTS],
                        allowDeselect=False,
                        flex=1,
                        miw=120,
                    ),
                ],
                gap="xs",
                wrap="nowrap",
            ),
            dmc.TextInput(
                id={"type": "coach-custom", "feature": feature},
                placeholder="e.g. claude-opus-5-5",
                display="none",
            ),
        ],
        gap=4,
    )


def coach_card() -> dmc.Card:
    has_key = get_settings().anthropic_api_key is not None
    return dmc.Card(
        dmc.Stack(
            [
                dmc.Group(
                    [
                        DashIconify(icon="tabler:message-chatbot", width=22),
                        dmc.Title("Coach (Claude)", order=4),
                    ],
                    gap="xs",
                ),
                dmc.SegmentedControl(
                    id="coach-backend",
                    data=[
                        {"value": "subscription", "label": "Claude subscription"},
                        {"value": "api", "label": "API key", "disabled": not has_key},
                    ],
                    fullWidth=True,
                ),
                dmc.Text(
                    "The subscription uses this PC's Claude Code login (your Max plan) and "
                    "shares its usage limits. The API key option bills ANTHROPIC_API_KEY from "
                    ".env" + ("." if has_key else " — add one there to enable it."),
                    size="xs",
                    c="dimmed",
                ),
                *[_feature_row(f, label) for f, label in FEATURES.items()],
                dmc.Text(
                    "Aliases follow Anthropic's latest version; a full model ID pins one. "
                    "Lower effort answers faster and uses less of your limits.",
                    size="xs",
                    c="dimmed",
                ),
                dmc.Select(
                    id="coach-fallback",
                    label="Fallback model",
                    description="Used if the chosen model is unavailable (subscription only)",
                    data=FALLBACK_DATA,
                    allowDeselect=False,
                ),
                dmc.Group(
                    [
                        dmc.Switch(id="coach-sum-enabled", label="Weekly summary"),
                        dmc.Select(
                            id="coach-sum-day",
                            data=[{"value": str(i), "label": d} for i, d in enumerate(WEEKDAYS)],
                            allowDeselect=False,
                            w=150,
                        ),
                    ],
                    justify="space-between",
                ),
                dmc.Text(
                    "Questions, food descriptions and your health data are sent to Anthropic "
                    "when you use the coach.",
                    size="xs",
                    c="dimmed",
                ),
                dmc.Group(
                    [
                        dmc.Button("Test", id="coach-test", variant="default", size="xs"),
                        dmc.Button("Save coach settings", id="coach-save", size="xs"),
                    ],
                    justify="space-between",
                ),
                dcc.Loading(dmc.Box(id="coach-feedback"), type="dot"),
            ],
            gap="sm",
        ),
        withBorder=True,
    )


@callback(
    Output("coach-backend", "value"),
    Output({"type": "coach-model", "feature": ALL}, "value"),
    Output({"type": "coach-custom", "feature": ALL}, "value"),
    Output({"type": "coach-effort", "feature": ALL}, "value"),
    Output("coach-fallback", "value"),
    Output("coach-sum-enabled", "checked"),
    Output("coach-sum-day", "value"),
    Input("prefs-load", "data"),
)
def load_coach(_):
    configs = [feature_config(f) for f in FEATURES]
    known = set(MODEL_ALIASES)
    fallback = configs[0].fallback_model or ""
    return (
        backend_name(),
        [c.model if c.model in known else CUSTOM for c in configs],
        ["" if c.model in known else c.model for c in configs],
        [c.effort for c in configs],
        fallback if fallback in known else "",
        summary_enabled(),
        str(summary_weekday()),
    )


@callback(
    Output({"type": "coach-custom", "feature": ALL}, "display"),
    Input({"type": "coach-model", "feature": ALL}, "value"),
)
def show_custom(models):
    return ["block" if m == CUSTOM else "none" for m in models]


@callback(
    Output("coach-feedback", "children"),
    Input("coach-save", "n_clicks"),
    State("coach-backend", "value"),
    State({"type": "coach-model", "feature": ALL}, "value"),
    State({"type": "coach-custom", "feature": ALL}, "value"),
    State({"type": "coach-effort", "feature": ALL}, "value"),
    State("coach-fallback", "value"),
    State("coach-sum-enabled", "checked"),
    State("coach-sum-day", "value"),
    prevent_initial_call=True,
)
def save_coach(_clicks, backend, models, customs, efforts, fallback, enabled, day):
    chosen = {}
    for feature, model, custom in zip(FEATURES, models, customs, strict=True):
        if model == CUSTOM:
            if not (custom or "").strip():
                return dmc.Text(f"Enter a model ID for {FEATURES[feature]}.", size="sm", c="red")
            model = custom.strip()
        chosen[feature] = model
    save_coach_settings(
        backend=backend or "subscription",
        models=chosen,
        efforts=dict(zip(FEATURES, efforts, strict=True)),
        fallback_model=fallback or None,
        summary_enabled=bool(enabled),
        summary_weekday=int(day or 0),
    )
    return dmc.Text("Saved.", size="sm", c="brand")


@callback(
    Output("coach-feedback", "children", allow_duplicate=True),
    Input("coach-test", "n_clicks"),
    prevent_initial_call=True,
)
def test_coach(_clicks):
    """One tiny request with the chat model's saved settings."""
    started = time.monotonic()
    try:
        result = get_backend().run(
            LLMRequest(
                system="You are testing a connection. Reply with exactly: OK",
                messages=[{"role": "user", "content": "Connection test."}],
                config=feature_config("chat"),
                max_turns=1,
            )
        )
    except LLMError as e:
        return dmc.Alert(str(e), color="red", variant="light", title="Test failed")
    seconds = time.monotonic() - started
    return dmc.Alert(
        f"{result.model or 'Claude'} replied “{result.text.strip()[:40]}” in {seconds:.1f}s "
        f"via the {backend_name()} backend.",
        color="brand",
        variant="light",
        title="Connected",
    )
