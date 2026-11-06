"""Gout pieces shared by the Gout page and Today: trigger factors, sources, look-backs."""

from datetime import date

import dash_mantine_components as dmc

from gwc.db.models import GoutDay, GoutFlare
from gwc.gout import flares
from gwc.gout.triggers import LEVEL_COLORS, LEVEL_LABELS, Factor, Level, Lookback, Report
from gwc.web.components.common import fmt_clock, icon

FOOD_KEYS = ("purines", "alcohol", "sweet")


def level_badge(level: Level | None, prefix: str = "") -> dmc.Badge:
    if level is None:
        return dmc.Badge(f"{prefix}No data", color="gray", variant="light")
    return dmc.Badge(f"{prefix}{LEVEL_LABELS[level]}", color=LEVEL_COLORS[level], variant="light")


def severity_badge(severity: int | None) -> dmc.Badge:
    if not severity:
        return dmc.Badge("Not recorded", color="gray", variant="outline", tt="none")
    label = flares.SEVERITY[severity][0]
    return dmc.Badge(
        f"{severity} · {label}", color=flares.SEVERITY_COLORS[severity], variant="light", tt="none"
    )


def severity_data(with_none: bool = False) -> list[dict[str, str]]:
    data = [{"value": "0", "label": "None"}] if with_none else []
    return data + [{"value": str(n), "label": str(n)} for n in flares.SEVERITY]


def severity_legend() -> dmc.Text:
    text = " · ".join(f"{n} {label.lower()}" for n, (label, _) in flares.SEVERITY.items())
    return dmc.Text(text, size="xs", c="dimmed")


def factor_row(f: Factor) -> dmc.Group:
    color = "gray" if f.level is None else LEVEL_COLORS[f.level]
    return dmc.Group(
        [
            dmc.ThemeIcon(
                icon("tabler:point-filled", 14), color=color, variant="light", size="sm", mt=2
            ),
            dmc.Stack(
                [
                    dmc.Group(
                        [
                            dmc.Text(f.label, size="sm", fw=500),
                            dmc.Text(f.window, size="xs", c="dimmed"),
                        ],
                        gap=6,
                        align="baseline",
                    ),
                    dmc.Text(f.text, size="sm"),
                    dmc.Text(f.detail, size="xs", c="dimmed") if f.detail else None,
                ],
                gap=0,
                flex=1,
                miw=0,
            ),
            level_badge(f.level) if f.level is not None else None,
        ],
        gap="xs",
        wrap="nowrap",
        align="flex-start",
    )


def _source_line(x) -> str:
    parts = []
    if x.purine_mg >= 1:
        parts.append(f"{x.purine_mg:,.0f} mg purines")
    if x.alcohol_g >= 1:
        parts.append(f"{x.drinks:.1f} drinks")
    if x.sweet_sugar_g >= 1:
        parts.append(f"{x.sweet_sugar_g:,.0f} g sweet-drink sugar")
    return " · ".join(parts)


def sources_list(report: Report, today: date, clickable: bool) -> list:
    rows = []
    for src in report.sources:
        body = dmc.Paper(
            dmc.Group(
                [
                    dmc.Text(fmt_clock(src.eaten_at, today), size="xs", c="dimmed", w=72),
                    dmc.Stack(
                        [
                            dmc.Text(src.description, size="sm", fw=500, lineClamp=1),
                            dmc.Text(_source_line(src.exposure), size="xs", c="dimmed"),
                        ],
                        gap=0,
                        flex=1,
                        miw=0,
                    ),
                ],
                wrap="nowrap",
                gap="sm",
            ),
            p="xs",
            withBorder=True,
            className="gwc-entry" if clickable else None,
        )
        if clickable:
            body = dmc.UnstyledButton(
                body, id={"type": "gout-src", "index": src.entry_id}, w="100%"
            )
        rows.append(body)
    return rows


def _fmt_typical(key: str, value: float | None) -> str:
    if value is None:
        return "—"
    if key == "purines":
        return f"{value:,.0f} mg"
    if key == "alcohol":
        return "None" if value < 0.05 else f"{value:.1f} drinks"
    return "None" if value < 0.5 else f"{value:,.0f} g sugar"


def lookback_panel(lb: Lookback, today: date) -> dmc.Stack:
    rep = lb.report
    rows = []
    for f in rep.factors:
        typical = _fmt_typical(f.key, lb.typical.get(f.key)) if f.key in FOOD_KEYS else "—"
        rows.append(
            dmc.TableTr(
                [
                    dmc.TableTd(dmc.Text(f.label, size="sm", fw=500)),
                    dmc.TableTd(
                        dmc.Group(
                            [
                                dmc.Text(f.text, size="sm"),
                                level_badge(f.level) if f.level else None,
                            ],
                            gap=6,
                        )
                    ),
                    dmc.TableTd(dmc.Text(typical, size="sm", c="dimmed")),
                ]
            )
        )
    table = dmc.Table(
        [
            dmc.TableThead(
                dmc.TableTr(
                    [dmc.TableTh("Factor"), dmc.TableTh("Before the flare"), dmc.TableTh("Typical")]
                )
            ),
            dmc.TableTbody(rows),
        ],
        verticalSpacing=4,
        fz="sm",
    )
    typical_note = (
        f"Typical: the median of {lb.samples} ordinary days at the same time of day, "
        "from the month before."
        if lb.samples
        else "Not enough logged days before this flare to show what's typical."
    )
    children = [
        dmc.Group(
            [dmc.Text("Trigger load when it started", size="sm"), level_badge(rep.level)],
            gap="xs",
        ),
        dmc.ScrollArea(table, type="auto"),
        dmc.Text(typical_note, size="xs", c="dimmed"),
    ]
    if not rep.logged:
        children.append(
            dmc.Text("No food was logged in the 48 hours before it.", size="xs", c="dimmed")
        )
    if rep.sources:
        children += [
            dmc.Text("Biggest sources in the 48 hours before", size="sm", fw=500, mt=4),
            *sources_list(rep, today, clickable=False),
        ]
    return dmc.Stack(children, gap=6)


def flare_title(flare: GoutFlare) -> str:
    first = flares.start_day(flare)
    when = f"{first:%a %b} {first.day}"
    if flare.ended_on and flare.ended_on != first:
        end = flare.ended_on
        when += f"–{end:%b} {end.day}" if end.month != first.month else f"–{end.day}"
    return when


def flare_summary(flare: GoutFlare) -> str:
    parts = [", ".join(flare.joints) or "Joint not noted"]
    top = flares.peak(flare)
    if top:
        parts.append(f"peak {top} ({flares.SEVERITY[top][0].lower()})")
    if flare.ended_on:
        n = (flare.ended_on - flares.start_day(flare)).days + 1
        parts.append(f"{n} day{'s' if n != 1 else ''}")
    return " · ".join(parts)


def day_entry(flare: GoutFlare, day: date) -> GoutDay | None:
    return next((d for d in flare.days if d.day == day), None)


def today_card(
    report: Report | None, active: GoutFlare | None, today: date, viewing_today: bool
) -> dmc.Card:
    """Today's compact version: overall load, what's notable, and today's flare severity."""
    children: list = []
    head = [dmc.Title("Gout triggers", order=5)]
    if report is not None:
        head.append(level_badge(report.level))
    children.append(dmc.Group(head, justify="space-between", align="center"))
    if report is not None:
        notable = [f for f in report.factors if f.level]
        if notable:
            children += [factor_row(f) for f in notable]
        elif report.logged:
            children.append(
                dmc.Text("Nothing notable in the last day or two.", size="sm", c="dimmed")
            )
        else:
            children.append(dmc.Text("Nothing logged in the last 48 hours.", size="sm", c="dimmed"))
        if n := report.unestimated:
            children.append(
                dmc.Text(
                    f"{n} recent entr{'y is' if n == 1 else 'ies are'} waiting for an estimate.",
                    size="xs",
                    c="dimmed",
                )
            )
    if active is not None and viewing_today:
        current = day_entry(active, today)
        n = flares.day_number(active, today)
        children.append(
            dmc.Paper(
                dmc.Stack(
                    [
                        dmc.Text(f"Flare day {n}: how bad is it today?", size="sm", fw=500),
                        dmc.SegmentedControl(
                            id={"type": "today-gout-sev", "index": active.id},
                            data=severity_data(with_none=True),
                            value=str(current.severity) if current else None,
                            fullWidth=True,
                            size="xs",
                        ),
                        severity_legend(),
                    ],
                    gap=6,
                ),
                p="xs",
                withBorder=True,
            )
        )
    children.append(
        dmc.Anchor(
            dmc.Group([icon("tabler:flame", 14), "Flares and details"], gap=4),
            href="/gout",
            size="sm",
        )
    )
    return dmc.Card(dmc.Stack(children, gap="xs"), withBorder=True)
