"""Deterministic multi-report portfolio analytics.

The portfolio module is the deep aggregation seam between independently
analysed MT5 reports and any future GUI or simulation consumer.  One report is
one strategy.  Reports are never trade-netted; their allocated tagged trade
streams and curves are combined only at the portfolio layer.
"""

from __future__ import annotations

import bisect
import csv
import hashlib
import io
import math
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Sequence

import numpy as np

from .analysis import (
    AnalysisResult,
    CurveResult,
    MonthlyDrawdown,
    MonthlyPerformance,
    MonthlyPerformanceTable,
    MonthlyPerformanceTableRow,
    _monthly_performance_table,
    _report_from_dict,
    _curve_monthly,
    analyze,
)
from .config import AnalysisConfig, SharpeConfig
from .correlation import (
    CorrelationResults,
    DailyProfitCorrelationResult,
    build_daily_profit_correlation,
    build_weekly_profit_correlation,
    build_weekly_profit_correlation_from_daily,
)
from .diagnostics import Diagnostic, ValidationResult
from .drawdown import DrawdownAnalysis, analyze_drawdowns
from .equity import CurveSeries
from .filters import FilterConfig, TradeFilter
from .errors import (
    CurrencyMismatchError,
    DuplicatePortfolioMemberError,
    PortfolioValidationError,
    TimezoneMismatchError,
)
from .load import InputSource, load_report, read_input
from .matrices import AnalysisMatrix
from .metrics import Metrics, compute_metrics
from .models import Report, Trade
from .periods import PeriodWindow, SamplePeriodConfig
from .serialization import deterministic_json, to_primitive
from .trade_profit import (
    TradeProfitAnalysis,
    build_trade_profit_analysis,
)
from .what_if import WhatIfConfig

_SUPPORTED_PRIMARY_CURVES = frozenset(("source_then_reconstructed", "source", "reconstructed"))


@dataclass(frozen=True)
class PortfolioMember:
    """One required user-described strategy report in a portfolio."""

    strategy_name: str
    description: str
    weight: float = 1.0
    source: InputSource | None = None
    filters: TradeFilter | None = None
    filter_config: FilterConfig | None = None
    sample_periods: SamplePeriodConfig | None = None
    what_if: WhatIfConfig | None = None

    def __post_init__(self) -> None:
        if not self.strategy_name.strip():
            raise ValueError("strategy_name is required")
        if not self.description.strip():
            raise ValueError("description is required")

    def with_metadata(
        self,
        *,
        strategy_name: str | None = None,
        description: str | None = None,
    ) -> "PortfolioMember":
        return replace(
            self,
            strategy_name=strategy_name if strategy_name is not None else self.strategy_name,
            description=description if description is not None else self.description,
        )


@dataclass(frozen=True)
class AnalyzedPortfolioMember:
    """A parsed/analyzed member that can be recombined without reparsing."""

    member_key: str
    member: PortfolioMember
    analysis: AnalysisResult


@dataclass(frozen=True)
class PortfolioConfig:
    """Configuration for one deterministic portfolio aggregation."""

    portfolio_initial_capital: float | None = None
    primary_curve: str = "source_then_reconstructed"
    minimum_correlation_observations: int = 2
    correlation_warning_observations: int = 12
    strict: bool = False
    analysis_config: AnalysisConfig = field(default_factory=AnalysisConfig)

    def validate(self) -> None:
        if self.primary_curve not in _SUPPORTED_PRIMARY_CURVES:
            raise ValueError(
                f"primary_curve must be one of {sorted(_SUPPORTED_PRIMARY_CURVES)}"
            )
        if self.portfolio_initial_capital is not None and (
            not math.isfinite(float(self.portfolio_initial_capital))
            or self.portfolio_initial_capital <= 0
        ):
            raise ValueError("portfolio_initial_capital must be positive and finite")
        if self.minimum_correlation_observations < 2:
            raise ValueError("minimum_correlation_observations must be at least 2")
        if self.correlation_warning_observations < self.minimum_correlation_observations:
            raise ValueError(
                "correlation_warning_observations cannot be below the minimum"
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PortfolioMemberResult:
    """Calculated member details retained inside a portfolio result."""

    member_key: str
    strategy_name: str
    description: str
    weight: float
    normalized_weight: float
    allocated_capital: float
    allocation_scale: float
    analysis: AnalysisResult
    raw_curve: CurveResult
    allocated_curve: CurveResult
    raw_drawdown_analysis: DrawdownAnalysis
    allocated_drawdown_analysis: DrawdownAnalysis
    active_start: datetime | None
    active_end: datetime | None

    def with_metadata(
        self,
        *,
        strategy_name: str | None = None,
        description: str | None = None,
    ) -> "PortfolioMemberResult":
        return replace(
            self,
            strategy_name=strategy_name if strategy_name is not None else self.strategy_name,
            description=description if description is not None else self.description,
        )


@dataclass(frozen=True)
class PortfolioPeriodResult:
    """Portfolio analytics for one effective named sample period."""

    name: str
    window: PeriodWindow
    analysis: AnalysisResult
    daily_profit_correlation: DailyProfitCorrelationResult
    warnings: tuple[Diagnostic, ...] = ()
    weekly_profit_correlation: DailyProfitCorrelationResult | None = None

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
    def drawdown_analysis(self) -> DrawdownAnalysis:
        return self.analysis.drawdown_analysis

    @property
    def monthly_performance(self) -> MonthlyPerformanceTable:
        return self.analysis.monthly_performance

    @property
    def trade_profit(self) -> TradeProfitAnalysis:
        return self.analysis.trade_profit

    @property
    def what_if(self):
        return self.analysis.what_if

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "window": self.window.to_dict(),
            "analysis": self.analysis.to_dict(),
            "daily_profit_correlation": self.daily_profit_correlation.to_dict(),
            "weekly_profit_correlation": (
                self.weekly_profit_correlation.to_dict()
                if self.weekly_profit_correlation is not None
                else None
            ),
            "warnings": [warning.to_dict() for warning in self.warnings],
        }

    @classmethod
    def from_dict(
        cls,
        payload: dict[str, Any],
        *,
        minimum_observations: int = 2,
        warning_observations: int = 12,
    ) -> "PortfolioPeriodResult":
        daily = DailyProfitCorrelationResult.from_dict(payload["daily_profit_correlation"])
        weekly_data = payload.get("weekly_profit_correlation")
        weekly = (
            DailyProfitCorrelationResult.from_dict(weekly_data)
            if isinstance(weekly_data, dict)
            else build_weekly_profit_correlation_from_daily(
                daily,
                minimum_observations=minimum_observations,
                warning_observations=warning_observations,
            )
        )
        return cls(
            name=payload["name"],
            window=PeriodWindow.from_dict(payload["window"]),
            analysis=AnalysisResult.from_dict(payload["analysis"]),
            daily_profit_correlation=daily,
            weekly_profit_correlation=weekly,
            warnings=tuple(Diagnostic(**item) for item in payload.get("warnings", [])),
        )


@dataclass(frozen=True)
class PortfolioAnalysisResult:
    """Complete aggregate portfolio result with member-level audit data."""

    members: tuple[PortfolioMemberResult, ...]
    portfolio_initial_capital: float
    currency: str
    timezone: str | None
    raw_weights: dict[str, float]
    normalized_weights: dict[str, float]
    portfolio_report: Report
    metrics: Metrics
    reported_metrics: dict[str, Any]
    balance: CurveResult
    equity: CurveResult
    source_balance: CurveResult | None
    source_equity: CurveResult | None
    monthly: tuple[MonthlyPerformance, ...]
    monthly_drawdown: tuple[MonthlyDrawdown, ...]
    drawdown_analysis: DrawdownAnalysis
    monthly_performance: MonthlyPerformanceTable
    trade_profit: TradeProfitAnalysis
    raw_trade_profit: dict[str, TradeProfitAnalysis]
    raw_equity_matrix: AnalysisMatrix
    equity_matrix: AnalysisMatrix
    raw_monthly_return_matrix: AnalysisMatrix
    allocated_monthly_return_matrix: AnalysisMatrix
    raw_monthly_contribution_matrix: AnalysisMatrix
    allocated_monthly_contribution_matrix: AnalysisMatrix
    correlation_matrix: AnalysisMatrix
    covariance_matrix: AnalysisMatrix
    correlations: CorrelationResults
    periods: dict[str, PortfolioPeriodResult]
    validation: ValidationResult
    warnings: tuple[Diagnostic, ...]
    provenance: dict[str, Any]
    config: PortfolioConfig

    @property
    def portfolio_metrics(self) -> Metrics:
        return self.metrics

    @property
    def portfolio_monthly(self) -> tuple[MonthlyPerformance, ...]:
        return self.monthly

    @property
    def portfolio_monthly_drawdown(self) -> tuple[MonthlyDrawdown, ...]:
        return self.monthly_drawdown

    @property
    def portfolio_drawdown_analysis(self) -> DrawdownAnalysis:
        return self.drawdown_analysis

    @property
    def daily_profit_correlation(self) -> AnalysisMatrix:
        return self.correlations.daily_profit.matrix

    @property
    def daily_profit_series(self):
        return self.correlations.daily_profit.series

    @property
    def weekly_profit_correlation(self) -> DailyProfitCorrelationResult | None:
        return self.correlations.weekly_profit

    @property
    def weekly_profit_series(self):
        return self.correlations.weekly_profit.series if self.correlations.weekly_profit else {}

    def to_dict(self) -> dict[str, Any]:
        payload = to_primitive(self)
        for index, member in enumerate(self.members):
            payload["members"][index]["analysis"] = member.analysis.to_dict()
        return payload

    def to_json(self) -> str:
        return deterministic_json(self.to_dict())

    def to_csv(self, section: str = "monthly") -> str:
        if section == "metrics":
            output = io.StringIO()
            writer = csv.writer(output, lineterminator="\n")
            writer.writerow(["metric", "value"])
            for key, value in self.metrics.to_dict().items():
                writer.writerow([key, value])
            return output.getvalue()
        matrix_map = {
            "raw_equity": self.raw_equity_matrix,
            "equity": self.equity_matrix,
            "raw_monthly_returns": self.raw_monthly_return_matrix,
            "allocated_monthly_returns": self.allocated_monthly_return_matrix,
            "raw_monthly_contributions": self.raw_monthly_contribution_matrix,
            "allocated_monthly_contributions": self.allocated_monthly_contribution_matrix,
            "correlation": self.correlation_matrix,
            "covariance": self.covariance_matrix,
            "daily_profit_correlation": self.correlations.daily_profit.matrix,
            "weekly_profit_correlation": (
                self.correlations.weekly_profit.matrix
                if self.correlations.weekly_profit is not None
                else AnalysisMatrix((), (), (), "weekly_profit_correlation")
            ),
        }
        if section in matrix_map:
            return matrix_map[section].to_csv()
        if section == "monthly":
            output = io.StringIO()
            writer = csv.writer(output, lineterminator="\n")
            writer.writerow([
                "period", "pnl", "return_on_starting_equity",
                "return_on_initial_capital", "cumulative_return", "trade_count",
            ])
            for row in self.monthly:
                writer.writerow([
                    row.period, row.pnl, row.return_on_starting_equity,
                    row.return_on_initial_capital, row.cumulative_return, row.trade_count,
                ])
            return output.getvalue()
        if section == "monthly_performance":
            output = io.StringIO()
            writer = csv.writer(output, lineterminator="\n")
            writer.writerow(["year", *self.monthly_performance.month_labels, "YTD"])
            for row in self.monthly_performance.rows:
                writer.writerow([
                    row.year,
                    *("" if value is None else value for value in row.monthly_returns_pct),
                    row.ytd_return_pct,
                ])
            return output.getvalue()
        if section == "monthly_drawdown":
            output = io.StringIO()
            writer = csv.writer(output, lineterminator="\n")
            writer.writerow([
                "period", "month_end_drawdown_amount", "month_end_drawdown_percent",
                "maximum_intramonth_drawdown_amount", "maximum_intramonth_drawdown_percent",
                "peak_to_trough_contained_amount", "peak_to_trough_contained_percent",
                "drawdown_duration_days",
            ])
            for row in self.monthly_drawdown:
                writer.writerow([
                    row.period, row.month_end_drawdown_amount,
                    row.month_end_drawdown_percent,
                    row.maximum_intramonth_drawdown_amount,
                    row.maximum_intramonth_drawdown_percent,
                    row.peak_to_trough_contained_amount,
                    row.peak_to_trough_contained_percent,
                    row.drawdown_duration_days,
                ])
            return output.getvalue()
        if section == "drawdown_summary":
            output = io.StringIO()
            writer = csv.writer(output, lineterminator="\n")
            writer.writerow(["axis", "unit", "count", "minimum", "p50", "p90", "p95", "p99", "maximum"])
            distributions = (
                ("depth_percent", self.drawdown_analysis.depth_distribution),
                ("depth_money", self.drawdown_analysis.depth_money_distribution),
                ("duration_days", self.drawdown_analysis.duration_distribution),
                ("duration_periods", self.drawdown_analysis.duration_periods_distribution),
            )
            for axis, distribution in distributions:
                writer.writerow([
                    axis, distribution.unit, distribution.count, distribution.minimum,
                    distribution.p50, distribution.p90, distribution.p95,
                    distribution.p99, distribution.maximum,
                ])
            return output.getvalue()
        if section == "drawdown_episodes":
            output = io.StringIO()
            writer = csv.writer(output, lineterminator="\n")
            writer.writerow([
                "episode_id", "status", "peak_index", "trough_index", "end_index",
                "recovery_index", "peak_time", "trough_time", "recovery_time", "end_time",
                "peak_value", "trough_value", "recovery_value", "end_value",
                "depth_money", "depth_percent", "duration_days", "duration_periods",
                "depth_percentile", "depth_tail_rarity_percent", "depth_ordinal_rank",
                "duration_percentile", "duration_tail_rarity_percent", "duration_ordinal_rank",
            ])
            for episode in self.drawdown_analysis.episodes:
                writer.writerow([
                    episode.episode_id, episode.status, episode.peak_index, episode.trough_index,
                    episode.end_index, episode.recovery_index,
                    episode.peak_time.isoformat() if episode.peak_time else None,
                    episode.trough_time.isoformat() if episode.trough_time else None,
                    episode.recovery_time.isoformat() if episode.recovery_time else None,
                    episode.end_time.isoformat() if episode.end_time else None,
                    episode.peak_value, episode.trough_value, episode.recovery_value,
                    episode.end_value, episode.depth_money, episode.depth_percent,
                    episode.duration_days, episode.duration_periods, episode.depth_percentile,
                    episode.depth_tail_rarity_percent, episode.depth_ordinal_rank,
                    episode.duration_percentile, episode.duration_tail_rarity_percent,
                    episode.duration_ordinal_rank,
                ])
            return output.getvalue()
        raise ValueError(f"Unknown portfolio CSV section: {section}")

    def to_markdown(self) -> str:
        lines = ["# Portfolio Trade Analysis", "", "## Metrics", "", "| Metric | Value |", "|---|---:|"]
        for key, value in self.metrics.to_dict().items():
            lines.append(f"| {key} | {value if value is not None else 'NA'} |")
        lines.extend(["", "## Members", "", "| Strategy | Weight | Allocation |", "|---|---:|---:|"])
        for member in self.members:
            lines.append(
                f"| {member.strategy_name} | {member.normalized_weight:.6%} | "
                f"{member.allocated_capital:.2f} |"
            )
        lines.extend(["", "## Monthly performance", "", "| Period | P&L | Return |", "|---|---:|---:|"])
        for row in self.monthly:
            value = "NA" if row.return_on_starting_equity is None else f"{row.return_on_starting_equity:.6%}"
            lines.append(f"| {row.period} | {row.pnl:.2f} | {value} |")
        lines.extend([
            "", "## Drawdown depth × duration", "",
            f"Curve: `{self.drawdown_analysis.curve_source}` / `{self.drawdown_analysis.curve_basis}`; "
            f"completed episodes: {self.drawdown_analysis.completed_episode_count}; "
            f"current: {'underwater' if self.drawdown_analysis.current_episode else 'not underwater'}.",
            "", "| Episode | Status | Depth | Duration | Depth percentile | Duration percentile |",
            "|---:|---|---:|---:|---:|---:|",
        ])
        episodes = sorted(
            self.drawdown_analysis.episodes,
            key=lambda episode: (episode.status != "open", -episode.episode_id),
        )
        for episode in episodes:
            depth = "NA" if episode.depth_percent is None else f"-{episode.depth_percent:.2f}%"
            duration = "NA" if episode.duration_days is None else f"{episode.duration_days:.2f} d"
            depth_rank = "NA" if episode.depth_percentile is None else f"{episode.depth_percentile:.2f}%"
            duration_rank = "NA" if episode.duration_percentile is None else f"{episode.duration_percentile:.2f}%"
            lines.append(
                f"| {episode.episode_id} | {episode.status} | {depth} | {duration} | "
                f"{depth_rank} | {duration_rank} |"
            )
        return "\n".join(lines) + "\n"

    def with_weights(self, weights: dict[str, float]) -> "PortfolioAnalysisResult":
        """Return a new result with the same analyses and different weights."""

        if set(weights) != {member.member_key for member in self.members}:
            raise ValueError("weights must contain exactly the existing member keys")
        analyzed = tuple(
            AnalyzedPortfolioMember(
                member_key=member.member_key,
                member=PortfolioMember(
                    strategy_name=member.strategy_name,
                    description=member.description,
                    weight=weights[member.member_key],
                    filters=member.analysis.filter_spec,
                    filter_config=member.analysis.filter_config,
                    sample_periods=member.analysis.sample_period_config,
                    what_if=member.analysis.what_if.config if member.analysis.what_if else None,
                ),
                analysis=member.analysis,
            )
            for member in self.members
        )
        return combine_analyses(analyzed, replace(self.config, portfolio_initial_capital=self.portfolio_initial_capital))

    def with_member_metadata(
        self,
        member_key: str,
        *,
        strategy_name: str | None = None,
        description: str | None = None,
    ) -> "PortfolioAnalysisResult":
        """Return a new result with updated required display metadata."""

        if member_key not in {member.member_key for member in self.members}:
            raise KeyError(member_key)
        analyzed = []
        for member in self.members:
            name = strategy_name if member.member_key == member_key and strategy_name is not None else member.strategy_name
            detail = description if member.member_key == member_key and description is not None else member.description
            analyzed.append(
                AnalyzedPortfolioMember(
                    member_key=member.member_key,
                    member=PortfolioMember(
                        name,
                        detail,
                        member.weight,
                        filters=member.analysis.filter_spec,
                        filter_config=member.analysis.filter_config,
                        sample_periods=member.analysis.sample_period_config,
                        what_if=member.analysis.what_if.config if member.analysis.what_if else None,
                    ),
                    analysis=member.analysis,
                )
            )
        return combine_analyses(tuple(analyzed), self.config)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PortfolioAnalysisResult":
        def restore_member(item: dict[str, Any]) -> PortfolioMemberResult:
            raw_curve = _curve_from_dict(item["raw_curve"])
            allocated_curve = _curve_from_dict(item["allocated_curve"])
            if raw_curve is None or allocated_curve is None:
                raise ValueError("serialized portfolio member curves are required")
            return PortfolioMemberResult(
                member_key=item["member_key"],
                strategy_name=item["strategy_name"],
                description=item["description"],
                weight=item["weight"],
                normalized_weight=item["normalized_weight"],
                allocated_capital=item["allocated_capital"],
                allocation_scale=item["allocation_scale"],
                analysis=AnalysisResult.from_dict(item["analysis"]),
                raw_curve=raw_curve,
                allocated_curve=allocated_curve,
                raw_drawdown_analysis=(
                    DrawdownAnalysis.from_dict(item["raw_drawdown_analysis"])
                    if isinstance(item.get("raw_drawdown_analysis"), dict)
                    else analyze_drawdowns(raw_curve)
                ),
                allocated_drawdown_analysis=(
                    DrawdownAnalysis.from_dict(item["allocated_drawdown_analysis"])
                    if isinstance(item.get("allocated_drawdown_analysis"), dict)
                    else analyze_drawdowns(allocated_curve)
                ),
                active_start=_datetime(item.get("active_start")),
                active_end=_datetime(item.get("active_end")),
            )

        members = tuple(restore_member(item) for item in payload["members"])
        config_data = dict(payload["config"])
        analysis_data = dict(config_data.pop("analysis_config", {}))
        sharpe_data = dict(analysis_data.pop("sharpe", {}))
        what_if_data = analysis_data.pop("what_if", None)
        trade_profit_data = analysis_data.pop("trade_profit", None) or {}
        from .trade_profit import TradeProfitConfig
        config = PortfolioConfig(
            **config_data,
            analysis_config=AnalysisConfig(
                **analysis_data,
                sharpe=SharpeConfig(**sharpe_data),
                what_if=WhatIfConfig.from_dict(what_if_data),
                trade_profit=TradeProfitConfig(**trade_profit_data),
            ),
        )
        if payload.get("correlations"):
            correlations = CorrelationResults.from_dict(
                payload["correlations"],
                minimum_observations=config.minimum_correlation_observations,
                warning_observations=config.correlation_warning_observations,
            )
        else:
            correlation_members = [
                (member.strategy_name, member.analysis.report.ordered_trades(), member.allocation_scale)
                for member in members
            ]
            daily_profit = build_daily_profit_correlation(
                correlation_members,
                scope="full_sample",
                timezone=payload.get("timezone"),
                minimum_observations=config.minimum_correlation_observations,
                warning_observations=config.correlation_warning_observations,
            )
            weekly_profit = build_weekly_profit_correlation(
                correlation_members,
                scope="full_sample",
                timezone=payload.get("timezone"),
                minimum_observations=config.minimum_correlation_observations,
                warning_observations=config.correlation_warning_observations,
            )
            correlations = CorrelationResults(
                daily_profit=daily_profit,
                weekly_profit=weekly_profit,
            )
        trade_profit_data = payload.get("trade_profit")
        trade_profit = (
            TradeProfitAnalysis.from_dict(trade_profit_data)
            if trade_profit_data is not None
            else build_trade_profit_analysis(
                _report_from_dict(payload["portfolio_report"]).ordered_trades(),
                initial_capital=payload["portfolio_initial_capital"],
                currency=payload.get("currency", ""),
                timezone=payload.get("timezone"),
                config=config.analysis_config.trade_profit,
            )
        )
        raw_trade_profit_data = payload.get("raw_trade_profit")
        if raw_trade_profit_data is not None:
            raw_trade_profit = {
                name: TradeProfitAnalysis.from_dict(item)
                for name, item in raw_trade_profit_data.items()
            }
        else:
            raw_trade_profit = {
                member.strategy_name: member.analysis.trade_profit
                for member in members
            }
        portfolio_equity = _curve_from_dict(payload["equity"])
        drawdown_data = payload.get("drawdown_analysis")
        portfolio_drawdown = (
            DrawdownAnalysis.from_dict(drawdown_data)
            if isinstance(drawdown_data, dict)
            else analyze_drawdowns(portfolio_equity)
        )
        return cls(
            members=members,
            portfolio_initial_capital=payload["portfolio_initial_capital"],
            currency=payload["currency"],
            timezone=payload.get("timezone"),
            raw_weights=payload["raw_weights"],
            normalized_weights=payload["normalized_weights"],
            portfolio_report=_report_from_dict(payload["portfolio_report"]),
            metrics=Metrics(**payload["metrics"]),
            reported_metrics=payload.get("reported_metrics", {}),
            balance=_curve_from_dict(payload["balance"]),
            equity=portfolio_equity,
            source_balance=_curve_from_dict(payload.get("source_balance")),
            source_equity=_curve_from_dict(payload.get("source_equity")),
            monthly=tuple(MonthlyPerformance(**item) for item in payload.get("monthly", [])),
            monthly_drawdown=tuple(MonthlyDrawdown(**item) for item in payload.get("monthly_drawdown", [])),
            drawdown_analysis=portfolio_drawdown,
            monthly_performance=_monthly_table_from_payload(payload, tuple(MonthlyPerformance(**item) for item in payload.get("monthly", []))),
            trade_profit=trade_profit,
            raw_trade_profit=raw_trade_profit,
            equity_matrix=_matrix_from_dict(payload["equity_matrix"]),
            raw_equity_matrix=_matrix_from_dict(payload["raw_equity_matrix"]),
            raw_monthly_return_matrix=_matrix_from_dict(payload["raw_monthly_return_matrix"]),
            allocated_monthly_return_matrix=_matrix_from_dict(payload["allocated_monthly_return_matrix"]),
            raw_monthly_contribution_matrix=_matrix_from_dict(payload["raw_monthly_contribution_matrix"]),
            allocated_monthly_contribution_matrix=_matrix_from_dict(payload["allocated_monthly_contribution_matrix"]),
            correlation_matrix=_matrix_from_dict(payload["correlation_matrix"]),
            covariance_matrix=_matrix_from_dict(payload["covariance_matrix"]),
            correlations=correlations,
            periods={
                name: PortfolioPeriodResult.from_dict(
                    item,
                    minimum_observations=config.minimum_correlation_observations,
                    warning_observations=config.correlation_warning_observations,
                )
                for name, item in payload.get("periods", {}).items()
            },
            validation=ValidationResult(**payload["validation"]),
            warnings=tuple(Diagnostic(**item) for item in payload.get("warnings", [])),
            provenance=payload.get("provenance", {}),
            config=config,
        )


def _monthly_table_from_payload(
    payload: dict[str, Any],
    monthly: tuple[MonthlyPerformance, ...],
) -> MonthlyPerformanceTable:
    data = payload.get("monthly_performance")
    if data is None:
        return _monthly_performance_table(monthly, "", "")
    return MonthlyPerformanceTable(
        rows=tuple(
            MonthlyPerformanceTableRow(
                year=item["year"],
                monthly_returns_pct=tuple(item["monthly_returns_pct"]),
                ytd_return_pct=item.get("ytd_return_pct"),
                monthly_pnl=tuple(item["monthly_pnl"]),
                monthly_trade_counts=tuple(item["monthly_trade_counts"]),
            )
            for item in data.get("rows", [])
        ),
        source=data.get("source", ""),
        basis=data.get("basis", ""),
        month_labels=tuple(data.get("month_labels", (
            "Jan", "Feb", "Mar", "Apr", "May", "Jun",
            "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
        ))),
    )


def _datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _curve_from_dict(data: dict[str, Any] | None) -> CurveResult | None:
    if data is None:
        return None
    return CurveResult(
        timestamps=tuple(_datetime(value) for value in data["timestamps"]),
        values=tuple(data["values"]),
        source=data["source"],
        basis=data["basis"],
        initial_value=data["initial_value"],
    )


def _matrix_from_dict(data: dict[str, Any]) -> AnalysisMatrix:
    return AnalysisMatrix(
        tuple(data["row_labels"]),
        tuple(data["column_labels"]),
        tuple(tuple(row) for row in data["values"]),
        data["value_type"],
    )


def _analyze_member_bytes(
    member: PortfolioMember,
    data: bytes,
    filename: str,
    analysis_config: AnalysisConfig,
) -> AnalyzedPortfolioMember:
    key = hashlib.sha256(data).hexdigest()
    report = load_report(data)
    report.source_file = filename
    effective_config = replace(
        analysis_config,
        sample_periods=member.sample_periods or analysis_config.sample_periods,
        what_if=member.what_if or analysis_config.what_if,
    )
    result = analyze(report, effective_config)
    if member.filters is not None:
        result = result.apply_filters(member.filters, member.filter_config)
    return AnalyzedPortfolioMember(key, member, result)


def analyze_portfolio(
    members: Sequence[PortfolioMember],
    config: PortfolioConfig | None = None,
) -> PortfolioAnalysisResult:
    """Parse, eagerly analyse, and combine one or more report members."""

    config = config or PortfolioConfig()
    prepared: list[AnalyzedPortfolioMember] = []
    for member in members:
        if member.source is None:
            raise ValueError("PortfolioMember.source is required for analyze_portfolio")
        data, filename = read_input(member.source)
        prepared.append(_analyze_member_bytes(member, data, filename, config.analysis_config))
    return combine_analyses(prepared, config)


def _validate_member_identity(members: Sequence[AnalyzedPortfolioMember]) -> None:
    if not members:
        raise ValueError("at least one portfolio member is required")
    keys = [member.member_key for member in members]
    if len(set(keys)) != len(keys):
        raise DuplicatePortfolioMemberError("the same source report cannot be added twice")
    names = [member.member.strategy_name.casefold() for member in members]
    if len(set(names)) != len(names):
        raise DuplicatePortfolioMemberError("strategy names must be unique")


def _validate_weights(members: Sequence[AnalyzedPortfolioMember]) -> tuple[dict[str, float], dict[str, float]]:
    raw = {member.member_key: float(member.member.weight) for member in members}
    if any(not math.isfinite(value) or value < 0 for value in raw.values()):
        raise ValueError("portfolio weights must be finite and non-negative")
    total = sum(raw.values())
    if total <= 0:
        raise ValueError("at least one portfolio weight must be positive")
    return raw, {key: value / total for key, value in raw.items()}


def _validate_shared_metadata(
    members: Sequence[AnalyzedPortfolioMember],
) -> tuple[str, str | None]:
    currencies = {member.analysis.report.currency.strip().upper() for member in members}
    if len(currencies) != 1 or "" in currencies:
        raise CurrencyMismatchError(
            f"portfolio reports must use one non-empty currency; found {sorted(currencies)}"
        )
    timezones = {member.analysis.report.timezone for member in members}
    if len(timezones) != 1:
        raise TimezoneMismatchError(
            f"portfolio reports must use one timezone; found {sorted(timezones, key=str)}"
        )
    return next(iter(currencies)), next(iter(timezones))


def _curve_for_analysis(analysis: AnalysisResult, primary_curve: str) -> CurveResult | None:
    if primary_curve == "reconstructed":
        return analysis.balance
    if primary_curve == "source":
        return analysis.source_equity or analysis.source_balance
    # Match the single-report contract: source equity is preferred when it is
    # complete; a source balance series alone does not silently replace the
    # reconstructed closed-position balance used by the default analysis.
    return analysis.equity


def _scale_curve(curve: CurveResult, allocated_capital: float) -> CurveResult:
    if curve.initial_value == 0:
        values = tuple(allocated_capital for _ in curve.values)
    else:
        scale = allocated_capital / curve.initial_value
        values = tuple(float(value * scale) for value in curve.values)
    return CurveResult(
        timestamps=curve.timestamps,
        values=values,
        source=f"allocated:{curve.source}",
        basis=curve.basis,
        initial_value=allocated_capital,
    )


def _curve_value(curve: CurveResult, timestamp: datetime, allocated: bool = False) -> float:
    if not curve.timestamps:
        return curve.initial_value
    if timestamp < curve.timestamps[0]:
        return curve.initial_value
    index = bisect.bisect_right(curve.timestamps, timestamp) - 1
    index = max(0, min(index, len(curve.values) - 1))
    return float(curve.values[index])


def _aggregate_curves(
    curves: Sequence[CurveResult],
    capitals: Sequence[float],
    *,
    source: str,
    basis: str,
) -> CurveResult:
    if not curves:
        return CurveResult((), (), source, basis, 0.0)
    all_timestamps = sorted({timestamp for curve in curves for timestamp in curve.timestamps})
    initial = float(sum(capitals))
    if not all_timestamps:
        return CurveResult((), (initial,), source, basis, initial)
    first = all_timestamps[0]
    baseline = first - timedelta(microseconds=1) if first > datetime.min else first
    timestamps = [baseline] + all_timestamps
    values = []
    for timestamp in timestamps:
        values.append(float(sum(
            _curve_value(curve, timestamp)
            * (capital / curve.initial_value if curve.initial_value else 0.0)
            for curve, capital in zip(curves, capitals)
        )))
    return CurveResult(tuple(timestamps), tuple(values), source, basis, initial)


def _active_bounds(curve: CurveResult) -> tuple[datetime | None, datetime | None]:
    if not curve.timestamps:
        return None, None
    return curve.timestamps[0], curve.timestamps[-1]


def _member_monthly_values(
    curve: CurveResult,
    months: Sequence[str],
    active_start: datetime | None,
    active_end: datetime | None,
) -> list[float | None]:
    values: list[float | None] = []
    for period in months:
        year, month = (int(value) for value in period.split("-"))
        start = datetime(year, month, 1)
        if month == 12:
            next_start = datetime(year + 1, 1, 1)
        else:
            next_start = datetime(year, month + 1, 1)
        end = next_start - timedelta(microseconds=1)
        if active_start is None or active_end is None or end < active_start or start > active_end:
            values.append(None)
            continue
        beginning = _curve_value(curve, start)
        ending = _curve_value(curve, end)
        values.append((ending / beginning - 1.0) if beginning else None)
    return values


def _member_monthly_contributions(
    report: Report,
    months: Sequence[str],
    scale: float,
) -> tuple[list[float | None], list[float | None]]:
    raw: dict[str, float] = {month: 0.0 for month in months}
    allocated: dict[str, float] = {month: 0.0 for month in months}
    for trade in report.ordered_trades():
        if trade.close_time is None:
            continue
        month = f"{trade.close_time.year:04d}-{trade.close_time.month:02d}"
        if month in raw:
            raw[month] += trade.profit
            allocated[month] += trade.profit * scale
    return [raw[month] for month in months], [allocated[month] for month in months]


def _matrix_rows(columns: Sequence[Sequence[float | None]]) -> tuple[tuple[float | None, ...], ...]:
    return tuple(tuple(row) for row in zip(*columns)) if columns else ()


def _portfolio_matrix(
    rows: Sequence[str],
    strategy_names: Sequence[str],
    strategy_columns: Sequence[Sequence[float | None]],
    portfolio_column: Sequence[float | None],
    value_type: str,
) -> AnalysisMatrix:
    return AnalysisMatrix(
        tuple(rows),
        tuple(strategy_names) + ("PORTFOLIO",),
        _matrix_rows(tuple(strategy_columns) + (tuple(portfolio_column),)),
        value_type,
    )


def _correlation_matrices(
    matrix: AnalysisMatrix,
    config: PortfolioConfig,
    diagnostics: list[Diagnostic],
) -> tuple[AnalysisMatrix, AnalysisMatrix]:
    values = matrix.to_numpy()
    names = matrix.column_labels
    count = len(names)
    correlation = np.full((count, count), np.nan)
    covariance = np.full((count, count), np.nan)
    for left in range(count):
        for right in range(count):
            valid = np.isfinite(values[:, left]) & np.isfinite(values[:, right])
            observations = int(valid.sum())
            if observations < config.minimum_correlation_observations:
                diagnostics.append(Diagnostic(
                    "insufficient_correlation_observations",
                    "Correlation and covariance are undefined for insufficient overlapping months",
                    context={"left": names[left], "right": names[right], "observations": observations},
                ))
                continue
            left_values = values[valid, left]
            right_values = values[valid, right]
            if observations < config.correlation_warning_observations:
                diagnostics.append(Diagnostic(
                    "low_correlation_observations",
                    "Correlation uses fewer months than the recommended observation count",
                    context={"left": names[left], "right": names[right], "observations": observations},
                ))
            covariance[left, right] = float(np.cov(left_values, right_values, ddof=1)[0, 1])
            left_std = float(np.std(left_values, ddof=1))
            right_std = float(np.std(right_values, ddof=1))
            if left_std == 0 or right_std == 0:
                diagnostics.append(Diagnostic(
                    "undefined_correlation",
                    "Correlation is undefined when a return series has zero variance",
                    context={"left": names[left], "right": names[right]},
                ))
            else:
                correlation[left, right] = float(np.corrcoef(left_values, right_values)[0, 1])
    return (
        AnalysisMatrix.from_array(names, names, correlation, "monthly_return_correlation"),
        AnalysisMatrix.from_array(names, names, covariance, "monthly_return_covariance"),
    )


def _build_portfolio_periods(
    member_results: Sequence[PortfolioMemberResult],
    config: PortfolioConfig,
    currency: str,
    timezone: str | None,
    portfolio_capital: float,
    diagnostics: list[Diagnostic],
) -> tuple[
    dict[str, PortfolioPeriodResult],
    dict[str, DailyProfitCorrelationResult],
    dict[str, DailyProfitCorrelationResult],
]:
    """Combine compatible member periods using their effective intersection."""

    member_period_maps = [member.analysis.periods for member in member_results]
    if not any(member_period_maps):
        return {}, {}, {}
    all_names = set().union(*(set(item) for item in member_period_maps))
    common_names = set.intersection(*(set(item) for item in member_period_maps)) if member_period_maps else set()
    for name in sorted(all_names - common_names):
        diagnostics.append(Diagnostic(
            "portfolio_sample_period_missing_member",
            "A named sample period was not defined for every portfolio member",
            context={"period": name},
        ))

    period_results: dict[str, PortfolioPeriodResult] = {}
    correlations: dict[str, DailyProfitCorrelationResult] = {}
    weekly_correlations: dict[str, DailyProfitCorrelationResult] = {}
    for name in sorted(common_names):
        windows = [member.analysis.periods[name].window for member in member_results]
        start = max(window.start for window in windows)
        end = min(window.end for window in windows)
        if any(window.start != windows[0].start or window.end != windows[0].end for window in windows[1:]):
            diagnostics.append(Diagnostic(
                "portfolio_sample_period_differs",
                "Portfolio members do not share identical named sample-period boundaries; the intersection was used",
                context={
                    "period": name,
                    "member_windows": [window.to_dict() for window in windows],
                },
            ))
        if start >= end:
            diagnostics.append(Diagnostic(
                "portfolio_sample_period_no_overlap",
                "A named portfolio sample period has no common date range",
                context={"period": name},
            ))
            continue
        effective_window = PeriodWindow(
            name,
            start,
            end,
            source="explicit",
            evidence=("intersection of portfolio member windows",),
        )
        allocated_trades: list[Trade] = []
        period_capitals: list[float] = []
        period_members: list[tuple[str, Sequence[Trade], float]] = []
        period_warnings: list[Diagnostic] = []
        for member in member_results:
            period = member.analysis.periods[name]
            period_capitals.append(period.metrics.initial_deposit * member.allocation_scale)
            period_warnings.extend(period.warnings)
            period_members.append((member.strategy_name, period.analysis.report.trades, member.allocation_scale))
            for trade in period.analysis.report.ordered_trades():
                allocated_trades.append(replace(
                    trade,
                    profit=trade.profit * member.allocation_scale,
                    swap=trade.swap * member.allocation_scale,
                    commission=trade.commission * member.allocation_scale,
                    strategy_id=member.member_key,
                    source_report_hash=member.member_key,
                    allocation_scale=member.allocation_scale,
                ))
        period_report = Report(
            trades=allocated_trades,
            initial_deposit=float(sum(period_capitals)),
            currency=currency,
            source_format="portfolio",
            strategy_name=f"Portfolio {name}",
            timezone=timezone,
        )
        period_analysis = analyze(
            period_report,
            replace(config.analysis_config, sample_periods=None),
        )
        daily = build_daily_profit_correlation(
            period_members,
            scope=name,
            timezone=timezone,
            minimum_observations=config.minimum_correlation_observations,
            warning_observations=config.correlation_warning_observations,
        )
        weekly = build_weekly_profit_correlation_from_daily(
            daily,
            minimum_observations=config.minimum_correlation_observations,
            warning_observations=config.correlation_warning_observations,
        )
        period_warnings.extend(daily.warnings)
        period_warnings.extend(weekly.warnings)
        period_analysis = replace(
            period_analysis,
            warnings=tuple(list(period_analysis.warnings) + period_warnings),
            provenance={
                **period_analysis.provenance,
                "sample_period": effective_window.to_dict(),
                "sample_period_name": name,
            },
        )
        period_results[name] = PortfolioPeriodResult(
            name=name,
            window=effective_window,
            analysis=period_analysis,
            daily_profit_correlation=daily,
            weekly_profit_correlation=weekly,
            warnings=tuple(period_warnings),
        )
        correlations[name] = daily
        weekly_correlations[name] = weekly
        diagnostics.extend(period_warnings)
    return period_results, correlations, weekly_correlations


def _strict_or_warn(config: PortfolioConfig, diagnostics: list[Diagnostic]) -> None:
    if config.strict and diagnostics:
        raise PortfolioValidationError(diagnostics[0].message)


def combine_analyses(
    members: Sequence[AnalyzedPortfolioMember],
    config: PortfolioConfig | None = None,
) -> PortfolioAnalysisResult:
    """Combine already analysed members without reparsing their reports."""

    config = config or PortfolioConfig()
    config.validate()
    _validate_member_identity(members)
    raw_weights, normalized_weights = _validate_weights(members)
    currency, timezone = _validate_shared_metadata(members)

    deposits = [member.analysis.report.initial_deposit for member in members]
    if any(deposit <= 0 or not math.isfinite(deposit) for deposit in deposits):
        raise ValueError("all member initial deposits must be positive and finite")
    portfolio_capital = (
        float(config.portfolio_initial_capital)
        if config.portfolio_initial_capital is not None
        else float(sum(deposits))
    )

    diagnostics: list[Diagnostic] = []
    member_results: list[PortfolioMemberResult] = []
    selected_raw_curves: list[CurveResult] = []
    selected_allocated_curves: list[CurveResult] = []
    selected_raw_capitals: list[float] = []
    allocated_capitals: list[float] = []
    reconstructed_curves: list[CurveResult] = []
    source_balance_curves: list[CurveResult] = []
    source_equity_curves: list[CurveResult] = []
    strategy_names: list[str] = []
    all_allocated_trades: list[Trade] = []

    first_start: datetime | None = None
    first_end: datetime | None = None
    for prepared in members:
        report = prepared.analysis.report
        strategy_names.append(prepared.member.strategy_name)
        allocation = portfolio_capital * normalized_weights[prepared.member_key]
        scale = allocation / report.initial_deposit
        reconstructed = report_curve = prepared.analysis.balance
        if report_curve is None:
            raise ValueError(f"member {prepared.member_key} has no reconstructed curve")
        selected = _curve_for_analysis(prepared.analysis, config.primary_curve)
        if selected is None:
            if config.primary_curve == "source":
                diagnostics.append(Diagnostic(
                    "missing_source_curve_fallback",
                    "A member had no source curve and was excluded from the requested source-only curve",
                    context={"member_key": prepared.member_key},
                ))
                selected = reconstructed
            else:
                selected = reconstructed
        raw_selected = selected
        allocated_selected = _scale_curve(selected, allocation)
        raw_drawdown_analysis = analyze_drawdowns(raw_selected)
        allocated_drawdown_analysis = analyze_drawdowns(allocated_selected)
        raw_reconstructed = reconstructed
        active_start, active_end = _active_bounds(raw_selected)
        if first_start is None:
            first_start, first_end = active_start, active_end
        elif active_start != first_start or active_end != first_end:
            diagnostics.append(Diagnostic(
                "member_active_period_differs",
                "Portfolio members do not share identical active periods",
                context={
                    "member_key": prepared.member_key,
                    "active_start": active_start.isoformat() if active_start else None,
                    "active_end": active_end.isoformat() if active_end else None,
                },
            ))
        selected_raw_curves.append(raw_selected)
        selected_allocated_curves.append(allocated_selected)
        selected_raw_capitals.append(raw_selected.initial_value)
        allocated_capitals.append(allocation)
        reconstructed_curves.append(raw_reconstructed)
        if prepared.analysis.source_balance is not None:
            source_balance_curves.append(prepared.analysis.source_balance)
        if prepared.analysis.source_equity is not None:
            source_equity_curves.append(prepared.analysis.source_equity)
        for trade in report.ordered_trades():
            all_allocated_trades.append(replace(
                trade,
                profit=trade.profit * scale,
                swap=trade.swap * scale,
                commission=trade.commission * scale,
                strategy_id=prepared.member_key,
                source_report_hash=prepared.member_key,
                allocation_scale=scale,
            ))
        member_results.append(PortfolioMemberResult(
            member_key=prepared.member_key,
            strategy_name=prepared.member.strategy_name,
            description=prepared.member.description,
            weight=raw_weights[prepared.member_key],
            normalized_weight=normalized_weights[prepared.member_key],
            allocated_capital=allocation,
            allocation_scale=scale,
            analysis=prepared.analysis,
            raw_curve=raw_selected,
            allocated_curve=allocated_selected,
            raw_drawdown_analysis=raw_drawdown_analysis,
            allocated_drawdown_analysis=allocated_drawdown_analysis,
            active_start=active_start,
            active_end=active_end,
        ))
        diagnostics.extend(prepared.analysis.warnings)
        diagnostics.extend(raw_drawdown_analysis.warnings)
        diagnostics.extend(allocated_drawdown_analysis.warnings)

    reconstructed_allocated = _aggregate_curves(
        reconstructed_curves,
        allocated_capitals,
        source="portfolio_reconstructed_closed_positions",
        basis="balance",
    )
    raw_portfolio_curve = _aggregate_curves(
        selected_raw_curves,
        selected_raw_capitals,
        source="portfolio_raw_member_curves",
        basis="equity" if all(curve.basis == "equity" for curve in selected_raw_curves) else "balance",
    )
    allocated_portfolio_curve = _aggregate_curves(
        selected_allocated_curves,
        allocated_capitals,
        source="portfolio_allocated_member_curves",
        basis="equity" if all(curve.basis == "equity" for curve in selected_allocated_curves) else "balance",
    )
    portfolio_drawdown_analysis = analyze_drawdowns(allocated_portfolio_curve)

    source_balance = None
    if len(source_balance_curves) == len(members):
        source_balance = _aggregate_curves(
            source_balance_curves,
            allocated_capitals,
            source="portfolio_source_balance",
            basis="balance",
        )
    source_equity = None
    if len(source_equity_curves) == len(members):
        source_equity = _aggregate_curves(
            source_equity_curves,
            allocated_capitals,
            source="portfolio_source_equity",
            basis="equity",
        )

    portfolio_report = Report(
        trades=all_allocated_trades,
        initial_deposit=portfolio_capital,
        currency=currency,
        source_format="portfolio",
        strategy_name="Portfolio",
        timezone=timezone,
    )
    trade_profit = build_trade_profit_analysis(
        portfolio_report.ordered_trades(),
        initial_capital=portfolio_capital,
        currency=currency,
        timezone=timezone,
        config=config.analysis_config.trade_profit,
    )
    raw_trade_profit = {
        member.strategy_name: member.analysis.trade_profit
        for member in member_results
    }
    metric_diagnostics = list(diagnostics)
    metric_diagnostics.extend(portfolio_drawdown_analysis.warnings)
    metric_diagnostics.extend(trade_profit.warnings)
    metrics = compute_metrics(
        portfolio_report,
        primary_curve=CurveSeries(
            allocated_portfolio_curve.timestamps,
            allocated_portfolio_curve.values,
            allocated_portfolio_curve.source,
            allocated_portfolio_curve.basis,
            allocated_portfolio_curve.initial_value,
        ),
        config=config.analysis_config,
        diagnostics=metric_diagnostics,
    )
    primary_series = CurveSeries(
        allocated_portfolio_curve.timestamps,
        allocated_portfolio_curve.values,
        allocated_portfolio_curve.source,
        allocated_portfolio_curve.basis,
        allocated_portfolio_curve.initial_value,
    )
    monthly, monthly_drawdown = _curve_monthly(primary_series, portfolio_report)
    monthly_performance = _monthly_performance_table(
        monthly, allocated_portfolio_curve.source, allocated_portfolio_curve.basis
    )
    months = [row.period for row in monthly]

    raw_monthly_returns: list[list[float | None]] = []
    allocated_monthly_returns: list[list[float | None]] = []
    raw_monthly_contributions: list[list[float | None]] = []
    allocated_monthly_contributions: list[list[float | None]] = []
    for member_result in member_results:
        raw_monthly_returns.append(_member_monthly_values(
            member_result.raw_curve,
            months,
            member_result.active_start,
            member_result.active_end,
        ))
        allocated_monthly_returns.append(_member_monthly_values(
            member_result.allocated_curve,
            months,
            member_result.active_start,
            member_result.active_end,
        ))
        raw_contribution, allocated_contribution = _member_monthly_contributions(
            member_result.analysis.report,
            months,
            member_result.allocation_scale,
        )
        raw_monthly_contributions.append(raw_contribution)
        allocated_monthly_contributions.append(allocated_contribution)

    raw_portfolio_monthly_returns = _member_monthly_values(
        raw_portfolio_curve,
        months,
        raw_portfolio_curve.timestamps[0] if raw_portfolio_curve.timestamps else None,
        raw_portfolio_curve.timestamps[-1] if raw_portfolio_curve.timestamps else None,
    )
    allocated_portfolio_monthly_returns = [row.return_on_starting_equity for row in monthly]
    raw_portfolio_contribution = [sum(
        column[index] or 0.0 for column in raw_monthly_contributions
    ) for index in range(len(months))]
    allocated_portfolio_contribution = [row.pnl for row in monthly]

    equity_rows = [timestamp.isoformat() for timestamp in allocated_portfolio_curve.timestamps]
    equity_strategy_columns = [
        [
            _curve_value(member.allocated_curve, timestamp)
            for timestamp in allocated_portfolio_curve.timestamps
        ]
        for member in member_results
    ]
    equity_matrix = _portfolio_matrix(
        equity_rows,
        strategy_names,
        equity_strategy_columns,
        list(allocated_portfolio_curve.values),
        "allocated_equity",
    )
    raw_equity_matrix = _portfolio_matrix(
        equity_rows,
        strategy_names,
        [
            [
                _curve_value(member.raw_curve, timestamp)
                for timestamp in raw_portfolio_curve.timestamps
            ]
            for member in member_results
        ],
        list(raw_portfolio_curve.values),
        "raw_equity",
    )
    raw_return_matrix = _portfolio_matrix(
        months,
        strategy_names,
        raw_monthly_returns,
        raw_portfolio_monthly_returns,
        "raw_monthly_return",
    )
    allocated_return_matrix = _portfolio_matrix(
        months,
        strategy_names,
        allocated_monthly_returns,
        allocated_portfolio_monthly_returns,
        "allocated_monthly_return",
    )
    raw_contribution_matrix = _portfolio_matrix(
        months,
        strategy_names,
        raw_monthly_contributions,
        raw_portfolio_contribution,
        "raw_monthly_contribution",
    )
    allocated_contribution_matrix = _portfolio_matrix(
        months,
        strategy_names,
        allocated_monthly_contributions,
        allocated_portfolio_contribution,
        "allocated_monthly_contribution",
    )
    correlation_matrix, covariance_matrix = _correlation_matrices(
        AnalysisMatrix(
            tuple(months),
            tuple(strategy_names),
            _matrix_rows(allocated_monthly_returns),
            "allocated_monthly_return_for_correlation",
        ),
        config,
        metric_diagnostics,
    )
    daily_profit = build_daily_profit_correlation(
        [
            (member.strategy_name, member.analysis.report.ordered_trades(), member.allocation_scale)
            for member in member_results
        ],
        scope="full_sample",
        timezone=timezone,
        minimum_observations=config.minimum_correlation_observations,
        warning_observations=config.correlation_warning_observations,
    )
    weekly_profit = build_weekly_profit_correlation_from_daily(
        daily_profit,
        minimum_observations=config.minimum_correlation_observations,
        warning_observations=config.correlation_warning_observations,
    )
    metric_diagnostics.extend(daily_profit.warnings)
    metric_diagnostics.extend(weekly_profit.warnings)
    portfolio_periods, period_correlations, weekly_period_correlations = _build_portfolio_periods(
        member_results,
        config,
        currency,
        timezone,
        portfolio_capital,
        metric_diagnostics,
    )
    correlations = CorrelationResults(
        daily_profit=daily_profit,
        weekly_profit=weekly_profit,
        by_period=period_correlations,
        weekly_by_period=weekly_period_correlations,
    )
    validation_status = "warn" if metric_diagnostics else "match"
    validation = ValidationResult(
        status=validation_status,
        checks={
            "member_count": len(members),
            "currency": currency,
            "timezone": timezone,
            "portfolio_initial_capital": portfolio_capital,
            "allocated_capital_total": sum(allocated_capitals),
        },
        discrepancies=(),
    )
    _strict_or_warn(config, metric_diagnostics)
    provenance = {
        "input_reports": [
            {
                "member_key": member.member_key,
                "strategy_name": member.strategy_name,
                "description": member.description,
                "input_sha256": member.member_key,
                "weight": member.weight,
                "normalized_weight": member.normalized_weight,
            }
            for member in member_results
        ],
        "portfolio_initial_capital": portfolio_capital,
        "currency": currency,
        "timezone": timezone,
        "portfolio_config": config.to_dict(),
        "parser_version": "2",
        "package_version": "0.1.1",
    }
    return PortfolioAnalysisResult(
        members=tuple(member_results),
        portfolio_initial_capital=portfolio_capital,
        currency=currency,
        timezone=timezone,
        raw_weights=raw_weights,
        normalized_weights=normalized_weights,
        portfolio_report=portfolio_report,
        metrics=metrics,
        reported_metrics={},
        balance=CurveResult.from_curve(CurveSeries(
            reconstructed_allocated.timestamps,
            reconstructed_allocated.values,
            reconstructed_allocated.source,
            reconstructed_allocated.basis,
            reconstructed_allocated.initial_value,
        )),
        equity=allocated_portfolio_curve,
        source_balance=source_balance,
        source_equity=source_equity,
        monthly=monthly,
        monthly_drawdown=monthly_drawdown,
        drawdown_analysis=portfolio_drawdown_analysis,
        monthly_performance=monthly_performance,
        trade_profit=trade_profit,
        raw_trade_profit=raw_trade_profit,
        raw_equity_matrix=raw_equity_matrix,
        equity_matrix=equity_matrix,
        raw_monthly_return_matrix=raw_return_matrix,
        allocated_monthly_return_matrix=allocated_return_matrix,
        raw_monthly_contribution_matrix=raw_contribution_matrix,
        allocated_monthly_contribution_matrix=allocated_contribution_matrix,
        correlation_matrix=correlation_matrix,
        covariance_matrix=covariance_matrix,
        correlations=correlations,
        periods=portfolio_periods,
        validation=validation,
        warnings=tuple(metric_diagnostics),
        provenance=provenance,
        config=config,
    )
