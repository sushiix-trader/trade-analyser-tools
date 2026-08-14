"""Deterministic balance/equity curve construction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Sequence

import numpy as np

from .models import AccountPoint, Report, Trade


@dataclass(frozen=True)
class EquityCurve:
    """Legacy numeric curve used by the simulation helpers."""

    equity: np.ndarray
    initial_deposit: float

    @classmethod
    def from_profits(
        cls, profits: Sequence[float], initial_deposit: float
    ) -> "EquityCurve":
        profits_arr = np.asarray(profits, dtype=float)
        equity = np.empty(profits_arr.size + 1, dtype=float)
        equity[0] = initial_deposit
        if profits_arr.size:
            np.cumsum(profits_arr, out=equity[1:])
            equity[1:] += initial_deposit
        return cls(equity=equity, initial_deposit=float(initial_deposit))

    @property
    def final_balance(self) -> float:
        return float(self.equity[-1]) if self.equity.size else self.initial_deposit

    @property
    def net_profit(self) -> float:
        return self.final_balance - self.initial_deposit


@dataclass(frozen=True)
class CurveSeries:
    """Immutable timestamped balance/equity observations."""

    timestamps: tuple[datetime, ...]
    values: tuple[float, ...]
    source: str
    basis: str
    initial_value: float

    @property
    def empty(self) -> bool:
        return not self.values

    @property
    def final_value(self) -> float:
        return self.values[-1] if self.values else self.initial_value

    @property
    def net_change(self) -> float:
        return self.final_value - self.initial_value

    def as_numpy(self) -> np.ndarray:
        return np.asarray(self.values, dtype=float)


def _initial_timestamp(trades: Sequence[Trade]) -> datetime:
    candidates = [t.open_time or t.close_time for t in trades]
    candidates = [value for value in candidates if value is not None]
    return min(candidates) if candidates else datetime(1970, 1, 1)


def reconstructed_curve(report: Report) -> CurveSeries:
    ordered = report.ordered_trades()
    if not ordered:
        return CurveSeries((), (float(report.initial_deposit),), "reconstructed_closed_positions", "balance", report.initial_deposit)
    timestamps = [_initial_timestamp(ordered)]
    values = [float(report.initial_deposit)]
    current = float(report.initial_deposit)
    for trade in ordered:
        current += trade.profit
        timestamps.append(trade.close_time or timestamps[-1] + timedelta(microseconds=1))
        values.append(current)
    return CurveSeries(
        tuple(timestamps), tuple(values), "reconstructed_closed_positions", "balance", report.initial_deposit
    )


def source_balance_curve(report: Report) -> CurveSeries | None:
    points = sorted(report.source_balance_points, key=lambda point: (point.timestamp, point.source_id or ""))
    if not points:
        return None
    initial = report.initial_deposit
    timestamps = [point.timestamp for point in points if point.balance is not None]
    values = [float(point.balance) for point in points if point.balance is not None]
    if not values:
        return None
    return CurveSeries(tuple(timestamps), tuple(values), "source_report", "balance", initial)


def source_equity_curve(report: Report) -> CurveSeries | None:
    points = sorted(report.source_equity_points, key=lambda point: (point.timestamp, point.source_id or ""))
    if not points:
        return None
    values = [point.equity for point in points if point.equity is not None]
    if not values:
        return None
    timestamps = [point.timestamp for point in points if point.equity is not None]
    return CurveSeries(tuple(timestamps), tuple(float(v) for v in values), "source_report", "equity", report.initial_deposit)


def build_equity(profits: Sequence[float], initial_deposit: float) -> EquityCurve:
    """Legacy convenience constructor."""

    return EquityCurve.from_profits(profits, initial_deposit)
