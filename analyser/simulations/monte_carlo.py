"""Monte Carlo robustness analysis.

Reshuffles and resamples the trade sequence many times to estimate the
distribution of likely outcomes (drawdown, net profit, final equity, ruin)
under the assumption that past trades are representative but their order is
not. This mirrors QuantAnalyzer's Monte Carlo lab.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..equity import build_equity
from ..models import Report


@dataclass
class MonteCarloResult:
    iterations: int
    max_drawdowns: np.ndarray = field(default_factory=lambda: np.empty(0))
    net_profits: np.ndarray = field(default_factory=lambda: np.empty(0))
    final_equities: np.ndarray = field(default_factory=lambda: np.empty(0))
    max_consecutive_losses: np.ndarray = field(default_factory=lambda: np.empty(0))

    def percentile(self, values: np.ndarray, pct: float) -> float:
        return float(np.percentile(values, pct))

    def summary(self) -> dict:
        dd = self.max_drawdowns
        np_ = self.net_profits
        return {
            "iterations": self.iterations,
            "drawdown": {
                "p5": self.percentile(dd, 5),
                "p50": self.percentile(dd, 50),
                "p95": self.percentile(dd, 95),
                "mean": float(dd.mean()) if dd.size else 0.0,
                "worst": float(dd.max()) if dd.size else 0.0,
            },
            "net_profit": {
                "p5": self.percentile(np_, 5),
                "p50": self.percentile(np_, 50),
                "p95": self.percentile(np_, 95),
                "mean": float(np_.mean()) if np_.size else 0.0,
            },
            "probability_of_ruin_pct": float(
                (self.final_equities <= 0).mean() * 100.0
            )
            if self.final_equities.size
            else 0.0,
        }


def _max_drawdown(equity: np.ndarray) -> float:
    running_max = np.maximum.accumulate(equity)
    return float(np.max(running_max - equity))


def _max_consecutive_losses(profits: np.ndarray) -> int:
    longest = run = 0
    for p in profits:
        run = run + 1 if p < 0 else 0
        longest = max(longest, run)
    return longest


def run_monte_carlo(
    report: Report,
    iterations: int = 1000,
    skip_trades_pct: float = 0.0,
    ruin_equity: float = 0.0,
    seed: int | None = None,
) -> MonteCarloResult:
    """Run Monte Carlo simulations.

    Parameters
    ----------
    skip_trades_pct:
        Fraction of trades randomly discarded each run (>0 models the
        possibility that some trades were luck and won't recur).
    ruin_equity:
        Balance at/below which a run counts as "ruined".
    """
    profits = np.asarray(report.profits(), dtype=float)
    n = profits.size
    result = MonteCarloResult(iterations=iterations)
    if n == 0:
        return result

    rng = np.random.default_rng(seed)
    n_keep = max(1, int(round(n * (1.0 - skip_trades_pct / 100.0))))

    max_dds = np.empty(iterations)
    net_ps = np.empty(iterations)
    final_eqs = np.empty(iterations)
    max_cls = np.empty(iterations, dtype=int)

    for i in range(iterations):
        idx = rng.choice(n, size=n_keep, replace=False)
        sample = profits[idx]
        rng.shuffle(sample)
        curve = build_equity(sample, report.initial_deposit)
        max_dds[i] = _max_drawdown(curve.equity)
        net_ps[i] = curve.net_profit
        final_eqs[i] = curve.final_balance
        max_cls[i] = _max_consecutive_losses(sample)

    result.max_drawdowns = max_dds
    result.net_profits = net_ps
    result.final_equities = final_eqs
    result.max_consecutive_losses = max_cls
    return result
