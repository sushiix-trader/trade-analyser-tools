"""Deterministic daily and weekly realized-profit correlation analytics."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Mapping, Sequence

import numpy as np

from .diagnostics import Diagnostic
from .matrices import AnalysisMatrix
from .models import Trade


@dataclass(frozen=True)
class DailyProfitPoint:
    """A realized-profit observation labelled by its period start date."""

    day: date
    profit: float

    @property
    def period_start(self) -> date:
        """Return the date labelling this daily or weekly observation."""

        return self.day

    def to_dict(self) -> dict[str, Any]:
        return {"day": self.day.isoformat(), "profit": self.profit}


@dataclass(frozen=True)
class DailyProfitCorrelationResult:
    """Aligned realized-profit series and their labelled correlation matrix.

    The class name is retained for API compatibility. ``frequency`` identifies
    whether ``day`` labels individual report-timezone days or Monday-starting
    calendar weeks aggregated from the daily series.
    """

    scope: str
    timezone: str | None
    series: Mapping[str, tuple[DailyProfitPoint, ...]]
    allocated_series: Mapping[str, tuple[DailyProfitPoint, ...]]
    matrix: AnalysisMatrix
    observations: int
    active_start: date | None
    active_end: date | None
    included_dates: tuple[date, ...]
    warnings: tuple[Diagnostic, ...] = ()
    frequency: str = "daily"

    @property
    def raw_series(self) -> Mapping[str, tuple[DailyProfitPoint, ...]]:
        return self.series

    @property
    def period_start_dates(self) -> tuple[date, ...]:
        """Return the labels for the included daily or weekly observations."""

        return self.included_dates

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "timezone": self.timezone,
            "frequency": self.frequency,
            "series": {
                name: [point.to_dict() for point in points]
                for name, points in sorted(self.series.items())
            },
            "allocated_series": {
                name: [point.to_dict() for point in points]
                for name, points in sorted(self.allocated_series.items())
            },
            "matrix": self.matrix.to_dict(),
            "observations": self.observations,
            "active_start": self.active_start.isoformat() if self.active_start else None,
            "active_end": self.active_end.isoformat() if self.active_end else None,
            "included_dates": [item.isoformat() for item in self.included_dates],
            "warnings": [item.to_dict() for item in self.warnings],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DailyProfitCorrelationResult":
        def points(data: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, tuple[DailyProfitPoint, ...]]:
            return {
                name: tuple(
                    DailyProfitPoint(date.fromisoformat(item["day"]), float(item["profit"]))
                    for item in values
                )
                for name, values in data.items()
            }

        matrix_data = payload["matrix"]
        matrix = AnalysisMatrix(
            tuple(matrix_data["row_labels"]),
            tuple(matrix_data["column_labels"]),
            tuple(tuple(row) for row in matrix_data["values"]),
            matrix_data["value_type"],
        )
        return cls(
            scope=payload["scope"],
            timezone=payload.get("timezone"),
            series=points(payload.get("series", {})),
            allocated_series=points(payload.get("allocated_series", {})),
            matrix=matrix,
            observations=int(payload.get("observations", 0)),
            active_start=date.fromisoformat(payload["active_start"]) if payload.get("active_start") else None,
            active_end=date.fromisoformat(payload["active_end"]) if payload.get("active_end") else None,
            included_dates=tuple(date.fromisoformat(item) for item in payload.get("included_dates", [])),
            warnings=tuple(Diagnostic(**item) for item in payload.get("warnings", [])),
            frequency=payload.get("frequency", "daily"),
        )


@dataclass(frozen=True)
class CorrelationResults:
    """Daily and weekly realized-profit correlations for one eager portfolio."""

    daily_profit: DailyProfitCorrelationResult
    by_period: Mapping[str, DailyProfitCorrelationResult] = field(default_factory=dict)
    weekly_profit: DailyProfitCorrelationResult | None = None
    weekly_by_period: Mapping[str, DailyProfitCorrelationResult] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "daily_profit": self.daily_profit.to_dict(),
            "weekly_profit": self.weekly_profit.to_dict() if self.weekly_profit else None,
            "by_period": {
                name: value.to_dict() for name, value in sorted(self.by_period.items())
            },
            "weekly_by_period": {
                name: value.to_dict() for name, value in sorted(self.weekly_by_period.items())
            },
        }

    @classmethod
    def from_dict(
        cls,
        payload: dict[str, Any],
        *,
        minimum_observations: int = 2,
        warning_observations: int = 12,
    ) -> "CorrelationResults":
        daily = DailyProfitCorrelationResult.from_dict(payload["daily_profit"])
        weekly_data = payload.get("weekly_profit")
        weekly = (
            DailyProfitCorrelationResult.from_dict(weekly_data)
            if isinstance(weekly_data, dict)
            else build_weekly_profit_correlation_from_daily(
                daily,
                minimum_observations=minimum_observations,
                warning_observations=warning_observations,
            )
        )
        by_period = {
            name: DailyProfitCorrelationResult.from_dict(value)
            for name, value in payload.get("by_period", {}).items()
        }
        weekly_by_period_data = payload.get("weekly_by_period", {})
        weekly_by_period = {
            name: DailyProfitCorrelationResult.from_dict(value)
            for name, value in weekly_by_period_data.items()
        }
        if not weekly_by_period:
            weekly_by_period = {
                name: build_weekly_profit_correlation_from_daily(
                    value,
                    minimum_observations=minimum_observations,
                    warning_observations=warning_observations,
                )
                for name, value in by_period.items()
            }
        return cls(
            daily_profit=daily,
            weekly_profit=weekly,
            by_period=by_period,
            weekly_by_period=weekly_by_period,
        )


def _profit_by_day(trades: Sequence[Trade]) -> dict[date, float]:
    result: dict[date, float] = {}
    for trade in trades:
        if trade.close_time is None:
            continue
        # Trade.profit is the canonical normalized net position profit. The
        # parsers fold swap/commission into it when the source supplied gross
        # profit, so adding costs here would double count them.
        day = trade.close_time.date()
        result[day] = result.get(day, 0.0) + float(trade.profit)
    return result


def _correlate_aligned_series(
    names: Sequence[str],
    raw_maps: Mapping[str, Mapping[date, float]],
    allocated_maps: Mapping[str, Mapping[date, float]],
    included_dates: Sequence[date],
    *,
    scope: str,
    timezone: str | None,
    frequency: str,
    minimum_observations: int,
    warning_observations: int,
    active_start: date | None,
    active_end: date | None,
    diagnostics: Sequence[Diagnostic] = (),
) -> DailyProfitCorrelationResult:
    period_label = "daily" if frequency == "daily" else "weekly"
    diagnostics_list = list(diagnostics)
    periods = tuple(sorted(included_dates))
    observations = len(periods)
    if observations < minimum_observations:
        diagnostics_list.append(Diagnostic(
            f"insufficient_{period_label}_profit_observations",
            f"{period_label.title()} profit correlation is undefined for insufficient overlapping observations",
            context={"scope": scope, "observations": observations},
        ))
    elif observations < warning_observations:
        diagnostics_list.append(Diagnostic(
            f"low_{period_label}_profit_observations",
            f"{period_label.title()} profit correlation uses fewer observations than the recommended count",
            context={"scope": scope, "observations": observations},
        ))

    raw_series = {
        name: tuple(
            DailyProfitPoint(period, float(raw_maps[name].get(period, 0.0)))
            for period in periods
        )
        for name in names
    }
    allocated_series = {
        name: tuple(
            DailyProfitPoint(period, float(allocated_maps[name].get(period, 0.0)))
            for period in periods
        )
        for name in names
    }
    correlation = np.full((len(names), len(names)), np.nan)
    if observations >= minimum_observations:
        arrays = [
            np.asarray([point.profit for point in raw_series[name]], dtype=float)
            for name in names
        ]
        for left, left_values in enumerate(arrays):
            for right, right_values in enumerate(arrays):
                if left == right:
                    correlation[left, right] = 1.0
                    continue
                left_std = float(np.std(left_values, ddof=1)) if observations > 1 else 0.0
                right_std = float(np.std(right_values, ddof=1)) if observations > 1 else 0.0
                if left_std == 0.0 or right_std == 0.0:
                    diagnostics_list.append(Diagnostic(
                        f"undefined_{period_label}_profit_correlation",
                        f"{period_label.title()} profit correlation is undefined when a series has zero variance",
                        context={"scope": scope, "left": names[left], "right": names[right]},
                    ))
                else:
                    correlation[left, right] = float(np.corrcoef(left_values, right_values)[0, 1])
    matrix = AnalysisMatrix.from_array(
        names,
        names,
        correlation,
        f"{frequency}_profit_correlation",
    )
    return DailyProfitCorrelationResult(
        scope=scope,
        timezone=timezone,
        series=raw_series,
        allocated_series=allocated_series,
        matrix=matrix,
        observations=observations,
        active_start=active_start,
        active_end=active_end,
        included_dates=periods,
        warnings=tuple(diagnostics_list),
        frequency=frequency,
    )


def build_daily_profit_correlation(
    members: Sequence[tuple[str, Sequence[Trade], float]],
    *,
    scope: str,
    timezone: str | None,
    minimum_observations: int = 2,
    warning_observations: int = 12,
) -> DailyProfitCorrelationResult:
    """Build raw and allocated daily profit series over active-date overlap."""

    names = tuple(name for name, _, _ in members)
    raw_maps = {name: _profit_by_day(trades) for name, trades, _ in members}
    allocated_maps = {
        name: {day: value * float(scale) for day, value in raw_maps[name].items()}
        for name, _, scale in members
    }
    diagnostics: list[Diagnostic] = []
    ranges = [
        (min(values), max(values))
        for values in raw_maps.values()
        if values
    ]
    if len(ranges) != len(members):
        diagnostics.append(Diagnostic(
            "daily_profit_no_active_period",
            "Daily profit correlation is undefined when a strategy has no completed closing dates",
            context={"scope": scope},
        ))
        return _empty_result(names, scope, timezone, diagnostics, frequency="daily")
    active_start = max(item[0] for item in ranges)
    active_end = min(item[1] for item in ranges)
    if active_start > active_end:
        diagnostics.append(Diagnostic(
            "daily_profit_no_overlap",
            "Daily profit correlation is undefined because strategies have no overlapping active dates",
            context={"scope": scope},
        ))
        return _empty_result(names, scope, timezone, diagnostics, frequency="daily")
    included_dates = tuple(sorted({
        day
        for values in raw_maps.values()
        for day in values
        if active_start <= day <= active_end
    }))
    return _correlate_aligned_series(
        names,
        raw_maps,
        allocated_maps,
        included_dates,
        scope=scope,
        timezone=timezone,
        frequency="daily",
        minimum_observations=minimum_observations,
        warning_observations=warning_observations,
        active_start=active_start,
        active_end=active_end,
        diagnostics=diagnostics,
    )


def _week_start(day: date) -> date:
    """Return the Monday starting the report-timezone calendar week."""

    return day - timedelta(days=day.weekday())


def _aggregate_weekly_points(
    points: Mapping[str, Sequence[DailyProfitPoint]],
) -> dict[str, dict[date, float]]:
    result: dict[str, dict[date, float]] = {}
    for name, values in points.items():
        weekly: dict[date, float] = {}
        for point in values:
            week = _week_start(point.day)
            weekly[week] = weekly.get(week, 0.0) + float(point.profit)
        result[name] = weekly
    return result


def build_weekly_profit_correlation_from_daily(
    daily: DailyProfitCorrelationResult,
    *,
    minimum_observations: int = 2,
    warning_observations: int = 12,
) -> DailyProfitCorrelationResult:
    """Aggregate an aligned daily result into Monday-starting calendar weeks."""

    names = tuple(daily.series)
    if not daily.included_dates:
        reason = "no_active_period" if any(
            item.code == "daily_profit_no_active_period" for item in daily.warnings
        ) else "no_overlap"
        message = (
            "Weekly profit correlation is undefined when a strategy has no completed closing dates"
            if reason == "no_active_period"
            else "Weekly profit correlation is undefined because strategies have no overlapping active dates"
        )
        code = f"weekly_profit_{reason}"
        return _empty_result(
            names,
            daily.scope,
            daily.timezone,
            [Diagnostic(code, message, context={"scope": daily.scope})],
            frequency="weekly",
        )

    raw_maps = _aggregate_weekly_points(daily.series)
    allocated_maps = _aggregate_weekly_points(daily.allocated_series)
    included_weeks = tuple(sorted({
        week
        for values in raw_maps.values()
        for week in values
    }))
    return _correlate_aligned_series(
        names,
        raw_maps,
        allocated_maps,
        included_weeks,
        scope=daily.scope,
        timezone=daily.timezone,
        frequency="weekly",
        minimum_observations=minimum_observations,
        warning_observations=warning_observations,
        active_start=included_weeks[0] if included_weeks else None,
        active_end=included_weeks[-1] if included_weeks else None,
    )


def build_weekly_profit_correlation(
    members: Sequence[tuple[str, Sequence[Trade], float]],
    *,
    scope: str,
    timezone: str | None,
    minimum_observations: int = 2,
    warning_observations: int = 12,
) -> DailyProfitCorrelationResult:
    """Build weekly profit correlation by aggregating the aligned daily series."""

    daily = build_daily_profit_correlation(
        members,
        scope=scope,
        timezone=timezone,
        minimum_observations=minimum_observations,
        warning_observations=warning_observations,
    )
    return build_weekly_profit_correlation_from_daily(
        daily,
        minimum_observations=minimum_observations,
        warning_observations=warning_observations,
    )


def _empty_result(
    names: Sequence[str],
    scope: str,
    timezone: str | None,
    diagnostics: Sequence[Diagnostic],
    *,
    frequency: str = "daily",
) -> DailyProfitCorrelationResult:
    matrix = AnalysisMatrix(
        tuple(names),
        tuple(names),
        tuple(tuple(None for _ in names) for _ in names),
        f"{frequency}_profit_correlation",
    )
    return DailyProfitCorrelationResult(
        scope=scope,
        timezone=timezone,
        series={name: () for name in names},
        allocated_series={name: () for name in names},
        matrix=matrix,
        observations=0,
        active_start=None,
        active_end=None,
        included_dates=(),
        warnings=tuple(diagnostics),
        frequency=frequency,
    )
