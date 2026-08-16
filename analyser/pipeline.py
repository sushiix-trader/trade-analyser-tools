"""Internal deterministic preparation of transformed analysis views.

This module is deliberately private.  It owns the ordering policy for trade
transformations while the public analysis module remains responsible for
calculating metrics, curves, and result objects from a prepared report.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from .diagnostics import Diagnostic
from .equity import reconstructed_curve
from .filters import FilterConfig, TradeFilter, TradeSelection, filter_fingerprint, select_trades
from .models import Report, Trade
from .periods import PeriodWindow, SamplePeriodConfig
from .what_if import WhatIfConfig, WhatIfResult, transform_report


@dataclass(frozen=True)
class TransformationPlan:
    """Internal immutable intent for one deterministic analysis preparation."""

    sample_periods: SamplePeriodConfig | None = None
    filter_spec: TradeFilter | None = None
    filter_config: FilterConfig = field(default_factory=FilterConfig)
    what_if: WhatIfConfig | None = None
    source_validation: dict[str, Any] | None = None


@dataclass(frozen=True)
class PreparedView:
    """One full-sample or named-period report ready for core analysis."""

    report: Report
    selection: TradeSelection | None = None
    what_if: WhatIfResult | None = None
    diagnostics: tuple[Diagnostic, ...] = ()
    period_name: str | None = None
    period_window: PeriodWindow | None = None
    source_trade_count: int = 0
    selected_trade_count: int = 0
    cross_boundary_trade_count: int = 0
    excluded_trade_count: int = 0


@dataclass(frozen=True)
class PreparedAnalysis:
    """Prepared full-sample and period views from one canonical report."""

    source_report: Report
    full: PreparedView
    periods: tuple[PreparedView, ...] = ()
    sample_period_warnings: tuple[Diagnostic, ...] = ()


def _balance_before(report: Report, timestamp: datetime) -> float:
    curve = reconstructed_curve(report)
    value = curve.initial_value
    for point_time, point_value in zip(curve.timestamps, curve.values):
        if point_time < timestamp:
            value = float(point_value)
        else:
            break
    return value


def _trade_key(trade: Trade) -> tuple[str, str, str]:
    # Preserve the existing externally visible period-assignment identity.
    return trade.ticket, trade.position_id or "", trade.symbol


def _period_views(
    report: Report,
    sample_periods: SamplePeriodConfig | None,
) -> tuple[tuple[PreparedView, ...], tuple[Diagnostic, ...]]:
    if sample_periods is None or not sample_periods.enabled:
        return (), ()

    source_trade_count = len(report.trades)
    assigned_keys: set[tuple[str, str, str]] = set()
    prepared: list[PreparedView] = []
    for name, window in sample_periods.windows.items():
        selected = [
            trade for trade in report.ordered_trades()
            if window.contains(trade.open_time)
        ]
        assigned_keys.update(_trade_key(trade) for trade in selected)
        warnings: list[Diagnostic] = []
        cross_boundary = 0
        for trade in selected:
            if trade.open_time_inferred:
                warnings.append(Diagnostic(
                    "sample_period_inferred_open_time",
                    "A sample-period trade used an inferred open time",
                    context={"period": name, "ticket": trade.ticket},
                ))
            if trade.close_time is None or not window.contains(trade.close_time):
                cross_boundary += 1
                warnings.append(Diagnostic(
                    "sample_period_cross_boundary_trade",
                    "A completed position opened in the period but closed outside its boundaries",
                    context={
                        "period": name,
                        "ticket": trade.ticket,
                        "open_time": trade.open_time.isoformat() if trade.open_time else None,
                        "close_time": trade.close_time.isoformat() if trade.close_time else None,
                    },
                ))
        period_report = replace(
            report,
            trades=selected,
            initial_deposit=_balance_before(report, window.start),
            source_balance_points=[],
            source_equity_points=[],
            reported_metrics={},
            warnings=[],
            metadata={
                **report.metadata,
                "sample_period": name,
                "sample_period_start": window.start.isoformat(),
                "sample_period_end": window.end.isoformat(),
            },
        )
        prepared.append(PreparedView(
            report=period_report,
            diagnostics=tuple(warnings),
            period_name=name,
            period_window=window,
            source_trade_count=source_trade_count,
            selected_trade_count=len(selected),
            cross_boundary_trade_count=cross_boundary,
            excluded_trade_count=source_trade_count - len(selected),
        ))

    outside = source_trade_count - len(assigned_keys)
    sample_warnings = ()
    if outside:
        sample_warnings = (Diagnostic(
            "sample_period_trades_outside_windows",
            "Some completed positions were outside all named sample periods",
            context={"trade_count": outside},
        ),)
    return tuple(prepared), sample_warnings


def _prepare_view(
    base_view: PreparedView,
    source_report: Report,
    plan: TransformationPlan,
) -> PreparedView:
    current = base_view.report
    selection = None
    diagnostics = list(base_view.diagnostics)

    if plan.filter_spec is not None:
        selected, selection, filter_diagnostics = select_trades(
            current,
            plan.filter_spec,
            plan.filter_config,
        )
        filtered_metadata = dict(current.metadata)
        if base_view.period_name is None:
            filtered_metadata.update({
                "filtered": True,
                "filter_fingerprint": filter_fingerprint(plan.filter_spec, plan.filter_config),
                "selected_trade_count": selection.selected_trade_count,
                "source_trade_count": selection.source_trade_count,
            })
        report_warnings = list(current.warnings)
        report_warnings.extend(diagnostic.to_dict() for diagnostic in filter_diagnostics)
        if base_view.period_name is None:
            report_warnings.append({
                "code": "filtered_source_curves_unavailable",
                "message": "Filtered analysis uses reconstructed closed-position equity; source account curves were not filterable",
                "severity": "warning",
                "context": {},
            })
        current = replace(
            current,
            trades=selected,
            timezone=source_report.timezone or plan.filter_config.report_timezone,
            source_balance_points=[],
            source_equity_points=[],
            reported_metrics={},
            warnings=report_warnings,
            metadata=filtered_metadata,
        )

    what_if_result = None
    if plan.what_if is not None:
        sizing_report = current
        # Period metrics retain segment-relative capital, while risk sizing
        # remains based on the original report's fixed capital base.
        if (
            base_view.period_name is not None
            and plan.what_if.initial_capital is None
            and plan.what_if.mode in {WhatIfConfig.PERCENT_RISK, WhatIfConfig.DOLLAR_RISK}
        ):
            sizing_report = replace(sizing_report, initial_deposit=source_report.initial_deposit)
        if not current.trades and base_view.period_name is not None:
            capital_base = (
                float(plan.what_if.initial_capital)
                if plan.what_if.initial_capital is not None
                else float(source_report.initial_deposit)
            )
            what_if_result = WhatIfResult(
                config=plan.what_if,
                capital_base=capital_base,
                original_trade_count=0,
                transformed_trade_count=0,
                excluded_trade_count=0,
                audits=(),
                warnings=(),
            )
            current = replace(
                current,
                metadata={**current.metadata, "what_if": what_if_result.to_dict()},
            )
        else:
            current, what_if_result = transform_report(sizing_report, plan.what_if)
        if sizing_report is not base_view.report:
            current = replace(current, initial_deposit=base_view.report.initial_deposit)
        diagnostics.extend(what_if_result.warnings)

    return replace(
        base_view,
        report=current,
        selection=selection,
        what_if=what_if_result,
        diagnostics=tuple(diagnostics),
    )


def prepare_analysis(report: Report, plan: TransformationPlan | None = None) -> PreparedAnalysis:
    """Prepare every analysis view from one original canonical report."""

    plan = plan or TransformationPlan()
    periods, sample_warnings = _period_views(report, plan.sample_periods)
    full = _prepare_view(PreparedView(report=report), report, plan)
    prepared_periods = tuple(_prepare_view(period, report, plan) for period in periods)
    return PreparedAnalysis(
        source_report=report,
        full=full,
        periods=prepared_periods,
        sample_period_warnings=sample_warnings,
    )
