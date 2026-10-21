"""28-day weight projection with a Monte Carlo uncertainty band.

Simulated day by day: the day's energy balance (intake − burn) divided by ρ
gives the weight change, the fat share of that change follows Forbes, and the
burn drops by `kcal_per_kg` per kg lost (smaller body, some adaptation).

The band (10th–90th percentile) comes from repeating the simulation with
intake bootstrapped from the logged days and TDEE drawn from its estimate's
standard error.
"""

import random
import statistics
from dataclasses import dataclass
from datetime import date, timedelta

from gwc.analysis.energy import fat_fraction, rho

HORIZON_DAYS = 28
N_SIMS = 400


@dataclass(frozen=True)
class Projection:
    key: str
    label: str
    intake_kcal: float
    tdee_kcal: float
    days: list[date]  # start day, then each simulated day
    expected: list[float]  # kg, same length as `days`
    p10: list[float]
    p90: list[float]

    @property
    def change_kg(self) -> float:
        return self.expected[-1] - self.expected[0]


def simulate(
    start_kg: float,
    fat_kg: float,
    intake: float,
    tdee: float,
    days: int,
    kcal_per_kg: float,
) -> list[float]:
    """Weights at the start and at the end of each simulated day."""
    w, fm = start_kg, fat_kg
    out = [w]
    for _ in range(days):
        burn = tdee - kcal_per_kg * (start_kg - w)
        dw = (intake - burn) / rho(fm)
        fm = max(fm + dw * fat_fraction(fm), 0.0)
        w += dw
        out.append(w)
    return out


def _percentile(sorted_values: list[float], q: float) -> float:
    i = q * (len(sorted_values) - 1)
    lo = int(i)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (i - lo)


def project(
    *,
    key: str,
    label: str,
    start_day: date,
    start_kg: float,
    fat_kg: float,
    tdee_kcal: float,
    tdee_se: float,
    intakes: list[float] | None = None,
    fixed_intake: float | None = None,
    days: int = HORIZON_DAYS,
    kcal_per_kg: float = 22.0,
    n_sims: int = N_SIMS,
    seed: int = 0,
) -> Projection:
    """Project from logged `intakes` (bootstrapped) or a `fixed_intake` (e.g. the target)."""
    if not intakes and fixed_intake is None:
        raise ValueError("Need intakes or a fixed intake")
    intake = statistics.fmean(intakes) if intakes else fixed_intake
    expected = simulate(start_kg, fat_kg, intake, tdee_kcal, days, kcal_per_kg)

    rng = random.Random(seed)
    paths = []
    for _ in range(n_sims):
        sim_intake = statistics.fmean(rng.choices(intakes, k=len(intakes))) if intakes else intake
        sim_tdee = rng.gauss(tdee_kcal, tdee_se)
        paths.append(simulate(start_kg, fat_kg, sim_intake, sim_tdee, days, kcal_per_kg))
    p10, p90 = [], []
    for i in range(days + 1):
        col = sorted(p[i] for p in paths)
        p10.append(_percentile(col, 0.1))
        p90.append(_percentile(col, 0.9))

    return Projection(
        key=key,
        label=label,
        intake_kcal=intake,
        tdee_kcal=tdee_kcal,
        days=[start_day + timedelta(days=i) for i in range(days + 1)],
        expected=expected,
        p10=p10,
        p90=p90,
    )
