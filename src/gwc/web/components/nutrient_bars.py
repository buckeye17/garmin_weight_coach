import dash_mantine_components as dmc

from gwc.nutrition.nutrients import NUTRIENTS, TargetKind


def _status(kind: TargetKind, total: float, target: float) -> tuple[str, str]:
    """(remaining/over text, bar color override or '')."""
    diff = target - total
    n_fmt = f"{abs(diff):,.0f}"
    if kind is TargetKind.LIMIT:
        return (f"{n_fmt} left", "") if diff >= 0 else (f"{n_fmt} over limit", "red")
    if kind is TargetKind.MINIMUM:
        return (f"{n_fmt} to go", "") if diff > 0 else ("goal met", "green")
    if diff >= 0:
        return f"{n_fmt} left", ""
    return f"{n_fmt} over", "orange" if total > target * 1.05 else ""


def nutrient_bars(totals: dict[str, float], targets: dict[str, float | None]) -> list:
    rows = []
    for n in NUTRIENTS:
        total = totals.get(n.key) or 0
        target = targets.get(n.key)
        if target:
            status, override = _status(n.kind, total, target)
            pct = min(100.0, 100 * total / target)
            right = f"{total:,.0f} / {target:,.0f} {n.unit} · {status}"
            bar = dmc.Progress(value=pct, color=override or n.color, size="md", radius="xl")
        else:
            right = f"{total:,.0f} {n.unit}"
            bar = dmc.Progress(value=0, size="md", radius="xl")
        rows.append(
            dmc.Stack(
                [
                    dmc.Group(
                        [
                            dmc.Text(n.label, size="sm", fw=500),
                            dmc.Text(right, size="sm", c="dimmed", ta="right"),
                        ],
                        justify="space-between",
                        wrap="nowrap",
                    ),
                    bar,
                ],
                gap=4,
            )
        )
    return rows
