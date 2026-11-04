import dash
import dash_mantine_components as dmc
from dash import ALL, Input, Output, State, callback, ctx, dcc, no_update
from dash.exceptions import PreventUpdate

from gwc.coach import chat, proposals, summary
from gwc.coach.jobs import get_jobs
from gwc.coach.settings import WEEKDAYS, summary_enabled, summary_weekday
from gwc.db.models import FoodLog
from gwc.db.session import session_scope
from gwc.web.components.common import (
    chat_bubble,
    icon,
    now,
    page_header,
    triggered_click,
    tz,
)
from gwc.web.format import fmt_day

POLL_MS = 1000
SUGGESTIONS = [
    "How am I doing this week?",
    "Why did my weight go up?",
    "What should I eat for the rest of today to hit my protein?",
    "Is my calorie target realistic?",
]


def _chat_panel(entry_id: int | None = None) -> dmc.Stack:
    return dmc.Stack(
        [
            dmc.Group(
                [
                    dmc.Select(
                        id="coach-conv",
                        placeholder="New conversation",
                        data=[],
                        flex=1,
                        clearable=False,
                        allowDeselect=False,
                    ),
                    dmc.Button(
                        "New", id="coach-new", variant="default", leftSection=icon("tabler:plus")
                    ),
                    dmc.ActionIcon(
                        icon("tabler:trash"),
                        id="coach-delete",
                        variant="subtle",
                        color="red",
                        size="lg",
                        **{"aria-label": "Delete conversation"},
                    ),
                ],
                wrap="nowrap",
                gap="xs",
            ),
            dmc.Box(id="coach-about"),
            dmc.Stack(id="coach-messages", gap="sm"),
            dmc.Stack(id="coach-proposals", gap="xs"),
            dmc.Group(
                [
                    dmc.Paper(
                        dmc.Stack(
                            [
                                dmc.Group(
                                    [
                                        dmc.Loader(size="xs"),
                                        dmc.Text(id="coach-activity", size="xs", c="dimmed"),
                                    ],
                                    gap="xs",
                                ),
                                dcc.Markdown(id="coach-live-text", className="coach-md"),
                            ],
                            gap=4,
                        ),
                        p="sm",
                        radius="lg",
                        withBorder=True,
                        maw="95%",
                    )
                ],
                id="coach-live",
                display="none",
            ),
            dmc.Box(id="coach-error"),
            dmc.Group(
                [
                    dmc.Button(
                        s,
                        id={"type": "coach-suggest", "index": i},
                        variant="light",
                        size="compact-sm",
                        # Long suggestions wrap on phones instead of being cut off.
                        h="auto",
                        py=4,
                        styles={"label": {"whiteSpace": "normal", "textAlign": "left"}},
                    )
                    for i, s in enumerate(SUGGESTIONS)
                ],
                id="coach-suggestions",
                gap="xs",
            ),
            dmc.Group(
                [
                    dmc.Textarea(
                        id="coach-input",
                        placeholder=(
                            "What looks off about this entry?"
                            if entry_id
                            else "Ask about your weight, food, sleep or training…"
                        ),
                        autosize=True,
                        minRows=1,
                        maxRows=6,
                        flex=1,
                    ),
                    dmc.ActionIcon(
                        icon("tabler:send", 20),
                        id="coach-send",
                        size="xl",
                        **{"aria-label": "Send"},
                    ),
                ],
                wrap="nowrap",
                align="flex-end",
                gap="xs",
            ),
            dcc.Store(id="coach-conv-id", storage_type="session"),
            dcc.Store(id="coach-entry", data=entry_id),  # questioning this entry (not yet sent)
            dcc.Store(id="coach-job"),
            dcc.Store(id="coach-refresh", data=0),
            dcc.Interval(id="coach-poll", interval=POLL_MS, disabled=True),
        ],
        gap="sm",
        pt="sm",
    )


def _summary_panel() -> dmc.Stack:
    return dmc.Stack(
        [
            dmc.Group(
                [
                    dmc.Text(id="sum-schedule", size="sm", c="dimmed", flex=1),
                    dmc.Button(
                        "Write last week's summary",
                        id="sum-go",
                        size="xs",
                        leftSection=icon("tabler:sparkles", 16),
                    ),
                ],
                align="flex-start",
            ),
            dmc.Paper(
                dmc.Stack(
                    [
                        dmc.Group(
                            [dmc.Loader(size="xs"), dmc.Text(id="sum-activity", size="xs")],
                            gap="xs",
                        ),
                        dcc.Markdown(id="sum-live-text", className="coach-md"),
                    ],
                    gap=4,
                ),
                id="sum-live",
                p="sm",
                withBorder=True,
                display="none",
            ),
            dmc.Box(id="sum-error"),
            dmc.Box(id="sum-list"),
            dcc.Store(id="sum-job"),
            dcc.Store(id="sum-refresh", data=0),
            dcc.Interval(id="sum-poll", interval=POLL_MS, disabled=True),
        ],
        gap="sm",
        pt="sm",
    )


def _entry_param(value) -> int | None:
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


def layout(entry=None, **_kwargs):
    entry_id = _entry_param(entry)
    # Question this always lands on the chat, whichever tab was open last.
    tab_state = {"value": "chat"} if entry_id else {"value": "chat", "persistence": True}
    return dmc.Stack(
        [
            page_header(
                "Coach",
                "tabler:message-chatbot",
                "Ask about your data, question a logged food, or read the weekly summaries.",
            ),
            dmc.Tabs(
                [
                    dmc.TabsList(
                        [
                            dmc.TabsTab("Chat", value="chat", leftSection=icon("tabler:messages")),
                            dmc.TabsTab(
                                "Weekly summaries",
                                value="summaries",
                                leftSection=icon("tabler:calendar-week"),
                            ),
                        ],
                        grow=True,
                    ),
                    dmc.TabsPanel(_chat_panel(entry_id), value="chat"),
                    dmc.TabsPanel(_summary_panel(), value="summaries"),
                ],
                **tab_state,
                id="coach-tabs",
            ),
            dmc.Text(
                "Questions and your health and food data are sent to Anthropic to answer them.",
                size="xs",
                c="dimmed",
            ),
        ],
        gap="sm",
    )


# --------------------------------------------------------------------------- chat


def _conv_id(value) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


@callback(
    Output("coach-conv", "data"),
    Output("coach-conv", "value"),
    Output("coach-messages", "children"),
    Output("coach-suggestions", "display"),
    Input("coach-conv-id", "data"),
    Input("coach-refresh", "data"),
    Input("coach-entry", "data"),
)
def render_chat(conv_id, _refresh, entry_id=None):
    conv_id = None if entry_id else _conv_id(conv_id)  # an entry starts a new conversation
    convs = chat.list_conversations()
    data = [{"value": str(c.id), "label": c.title} for c in convs]
    if conv_id is not None and not any(c.id == conv_id for c in convs):
        conv_id = None
    msgs = chat.messages(conv_id) if conv_id else []
    bubbles = [chat_bubble(m.role, m.content, m.model) for m in msgs]
    if entry_id:
        bubbles = [
            dmc.Text(
                "Say what looks off. The coach can look it up, and suggest a change you can "
                "apply or dismiss. Nothing changes unless you press Apply.",
                size="sm",
                c="dimmed",
            )
        ]
    elif not bubbles:
        bubbles = [
            dmc.Text(
                "Ask the coach anything about your data. It reads your weigh-ins, food log, "
                "targets, sleep and activities before answering.",
                size="sm",
                c="dimmed",
            )
        ]
    suggestions = "none" if msgs or entry_id else "flex"
    return data, str(conv_id) if conv_id else None, bubbles, suggestions


@callback(
    Output("coach-conv-id", "data", allow_duplicate=True),
    Output("coach-entry", "data", allow_duplicate=True),
    Input("coach-conv", "value"),
    State("coach-conv-id", "data"),
    State("coach-entry", "data"),
    prevent_initial_call=True,
)
def pick_conversation(value, current, entry_id=None):
    value = _conv_id(value)
    if value is None or (value == _conv_id(current) and not entry_id):
        raise PreventUpdate
    return value, None


@callback(
    Output("coach-conv-id", "data", allow_duplicate=True),
    Output("coach-refresh", "data", allow_duplicate=True),
    Output("coach-entry", "data", allow_duplicate=True),
    Input("coach-new", "n_clicks"),
    Input("coach-delete", "n_clicks"),
    State("coach-conv-id", "data"),
    State("coach-refresh", "data"),
    prevent_initial_call=True,
)
def new_or_delete(_new, _delete, conv_id, refresh):
    if ctx.triggered_id == "coach-delete" and _conv_id(conv_id):
        chat.delete_conversation(_conv_id(conv_id))
    return None, (refresh or 0) + 1, None


@callback(
    Output("coach-job", "data"),
    Output("coach-poll", "disabled"),
    Output("coach-input", "value"),
    Output("coach-conv-id", "data", allow_duplicate=True),
    Output("coach-refresh", "data", allow_duplicate=True),
    Output("coach-error", "children"),
    Output("coach-entry", "data", allow_duplicate=True),
    Input("coach-send", "n_clicks"),
    Input({"type": "coach-suggest", "index": ALL}, "n_clicks"),
    State("coach-input", "value"),
    State("coach-conv-id", "data"),
    State("coach-refresh", "data"),
    State("coach-job", "data"),
    State("coach-entry", "data"),
    prevent_initial_call=True,
)
def send(_send, _suggest, text, conv_id, refresh, job_id, entry_id=None):
    if ctx.triggered_id == "coach-send":
        if not ctx.triggered[0].get("value"):
            raise PreventUpdate
    else:
        clicked = triggered_click()
        if clicked is None:
            raise PreventUpdate
        text = SUGGESTIONS[clicked["index"]]
    current = get_jobs().get(job_id)
    if current is not None and current.running:
        raise PreventUpdate
    if not text or not text.strip():
        raise PreventUpdate
    conv = None if entry_id else _conv_id(conv_id)  # an entry starts its own conversation
    new_id, job = chat.send(conv, text, tz(), now(), food_log_id=entry_id)
    return job.id, False, "", new_id, (refresh or 0) + 1, None, None


def _about(entry_id: int | None) -> dmc.Alert | None:
    if not entry_id:
        return None
    with session_scope() as s:
        entry = s.get(FoodLog, entry_id)
        if entry is None:
            return None
        day = fmt_day(entry.eaten_at.astimezone(tz()).date())
        text = f"{entry.description}: {entry.kcal:,.0f} kcal, logged for {day}."
    return dmc.Alert(text, title="About this entry", color="brand", variant="light", p="xs")


def proposal_card(view: proposals.ProposalView) -> dmc.Paper:
    """A change the coach suggested, with the user's decision: nothing changes until Apply."""
    lines = [
        dmc.Text(view.entry_name or "Deleted entry", size="sm", fw=600),
        dmc.Text(proposals.describe_changes(view), size="sm"),
        dmc.Text(view.reason, size="xs", c="dimmed"),
    ]
    if view.source:
        lines.append(dmc.Text(f"Source: {view.source}", size="xs", c="dimmed"))
    if view.update_library:
        lines.append(
            dmc.Text(f"Also corrects {view.library_food} in your library.", size="xs", c="dimmed")
        )
    elif view.library_food:
        lines.append(
            dmc.Text(
                f"Only this entry changes: it stops following {view.library_food} in your library.",
                size="xs",
                c="dimmed",
            )
        )
    if view.status == "pending" and view.entry_id:
        lines.append(
            dmc.Group(
                [
                    dmc.Button(
                        "Dismiss",
                        id={"type": "coach-proposal-dismiss", "index": view.id},
                        variant="subtle",
                        color="gray",
                        size="xs",
                    ),
                    dmc.Button(
                        "Apply",
                        id={"type": "coach-proposal-apply", "index": view.id},
                        size="xs",
                        leftSection=icon("tabler:check", 14),
                    ),
                ],
                justify="flex-end",
                gap="xs",
            )
        )
    else:
        words = {"applied": "Applied", "dismissed": "Dismissed"}
        lines.append(dmc.Text(words.get(view.status, "No longer open"), size="xs", c="dimmed"))
    return dmc.Paper(dmc.Stack(lines, gap=4), p="sm", withBorder=True)


@callback(
    Output("coach-proposals", "children"),
    Output("coach-about", "children"),
    Input("coach-conv-id", "data"),
    Input("coach-refresh", "data"),
    Input("coach-entry", "data"),
)
def render_proposals(conv_id, _refresh, entry_id):
    conv_id = None if entry_id else _conv_id(conv_id)
    cards = [proposal_card(v) for v in proposals.for_conversation(conv_id)] if conv_id else []
    return cards, _about(entry_id or chat.conversation_entry(conv_id))


@callback(
    Output("coach-refresh", "data", allow_duplicate=True),
    Output("coach-error", "children", allow_duplicate=True),
    Input({"type": "coach-proposal-apply", "index": ALL}, "n_clicks"),
    Input({"type": "coach-proposal-dismiss", "index": ALL}, "n_clicks"),
    State("coach-refresh", "data"),
    prevent_initial_call=True,
)
def decide_proposal(_apply, _dismiss, refresh):
    clicked = triggered_click()
    if clicked is None:
        raise PreventUpdate
    error = None
    if clicked["type"] == "coach-proposal-apply":
        try:
            proposals.apply(clicked["index"])
        except ValueError as e:
            error = dmc.Alert(str(e), color="red", variant="light")
    else:
        proposals.dismiss(clicked["index"])
    return (refresh or 0) + 1, error


@callback(
    Output("coach-job", "data", allow_duplicate=True),
    Output("coach-poll", "disabled", allow_duplicate=True),
    Input("coach-conv-id", "data"),
    prevent_initial_call="initial_duplicate",
)
def resume_job(conv_id):
    """Pick a reply back up after navigating away while Claude was answering."""
    conv_id = _conv_id(conv_id)
    if conv_id is None:
        return None, True
    running = [j for j in get_jobs().running("chat") if j.key == f"chat:{conv_id}"]
    return (running[0].id, False) if running else (None, True)


@callback(
    Output("coach-live", "display"),
    Output("coach-activity", "children"),
    Output("coach-live-text", "children"),
    Output("coach-poll", "disabled", allow_duplicate=True),
    Output("coach-refresh", "data", allow_duplicate=True),
    Output("coach-error", "children", allow_duplicate=True),
    Input("coach-poll", "n_intervals"),
    State("coach-job", "data"),
    State("coach-refresh", "data"),
    prevent_initial_call=True,
)
def poll_chat(_n, job_id, refresh):
    job = get_jobs().get(job_id)
    if job is None:
        return "none", "", "", True, no_update, no_update
    if job.running:
        return "flex", f"{job.activity} ({job.elapsed:.0f}s)", job.text, False, no_update, None
    error = None
    if job.status == "error":
        error = dmc.Alert(job.error, color="red", variant="light", title="The coach couldn't reply")
    return "none", "", "", True, (refresh or 0) + 1, error


# --------------------------------------------------------------------------- summaries


@callback(
    Output("sum-list", "children"),
    Output("sum-schedule", "children"),
    Input("sum-refresh", "data"),
)
def render_summaries(_refresh):
    rows = summary.list_summaries()
    if summary_enabled():
        schedule = (
            f"A summary of the previous week is written every {WEEKDAYS[summary_weekday()]} "
            "after the morning sync."
        )
    else:
        schedule = "Automatic weekly summaries are off (Settings → Coach)."
    if not rows:
        return dmc.Text("No summaries yet.", size="sm", c="dimmed"), schedule
    items = [
        dmc.AccordionItem(
            [
                dmc.AccordionControl(
                    f"Week of {fmt_day(r.week_start)}" + (f" · {r.model}" if r.model else "")
                ),
                dmc.AccordionPanel(dcc.Markdown(r.content, className="coach-md")),
            ],
            value=str(r.week_start),
        )
        for r in rows
    ]
    return (
        dmc.Accordion(items, value=str(rows[0].week_start), variant="separated"),
        schedule,
    )


@callback(
    Output("sum-job", "data"),
    Output("sum-poll", "disabled"),
    Output("sum-error", "children"),
    Input("sum-go", "n_clicks"),
    prevent_initial_call=True,
)
def start_summary(_clicks):
    from gwc.analysis.food_day import current_food_day
    from gwc.db.session import session_scope

    zone, at = tz(), now()
    with session_scope() as s:
        week = summary.last_full_week(current_food_day(s, zone, at))
    job = summary.start_generate(week, zone, at)
    return job.id, False, None


@callback(
    Output("sum-live", "display"),
    Output("sum-activity", "children"),
    Output("sum-live-text", "children"),
    Output("sum-poll", "disabled", allow_duplicate=True),
    Output("sum-refresh", "data"),
    Output("sum-error", "children", allow_duplicate=True),
    Output("sum-go", "disabled"),
    Input("sum-poll", "n_intervals"),
    State("sum-job", "data"),
    State("sum-refresh", "data"),
    prevent_initial_call=True,
)
def poll_summary(_n, job_id, refresh):
    job = get_jobs().get(job_id)
    if job is None:
        return "none", "", "", True, no_update, no_update, False
    if job.running:
        activity = f"{job.activity} ({job.elapsed:.0f}s)"
        return "block", activity, job.text, False, no_update, None, True
    error = None
    if job.status == "error":
        error = dmc.Alert(
            job.error, color="red", variant="light", title="Couldn't write the summary"
        )
    return "none", "", "", True, (refresh or 0) + 1, error, False


dash.register_page(
    __name__, path="/coach", name="Coach", title="Coach · Weight Coach", order=3, layout=layout
)
