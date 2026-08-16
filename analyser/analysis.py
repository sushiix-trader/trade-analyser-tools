"""Eager analysis orchestration and typed result objects."""

from __future__ import annotations

import csv
import hashlib
import io
from calendar import monthrange
from dataclasses import dataclass, field, replace
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from .config import AnalysisConfig, SharpeConfig
from .diagnostics import Diagnostic, ValidationResult
from .filters import (
    AllOf,
    FilterConfig,
    TradeFilter,
    TradeSelection,
    filter_from_dict,
    filter_fingerprint,
    select_trades,
)
from .equity import CurveSeries, reconstructed_curve, source_balance_curve, source_equity_curve
from .load import InputSource, load_report
from .metrics import Metrics, compute_metrics
from .periods import PeriodWindow, SamplePeriodConfig
from .models import Report, Trade
from .serialization import deterministic_json, to_primitive


def _report_from_dict(report_data: dict[str, Any]) -> Report:
    """Restore a canonical report from deterministic serialized data."""

    from .models import AccountPoint, Trade, TradeSide

    def parse_datetime(value: str | None) -> datetime | None:
        return datetime.fromisoformat(value) if value else None

    def restore_trade(data: dict[str, Any]) -> Trade:
        return Trade(
            ticket=data["ticket"],
            symbol=data["symbol"],
            side=TradeSide(data["side"]),
            volume=data["volume"],
            open_time=parse_datetime(data.get("open_time")),
            close_time=parse_datetime(data.get("close_time")),
            open_price=data.get("open_price"),
            close_price=data.get("close_price"),
            profit=data["profit"],
            swap=data.get("swap", 0.0),
            commission=data.get("commission", 0.0),
            sl=data.get("sl"),
            tp=data.get("tp"),
            comment=data.get("comment"),
            magic=data.get("magic"),
            position_id=data.get("position_id"),
            deal_ids=tuple(data.get("deal_ids", ())),
            strategy_id=data.get("strategy_id"),
            source_report_hash=data.get("source_report_hash"),
            allocation_scale=data.get("allocation_scale"),
            bars=data.get("bars"),
            r_multiple=data.get("r_multiple"),
            open_time_inferred=data.get("open_time_inferred", False),
        )

    return Report(
        trades=[restore_trade(item) for item in report_data.get("trades", [])],
        initial_deposit=report_data.get("initial_deposit", 0.0),
        currency=report_data.get("currency", ""),
        broker=report_data.get("broker", ""),
        leverage=report_data.get("leverage", ""),
        source_file=report_data.get("source_file", ""),
        source_format=report_data.get("source_format", ""),
        strategy_name=report_data.get("strategy_name", ""),
        server=report_data.get("server", ""),
        timezone=report_data.get("timezone"),
        source_balance_points=[
            AccountPoint(
                parse_datetime(item["timestamp"]),
                item.get("balance"),
                item.get("equity"),
                item.get("source_id"),
            )
            for item in report_data.get("source_balance_points", [])
        ],
        source_equity_points=[
            AccountPoint(
                parse_datetime(item["timestamp"]),
                item.get("balance"),
                item.get("equity"),
                item.get("source_id"),
            )
            for item in report_data.get("source_equity_points", [])
        ],
        reported_metrics=report_data.get("reported_metrics", {}),
        warnings=report_data.get("warnings", []),
        metadata=report_data.get("metadata", {}),
    )


@dataclass(frozen=True)
class MonthlyPerformance:
    period: str
    pnl: float
    return_on_starting_equity: float | None
    return_on_initial_capital: float | None
    cumulative_return: float | None
    trade_count: int


MONTH_LABELS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


@dataclass(frozen=True)
class MonthlyPerformanceTableRow:
    """One year in a QuantAnalyzer-style monthly performance table.

    Returns are percentages, not fractions.  ``None`` means that the month is
    outside the report's active period; a zero means the month was active but
    flat/no-trade.  ``ytd_return_pct`` is compounded from the first active
    month in that calendar year.
    """

    year: int
    monthly_returns_pct: tuple[float | None, ...]
    ytd_return_pct: float | None
    monthly_pnl: tuple[float | None, ...]
    monthly_trade_counts: tuple[int | None, ...]


@dataclass(frozen=True)
class MonthlyPerformanceTable:
    """Matrix-ready monthly returns with a deterministic YTD calculation."""

    rows: tuple[MonthlyPerformanceTableRow, ...]
    source: str
    basis: str
    month_labels: tuple[str, ...] = MONTH_LABELS

    @property
    def years(self) -> tuple[int, ...]:
        return tuple(row.year for row in self.rows)

    def row(self, year: int) -> MonthlyPerformanceTableRow:
        for item in self.rows:
            if item.year == year:
                return item
        raise KeyError(year)

    def to_dict(self) -> dict[str, Any]:
        return to_primitive(self)


@dataclass(frozen=True)
class MonthlyDrawdown:
    period: str
    month_end_drawdown_amount: float
    month_end_drawdown_percent: float | None
    maximum_intramonth_drawdown_amount: float
    maximum_intramonth_drawdown_percent: float | None
    peak_to_trough_contained_amount: float
    peak_to_trough_contained_percent: float | None
    drawdown_duration_days: float | None


@dataclass(frozen=True)
class CurveResult:
    timestamps: tuple[datetime, ...]
    values: tuple[float, ...]
    source: str
    basis: str
    initial_value: float

    @classmethod
    def from_curve(cls, curve: CurveSeries | None) -> "CurveResult | None":
        if curve is None:
            return None
        return cls(curve.timestamps, curve.values, curve.source, curve.basis, curve.initial_value)


@dataclass(frozen=True)
class PeriodAnalysisResult:
    """Analysis of completed positions assigned to one named sample period."""

    name: str
    window: PeriodWindow
    analysis: "AnalysisResult"
    source_trade_count: int
    selected_trade_count: int
    cross_boundary_trade_count: int
    excluded_trade_count: int
    warnings: tuple[Diagnostic, ...] = ()

    @property
    def report(self) -> Report:
        return self.analysis.report

    @property
    def metrics(self) -> Metrics:
        return self.analysis.metrics

    @property
    def balance(self) -> CurveResult:
        return self.analysis.balance

    @property
    def equity(self) -> CurveResult:
        return self.analysis.equity

    @property
    def monthly(self) -> tuple[MonthlyPerformance, ...]:
        return self.analysis.monthly

    @property
    def monthly_drawdown(self) -> tuple[MonthlyDrawdown, ...]:
        return self.analysis.monthly_drawdown

    @property
    def monthly_performance(self) -> MonthlyPerformanceTable:
        return self.analysis.monthly_performance

    @property
    def validation(self) -> ValidationResult:
        return self.analysis.validation

    @property
    def daily_profit(self):
        """Daily realized net-profit points for this period."""
        return self.analysis.daily_profit

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "window": self.window.to_dict(),
            "analysis": self.analysis.to_dict(),
            "source_trade_count": self.source_trade_count,
            "selected_trade_count": self.selected_trade_count,
            "cross_boundary_trade_count": self.cross_boundary_trade_count,
            "excluded_trade_count": self.excluded_trade_count,
            "warnings": [warning.to_dict() for warning in self.warnings],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PeriodAnalysisResult":
        return cls(
            name=payload["name"],
            window=PeriodWindow.from_dict(payload["window"]),
            analysis=AnalysisResult.from_dict(payload["analysis"]),
            source_trade_count=payload["source_trade_count"],
            selected_trade_count=payload["selected_trade_count"],
            cross_boundary_trade_count=payload["cross_boundary_trade_count"],
            excluded_trade_count=payload["excluded_trade_count"],
            warnings=tuple(Diagnostic(**item) for item in payload.get("warnings", ())),
        )


@dataclass(frozen=True)
class AnalysisResult:
    report: Report
    metrics: Metrics
    reported_metrics: dict[str, Any]
    balance: CurveResult
    equity: CurveResult
    source_balance: CurveResult | None
    source_equity: CurveResult | None
    monthly: tuple[MonthlyPerformance, ...]
    monthly_drawdown: tuple[MonthlyDrawdown, ...]
    monthly_performance: MonthlyPerformanceTable
    by_symbol: dict[str, dict[str, Any]]
    validation: ValidationResult
    warnings: tuple[Diagnostic, ...]
    provenance: dict[str, Any]
    filter_spec: TradeFilter | None = None
    filter_config: FilterConfig | None = None
    selection: TradeSelection | None = None
    source_report: Report | None = None
    source_reported_metrics: dict[str, Any] | None = None
    sample_period_config: SamplePeriodConfig | None = None
    periods: dict[str, PeriodAnalysisResult] = field(default_factory=dict)

    def _apply_filter_after_periods(
        self,
        filter_spec: TradeFilter,
        filter_config: FilterConfig | None = None,
    ) -> "AnalysisResult":
        """Filter a freshly classified period set without changing segment capital."""

        original = self.source_report or self.report
        combined = (
            AllOf(self.filter_spec, filter_spec)
            if self.filter_spec is not None
            else filter_spec
        )
        active_config = filter_config or self.filter_config or FilterConfig()
        selected, selection, filter_diagnostics = select_trades(original, combined, active_config)
        effective_timezone = original.timezone or active_config.report_timezone
        filtered_report = replace(
            original,
            trades=selected,
            timezone=effective_timezone,
            source_balance_points=[],
            source_equity_points=[],
            reported_metrics={},
            warnings=list(original.warnings),
            metadata={
                **original.metadata,
                "filtered": True,
                "filter_fingerprint": filter_fingerprint(combined, active_config),
            },
        )
        config = replace(_analysis_config_from_provenance(self.provenance), sample_periods=None)
        filtered = analyze(filtered_report, config)
        fresh_periods = analyze(original, config).with_sample_periods(self.sample_period_config).periods
        period_results: dict[str, PeriodAnalysisResult] = {}
        for name, period in fresh_periods.items():
            period_selected, period_selection, period_diagnostics = select_trades(
                period.analysis.report,
                combined,
                active_config,
            )
            period_report = replace(
                period.analysis.report,
                trades=period_selected,
                source_balance_points=[],
                source_equity_points=[],
                reported_metrics={},
                warnings=[],
            )
            period_analysis = analyze(period_report, config)
            period_warnings = list(period.warnings)
            period_warnings.extend(period_diagnostics)
            period_warnings.extend(period_analysis.warnings)
            period_results[name] = PeriodAnalysisResult(
                name=name,
                window=period.window,
                analysis=replace(period_analysis, warnings=tuple(period_warnings)),
                source_trade_count=period.source_trade_count,
                selected_trade_count=period_selection.selected_trade_count,
                cross_boundary_trade_count=period.cross_boundary_trade_count,
                excluded_trade_count=period.source_trade_count - period_selection.selected_trade_count,
                warnings=tuple(period_warnings),
            )
        source_validation = self.provenance.get("source_validation") or self.validation.to_dict()
        warnings = list(filtered.warnings)
        warnings.extend(filter_diagnostics)
        warnings.extend(
            diagnostic for period in period_results.values() for diagnostic in period.warnings
        )
        warnings.append(Diagnostic(
            "filtered_reported_metrics_not_applicable",
            "MT5 reported metrics describe the unfiltered report and were not used for filtered validation",
            context={"selected_trade_count": selection.selected_trade_count},
        ))
        source_report_sha256 = (
            self.provenance.get("source_report_sha256")
            or self.provenance.get("input_sha256")
            or original.metadata.get("input_sha256")
            or hashlib.sha256(deterministic_json(to_primitive(original)).encode("utf-8")).hexdigest()
        )
        provenance = dict(filtered.provenance)
        provenance.update({
            "filtered": True,
            "filter_spec": combined.to_dict(),
            "filter_config": active_config.to_dict(),
            "filter_fingerprint": filter_fingerprint(combined, active_config),
            "source_report_sha256": source_report_sha256,
            "source_trade_count": selection.source_trade_count,
            "selected_trade_count": selection.selected_trade_count,
            "excluded_trade_count": selection.excluded_trade_count,
            "source_validation": source_validation,
            "sample_period_config": self.sample_period_config.to_dict(),
            "sample_period_then_filter": True,
        })
        return replace(
            filtered,
            reported_metrics={},
            validation=ValidationResult(
                status="not_applicable",
                checks={"source_validation": source_validation},
                discrepancies=(),
            ),
            warnings=tuple(warnings),
            provenance=provenance,
            filter_spec=combined,
            filter_config=active_config,
            selection=selection,
            source_report=original,
            source_reported_metrics=dict(original.reported_metrics),
            sample_period_config=self.sample_period_config,
            periods=period_results,
        )

    def apply_filters(
        self,
        filter_spec: TradeFilter,
        filter_config: FilterConfig | None = None,
    ) -> "AnalysisResult":
        """Return a filtered analysis evaluated from the original report.

        Chained filters are combined against ``source_report`` rather than
        repeatedly filtering an already-filtered result. Filtered analyses
        always reconstruct equity from selected completed positions.
        """

        if not isinstance(filter_spec, TradeFilter):
            raise TypeError("filter_spec must be a TradeFilter")
        if self.sample_period_config is not None:
            return self._apply_filter_after_periods(filter_spec, filter_config)
        original = self.source_report or self.report
        combined = (
            AllOf(self.filter_spec, filter_spec)
            if self.filter_spec is not None
            else filter_spec
        )
        active_config = filter_config or self.filter_config or FilterConfig()
        selected, selection, filter_diagnostics = select_trades(original, combined, active_config)
        effective_timezone = original.timezone or active_config.report_timezone
        filtered_metadata = dict(original.metadata)
        filtered_metadata.update({
            "filtered": True,
            "filter_fingerprint": filter_fingerprint(combined, active_config),
            "selected_trade_count": selection.selected_trade_count,
            "source_trade_count": selection.source_trade_count,
        })
        report_warnings = list(original.warnings)
        report_warnings.extend(diagnostic.to_dict() for diagnostic in filter_diagnostics)
        report_warnings.append({
            "code": "filtered_source_curves_unavailable",
            "message": "Filtered analysis uses reconstructed closed-position equity; source account curves were not filterable",
            "severity": "warning",
            "context": {},
        })
        filtered_report = replace(
            original,
            trades=selected,
            timezone=effective_timezone,
            source_balance_points=[],
            source_equity_points=[],
            reported_metrics={},
            warnings=report_warnings,
            metadata=filtered_metadata,
        )
        config = _analysis_config_from_provenance(self.provenance)
        filtered = analyze(filtered_report, config=config)
        filtered_curve_source = "filtered_reconstructed_closed_positions"
        filtered_balance = replace(filtered.balance, source=filtered_curve_source) if filtered.balance is not None else None
        filtered_equity = replace(filtered.equity, source=filtered_curve_source) if filtered.equity is not None else None
        filtered_monthly_performance = replace(
            filtered.monthly_performance,
            source=filtered_curve_source,
            basis="balance",
        )
        filtered = replace(
            filtered,
            balance=filtered_balance,
            equity=filtered_equity,
            monthly_performance=filtered_monthly_performance,
        )
        source_validation = self.provenance.get("source_validation") or self.validation.to_dict()
        validation = ValidationResult(
            status="not_applicable",
            checks={"source_validation": source_validation},
            discrepancies=(),
        )
        warnings = list(filtered.warnings)
        warnings.append(Diagnostic(
            "filtered_reported_metrics_not_applicable",
            "MT5 reported metrics describe the unfiltered report and were not used for filtered validation",
            context={"selected_trade_count": selection.selected_trade_count},
        ))
        provenance = dict(filtered.provenance)
        source_report_sha256 = (
            self.provenance.get("source_report_sha256")
            or self.provenance.get("input_sha256")
            or original.metadata.get("input_sha256")
            or hashlib.sha256(deterministic_json(to_primitive(original)).encode("utf-8")).hexdigest()
        )
        provenance.update({
            "filtered": True,
            "filter_spec": combined.to_dict(),
            "filter_config": active_config.to_dict(),
            "filter_fingerprint": filter_fingerprint(combined, active_config),
            "source_report_sha256": source_report_sha256,
            "source_trade_count": selection.source_trade_count,
            "selected_trade_count": selection.selected_trade_count,
            "excluded_trade_count": selection.excluded_trade_count,
            "source_validation": source_validation,
        })
        return replace(
            filtered,
            reported_metrics={},
            validation=validation,
            warnings=tuple(warnings),
            provenance=provenance,
            filter_spec=combined,
            filter_config=active_config,
            selection=selection,
            source_report=original,
            source_reported_metrics=dict(original.reported_metrics),
        )

    @property
    def daily_profit(self):
        """Daily realized net-profit points for the canonical report trades."""
        from .correlation import DailyProfitPoint

        by_day: dict[Any, float] = {}
        for trade in self.report.ordered_trades():
            if trade.close_time is None:
                continue
            day = trade.close_time.date()
            by_day[day] = by_day.get(day, 0.0) + float(trade.profit)
        return tuple(DailyProfitPoint(day, by_day[day]) for day in sorted(by_day))

    def with_sample_periods(self, sample_periods: SamplePeriodConfig) -> "AnalysisResult":
        """Eagerly derive named period analyses from the original report."""

        if not isinstance(sample_periods, SamplePeriodConfig) or not sample_periods.enabled:
            raise ValueError("sample_periods must be an enabled SamplePeriodConfig")
        original = self.source_report or self.report
        source_curve = reconstructed_curve(original)
        source_trade_count = len(original.trades)
        diagnostics = list(self.warnings)
        period_results: dict[str, PeriodAnalysisResult] = {}
        assigned_keys: set[tuple[str, str, str]] = set()
        base_config = _analysis_config_from_provenance(self.provenance)
        base_config = replace(base_config, sample_periods=None)
        for name, window in sample_periods.windows.items():
            selected = [
                trade for trade in original.ordered_trades()
                if window.contains(trade.open_time)
            ]
            selected_keys = {
                (trade.ticket, trade.position_id or "", trade.symbol)
                for trade in selected
            }
            assigned_keys.update(selected_keys)
            period_warnings: list[Diagnostic] = []
            cross_boundary = 0
            for trade in selected:
                if trade.open_time_inferred:
                    period_warnings.append(Diagnostic(
                        "sample_period_inferred_open_time",
                        "A sample-period trade used an inferred open time",
                        context={"period": name, "ticket": trade.ticket},
                    ))
                if trade.close_time is None or not window.contains(trade.close_time):
                    cross_boundary += 1
                    period_warnings.append(Diagnostic(
                        "sample_period_cross_boundary_trade",
                        "A completed position opened in the period but closed outside its boundaries",
                        context={
                            "period": name,
                            "ticket": trade.ticket,
                            "open_time": trade.open_time.isoformat() if trade.open_time else None,
                            "close_time": trade.close_time.isoformat() if trade.close_time else None,
                        },
                    ))
            starting_balance = _balance_before(source_curve, window.start)
            period_report = replace(
                original,
                trades=selected,
                initial_deposit=starting_balance,
                source_balance_points=[],
                source_equity_points=[],
                reported_metrics={},
                warnings=[],
                metadata={
                    **original.metadata,
                    "sample_period": name,
                    "sample_period_start": window.start.isoformat(),
                    "sample_period_end": window.end.isoformat(),
                },
            )
            period_analysis = analyze(period_report, base_config)
            period_warnings.extend(period_analysis.warnings)
            period_analysis = replace(
                period_analysis,
                warnings=tuple(period_warnings),
                provenance={
                    **period_analysis.provenance,
                    "sample_period": window.to_dict(),
                    "sample_period_name": name,
                },
            )
            period_results[name] = PeriodAnalysisResult(
                name=name,
                window=window,
                analysis=period_analysis,
                source_trade_count=source_trade_count,
                selected_trade_count=len(selected),
                cross_boundary_trade_count=cross_boundary,
                excluded_trade_count=source_trade_count - len(selected),
                warnings=tuple(period_warnings),
            )
        outside = source_trade_count - len(assigned_keys)
        if outside:
            diagnostics.append(Diagnostic(
                "sample_period_trades_outside_windows",
                "Some completed positions were outside all named sample periods",
                context={"trade_count": outside},
            ))
        provenance = dict(self.provenance)
        provenance["sample_period_config"] = sample_periods.to_dict()
        return replace(
            self,
            sample_period_config=sample_periods,
            periods=period_results,
            warnings=tuple(diagnostics),
            provenance=provenance,
        )

    def analyze_periods(
        self,
        sample_periods: SamplePeriodConfig,
        *,
        filters: TradeFilter | None = None,
        filter_config: FilterConfig | None = None,
    ) -> "AnalysisResult":
        """Apply sample-period classification before optional trade filtering."""

        result = self.with_sample_periods(sample_periods)
        if filters is None:
            return result
        return result.apply_filters(filters, filter_config)

    def to_dict(self) -> dict[str, Any]:
        payload = to_primitive(self)
        payload["filter_spec"] = self.filter_spec.to_dict() if self.filter_spec is not None else None
        payload["filter_config"] = self.filter_config.to_dict() if self.filter_config is not None else None
        return payload

    def to_json(self) -> str:
        return deterministic_json(self.to_dict())

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "AnalysisResult":
        from .diagnostics import Diagnostic

        def parse_datetime(value: str | None) -> datetime | None:
            return datetime.fromisoformat(value) if value else None

        report_data = payload["report"]
        report = _report_from_dict(report_data)

        def restore_curve(data: dict[str, Any] | None) -> CurveResult | None:
            if data is None:
                return None
            return CurveResult(
                timestamps=tuple(parse_datetime(value) for value in data["timestamps"]),
                values=tuple(data["values"]),
                source=data["source"],
                basis=data["basis"],
                initial_value=data["initial_value"],
            )

        monthly = tuple(MonthlyPerformance(**item) for item in payload.get("monthly", []))
        table_data = payload.get("monthly_performance")
        if table_data is not None:
            monthly_performance = MonthlyPerformanceTable(
                rows=tuple(
                    MonthlyPerformanceTableRow(
                        year=item["year"],
                        monthly_returns_pct=tuple(item["monthly_returns_pct"]),
                        ytd_return_pct=item.get("ytd_return_pct"),
                        monthly_pnl=tuple(item["monthly_pnl"]),
                        monthly_trade_counts=tuple(item["monthly_trade_counts"]),
                    )
                    for item in table_data.get("rows", [])
                ),
                source=table_data.get("source", ""),
                basis=table_data.get("basis", ""),
                month_labels=tuple(table_data.get("month_labels", MONTH_LABELS)),
            )
        else:
            curve_data = payload.get("equity") or {}
            monthly_performance = _monthly_performance_table(
                monthly, curve_data.get("source", ""), curve_data.get("basis", "")
            )

        filter_spec_data = payload.get("filter_spec")
        filter_spec = filter_from_dict(filter_spec_data) if filter_spec_data else None
        filter_config_data = payload.get("filter_config")
        filter_config = FilterConfig(**filter_config_data) if filter_config_data is not None else None
        selection_data = payload.get("selection")
        selection = TradeSelection.from_dict(selection_data) if selection_data is not None else None
        source_report_data = payload.get("source_report")
        source_report = _report_from_dict(source_report_data) if source_report_data is not None else None

        validation_data = payload["validation"]
        validation = ValidationResult(
            status=validation_data.get("status", "not_run"),
            checks=validation_data.get("checks", {}),
            discrepancies=tuple(validation_data.get("discrepancies", ())),
        )
        return cls(
            report=report,
            metrics=Metrics(**payload["metrics"]),
            reported_metrics=payload.get("reported_metrics", {}),
            balance=restore_curve(payload["balance"]),
            equity=restore_curve(payload["equity"]),
            source_balance=restore_curve(payload.get("source_balance")),
            source_equity=restore_curve(payload.get("source_equity")),
            monthly=monthly,
            monthly_drawdown=tuple(MonthlyDrawdown(**item) for item in payload.get("monthly_drawdown", [])),
            monthly_performance=monthly_performance,
            by_symbol=payload.get("by_symbol", {}),
            validation=validation,
            warnings=tuple(Diagnostic(**item) for item in payload.get("warnings", [])),
            provenance=payload.get("provenance", {}),
            filter_spec=filter_spec,
            filter_config=filter_config,
            selection=selection,
            source_report=source_report,
            source_reported_metrics=payload.get("source_reported_metrics"),
            sample_period_config=SamplePeriodConfig.from_dict(payload.get("sample_period_config")),
            periods={
                name: PeriodAnalysisResult.from_dict(item)
                for name, item in payload.get("periods", {}).items()
            },
        )

    def to_csv(self, section: str = "monthly") -> str:
        output = io.StringIO()
        writer = csv.writer(output, lineterminator="\n")
        if section == "metrics":
            writer.writerow(["metric", "value"])
            for key, value in self.metrics.to_dict().items():
                writer.writerow([key, value])
        elif section == "monthly":
            writer.writerow([
                "period", "pnl", "return_on_starting_equity",
                "return_on_initial_capital", "cumulative_return", "trade_count",
            ])
            for row in self.monthly:
                writer.writerow([
                    row.period, row.pnl, row.return_on_starting_equity,
                    row.return_on_initial_capital, row.cumulative_return, row.trade_count,
                ])
        elif section == "monthly_performance":
            writer.writerow(["year", *MONTH_LABELS, "YTD"])
            for row in self.monthly_performance.rows:
                writer.writerow([
                    row.year,
                    *("" if value is None else value for value in row.monthly_returns_pct),
                    row.ytd_return_pct,
                ])
        elif section == "monthly_drawdown":
            writer.writerow([
                "period", "month_end_drawdown_amount", "month_end_drawdown_percent",
                "maximum_intramonth_drawdown_amount", "maximum_intramonth_drawdown_percent",
                "peak_to_trough_contained_amount", "peak_to_trough_contained_percent",
                "drawdown_duration_days",
            ])
            for row in self.monthly_drawdown:
                writer.writerow([
                    row.period, row.month_end_drawdown_amount, row.month_end_drawdown_percent,
                    row.maximum_intramonth_drawdown_amount, row.maximum_intramonth_drawdown_percent,
                    row.peak_to_trough_contained_amount, row.peak_to_trough_contained_percent,
                    row.drawdown_duration_days,
                ])
        else:
            raise ValueError(f"Unknown CSV section: {section}")
        return output.getvalue()

    def to_markdown(self) -> str:
        lines = ["# Trade Analysis", "", "## Metrics", "", "| Metric | Value |", "|---|---:|"]
        for key, value in self.metrics.to_dict().items():
            lines.append(f"| {key} | {value if value is not None else 'NA'} |")
        lines.extend(["", "## Monthly performance", "", "| Period | P&L | Return |", "|---|---:|---:|"])
        for row in self.monthly:
            value = "NA" if row.return_on_starting_equity is None else f"{row.return_on_starting_equity:.6%}"
            lines.append(f"| {row.period} | {row.pnl:.2f} | {value} |")
        return "\n".join(lines) + "\n"


def _month_increment(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def _month_keys(start: datetime, end: datetime) -> list[str]:
    year, month = start.year, start.month
    result: list[str] = []
    while (year, month) <= (end.year, end.month):
        result.append(f"{year:04d}-{month:02d}")
        year, month = _month_increment(year, month)
    return result


def _curve_monthly(
    curve: CurveSeries,
    report: Report,
) -> tuple[tuple[MonthlyPerformance, ...], tuple[MonthlyDrawdown, ...]]:
    if not curve.timestamps:
        return (), ()
    values = np.asarray(curve.values, dtype=float)
    timestamps = list(curve.timestamps)
    global_peak = np.maximum.accumulate(values)
    global_dd_money = global_peak - values
    with np.errstate(divide="ignore", invalid="ignore"):
        global_dd_pct = np.divide(
            global_dd_money,
            global_peak,
            out=np.zeros_like(global_dd_money),
            where=global_peak != 0,
        )
    keys = _month_keys(timestamps[0], timestamps[-1])
    ordered_trades = report.ordered_trades()
    monthly: list[MonthlyPerformance] = []
    drawdowns: list[MonthlyDrawdown] = []
    previous_value = values[0]
    for key in keys:
        year, month = (int(x) for x in key.split("-"))
        indices = [i for i, timestamp in enumerate(timestamps) if timestamp.year == year and timestamp.month == month]
        if indices:
            start_value = previous_value
            end_value = values[indices[-1]]
            previous_value = end_value
        else:
            start_value = previous_value
            end_value = previous_value
        period_trades = [trade for trade in ordered_trades if trade.close_time and trade.close_time.year == year and trade.close_time.month == month]
        pnl = float(sum(trade.profit for trade in period_trades))
        monthly.append(
            MonthlyPerformance(
                period=key,
                pnl=pnl,
                return_on_starting_equity=float(end_value / start_value - 1.0) if start_value else None,
                return_on_initial_capital=float(pnl / report.initial_deposit) if report.initial_deposit else None,
                cumulative_return=float(end_value / report.initial_deposit - 1.0) if report.initial_deposit else None,
                trade_count=len(period_trades),
            )
        )
        if indices:
            segment = values[indices]
            local_peak = np.maximum.accumulate(segment)
            local_dd = local_peak - segment
            with np.errstate(divide="ignore", invalid="ignore"):
                local_dd_pct = np.divide(local_dd, local_peak, out=np.zeros_like(local_dd), where=local_peak != 0)
            end_index = indices[-1]
            max_index = indices[int(np.argmax(global_dd_money[indices]))]
            contained_index = indices[int(np.argmax(local_dd))]
            duration = None
            if local_dd.max(initial=0.0) > 0:
                trough_offset = int(np.argmax(local_dd))
                peak_offset = int(np.argmax(segment[:trough_offset + 1]))
                duration = (timestamps[indices[trough_offset]] - timestamps[indices[peak_offset]]).total_seconds() / 86400
            drawdowns.append(
                MonthlyDrawdown(
                    period=key,
                    month_end_drawdown_amount=float(global_dd_money[end_index]),
                    month_end_drawdown_percent=float(global_dd_pct[end_index] * 100),
                    maximum_intramonth_drawdown_amount=float(global_dd_money[max_index]),
                    maximum_intramonth_drawdown_percent=float(global_dd_pct[max_index] * 100),
                    peak_to_trough_contained_amount=float(local_dd.max(initial=0.0)),
                    peak_to_trough_contained_percent=float(local_dd_pct.max(initial=0.0) * 100),
                    drawdown_duration_days=duration,
                )
            )
        else:
            drawdowns.append(
                MonthlyDrawdown(key, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
            )
    return tuple(monthly), tuple(drawdowns)


def _monthly_performance_table(
    monthly: tuple[MonthlyPerformance, ...],
    source: str,
    basis: str,
) -> MonthlyPerformanceTable:
    if not monthly:
        return MonthlyPerformanceTable((), source, basis)
    by_year: dict[int, dict[int, MonthlyPerformance]] = defaultdict(dict)
    for item in monthly:
        year, month = (int(value) for value in item.period.split("-"))
        by_year[year][month] = item
    rows: list[MonthlyPerformanceTableRow] = []
    for year in sorted(by_year):
        returns: list[float | None] = []
        pnl: list[float | None] = []
        counts: list[int | None] = []
        ytd_factor = 1.0
        has_active_month = False
        for month in range(1, 13):
            item = by_year[year].get(month)
            if item is None:
                returns.append(None)
                pnl.append(None)
                counts.append(None)
                continue
            value = item.return_on_starting_equity
            returns.append(float(value * 100) if value is not None else None)
            pnl.append(float(item.pnl))
            counts.append(item.trade_count)
            if value is not None:
                ytd_factor *= 1.0 + value
                has_active_month = True
        rows.append(
            MonthlyPerformanceTableRow(
                year=year,
                monthly_returns_pct=tuple(returns),
                ytd_return_pct=float((ytd_factor - 1.0) * 100) if has_active_month else None,
                monthly_pnl=tuple(pnl),
                monthly_trade_counts=tuple(counts),
            )
        )
    return MonthlyPerformanceTable(tuple(rows), source, basis)


def _by_symbol(report: Report) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for symbol in report.symbols():
        trades = [trade for trade in report.trades if trade.symbol == symbol]
        profits = np.asarray([trade.profit for trade in trades], dtype=float)
        result[symbol] = {
            "trades": len(trades),
            "net_profit": float(profits.sum()) if profits.size else 0.0,
            "winning_trades": int(np.count_nonzero(profits > 0)),
            "losing_trades": int(np.count_nonzero(profits < 0)),
            "win_rate_pct": float(np.count_nonzero(profits > 0) / len(trades) * 100) if trades else None,
        }
    return result


def _validate(report: Report, metrics: Metrics) -> ValidationResult:
    checks: dict[str, Any] = {}
    discrepancies: list[dict[str, Any]] = []
    reported = report.reported_metrics
    if "totalnetprofit" in reported and isinstance(reported["totalnetprofit"], (int, float)):
        expected = float(reported["totalnetprofit"])
        actual = metrics.net_profit
        delta = actual - expected
        checks["reported_net_profit"] = {"reported": expected, "computed": actual, "delta": delta}
        if abs(delta) > max(0.01, abs(expected) * 1e-8):
            discrepancies.append({"field": "net_profit", "reported": expected, "computed": actual, "delta": delta})
    if "totaltrades" in reported and isinstance(reported["totaltrades"], (int, float)):
        expected = int(reported["totaltrades"])
        checks["reported_total_trades"] = {"reported": expected, "computed": metrics.total_trades}
        if expected != metrics.total_trades:
            discrepancies.append({"field": "total_trades", "reported": expected, "computed": metrics.total_trades})
    status = "warn" if discrepancies else "match"
    return ValidationResult(status=status, checks=checks, discrepancies=tuple(discrepancies))


def _provenance(report: Report, config: AnalysisConfig) -> dict[str, Any]:
    metadata = report.metadata
    result = {
        "input_sha256": metadata.get("input_sha256"),
        "input_size": metadata.get("input_size"),
        "input_format": report.source_format,
        "source_filename": Path(report.source_file).name if report.source_file else None,
        "parser_version": "2",
        "package_version": "0.1.0",
        "timezone": report.timezone or config.timezone,
        "analysis_config": config.to_dict(),
    }
    return result


def _analysis_config_from_provenance(provenance: dict[str, Any]) -> AnalysisConfig:
    data = dict(provenance.get("analysis_config", {}))
    sharpe_data = dict(data.pop("sharpe", {}))
    sample_periods = SamplePeriodConfig.from_dict(data.pop("sample_periods", None))
    return AnalysisConfig(**data, sharpe=SharpeConfig(**sharpe_data), sample_periods=sample_periods)


def _balance_before(curve: CurveSeries, timestamp: datetime) -> float:
    value = curve.initial_value
    for point_time, point_value in zip(curve.timestamps, curve.values):
        if point_time < timestamp:
            value = float(point_value)
        else:
            break
    return value


def analyze(report: Report, config: AnalysisConfig | None = None) -> AnalysisResult:
    """Eagerly calculate the full v1 analysis result."""

    config = config or AnalysisConfig()
    sample_periods = config.sample_periods
    base_config = replace(config, sample_periods=None)
    diagnostics = [Diagnostic(**warning) for warning in report.warnings]
    reconstructed = reconstructed_curve(report)
    source_equity = source_equity_curve(report)
    source_balance = source_balance_curve(report)
    primary = source_equity if source_equity is not None and config.primary_curve == "source_then_reconstructed" else reconstructed
    metrics = compute_metrics(report, primary_curve=primary, config=base_config, diagnostics=diagnostics)
    monthly, monthly_drawdown = _curve_monthly(primary, report) if config.include_monthly else ((), ())
    monthly_performance = _monthly_performance_table(monthly, primary.source, primary.basis)
    validation = _validate(report, metrics)
    if validation.status != "match":
        diagnostics.append(Diagnostic(
            "validation_mismatch",
            "Reported MT5 summary values differ from recalculated values",
            "warning",
            {"status": validation.status},
        ))
    provenance = _provenance(report, config)
    result = AnalysisResult(
        report=report,
        metrics=metrics,
        reported_metrics=dict(report.reported_metrics),
        balance=CurveResult.from_curve(reconstructed),
        equity=CurveResult.from_curve(primary),
        source_balance=CurveResult.from_curve(source_balance),
        source_equity=CurveResult.from_curve(source_equity),
        monthly=monthly,
        monthly_drawdown=monthly_drawdown,
        monthly_performance=monthly_performance,
        by_symbol=_by_symbol(report) if config.include_breakdowns else {},
        validation=validation,
        warnings=tuple(diagnostics),
        provenance=provenance,
        sample_period_config=None,
        periods={},
    )
    return result.with_sample_periods(sample_periods) if sample_periods is not None and sample_periods.enabled else result


def analyze_file(source: InputSource, config: AnalysisConfig | None = None) -> AnalysisResult:
    return analyze(load_report(source), config=config)
