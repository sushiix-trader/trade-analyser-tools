"""Extensible, deterministic performance and risk metrics.

The metric names intentionally separate values that can be recreated from the
canonical closed-position stream from values that require optional inputs such
as OHLC bars or a user-defined R-risk model.  Unsupported values are ``None``
and receive a structured diagnostic rather than being guessed.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal

import numpy as np

from .config import AnalysisConfig
from .diagnostics import Diagnostic, add_diagnostic
from .equity import CurveSeries, reconstructed_curve
from .models import Report, Trade


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
    win_runs: list[int] = []
    loss_runs: list[int] = []
    for profit in profits:
        if profit > 0:
            if current_losses:
                loss_runs.append(current_losses)
            current_wins += 1
            current_win_money += float(profit)
            current_losses = 0
            current_loss_money = 0.0
            if current_wins > best_wins:
                best_wins = current_wins
                best_win_money = current_win_money
        elif profit < 0:
            if current_wins:
                win_runs.append(current_wins)
            current_losses += 1
            current_loss_money += float(profit)
            current_wins = 0
            current_win_money = 0.0
            if current_losses > best_losses:
                best_losses = current_losses
                best_loss_money = current_loss_money
        else:
            if current_wins:
                win_runs.append(current_wins)
            if current_losses:
                loss_runs.append(current_losses)
            current_wins = current_losses = 0
            current_win_money = current_loss_money = 0.0
    if current_wins:
        win_runs.append(current_wins)
    if current_losses:
        loss_runs.append(current_losses)
    return {
        "max_consecutive_wins": best_wins,
        "max_consecutive_losses": best_losses,
        "max_consecutive_wins_money": best_win_money,
        "max_consecutive_losses_money": best_loss_money,
        "average_consecutive_wins": float(np.mean(win_runs)) if win_runs else None,
        "average_consecutive_losses": float(np.mean(loss_runs)) if loss_runs else None,
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


def _period_key(timestamp: datetime, kind: Literal["day", "month", "year"]) -> object:
    if kind == "day":
        return timestamp.date()
    if kind == "month":
        return (timestamp.year, timestamp.month)
    return timestamp.year


def _first_period(timestamp: datetime, kind: Literal["day", "month", "year"]) -> object:
    if kind == "day":
        return timestamp.date()
    if kind == "month":
        return (timestamp.year, timestamp.month)
    return timestamp.year


def _next_period(key: object, kind: Literal["day", "month", "year"]) -> object:
    if kind == "day":
        return key + timedelta(days=1)  # type: ignore[operator]
    if kind == "month":
        year, month = key  # type: ignore[misc]
        return (year + 1, 1) if month == 12 else (year, month + 1)
    return key + 1  # type: ignore[operator]


def _period_stats(
    curve: CurveSeries,
    kind: Literal["day", "month", "year"],
) -> list[tuple[object, float, float | None, float]]:
    """Return calendar-period end values, returns, and P&L.

    Missing calendar periods are retained as zero-return periods.  This is
    important for a monthly table and makes the averages explicit and
    reproducible instead of depending on how often the strategy traded.
    """

    if not curve.timestamps:
        return []
    end_values: dict[object, float] = {}
    for timestamp, value in zip(curve.timestamps, curve.values):
        end_values[_period_key(timestamp, kind)] = float(value)
    start = _first_period(curve.timestamps[0], kind)
    end = _period_key(curve.timestamps[-1], kind)
    previous = float(curve.values[0])
    result: list[tuple[object, float, float | None, float]] = []
    current = start
    while current <= end:  # type: ignore[operator]
        value = end_values.get(current, previous)
        period_return = _safe_div(value, previous)
        period_return = period_return - 1.0 if period_return is not None else None
        result.append((current, previous, period_return, value - previous))
        previous = value
        current = _next_period(current, kind)
    return result


def _trade_event_returns(trades: list[Trade], initial: float) -> np.ndarray:
    current = float(initial)
    values: list[float] = []
    for trade in trades:
        if current != 0:
            values.append(float(trade.profit) / current)
        current += float(trade.profit)
    return np.asarray(values, dtype=float)


def _run_test(profits: np.ndarray) -> tuple[float | None, float | None]:
    labels = [profit > 0 for profit in profits if profit != 0]
    wins = sum(labels)
    losses = len(labels) - wins
    if len(labels) < 2 or wins == 0 or losses == 0:
        return None, None
    runs = 1 + sum(left != right for left, right in zip(labels, labels[1:]))
    n = wins + losses
    expected = 1.0 + (2.0 * wins * losses / n)
    variance = (2.0 * wins * losses * (2.0 * wins * losses - n)) / (n * n * (n - 1))
    if variance <= 0:
        return None, None
    z_score = (runs - expected) / math.sqrt(variance)
    two_sided_p = math.erfc(abs(z_score) / math.sqrt(2.0))
    confidence_pct = (1.0 - two_sided_p) * 100.0
    return float(z_score), float(confidence_pct)


def _stagnation(curve: CurveSeries) -> tuple[float | None, float | None]:
    if len(curve.timestamps) < 2:
        return None, None
    peak = float(curve.values[0])
    peak_time = curve.timestamps[0]
    underwater = False
    longest_seconds = 0.0
    for timestamp, value in zip(curve.timestamps[1:], curve.values[1:]):
        value = float(value)
        if value < peak:
            underwater = True
            continue
        if underwater:
            longest_seconds = max(longest_seconds, (timestamp - peak_time).total_seconds())
        peak = value
        peak_time = timestamp
        underwater = False
    if underwater:
        longest_seconds = max(
            longest_seconds,
            (curve.timestamps[-1] - peak_time).total_seconds(),
        )
    total_seconds = (curve.timestamps[-1] - curve.timestamps[0]).total_seconds()
    if total_seconds <= 0:
        return longest_seconds / 86400.0, None
    return longest_seconds / 86400.0, longest_seconds / total_seconds * 100.0


def _exposure(trades: list[Trade]) -> tuple[int, float]:
    events: list[tuple[datetime, int, float]] = []
    for trade in trades:
        if trade.open_time is None or trade.close_time is None:
            continue
        if trade.close_time < trade.open_time:
            continue
        # Closures are applied before entries at the same timestamp, avoiding
        # an artificial overlap when one position rolls into another.
        events.append((trade.open_time, 1, float(trade.volume)))
        events.append((trade.close_time, -1, -float(trade.volume)))
    active_count = 0
    active_lots = 0.0
    max_count = 0
    max_lots = 0.0
    for timestamp, delta_count, delta_lots in sorted(events, key=lambda item: (item[0], item[1])):
        active_count += delta_count
        active_lots += delta_lots
        active_count = max(active_count, 0)
        active_lots = max(active_lots, 0.0)
        max_count = max(max_count, active_count)
        max_lots = max(max_lots, active_lots)
    return max_count, max_lots


@dataclass(frozen=True)
class Metrics:
    initial_deposit: float = 0.0
    final_balance: float = 0.0
    final_equity: float | None = None
    net_profit: float = 0.0
    return_on_capital_pct: float | None = None
    total_profit_pct: float | None = None
    cagr_pct: float | None = None
    annual_return_pct: float | None = None
    average_annual_profit_pct: float | None = None
    average_annual_return_pct: float | None = None
    average_monthly_profit_pct: float | None = None
    average_monthly_return_pct: float | None = None
    average_daily_profit_pct: float | None = None
    average_daily_return_pct: float | None = None

    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    break_even_trades: int = 0
    win_rate_pct: float | None = None
    wins_losses_ratio: float | None = None

    gross_profit: float = 0.0
    gross_loss: float = 0.0
    gross_profit_pct: float | None = None
    gross_loss_pct: float | None = None
    profit_factor: float | None = None
    payoff_ratio: float | None = None
    payout_ratio: float | None = None
    expectancy: float | None = None
    average_win: float | None = None
    average_loss: float | None = None
    average_trade: float | None = None
    average_win_pct: float | None = None
    average_loss_pct: float | None = None
    average_trade_pct: float | None = None
    largest_win: float | None = None
    largest_loss: float | None = None
    largest_win_pct: float | None = None
    largest_loss_pct: float | None = None

    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0
    max_consecutive_wins_money: float = 0.0
    max_consecutive_losses_money: float = 0.0
    average_consecutive_wins: float | None = None
    average_consecutive_losses: float | None = None

    max_drawdown_money: float = 0.0
    max_drawdown_pct: float | None = None
    average_drawdown_money: float | None = None
    longest_drawdown_points: int = 0
    max_runup_money: float = 0.0
    max_runup_pct: float | None = None
    ulcer_index_pct: float | None = None
    return_drawdown_ratio: float | None = None
    annual_return_to_max_drawdown_ratio: float | None = None
    max_stagnation_days: float | None = None
    max_stagnation_pct: float | None = None

    custom_trade_event_sharpe: float | None = None
    sharpe_ratio: float | None = None
    sortino_ratio: float | None = None
    std_deviation: float | None = None
    recovery_factor: float | None = None
    calmar_ratio: float | None = None
    mar_ratio: float | None = None
    omega_ratio: float | None = None
    gain_to_pain_ratio: float | None = None

    ahpr_pct: float | None = None
    average_holding_period_return_pct: float | None = None
    trade_profit_stddev: float | None = None
    trade_profit_stddev_pct: float | None = None
    deviation_pct: float | None = None
    strategy_quality_number: float | None = None
    sqn: float | None = None
    sqn_score: float | None = None
    z_score: float | None = None
    z_probability_pct: float | None = None

    max_position_exposure: int = 0
    max_pos_exposure: int = 0
    max_lots_exposure: float = 0.0
    average_bars_in_trade: float | None = None
    average_bars_in_wins: float | None = None
    average_bars_in_losses: float | None = None
    cancelled_expired_trades: int | None = None
    r_expectancy: float | None = None
    r_expectancy_score: float | None = None

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
    """Compute the deterministic metric set exposed by the platform.

    Percentage fields whose names end in ``_pct`` are percentages, not
    fractions.  Money fields use the report currency.  The screenshot-style
    fields are calculated from completed positions; metrics requiring a bar
    series, cancellation events, or an explicit R-risk model remain undefined
    until that optional input is supplied.
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
    total_return_pct = _safe_div(net, initial) * 100 if initial else None
    result: dict[str, Any] = {
        "initial_deposit": initial,
        "final_balance": final,
        "final_equity": final if curve.basis == "equity" else None,
        "net_profit": net,
        "return_on_capital_pct": total_return_pct,
        "total_profit_pct": total_return_pct,
    }

    count = int(profits.size)
    wins = profits[profits > 0]
    losses = profits[profits < 0]
    result.update(
        total_trades=count,
        winning_trades=int(wins.size),
        losing_trades=int(losses.size),
        break_even_trades=int(np.count_nonzero(profits == 0)),
        win_rate_pct=(float(wins.size / count * 100) if count else None),
        wins_losses_ratio=_safe_div(float(wins.size), float(losses.size)),
        gross_profit=float(wins.sum()) if wins.size else 0.0,
        gross_loss=float(losses.sum()) if losses.size else 0.0,
        gross_profit_pct=_safe_div(float(wins.sum()), initial) * 100 if initial and wins.size else 0.0 if initial else None,
        gross_loss_pct=_safe_div(float(losses.sum()), initial) * 100 if initial and losses.size else 0.0 if initial else None,
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
    result["payout_ratio"] = result["payoff_ratio"]
    for name, value in (
        ("average_win_pct", result["average_win"]),
        ("average_loss_pct", result["average_loss"]),
        ("average_trade_pct", result["average_trade"]),
        ("largest_win_pct", result["largest_win"]),
        ("largest_loss_pct", result["largest_loss"]),
    ):
        result[name] = _safe_div(value, initial) * 100 if value is not None and initial else None
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

    result["return_drawdown_ratio"] = _safe_div(total_return_pct or 0.0, max_dd_pct * 100)

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
        else:
            result["custom_trade_event_sharpe"] = None
            result["sharpe_ratio"] = None
            add_diagnostic(diagnostics, "undefined_sharpe", "Custom trade-event Sharpe is undefined with zero return dispersion")
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
        log_growth = math.log(final / initial) / years
        try:
            cagr = math.expm1(log_growth)
        except OverflowError:
            cagr = None
            add_diagnostic(
                diagnostics,
                "undefined_cagr_overflow",
                "CAGR is undefined because the annualized growth exceeds floating-point range",
            )
        result["cagr_pct"] = cagr * 100 if cagr is not None else None
        result["annual_return_pct"] = net / initial / years * 100
    else:
        result["cagr_pct"] = None
        result["annual_return_pct"] = None
    if max_dd_pct > 0 and result.get("cagr_pct") is not None:
        result["calmar_ratio"] = result["cagr_pct"] / (max_dd_pct * 100)
        result["mar_ratio"] = result["annual_return_pct"] / (max_dd_pct * 100)
        result["annual_return_to_max_drawdown_ratio"] = result["cagr_pct"] / (max_dd_pct * 100)
    else:
        result["calmar_ratio"] = None
        result["mar_ratio"] = None
        result["annual_return_to_max_drawdown_ratio"] = None
        add_diagnostic(diagnostics, "undefined_calmar", "Calmar and MAR are undefined when maximum drawdown is zero")

    result["recovery_factor"] = _safe_div(net, max_dd_money)
    result["omega_ratio"] = _safe_div(float(wins.sum()), abs(float(losses.sum()))) if losses.size else None
    result["gain_to_pain_ratio"] = result["omega_ratio"]

    # Calendar averages use the selected primary curve and include flat
    # calendar periods.  They are deliberately separate from CAGR and the
    # simple annualised net-profit field above.
    daily = _period_stats(curve, "day")
    monthly = _period_stats(curve, "month")
    annual = _period_stats(curve, "year")
    for prefix, periods in (("daily", daily), ("monthly", monthly), ("annual", annual)):
        returns_pct = [period_return * 100 for _, _, period_return, _ in periods if period_return is not None]
        profit_pct = [_safe_div(pnl, initial) * 100 for _, _, _, pnl in periods] if initial else []
        profit_pct = [value for value in profit_pct if value is not None]
        result[f"average_{prefix}_return_pct"] = float(np.mean(returns_pct)) if returns_pct else None
        result[f"average_{prefix}_profit_pct"] = float(np.mean(profit_pct)) if profit_pct else None

    event_returns = _trade_event_returns(ordered, initial)
    result["ahpr_pct"] = float(np.mean(event_returns) * 100) if event_returns.size else None
    result["average_holding_period_return_pct"] = result["ahpr_pct"]

    if count > 1:
        trade_std = float(np.std(profits, ddof=1))
        result["trade_profit_stddev"] = trade_std
        result["trade_profit_stddev_pct"] = _safe_div(trade_std, initial) * 100 if initial else None
        result["deviation_pct"] = result["trade_profit_stddev_pct"]
        result["strategy_quality_number"] = _safe_div(float(profits.mean()), trade_std)
        result["sqn"] = result["strategy_quality_number"] * math.sqrt(count) if result["strategy_quality_number"] is not None else None
        result["sqn_score"] = result["sqn"]
    else:
        result.update(
            trade_profit_stddev=None,
            trade_profit_stddev_pct=None,
            deviation_pct=None,
            strategy_quality_number=None,
            sqn=None,
            sqn_score=None,
        )
        add_diagnostic(diagnostics, "undefined_sqn", "Trade quality and SQN are undefined with fewer than two trades")

    result["z_score"], result["z_probability_pct"] = _run_test(profits)
    if result["z_score"] is None:
        add_diagnostic(diagnostics, "undefined_z_score", "Z-score is undefined without both winning and losing positions")

    result["max_position_exposure"], result["max_lots_exposure"] = _exposure(ordered)
    result["max_pos_exposure"] = result["max_position_exposure"]

    bar_values = [trade.bars for trade in ordered]
    if ordered and all(value is not None for value in bar_values):
        result["average_bars_in_trade"] = float(np.mean(bar_values))
        result["average_bars_in_wins"] = float(np.mean([trade.bars for trade in ordered if trade.is_win])) if wins.size else None
        result["average_bars_in_losses"] = float(np.mean([trade.bars for trade in ordered if trade.is_loss])) if losses.size else None
    else:
        result.update(average_bars_in_trade=None, average_bars_in_wins=None, average_bars_in_losses=None)
        add_diagnostic(diagnostics, "undefined_bars_metrics", "Bar-count metrics require a timeframe/bar data source")

    result["cancelled_expired_trades"] = None
    add_diagnostic(
        diagnostics,
        "undefined_cancelled_expired",
        "Cancelled/expired events are outside the closed-position analysis contract",
    )

    r_values = [trade.r_multiple for trade in ordered]
    if ordered and all(value is not None for value in r_values):
        r_array = np.asarray(r_values, dtype=float)
        result["r_expectancy"] = float(r_array.mean())
        result["r_expectancy_score"] = float(r_array.mean() * math.sqrt(r_array.size))
    else:
        result["r_expectancy"] = None
        result["r_expectancy_score"] = None
        add_diagnostic(diagnostics, "undefined_r_metrics", "R metrics require an explicit per-position R-risk model")

    stagnation_days, stagnation_pct = _stagnation(curve)
    result["max_stagnation_days"] = stagnation_days
    result["max_stagnation_pct"] = stagnation_pct

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
