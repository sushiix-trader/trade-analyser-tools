"""Extensible, deterministic performance and risk metrics."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

import numpy as np

from .config import AnalysisConfig
from .diagnostics import Diagnostic, add_diagnostic
from .equity import CurveSeries, reconstructed_curve
from .models import Report


def _safe_div(num: float, den: float) -> float | None:
    return None if den == 0 or not math.isfinite(den) else num / den


def _drawdown(values: np.ndarray) -> tuple[np.ndarray, np.ndarray, float, float, float, int]:
    if values.size == 0:
        return np.empty(0), np.empty(0), 0.0, 0.0, 0.0, 0
    peaks = np.maximum.accumulate(values)
    money = peaks - values
    with np.errstate(divide="ignore", invalid="ignore"):
        pct = np.divide(money, peaks, out=np.zeros_like(money), where=peaks != 0)
    underwater = money > 0
    avg = float(money[underwater].mean()) if underwater.any() else 0.0
    longest = run = 0
    for value in underwater:
        run = run + 1 if value else 0
        longest = max(longest, run)
    return money, pct, float(money.max(initial=0.0)), float(pct.max(initial=0.0)), avg, longest


def _streaks(profits: np.ndarray) -> dict[str, Any]:
    best_wins = best_losses = current_wins = current_losses = 0
    best_win_money = best_loss_money = current_win_money = current_loss_money = 0.0
    for profit in profits:
        if profit > 0:
            current_wins += 1
            current_win_money += float(profit)
            current_losses = 0
            current_loss_money = 0.0
            if current_wins > best_wins:
                best_wins = current_wins
                best_win_money = current_win_money
        elif profit < 0:
            current_losses += 1
            current_loss_money += float(profit)
            current_wins = 0
            current_win_money = 0.0
            if current_losses > best_losses:
                best_losses = current_losses
                best_loss_money = current_loss_money
        else:
            current_wins = current_losses = 0
            current_win_money = current_loss_money = 0.0
    return {
        "max_consecutive_wins": best_wins,
        "max_consecutive_losses": best_losses,
        "max_consecutive_wins_money": best_win_money,
        "max_consecutive_losses_money": best_loss_money,
    }


def _returns(values: np.ndarray, config: AnalysisConfig, diagnostics: list[Diagnostic]) -> np.ndarray:
    if values.size < 2:
        return np.empty(0, dtype=float)
    previous = values[:-1]
    current = values[1:]
    valid = previous != 0
    if not np.all(valid):
        add_diagnostic(
            diagnostics,
            "zero_previous_equity",
            "Skipped return observations with a zero previous equity value",
        )
    if not valid.any():
        return np.empty(0, dtype=float)
    ratio = current[valid] / previous[valid]
    if config.sharpe.return_type == "log":
        positive = ratio > 0
        if not positive.all():
            add_diagnostic(
                diagnostics,
                "invalid_log_return",
                "Skipped non-positive equity ratios for log returns",
            )
        return np.log(ratio[positive])
    return ratio - 1.0


def _annualized(value: float, factor: float | None) -> float:
    return value if factor is None else value * math.sqrt(factor)


@dataclass(frozen=True)
class Metrics:
    initial_deposit: float = 0.0
    final_balance: float = 0.0
    final_equity: float | None = None
    net_profit: float = 0.0
    return_on_capital_pct: float | None = None
    cagr_pct: float | None = None
    annual_return_pct: float | None = None

    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    break_even_trades: int = 0
    win_rate_pct: float | None = None

    gross_profit: float = 0.0
    gross_loss: float = 0.0
    profit_factor: float | None = None
    payoff_ratio: float | None = None
    expectancy: float | None = None
    average_win: float | None = None
    average_loss: float | None = None
    average_trade: float | None = None
    largest_win: float | None = None
    largest_loss: float | None = None

    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0
    max_consecutive_wins_money: float = 0.0
    max_consecutive_losses_money: float = 0.0

    max_drawdown_money: float = 0.0
    max_drawdown_pct: float | None = None
    average_drawdown_money: float | None = None
    longest_drawdown_points: int = 0
    max_runup_money: float = 0.0
    max_runup_pct: float | None = None
    ulcer_index_pct: float | None = None

    custom_trade_event_sharpe: float | None = None
    sharpe_ratio: float | None = None
    sortino_ratio: float | None = None
    std_deviation: float | None = None
    recovery_factor: float | None = None
    calmar_ratio: float | None = None
    mar_ratio: float | None = None
    omega_ratio: float | None = None
    gain_to_pain_ratio: float | None = None

    avg_holding_hours: float | None = None
    first_trade_time: str | None = None
    last_trade_time: str | None = None
    trading_years: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_metrics(
    report: Report,
    *,
    primary_curve: CurveSeries | None = None,
    config: AnalysisConfig | None = None,
    diagnostics: list[Diagnostic] | None = None,
) -> Metrics:
    """Compute the core deterministic metric set.

    ``compute_metrics(report)`` remains supported for the original library
    API; the full platform calls it with an explicitly selected primary curve.
    """

    config = config or AnalysisConfig()
    diagnostics = diagnostics if diagnostics is not None else []
    ordered = report.ordered_trades()
    profits = np.asarray([trade.profit for trade in ordered], dtype=float)
    curve = primary_curve or reconstructed_curve(report)
    values = curve.as_numpy()
    initial = float(report.initial_deposit)
    final = curve.final_value
    net = final - initial
    result: dict[str, Any] = {
        "initial_deposit": initial,
        "final_balance": final,
        "final_equity": final if curve.basis == "equity" else None,
        "net_profit": net,
    }
    result["return_on_capital_pct"] = _safe_div(net, initial) * 100 if initial else None

    count = int(profits.size)
    wins = profits[profits > 0]
    losses = profits[profits < 0]
    result.update(
        total_trades=count,
        winning_trades=int(wins.size),
        losing_trades=int(losses.size),
        break_even_trades=int(np.count_nonzero(profits == 0)),
        win_rate_pct=(float(wins.size / count * 100) if count else None),
        gross_profit=float(wins.sum()) if wins.size else 0.0,
        gross_loss=float(losses.sum()) if losses.size else 0.0,
        profit_factor=_safe_div(float(wins.sum()), abs(float(losses.sum()))) if losses.size else None,
        average_win=float(wins.mean()) if wins.size else None,
        average_loss=float(losses.mean()) if losses.size else None,
        expectancy=float(profits.mean()) if count else None,
        average_trade=float(profits.mean()) if count else None,
        largest_win=float(wins.max()) if wins.size else None,
        largest_loss=float(losses.min()) if losses.size else None,
    )
    if wins.size and losses.size:
        result["payoff_ratio"] = _safe_div(float(wins.mean()), abs(float(losses.mean())))
    else:
        result["payoff_ratio"] = None
    result.update(_streaks(profits))

    dd_money, dd_pct, max_dd_money, max_dd_pct, avg_dd, longest_dd = _drawdown(values)
    result.update(
        max_drawdown_money=max_dd_money,
        max_drawdown_pct=max_dd_pct * 100 if values.size else None,
        average_drawdown_money=avg_dd if values.size else None,
        longest_drawdown_points=longest_dd,
    )
    if values.size:
        troughs = values - np.minimum.accumulate(values)
        result["max_runup_money"] = float(troughs.max(initial=0.0))
        running_min = np.minimum.accumulate(values)
        with np.errstate(divide="ignore", invalid="ignore"):
            runup_pct = np.divide(troughs, running_min, out=np.zeros_like(troughs), where=running_min != 0)
        result["max_runup_pct"] = float(runup_pct.max(initial=0.0) * 100)
        result["ulcer_index_pct"] = float(np.sqrt(np.mean((dd_pct * 100) ** 2)))
    else:
        result["max_runup_money"] = 0.0
        result["max_runup_pct"] = None
        result["ulcer_index_pct"] = None

    # Custom trade-event Sharpe is intentionally based on closed-position
    # events, not an irregular mixture of order/deal events.
    trade_curve = reconstructed_curve(report)
    returns = _returns(trade_curve.as_numpy(), config, diagnostics)
    if returns.size > 1:
        rf = float(config.sharpe.risk_free_rate)
        excess = returns - rf
        std = float(np.std(excess, ddof=config.sharpe.ddof))
        result["std_deviation"] = std
        if std > 0:
            sharpe = float(np.mean(excess) / std)
            result["custom_trade_event_sharpe"] = _annualized(sharpe, config.sharpe.annualization_factor)
            result["sharpe_ratio"] = result["custom_trade_event_sharpe"]
        downside = excess[excess < 0]
        downside_dev = float(np.sqrt(np.mean(np.square(downside)))) if downside.size else 0.0
        if downside_dev > 0:
            result["sortino_ratio"] = _annualized(float(np.mean(excess) / downside_dev), config.sharpe.annualization_factor)
        else:
            result["sortino_ratio"] = None
    else:
        result.update(std_deviation=None, custom_trade_event_sharpe=None, sharpe_ratio=None, sortino_ratio=None)
        add_diagnostic(diagnostics, "undefined_sharpe", "Custom trade-event Sharpe is undefined with fewer than two valid returns")

    years = None
    if curve.timestamps and len(curve.timestamps) > 1:
        seconds = (curve.timestamps[-1] - curve.timestamps[0]).total_seconds()
        years = seconds / (365.2425 * 24 * 3600)
    if years and years > 0 and initial > 0 and final > 0:
        cagr = (final / initial) ** (1.0 / years) - 1.0
        result["cagr_pct"] = cagr * 100
        result["annual_return_pct"] = net / initial / years * 100
    else:
        result["cagr_pct"] = None
        result["annual_return_pct"] = None
    if max_dd_pct > 0 and result.get("cagr_pct") is not None:
        result["calmar_ratio"] = result["cagr_pct"] / (max_dd_pct * 100)
        result["mar_ratio"] = result["annual_return_pct"] / (max_dd_pct * 100)
    else:
        result["calmar_ratio"] = None
        result["mar_ratio"] = None
        add_diagnostic(diagnostics, "undefined_calmar", "Calmar and MAR are undefined when maximum drawdown is zero")
    result["recovery_factor"] = _safe_div(net, max_dd_money)
    result["omega_ratio"] = _safe_div(float(wins.sum()), abs(float(losses.sum()))) if losses.size else None
    result["gain_to_pain_ratio"] = result["omega_ratio"]

    durations = [trade.duration_seconds for trade in ordered if trade.duration_seconds is not None and trade.duration_seconds >= 0]
    result["avg_holding_hours"] = float(np.mean(durations) / 3600) if durations else None
    if ordered:
        first = ordered[0].open_time or ordered[0].close_time
        last = ordered[-1].close_time or ordered[-1].open_time
        result["first_trade_time"] = first.isoformat() if first else None
        result["last_trade_time"] = last.isoformat() if last else None
        result["trading_years"] = years
    else:
        result["first_trade_time"] = result["last_trade_time"] = None
        result["trading_years"] = None
    return Metrics(**result)
