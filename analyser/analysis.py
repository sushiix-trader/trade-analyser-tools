"""Eager analysis orchestration and typed result objects."""

from __future__ import annotations

import csv
import hashlib
import io
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
    filter_fingerprint,
    filter_from_dict,
)
from .equity import CurveSeries, reconstructed_curve, source_balance_curve, source_equity_curve
from .load import InputSource, load_report
from .metrics import Metrics, compute_metrics
from .periods import PeriodWindow, SamplePeriodConfig
from .trade_profit import TradeProfitAnalysis, build_trade_profit_analysis
from .models import Report, Trade
from .what_if import WhatIfConfig, WhatIfResult
from .pipeline import PreparedView, TransformationPlan, prepare_analysis
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
    def trade_profit(self) -> TradeProfitAnalysis:
        return self.analysis.trade_profit

    @property
    def what_if(self) -> WhatIfResult | None:
        return self.analysis.what_if

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
    trade_profit: TradeProfitAnalysis
    by_symbol: dict[str, dict[str, Any]]
    validation: ValidationResult
    warnings: tuple[Diagnostic, ...]
    provenance: dict[str, Any]
    filter_spec: TradeFilter | None = None
    filter_config: FilterConfig | None = None
    selection: TradeSelection | None = None
    source_report: Report | None = None
    source_reported_metrics: dict[str, Any] | None = None
    what_if: WhatIfResult | None = None
    sample_period_config: SamplePeriodConfig | None = None
    periods: dict[str, PeriodAnalysisResult] = field(default_factory=dict)

    def _rebuild(self, plan: TransformationPlan) -> "AnalysisResult":
        original = self.source_report or self.report
        config = _analysis_config_from_provenance(self.provenance)
        config = replace(
            config,
            sample_periods=plan.sample_periods,
            what_if=plan.what_if,
        )
        return _analyze_with_plan(original, config, plan)

    def _existing_what_if(self) -> WhatIfConfig | None:
        if self.what_if is not None:
            return self.what_if.config
        return _analysis_config_from_provenance(self.provenance).what_if

    def _source_validation(self) -> dict[str, Any] | None:
        return self.provenance.get("source_validation") or self.validation.to_dict()

    def apply_filters(
        self,
        filter_spec: TradeFilter,
        filter_config: FilterConfig | None = None,
    ) -> "AnalysisResult":
        """Return a filtered analysis evaluated from the original report.

        Chained filters, sample periods, and what-if sizing all use the same
        internal transformation pipeline and are re-evaluated from the
        untouched canonical report.
        """

        if not isinstance(filter_spec, TradeFilter):
            raise TypeError("filter_spec must be a TradeFilter")
        combined = (
            AllOf(self.filter_spec, filter_spec)
            if self.filter_spec is not None
            else filter_spec
        )
        active_config = filter_config or self.filter_config or FilterConfig()
        return self._rebuild(TransformationPlan(
            sample_periods=self.sample_period_config,
            filter_spec=combined,
            filter_config=active_config,
            what_if=self._existing_what_if(),
            source_validation=self._source_validation(),
        ))

    def with_sample_periods(self, sample_periods: SamplePeriodConfig) -> "AnalysisResult":
        """Eagerly derive named period analyses from the original report."""

        if not isinstance(sample_periods, SamplePeriodConfig) or not sample_periods.enabled:
            raise ValueError("sample_periods must be an enabled SamplePeriodConfig")
        return self._rebuild(TransformationPlan(
            sample_periods=sample_periods,
            filter_spec=self.filter_spec,
            filter_config=self.filter_config or FilterConfig(),
            what_if=self._existing_what_if(),
            source_validation=self._source_validation() if self.filter_spec else None,
        ))

    def analyze_periods(
        self,
        sample_periods: SamplePeriodConfig,
        *,
        filters: TradeFilter | None = None,
        filter_config: FilterConfig | None = None,
    ) -> "AnalysisResult":
        """Apply sample-period classification before optional trade filtering."""

        if filters is None:
            return self.with_sample_periods(sample_periods)
        if not isinstance(filters, TradeFilter):
            raise TypeError("filters must be a TradeFilter")
        combined = (
            AllOf(self.filter_spec, filters)
            if self.filter_spec is not None
            else filters
        )
        return self._rebuild(TransformationPlan(
            sample_periods=sample_periods,
            filter_spec=combined,
            filter_config=filter_config or self.filter_config or FilterConfig(),
            what_if=self._existing_what_if(),
            source_validation=self._source_validation(),
        ))

    def apply_what_if(self, config: WhatIfConfig) -> "AnalysisResult":
        """Return a fresh analysis re-sized from the original canonical report."""

        if not isinstance(config, WhatIfConfig):
            raise TypeError("config must be a WhatIfConfig")
        return self._rebuild(TransformationPlan(
            sample_periods=self.sample_period_config,
            filter_spec=self.filter_spec,
            filter_config=self.filter_config or FilterConfig(),
            what_if=config,
            source_validation=self._source_validation() if self.filter_spec else None,
        ))

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
        trade_profit_data = payload.get("trade_profit")
        trade_profit = (
            TradeProfitAnalysis.from_dict(trade_profit_data)
            if trade_profit_data is not None
            else build_trade_profit_analysis(
                report.ordered_trades(),
                initial_capital=report.initial_deposit,
                currency=report.currency,
                timezone=report.timezone,
            )
        )

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
            trade_profit=trade_profit,
            by_symbol=payload.get("by_symbol", {}),
            validation=validation,
            warnings=tuple(Diagnostic(**item) for item in payload.get("warnings", [])),
            provenance=payload.get("provenance", {}),
            filter_spec=filter_spec,
            filter_config=filter_config,
            selection=selection,
            source_report=source_report,
            source_reported_metrics=payload.get("source_reported_metrics"),
            what_if=WhatIfResult.from_dict(payload["what_if"]) if payload.get("what_if") else None,
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
        "package_version": "0.1.1",
        "timezone": report.timezone or config.timezone,
        "analysis_config": config.to_dict(),
    }
    return result


def _analysis_config_from_provenance(provenance: dict[str, Any]) -> AnalysisConfig:
    data = dict(provenance.get("analysis_config", {}))
    sharpe_data = dict(data.pop("sharpe", {}))
    sample_periods = SamplePeriodConfig.from_dict(data.pop("sample_periods", None))
    what_if = WhatIfConfig.from_dict(data.pop("what_if", None))
    trade_profit_data = data.pop("trade_profit", None) or {}
    from .trade_profit import TradeProfitConfig
    trade_profit = TradeProfitConfig(**trade_profit_data)
    return AnalysisConfig(
        **data,
        sharpe=SharpeConfig(**sharpe_data),
        sample_periods=sample_periods,
        what_if=what_if,
        trade_profit=trade_profit,
    )


def _analyze_core(report: Report, config: AnalysisConfig) -> AnalysisResult:
    """Calculate metrics and curves for one already-prepared report."""

    diagnostics = [Diagnostic(**warning) for warning in report.warnings]
    reconstructed = reconstructed_curve(report)
    source_equity = source_equity_curve(report)
    source_balance = source_balance_curve(report)
    primary = (
        source_equity
        if source_equity is not None and config.primary_curve == "source_then_reconstructed"
        else reconstructed
    )
    metrics = compute_metrics(report, primary_curve=primary, config=config, diagnostics=diagnostics)
    monthly, monthly_drawdown = _curve_monthly(primary, report) if config.include_monthly else ((), ())
    monthly_performance = _monthly_performance_table(monthly, primary.source, primary.basis)
    trade_profit = build_trade_profit_analysis(
        report.ordered_trades(),
        initial_capital=report.initial_deposit,
        currency=report.currency,
        timezone=report.timezone or config.timezone,
        config=config.trade_profit,
    )
    diagnostics.extend(trade_profit.warnings)
    validation = _validate(report, metrics)
    if validation.status != "match":
        diagnostics.append(Diagnostic(
            "validation_mismatch",
            "Reported MT5 summary values differ from recalculated values",
            "warning",
            {"status": validation.status},
        ))
    return AnalysisResult(
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
        trade_profit=trade_profit,
        by_symbol=_by_symbol(report) if config.include_breakdowns else {},
        validation=validation,
        warnings=tuple(diagnostics),
        provenance=_provenance(report, config),
        sample_period_config=None,
        periods={},
    )


def _result_from_prepared_view(
    view: PreparedView,
    core_config: AnalysisConfig,
    provenance_config: AnalysisConfig,
    plan: TransformationPlan,
    source_report: Report,
) -> AnalysisResult:
    result = _analyze_core(view.report, core_config)
    warnings = list(result.warnings)
    warnings.extend(view.diagnostics)
    provenance = dict(result.provenance)
    provenance["analysis_config"] = provenance_config.to_dict()

    if view.what_if is not None:
        provenance["what_if"] = view.what_if.to_dict()

    if plan.filter_spec is not None and view.period_name is None:
        source_validation = plan.source_validation or result.validation.to_dict()
        warnings.append(Diagnostic(
            "filtered_reported_metrics_not_applicable",
            "MT5 reported metrics describe the unfiltered report and were not used for filtered validation",
            context={
                "selected_trade_count": (
                    view.selection.selected_trade_count if view.selection is not None else 0
                ),
            },
        ))
        provenance.update({
            "filtered": True,
            "filter_spec": plan.filter_spec.to_dict(),
            "filter_config": plan.filter_config.to_dict(),
            "filter_fingerprint": filter_fingerprint(plan.filter_spec, plan.filter_config),
            "source_report_sha256": (
                source_report.metadata.get("input_sha256")
                or source_report.metadata.get("source_report_sha256")
                or hashlib.sha256(
                    deterministic_json(to_primitive(source_report)).encode("utf-8")
                ).hexdigest()
            ),
            "source_trade_count": (
                view.selection.source_trade_count if view.selection is not None else 0
            ),
            "selected_trade_count": (
                view.selection.selected_trade_count if view.selection is not None else 0
            ),
            "excluded_trade_count": (
                view.selection.excluded_trade_count if view.selection is not None else 0
            ),
            "source_validation": source_validation,
        })
        result = replace(
            result,
            reported_metrics={},
            validation=ValidationResult(
                status="not_applicable",
                checks={"source_validation": source_validation},
                discrepancies=(),
            ),
        )
        if not plan.sample_periods and view.period_name is None:
            result = replace(
                result,
                balance=(
                    replace(result.balance, source="filtered_reconstructed_closed_positions")
                    if result.balance is not None else None
                ),
                equity=(
                    replace(result.equity, source="filtered_reconstructed_closed_positions")
                    if result.equity is not None else None
                ),
                monthly_performance=replace(
                    result.monthly_performance,
                    source="filtered_reconstructed_closed_positions",
                    basis="balance",
                ),
            )

    transformed = (
        plan.what_if is not None
        or (plan.filter_spec is not None and view.period_name is None)
    )
    expose_filter_selection = view.period_name is None
    return replace(
        result,
        warnings=tuple(warnings),
        provenance=provenance,
        filter_spec=plan.filter_spec if expose_filter_selection else None,
        filter_config=(
            plan.filter_config
            if plan.filter_spec is not None and expose_filter_selection
            else None
        ),
        selection=view.selection if expose_filter_selection else None,
        source_report=source_report if transformed else None,
        source_reported_metrics=dict(source_report.reported_metrics) if transformed else None,
        what_if=view.what_if,
    )


def _analyze_with_plan(
    report: Report,
    config: AnalysisConfig,
    plan: TransformationPlan,
) -> AnalysisResult:
    prepared = prepare_analysis(report, plan)
    core_config = replace(config, sample_periods=None, what_if=None)
    result = _result_from_prepared_view(
        prepared.full,
        core_config,
        config,
        plan,
        prepared.source_report,
    )
    if not plan.sample_periods or not plan.sample_periods.enabled:
        return result

    period_results: dict[str, PeriodAnalysisResult] = {}
    period_provenance_config = replace(config, sample_periods=None)
    for view in prepared.periods:
        period_analysis = _result_from_prepared_view(
            view,
            core_config,
            period_provenance_config,
            plan,
            prepared.source_report,
        )
        period_provenance = dict(period_analysis.provenance)
        if not (plan.filter_spec is not None and view.period_name is not None):
            period_provenance.update({
                "sample_period": view.period_window.to_dict() if view.period_window else None,
                "sample_period_name": view.period_name,
            })
        period_analysis = replace(period_analysis, provenance=period_provenance)
        period_results[view.period_name or ""] = PeriodAnalysisResult(
            name=view.period_name or "",
            window=view.period_window,
            analysis=period_analysis,
            source_trade_count=view.source_trade_count,
            selected_trade_count=(
                view.selection.selected_trade_count
                if view.selection is not None
                else view.selected_trade_count
            ),
            cross_boundary_trade_count=view.cross_boundary_trade_count,
            excluded_trade_count=(
                view.selection.excluded_trade_count
                if view.selection is not None
                else view.excluded_trade_count
            ),
            warnings=period_analysis.warnings,
        )
    provenance = dict(result.provenance)
    provenance["sample_period_config"] = plan.sample_periods.to_dict()
    return replace(
        result,
        sample_period_config=plan.sample_periods,
        periods=period_results,
        warnings=tuple(list(result.warnings) + list(prepared.sample_period_warnings)),
        provenance=provenance,
    )


def analyze(report: Report, config: AnalysisConfig | None = None) -> AnalysisResult:
    """Eagerly calculate a full analysis through the shared preparation pipeline."""

    config = config or AnalysisConfig()
    return _analyze_with_plan(
        report,
        config,
        TransformationPlan(
            sample_periods=config.sample_periods,
            what_if=config.what_if,
        ),
    )


def analyze_file(source: InputSource, config: AnalysisConfig | None = None) -> AnalysisResult:
    return analyze(load_report(source), config=config)
