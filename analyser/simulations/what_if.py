"""What-If scenarios.

Filter or tweak the trade list to answer "what if I had only traded on certain
days / hours / symbols, or skipped my worst trades?" Each scenario returns a
new :class:`Report` that can be fed straight back into :func:`compute_metrics`.
"""

from __future__ import annotations

import copy

from ..models import Report, Trade


def _clone(report: Report, trades: list[Trade]) -> Report:
    new = copy.copy(report)
    new.trades = trades
    return new


def by_hours(report: Report, hours: range | list[int]) -> Report:
    allowed = set(hours)
    return _clone(
        report, [t for t in report.trades if t.open_time.hour in allowed]
    )


def by_weekdays(report: Report, days: list[int]) -> Report:
    """``days`` are Python weekday numbers (Mon=0 .. Sun=6)."""
    allowed = set(days)
    return _clone(
        report, [t for t in report.trades if t.open_time.weekday() in allowed]
    )


def by_symbol(report: Report, symbols: list[str]) -> Report:
    allowed = {s.upper() for s in symbols}
    return _clone(
        report,
        [t for t in report.trades if t.symbol.upper() in allowed],
    )


def remove_worst_percent(report: Report, pct: float) -> Report:
    """Drop the worst ``pct``% of trades by net profit."""
    if not report.trades:
        return _clone(report, [])
    ordered = sorted(report.trades, key=lambda t: t.profit)
    keep = max(0, len(ordered) - int(round(len(ordered) * pct / 100.0)))
    return _clone(report, ordered[-keep:] if keep else [])


def add_commission(report: Report, per_trade: float) -> Report:
    """Apply an extra flat commission to every trade."""
    adjusted = []
    for t in report.trades:
        new = copy.copy(t)
        new = Trade(
            **{**t.__dict__, "commission": t.commission + per_trade,
               "profit": t.profit - per_trade}
        )
        adjusted.append(new)
    return _clone(report, adjusted)


def in_date_range(report: Report, start, end) -> Report:
    return _clone(
        report,
        [t for t in report.trades if start <= t.open_time <= end],
    )
