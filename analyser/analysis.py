"""Eager analysis orchestration and typed result objects."""

from __future__ import annotations

import csv
import hashlib
import io
import math
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from .config import AnalysisConfig
from .diagnostics import Diagnostic, ValidationResult
from .equity import CurveSeries, reconstructed_curve, source_balance_curve, source_equity_curve
from .load import InputSource, load_report
from .metrics import Metrics, compute_metrics
from .models import Report, Trade
from .serialization import deterministic_json, to_primitive


@dataclass(frozen=True)
class MonthlyPerformance:
    period: str
    pnl: float
    return_on_starting_equity: float | None
    return_on_initial_capital: float | None
    cumulative_return: float | None
    trade_count: int


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
    by_symbol: dict[str, dict[str, Any]]
    validation: ValidationResult
    warnings: tuple[Diagnostic, ...]
    provenance: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return to_primitive(self)

    def to_json(self) -> str:
        return deterministic_json(self)

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
                return_on_starting_equity=(end_value / start_value - 1.0) if start_value else None,
                return_on_initial_capital=(pnl / report.initial_deposit) if report.initial_deposit else None,
                cumulative_return=(end_value / report.initial_deposit - 1.0) if report.initial_deposit else None,
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
        "parser_version": "1",
        "package_version": "0.1.0",
        "timezone": report.timezone or config.timezone,
        "analysis_config": config.to_dict(),
    }
    return result


def analyze(report: Report, config: AnalysisConfig | None = None) -> AnalysisResult:
    """Eagerly calculate the full v1 analysis result."""

    config = config or AnalysisConfig()
    diagnostics = [Diagnostic(**warning) for warning in report.warnings]
    reconstructed = reconstructed_curve(report)
    source_equity = source_equity_curve(report)
    source_balance = source_balance_curve(report)
    primary = source_equity if source_equity is not None and config.primary_curve == "source_then_reconstructed" else reconstructed
    metrics = compute_metrics(report, primary_curve=primary, config=config, diagnostics=diagnostics)
    monthly, monthly_drawdown = _curve_monthly(primary, report) if config.include_monthly else ((), ())
    validation = _validate(report, metrics)
    if validation.status != "match":
        diagnostics.append(Diagnostic(
            "validation_mismatch",
            "Reported MT5 summary values differ from recalculated values",
            "warning",
            {"status": validation.status},
        ))
    provenance = _provenance(report, config)
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
        by_symbol=_by_symbol(report) if config.include_breakdowns else {},
        validation=validation,
        warnings=tuple(diagnostics),
        provenance=provenance,
    )


def analyze_file(source: InputSource, config: AnalysisConfig | None = None) -> AnalysisResult:
    return analyze(load_report(source), config=config)
