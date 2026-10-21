import dash
import dash_mantine_components as dmc
from dash import ALL, Input, Output, State, callback, dcc, no_update
from dash_iconify import DashIconify

from gwc.analysis.targets import manual_targets, set_manual_targets
from gwc.analysis.units import food_unit, set_units, weight_unit
from gwc.db.models import SyncKind, SyncStatus
from gwc.garmin.client import LoginState
from gwc.garmin.service import get_auth, get_runner, get_sync_service
from gwc.garmin.status import GarminOverview, get_overview
from gwc.nutrition.nutrients import NUTRIENTS
from gwc.prefs import get_pref, set_pref
from gwc.web.format import fmt_day, fmt_duration, fmt_when

FAST_POLL_MS = 2_000
SLOW_POLL_MS = 15_000

STATUS_COLORS = {
    SyncStatus.SUCCESS: "teal",
    SyncStatus.RUNNING: "blue",
    SyncStatus.FAILED: "red",
    SyncStatus.RATE_LIMITED: "red",
    SyncStatus.AUTH_REQUIRED: "orange",
}


def _icon(name: str, size: int = 18) -> DashIconify:
    return DashIconify(icon=name, width=size, height=size)


def _row(label: str, value_id: str) -> dmc.Group:
    return dmc.Group(
        [dmc.Text(label, c="dimmed", size="sm"), dmc.Text(id=value_id, size="sm", ta="right")],
        justify="space-between",
        wrap="nowrap",
    )


def _connection_card() -> dmc.Card:
    return dmc.Card(
        dmc.Stack(
            [
                dmc.Group(
                    [
                        dmc.Group(
                            [_icon("tabler:watch", 22), dmc.Title("Garmin Connect", order=4)],
                            gap="xs",
                        ),
                        dmc.Badge(id="garmin-conn-badge", variant="light"),
                    ],
                    justify="space-between",
                ),
                dmc.Text(id="garmin-conn-text", size="sm"),
                dmc.Alert(
                    "Add GARMIN_EMAIL and GARMIN_PASSWORD to the .env file in the project "
                    "folder, then restart the app.",
                    id="garmin-creds-alert",
                    color="yellow",
                    variant="light",
                    icon=_icon("tabler:key"),
                    display="none",
                ),
                dmc.Group(
                    [
                        dmc.Button(
                            "Connect",
                            id="garmin-connect-btn",
                            leftSection=_icon("tabler:plug-connected"),
                        ),
                        dmc.Button(
                            "Disconnect",
                            id="garmin-disconnect-btn",
                            variant="subtle",
                            color="gray",
                            display="none",
                        ),
                    ]
                ),
                dmc.Text(
                    "Logging in counts toward a limit of 3 login attempts per 24 hours. "
                    "After connecting, the saved login refreshes itself.",
                    size="xs",
                    c="dimmed",
                ),
                dmc.Alert(id="garmin-connect-feedback", display="none", variant="light"),
            ],
            gap="sm",
        ),
        withBorder=True,
    )


def _sync_card() -> dmc.Card:
    return dmc.Card(
        dmc.Stack(
            [
                dmc.Group(
                    [
                        dmc.Group(
                            [_icon("tabler:refresh", 22), dmc.Title("Sync", order=4)], gap="xs"
                        ),
                        dmc.Button(
                            "Sync now",
                            id="sync-now-btn",
                            leftSection=_icon("tabler:cloud-download"),
                            variant="light",
                        ),
                    ],
                    justify="space-between",
                ),
                dmc.Text(id="sync-now-hint", size="xs", c="dimmed", ta="right"),
                dmc.Alert(id="sync-feedback", display="none", variant="light"),
                dmc.Alert(
                    id="lockout-alert",
                    color="red",
                    variant="light",
                    icon=_icon("tabler:hand-stop"),
                    display="none",
                ),
                _row("Last successful sync", "sync-last"),
                _row("Next scheduled sync", "sync-next"),
                _row("Watch last uploaded", "sync-device"),
                _row("Provisional days", "sync-provisional"),
                dmc.Divider(label="Garmin usage (self-imposed limits)", labelPosition="left"),
                dmc.Stack(
                    [
                        dmc.Group(
                            [
                                dmc.Text("Requests, last 24 h", size="sm", c="dimmed"),
                                dmc.Text(id="usage-requests", size="sm"),
                            ],
                            justify="space-between",
                        ),
                        dmc.Progress(id="usage-requests-bar", value=0, size="sm"),
                    ],
                    gap=4,
                ),
                _row("Manual syncs today", "usage-manual"),
                _row("Login attempts, last 24 h", "usage-logins"),
            ],
            gap="sm",
        ),
        withBorder=True,
    )


def _runs_card() -> dmc.Card:
    return dmc.Card(
        dmc.Stack(
            [
                dmc.Title("Recent syncs", order=5),
                dmc.ScrollArea(
                    dmc.Table(id="runs-table", striped=True, fz="sm", verticalSpacing=4),
                    type="auto",
                ),
            ],
            gap="xs",
        ),
        withBorder=True,
    )


def _mfa_modal() -> dmc.Modal:
    return dmc.Modal(
        dmc.Stack(
            [
                dmc.Text("Garmin sent a verification code to your email or phone.", size="sm"),
                dmc.TextInput(
                    id="mfa-code",
                    label="Verification code",
                    placeholder="123456",
                    autoComplete="one-time-code",
                    inputProps={"inputMode": "numeric"},
                    debounce=True,
                ),
                dmc.Text(id="mfa-feedback", size="sm", c="red"),
                dmc.Group(
                    [
                        dmc.Button("Cancel", id="mfa-cancel-btn", variant="subtle", color="gray"),
                        dmc.Button("Verify", id="mfa-submit-btn"),
                    ],
                    justify="flex-end",
                ),
            ]
        ),
        id="mfa-modal",
        title="Two-factor verification",
        closeOnClickOutside=False,
        withCloseButton=False,
        centered=True,
    )


def _prefs_card() -> dmc.Card:
    return dmc.Card(
        dmc.Stack(
            [
                dmc.Group(
                    [_icon("tabler:adjustments", 22), dmc.Title("Preferences", order=4)], gap="xs"
                ),
                dmc.Group(
                    [
                        dmc.Text("Body weight", size="sm", c="dimmed"),
                        dmc.SegmentedControl(id="pref-weight-unit", data=["lb", "kg"], size="xs"),
                    ],
                    justify="space-between",
                ),
                dmc.Group(
                    [
                        dmc.Text("Food amounts", size="sm", c="dimmed"),
                        dmc.SegmentedControl(id="pref-food-unit", data=["g", "oz"], size="xs"),
                    ],
                    justify="space-between",
                ),
                dmc.NumberInput(
                    id="pref-fallback-hour",
                    label="Food day ends at (if Garmin has no sleep)",
                    description="Hour of day, 0–23. Normally the day ends when you fall asleep.",
                    min=0,
                    max=23,
                    allowDecimal=False,
                    w=260,
                ),
                dmc.Group(
                    [
                        dmc.Text(id="prefs-feedback", size="sm", c="teal"),
                        dmc.Button("Save preferences", id="prefs-save", size="xs"),
                    ],
                    justify="space-between",
                ),
            ],
            gap="sm",
        ),
        withBorder=True,
    )


def _targets_card() -> dmc.Card:
    inputs = [
        dmc.NumberInput(
            id={"type": "target-input", "key": n.key},
            label=f"{n.label} ({n.unit})",
            description={"target": "target", "minimum": "at least", "limit": "at most"}[
                n.kind.value
            ],
            min=0,
            hideControls=True,
            allowDecimal=False,
            inputProps={"inputMode": "numeric"},
        )
        for n in NUTRIENTS
    ]
    return dmc.Card(
        dmc.Stack(
            [
                dmc.Group(
                    [_icon("tabler:target-arrow", 22), dmc.Title("Daily targets", order=4)],
                    gap="xs",
                ),
                dmc.Text(
                    "Leave blank for no target. Milestone 4 suggests these from your monthly "
                    "goal; anything entered here overrides the suggestion.",
                    size="xs",
                    c="dimmed",
                ),
                dmc.SimpleGrid(inputs, cols=2, spacing="xs"),
                dmc.Group(
                    [
                        dmc.Text(id="targets-feedback", size="sm", c="teal"),
                        dmc.Button("Save targets", id="targets-save", size="xs"),
                    ],
                    justify="space-between",
                ),
            ],
            gap="sm",
        ),
        withBorder=True,
    )


def layout(**_kwargs):
    return dmc.Stack(
        [
            dmc.Group([_icon("tabler:settings", 28), dmc.Title("Settings", order=2)], gap="sm"),
            dmc.SimpleGrid(
                [_connection_card(), _sync_card()],
                cols={"base": 1, "md": 2},
                spacing="md",
            ),
            dmc.SimpleGrid(
                [_prefs_card(), _targets_card()],
                cols={"base": 1, "md": 2},
                spacing="md",
            ),
            _runs_card(),
            dmc.Card(
                dmc.Text(
                    "The monthly goal, suggested targets and the LLM backend arrive with "
                    "milestones 4–5.",
                    size="sm",
                    c="dimmed",
                ),
                withBorder=True,
            ),
            dcc.Store(id="prefs-load", data=0),
            _mfa_modal(),
            dcc.Store(id="settings-refresh", data=0),
            dcc.Interval(id="settings-poll", interval=SLOW_POLL_MS),
        ],
        gap="md",
    )


def _runs_table(ov: GarminOverview, fmt) -> list:
    body = []
    for r in ov.recent_runs:
        window = (
            f"{fmt_day(r.window_start)} – {fmt_day(r.window_end)}"
            if r.window_start and r.window_end
            else "—"
        )
        body.append(
            [
                fmt(r.started_at),
                r.kind.value,
                dmc.Text(
                    r.status.value.replace("_", " "),
                    c=STATUS_COLORS[r.status],
                    size="sm",
                    fw=600,
                    style={"whiteSpace": "nowrap"},
                ),
                str(r.requests_made),
                window,
                fmt_duration(r.started_at, r.finished_at),
                r.error or "",
            ]
        )
    head = ["Started", "Type", "Status", "Requests", "Window", "Took", "Note"]
    if body:
        rows = [dmc.TableTr([dmc.TableTd(cell) for cell in row]) for row in body]
    else:
        rows = [dmc.TableTr([dmc.TableTd("No syncs yet", c="dimmed")] + [dmc.TableTd("")] * 6)]
    return [
        dmc.TableThead(dmc.TableTr([dmc.TableTh(h) for h in head])),
        dmc.TableTbody(rows),
    ]


@callback(
    Output("garmin-conn-badge", "children"),
    Output("garmin-conn-badge", "color"),
    Output("garmin-conn-text", "children"),
    Output("garmin-creds-alert", "display"),
    Output("garmin-connect-btn", "children"),
    Output("garmin-connect-btn", "disabled"),
    Output("garmin-disconnect-btn", "display"),
    Output("sync-now-btn", "disabled"),
    Output("sync-now-hint", "children"),
    Output("lockout-alert", "children"),
    Output("lockout-alert", "display"),
    Output("sync-last", "children"),
    Output("sync-next", "children"),
    Output("sync-device", "children"),
    Output("sync-provisional", "children"),
    Output("usage-requests", "children"),
    Output("usage-requests-bar", "value"),
    Output("usage-manual", "children"),
    Output("usage-logins", "children"),
    Output("runs-table", "children"),
    Output("settings-poll", "interval"),
    Input("settings-poll", "n_intervals"),
    Input("settings-refresh", "data"),
)
def refresh(_n, _signal):
    ov = get_overview()
    service = get_sync_service()
    now = service.clock()

    def fmt(dt):
        return fmt_when(dt, service.tz, now)

    if ov.connected:
        badge = ("Connected", "teal")
        text = f"Signed in as {ov.display_name}." if ov.display_name else "Signed in."
    elif ov.mfa_pending:
        badge = ("Waiting for code", "blue")
        text = "Enter the verification code Garmin sent you."
    else:
        badge = ("Not connected", "gray")
        text = "Connect once to save a login token; syncs then run without your password."
    if ov.last_run is not None and ov.last_run.status is SyncStatus.AUTH_REQUIRED:
        badge = ("Reconnect needed", "orange")
        text = "Garmin rejected the saved login. Reconnect to resume syncing."

    lockout = ov.guard.lockout
    lockout_text = (
        f"Garmin rate-limited us ({lockout.reason}). All Garmin traffic is paused until "
        f"{fmt(lockout.until)}."
        if lockout
        else ""
    )
    if ov.running:
        hint = "Sync in progress…"
    elif ov.manual.allowed:
        hint = "Fetches today and the previous 3 days."
    else:
        hint = ov.manual.reason or ""
        if ov.manual.retry_at:
            hint += f" — available {fmt(ov.manual.retry_at)}"

    g = ov.guard
    provisional = ", ".join(fmt_day(d) for d in ov.provisional_days) or "None"
    return (
        badge[0],
        badge[1],
        text,
        "block" if not ov.has_credentials and not ov.connected else "none",
        "Reconnect" if ov.connected else "Connect",
        not ov.has_credentials or ov.mfa_pending,
        "block" if ov.connected else "none",
        ov.running or not ov.manual.allowed,
        hint,
        lockout_text,
        "block" if lockout else "none",
        fmt(ov.last_success.started_at) if ov.last_success else "Never",
        fmt(ov.next_sync),
        fmt(ov.last_device_sync) if ov.last_device_sync else "Unknown",
        provisional,
        f"{g.requests_24h} / {g.max_requests_24h}",
        min(100, 100 * g.requests_24h / g.max_requests_24h),
        f"{g.manual_today} / {g.max_manual_per_day}",
        f"{g.logins_24h} / {g.max_logins_24h}",
        _runs_table(ov, fmt),
        FAST_POLL_MS if ov.running else SLOW_POLL_MS,
    )


def _start_first_sync() -> None:
    service = get_sync_service()
    get_runner().start(SyncKind.CATCHUP if service.has_backfilled() else SyncKind.BACKFILL)


def _feedback(message: str | None, color: str) -> tuple:
    return message or "", color, "block" if message else "none"


@callback(
    Output("garmin-connect-feedback", "children"),
    Output("garmin-connect-feedback", "color"),
    Output("garmin-connect-feedback", "display"),
    Output("mfa-modal", "opened", allow_duplicate=True),
    Output("settings-refresh", "data", allow_duplicate=True),
    Input("garmin-connect-btn", "n_clicks"),
    State("settings-refresh", "data"),
    running=[(Output("garmin-connect-btn", "loading"), True, False)],
    prevent_initial_call=True,
)
def connect(_clicks, signal):
    result = get_auth().connect()
    if result.state is LoginState.CONNECTED:
        _start_first_sync()
        return *_feedback("Connected. Fetching your Garmin data…", "teal"), False, signal + 1
    if result.state is LoginState.NEEDS_MFA:
        return *_feedback(None, "blue"), True, signal + 1
    color = "red" if result.state is LoginState.BLOCKED else "orange"
    return *_feedback(result.message, color), False, signal + 1


@callback(
    Output("mfa-feedback", "children"),
    Output("mfa-modal", "opened", allow_duplicate=True),
    Output("mfa-code", "value"),
    Output("garmin-connect-feedback", "children", allow_duplicate=True),
    Output("garmin-connect-feedback", "color", allow_duplicate=True),
    Output("garmin-connect-feedback", "display", allow_duplicate=True),
    Output("settings-refresh", "data", allow_duplicate=True),
    Input("mfa-submit-btn", "n_clicks"),
    Input("mfa-code", "n_submit"),
    State("mfa-code", "value"),
    State("settings-refresh", "data"),
    running=[(Output("mfa-submit-btn", "loading"), True, False)],
    prevent_initial_call=True,
)
def submit_mfa(_clicks, _submits, code, signal):
    if not code or not code.strip():
        return "Enter the code first.", no_update, no_update, *([no_update] * 3), no_update
    result = get_auth().submit_mfa(code)
    if result.state is LoginState.CONNECTED:
        _start_first_sync()
        return (
            "",
            False,
            "",
            *_feedback("Connected. Fetching your Garmin data…", "teal"),
            signal + 1,
        )
    if result.state is LoginState.NEEDS_MFA:
        return result.message, True, "", *([no_update] * 3), signal + 1
    return "", False, "", *_feedback(result.message, "red"), signal + 1


@callback(
    Output("mfa-modal", "opened", allow_duplicate=True),
    Output("settings-refresh", "data", allow_duplicate=True),
    Input("mfa-cancel-btn", "n_clicks"),
    State("settings-refresh", "data"),
    prevent_initial_call=True,
)
def cancel_mfa(_clicks, signal):
    get_auth().cancel_mfa()
    return False, signal + 1


@callback(
    Output("settings-refresh", "data", allow_duplicate=True),
    Output("garmin-connect-feedback", "children", allow_duplicate=True),
    Output("garmin-connect-feedback", "color", allow_duplicate=True),
    Output("garmin-connect-feedback", "display", allow_duplicate=True),
    Input("garmin-disconnect-btn", "n_clicks"),
    State("settings-refresh", "data"),
    prevent_initial_call=True,
)
def disconnect(_clicks, signal):
    get_auth().disconnect()
    return signal + 1, *_feedback("Disconnected. Saved Garmin login removed.", "gray")


@callback(
    Output("sync-feedback", "children"),
    Output("sync-feedback", "color"),
    Output("sync-feedback", "display"),
    Output("settings-refresh", "data", allow_duplicate=True),
    Input("sync-now-btn", "n_clicks"),
    State("settings-refresh", "data"),
    prevent_initial_call=True,
)
def sync_now(_clicks, signal):
    result = get_runner().start(SyncKind.MANUAL)
    return *_feedback(result.message, "blue" if result.started else "orange"), signal + 1


@callback(
    Output("pref-weight-unit", "value"),
    Output("pref-food-unit", "value"),
    Output("pref-fallback-hour", "value"),
    Output({"type": "target-input", "key": ALL}, "value"),
    Input("prefs-load", "data"),
)
def load_prefs(_):
    targets = manual_targets()
    return (
        weight_unit(),
        food_unit(),
        get_pref("food_day_fallback_hour"),
        [targets.get(n.key) for n in NUTRIENTS],
    )


@callback(
    Output("prefs-feedback", "children"),
    Input("prefs-save", "n_clicks"),
    State("pref-weight-unit", "value"),
    State("pref-food-unit", "value"),
    State("pref-fallback-hour", "value"),
    prevent_initial_call=True,
)
def save_prefs(_clicks, weight, food, hour):
    set_units(weight or "lb", food or "g")
    if hour not in (None, ""):
        set_pref("food_day_fallback_hour", max(0, min(23, int(hour))))
    return "Saved."


@callback(
    Output("targets-feedback", "children"),
    Input("targets-save", "n_clicks"),
    State({"type": "target-input", "key": ALL}, "value"),
    prevent_initial_call=True,
)
def save_targets(_clicks, values):
    set_manual_targets({n.key: v for n, v in zip(NUTRIENTS, values, strict=True)})
    return "Saved."


dash.register_page(
    __name__,
    path="/settings",
    name="Settings",
    title="Settings · Weight Coach",
    order=6,
    layout=layout,
)
