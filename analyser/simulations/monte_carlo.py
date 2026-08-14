"""Deterministic Monte Carlo robustness analysis.

Monte Carlo operates on the canonical completed-position profit sequence.  It
is deliberately separate from the primary eager report analysis because it is
an optional, potentially expensive simulation and does not recreate intrabar
floating equity.  It answers questions such as:

* How sensitive is drawdown to the order of otherwise identical trades?
* What happens if historical outcomes are treated as a bootstrap sample?
* How often does a simulated path cross a configured ruin threshold?

The default ``permutation`` method samples every historical trade exactly once
and randomises its order.  ``bootstrap`` samples the historical trade outcomes
with replacement.  Both methods are deterministic for a given seed and
configuration.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math
from typing import Any, Literal

import numpy as np

from ..equity import build_equity
from ..load import InputSource, load_report
from ..models import Report
from ..serialization import deterministic_json

MonteCarloMethod = Literal["permutation", "bootstrap"]
_SUPPORTED_METHODS = frozenset(("permutation", "bootstrap"))


@dataclass(frozen=True)
class MonteCarloConfig:
    """Configuration for one reproducible Monte Carlo run.

    ``permutation`` is the conservative default: it preserves the observed
    profit distribution and only changes trade order.  ``bootstrap`` draws
    outcomes with replacement and therefore also varies the realised trade
    distribution.

    ``skip_trades_pct`` removes a random subset before each run.  It is an
    optional stress test and defaults to zero.  For permutation runs, the
    retained trades are still shuffled.  For bootstrap runs, the retained
    count is the number of draws made with replacement.
    """

    iterations: int = 1_000
    method: MonteCarloMethod = "permutation"
    skip_trades_pct: float = 0.0
    ruin_equity: float = 0.0
    seed: int = 0

    def validate(self) -> None:
        if (
            isinstance(self.iterations, bool)
            or not isinstance(self.iterations, (int, np.integer))
            or self.iterations < 1
        ):
            raise ValueError("iterations must be an integer greater than zero")
        if self.method not in _SUPPORTED_METHODS:
            raise ValueError(
                f"method must be one of {sorted(_SUPPORTED_METHODS)}, got {self.method!r}"
            )
        if not math.isfinite(float(self.skip_trades_pct)) or not 0.0 <= self.skip_trades_pct <= 100.0:
            raise ValueError("skip_trades_pct must be between 0 and 100")
        if not math.isfinite(float(self.ruin_equity)):
            raise ValueError("ruin_equity must be finite")
        if isinstance(self.seed, bool) or not isinstance(self.seed, (int, np.integer)):
            raise ValueError("seed must be an integer")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MonteCarloResult:
    """Simulation distributions and metadata.

    Array entries are aligned by iteration.  ``ruined`` is true when any point
    in that simulated balance path is at or below ``config.ruin_equity``; it is
    not merely a test of the final balance.
    """

    config: MonteCarloConfig
    trade_count: int
    sample_size: int
    max_drawdowns: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=float))
    max_drawdown_pcts: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=float))
    net_profits: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=float))
    final_equities: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=float))
    max_consecutive_losses: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=int))
    ruined: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=bool))

    @property
    def iterations(self) -> int:
        return self.config.iterations

    @property
    def probability_of_ruin_pct(self) -> float:
        return float(self.ruined.mean() * 100.0) if self.ruined.size else 0.0

    def percentile(self, values: np.ndarray, pct: float) -> float:
        """Return a percentile from one of the aligned result arrays."""

        if not 0.0 <= pct <= 100.0:
            raise ValueError("pct must be between 0 and 100")
        if values.size == 0:
            return float("nan")
        return float(np.percentile(values, pct))

    def _distribution_summary(
        self,
        values: np.ndarray,
        *,
        worst_is: Literal["min", "max"] = "max",
    ) -> dict[str, float | None]:
        if values.size == 0:
            return {"p5": None, "p50": None, "p95": None, "mean": None, "worst": None}
        worst = values.min() if worst_is == "min" else values.max()
        return {
            "p5": self.percentile(values, 5),
            "p50": self.percentile(values, 50),
            "p95": self.percentile(values, 95),
            "mean": float(values.mean()),
            "worst": float(worst),
        }

    def summary(self) -> dict[str, Any]:
        """Return deterministic percentile summaries suitable for a GUI/API."""

        drawdown = self._distribution_summary(self.max_drawdowns)
        return {
            "iterations": self.iterations,
            "trade_count": self.trade_count,
            "sample_size": self.sample_size,
            "method": self.config.method,
            "seed": int(self.config.seed),
            "skip_trades_pct": self.config.skip_trades_pct,
            "ruin_equity": self.config.ruin_equity,
            "max_drawdown_money": drawdown,
            # Retain the original short key while exposing the unambiguous
            # money-specific name for new consumers.
            "drawdown": drawdown,
            "max_drawdown_pct": self._distribution_summary(self.max_drawdown_pcts),
            "net_profit": self._distribution_summary(self.net_profits, worst_is="min"),
            "final_equity": self._distribution_summary(self.final_equities, worst_is="min"),
            "max_consecutive_losses": self._distribution_summary(self.max_consecutive_losses),
            "probability_of_ruin_pct": self.probability_of_ruin_pct,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "config": self.config.to_dict(),
            "trade_count": self.trade_count,
            "sample_size": self.sample_size,
            "max_drawdowns": self.max_drawdowns.tolist(),
            "max_drawdown_pcts": self.max_drawdown_pcts.tolist(),
            "net_profits": self.net_profits.tolist(),
            "final_equities": self.final_equities.tolist(),
            "max_consecutive_losses": self.max_consecutive_losses.tolist(),
            "ruined": self.ruined.tolist(),
        }

    def to_json(self) -> str:
        return deterministic_json(self.to_dict())


def _max_drawdown(equity: np.ndarray) -> tuple[float, float]:
    if equity.size == 0:
        return 0.0, 0.0
    running_max = np.maximum.accumulate(equity)
    drawdown = running_max - equity
    peak = running_max[np.argmax(drawdown)]
    drawdown_money = float(drawdown.max(initial=0.0))
    drawdown_pct = drawdown_money / abs(float(peak)) * 100.0 if peak else 0.0
    return drawdown_money, float(drawdown_pct)


def _max_consecutive_losses(profits: np.ndarray) -> int:
    longest = run = 0
    for profit in profits:
        if profit < 0:
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    return longest


def _sample_profits(
    profits: np.ndarray,
    *,
    sample_size: int,
    method: MonteCarloMethod,
    rng: np.random.Generator,
) -> np.ndarray:
    if sample_size == 0:
        return np.empty(0, dtype=float)
    if method == "permutation":
        return rng.permutation(profits)[:sample_size]
    indices = rng.integers(0, profits.size, size=sample_size)
    return profits[indices]


def run_monte_carlo(
    report: Report,
    config: MonteCarloConfig | int | None = None,
    skip_trades_pct: float | None = None,
    ruin_equity: float | None = None,
    seed: int | None = None,
    *,
    method: MonteCarloMethod | None = None,
    iterations: int | None = None,
) -> MonteCarloResult:
    """Run deterministic Monte Carlo simulations for one parsed report.

    The simulation uses ordered closed-position net profits.  It does not use
    source floating-equity observations, because those observations cannot be
    reconstructed for a counterfactual trade order without price data.

    ``config`` is the preferred interface.  The scalar keyword arguments are
    retained for compatibility with the original simulation helper, so calls
    such as ``run_monte_carlo(report, iterations=2, seed=42)`` remain valid.
    """

    overrides = (skip_trades_pct, ruin_equity, seed, method, iterations)
    if isinstance(config, MonteCarloConfig):
        if any(value is not None for value in overrides):
            raise ValueError("provide either MonteCarloConfig or scalar options, not both")
    else:
        if config is not None and not isinstance(config, (int, np.integer)):
            raise TypeError("config must be MonteCarloConfig, an iteration count, or None")
        if config is not None and iterations is not None:
            raise ValueError("iteration count was provided twice")
        iteration_value = iterations if iterations is not None else config
        config = MonteCarloConfig(
            iterations=iteration_value if iteration_value is not None else 1_000,
            method=method or "permutation",
            skip_trades_pct=skip_trades_pct if skip_trades_pct is not None else 0.0,
            ruin_equity=ruin_equity if ruin_equity is not None else 0.0,
            seed=seed if seed is not None else 0,
        )
    config.validate()
    profits = np.asarray(report.profits(), dtype=float)
    trade_count = int(profits.size)
    sample_size = int(round(trade_count * (1.0 - config.skip_trades_pct / 100.0)))
    rng = np.random.default_rng(int(config.seed))

    max_drawdowns = np.empty(config.iterations, dtype=float)
    max_drawdown_pcts = np.empty(config.iterations, dtype=float)
    net_profits = np.empty(config.iterations, dtype=float)
    final_equities = np.empty(config.iterations, dtype=float)
    max_consecutive_losses = np.empty(config.iterations, dtype=int)
    ruined = np.empty(config.iterations, dtype=bool)

    for index in range(config.iterations):
        sample = _sample_profits(
            profits,
            sample_size=sample_size,
            method=config.method,
            rng=rng,
        )
        curve = build_equity(sample, report.initial_deposit)
        max_drawdowns[index], max_drawdown_pcts[index] = _max_drawdown(curve.equity)
        net_profits[index] = curve.net_profit
        final_equities[index] = curve.final_balance
        max_consecutive_losses[index] = _max_consecutive_losses(sample)
        ruined[index] = bool(np.any(curve.equity <= config.ruin_equity))

    return MonteCarloResult(
        config=config,
        trade_count=trade_count,
        sample_size=sample_size,
        max_drawdowns=max_drawdowns,
        max_drawdown_pcts=max_drawdown_pcts,
        net_profits=net_profits,
        final_equities=final_equities,
        max_consecutive_losses=max_consecutive_losses,
        ruined=ruined,
    )


def run_monte_carlo_file(
    source: InputSource,
    config: MonteCarloConfig | None = None,
) -> MonteCarloResult:
    """Parse one MT5 report and run Monte Carlo on its closed positions."""

    return run_monte_carlo(load_report(source), config)
