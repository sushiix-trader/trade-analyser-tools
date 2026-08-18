"""Deterministic chart artifacts built from eager analysis results.

Chart rendering is deliberately optional and separate from the analytical
engine.  It consumes the already-selected ``result.equity`` curve, so a chart
never silently recalculates or chooses a different equity basis.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np

from .matrices import AnalysisMatrix
from .trade_profit import (
    TradeProfitAnalysis,
    TradeProfitGrouping,
    TradeProfitGroupingResult,
    TradeProfitMeasure,
)


@dataclass(frozen=True)
class ChartConfig:
    """Deterministic visual options for equity/drawdown charts."""

    show_sample_periods: bool = False
    show_member_equity: bool = False
    normalize_equity: bool = False
    show_excluded_periods: bool = True
    in_sample_color: str = "#4f81bd"
    out_of_sample_color: str = "#f28e2b"
    excluded_color: str = "#9e9e9e"


_MEMBER_EQUITY_COLORS = (
    "#2ca02c",
    "#9467bd",
    "#ff7f0e",
    "#17becf",
    "#8c564b",
    "#e377c2",
    "#7f7f7f",
    "#bcbd22",
)


@dataclass(frozen=True)
class MonthlyPerformanceTableChartConfig:
    """Deterministic visual options for monthly-performance table images."""

    dpi: int = 140
    font_size: float = 10.0
    title_size: float = 14.0
    header_color: str = "#4f81bd"
    header_text_color: str = "#ffffff"
    positive_color: str = "#188038"
    negative_color: str = "#c62828"
    neutral_color: str = "#374151"
    missing_color: str = "#6b7280"
    odd_row_color: str = "#f5f7fa"
    border_color: str = "#d1d5db"

    def validate(self) -> None:
        if self.dpi <= 0:
            raise ValueError("dpi must be positive")
        if self.font_size <= 0.0 or self.title_size <= 0.0:
            raise ValueError("table font sizes must be positive")


@dataclass(frozen=True)
class TradeProfitBarChartConfig:
    """Deterministic visual options for grouped trade-profit bar charts."""

    positive_color: str = "#188038"
    negative_color: str = "#c62828"
    zero_color: str = "#6b7280"
    show_value_labels: bool = True
    show_trade_counts: bool = True
    dpi: int = 140
    font_size: float = 8.5
    title_size: float = 14.0

    def validate(self) -> None:
        if self.dpi <= 0:
            raise ValueError("trade-profit chart dpi must be positive")
        if self.font_size <= 0.0 or self.title_size <= 0.0:
            raise ValueError("trade-profit chart font sizes must be positive")


@dataclass(frozen=True)
class MonteCarloPathInterval:
    """A percentile interval to shade around simulated Monte Carlo paths."""

    lower_percentile: float
    upper_percentile: float
    color: str = "#4f81bd"
    alpha: float = 0.18
    label: str | None = None

    def validate(self) -> None:
        if not 0.0 <= self.lower_percentile < self.upper_percentile <= 100.0:
            raise ValueError(
                "Monte Carlo interval percentiles must satisfy "
                "0 <= lower < upper <= 100"
            )
        if not 0.0 < self.alpha <= 1.0:
            raise ValueError("Monte Carlo interval alpha must be greater than 0 and at most 1")

    @property
    def display_label(self) -> str:
        return self.label or (
            f"{self.lower_percentile:g}–{self.upper_percentile:g}th percentile interval"
        )


@dataclass(frozen=True)
class MonteCarloPathChartConfig:
    """Visual options for a deterministic simulated-equity-path chart.

    Percentile intervals are calculated across the retained paths at each
    simulated trade step.  The widest interval is rendered first, so narrower
    user-supplied intervals remain visible on top.
    """

    intervals: tuple[MonteCarloPathInterval, ...] = (
        MonteCarloPathInterval(5.0, 95.0, color="#4f81bd", alpha=0.16),
        MonteCarloPathInterval(25.0, 75.0, color="#1f77b4", alpha=0.24),
    )
    path_color: str = "#6b7280"
    path_alpha: float = 0.08
    path_linewidth: float = 0.45
    median_color: str = "#111827"
    median_linewidth: float = 1.4
    show_drawdown: bool = True
    show_streaks: bool = False

    def validate(self) -> None:
        if not self.intervals:
            raise ValueError("at least one Monte Carlo path interval is required")
        for interval in self.intervals:
            interval.validate()
        if not 0.0 < self.path_alpha <= 1.0:
            raise ValueError("path_alpha must be greater than 0 and at most 1")
        if self.path_linewidth <= 0.0 or self.median_linewidth <= 0.0:
            raise ValueError("path line widths must be positive")


def _matplotlib():
    try:
        import matplotlib
        matplotlib.use("Agg", force=True)
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError(
            "Chart rendering requires matplotlib; install the 'charts' extra"
        ) from exc
    return matplotlib, mdates, plt


def _report_metadata(result: Any) -> tuple[str, str]:
    report = getattr(result, "report", None) or getattr(result, "portfolio_report", None)
    if report is None:
        return "Strategy", "account currency"
    name = getattr(report, "strategy_name", "") or "Strategy"
    currency = getattr(report, "currency", "") or "account currency"
    return name, currency


def _portfolio_member_equity_columns(
    result: Any,
    timestamps: list[Any],
) -> tuple[tuple[str, np.ndarray], ...]:
    """Return capital-allocated member equity columns aligned to ``timestamps``."""

    equity_matrix = getattr(result, "equity_matrix", None)
    if equity_matrix is None:
        raise ValueError(
            "show_member_equity=True requires a PortfolioAnalysisResult"
        )
    if len(equity_matrix.row_labels) != len(timestamps):
        raise ValueError(
            "portfolio equity matrix rows must align with the portfolio equity curve"
        )
    try:
        portfolio_index = equity_matrix.column_labels.index("PORTFOLIO")
    except ValueError as exc:
        raise ValueError(
            "portfolio equity matrix must contain a PORTFOLIO column"
        ) from exc

    columns: list[tuple[str, np.ndarray]] = []
    for index, label in enumerate(equity_matrix.column_labels):
        if index == portfolio_index:
            continue
        values = np.asarray(
            [
                np.nan if value is None else value
                for value in (row[index] for row in equity_matrix.values)
            ],
            dtype=float,
        )
        columns.append((label, values))
    return tuple(columns)


def _normalize_curve(values: np.ndarray, label: str) -> np.ndarray:
    """Convert an equity curve to cumulative percentage return.

    Each curve is normalized against its own first finite equity value.  This
    makes allocated member curves comparable even when their opening capital
    differs from the portfolio opening capital.
    """

    finite_indexes = np.flatnonzero(np.isfinite(values))
    if finite_indexes.size == 0:
        raise ValueError(f"cannot normalize {label}: equity curve has no finite values")
    initial = float(values[finite_indexes[0]])
    if initial == 0.0:
        raise ValueError(f"cannot normalize {label}: opening equity is zero")
    normalized = (values / initial - 1.0) * 100.0
    normalized[~np.isfinite(values)] = np.nan
    return normalized


def _high_water_mark_drawdown(
    values: np.ndarray,
    *,
    percentage: bool,
) -> np.ndarray:
    """Return signed drawdown from the running high-water mark.

    Percentage drawdown uses the high-water mark as its denominator, matching
    ``Metrics.max_drawdown_pct``. Currency drawdown remains a signed money
    difference from that same high-water mark.
    """

    peaks = np.maximum.accumulate(values)
    if not percentage:
        return values - peaks
    with np.errstate(divide="ignore", invalid="ignore"):
        drawdown = np.divide(
            values - peaks,
            peaks,
            out=np.zeros_like(values),
            where=peaks != 0,
        )
    return drawdown * 100.0


def render_equity_drawdown_chart(
    result: Any,
    *,
    title: str | None = None,
    image_format: str = "png",
    dpi: int = 140,
    chart_config: ChartConfig | None = None,
    show_sample_periods: bool | None = None,
    show_member_equity: bool | None = None,
    normalize_equity: bool | None = None,
) -> bytes:
    """Render a deterministic PNG/SVG chart containing equity and drawdown.

    The upper panel shows the selected eager-analysis equity curve.  The lower
    panel shows high-water-mark drawdown in account currency, with zero at the
    top and larger losses extending downward.  For a portfolio,
    ``show_member_equity=True`` overlays each member's capital-allocated equity
    curve on the upper panel.  With ``normalize_equity=True``, every curve is
    rebased to its own opening capital, the upper panel shows cumulative
    percentage return from 0.00%, and the lower panel shows peak-relative
    percentage drawdown, matching the analytical metrics. ``result`` is an
    ``AnalysisResult`` or a compatible ``PortfolioAnalysisResult``.
    """

    if image_format.lower() not in {"png", "svg"}:
        raise ValueError("image_format must be 'png' or 'svg'")
    if dpi <= 0:
        raise ValueError("dpi must be positive")
    chart_config = chart_config or ChartConfig()
    if show_sample_periods is not None:
        chart_config = replace(chart_config, show_sample_periods=show_sample_periods)
    if show_member_equity is not None:
        chart_config = replace(chart_config, show_member_equity=show_member_equity)
    if normalize_equity is not None:
        chart_config = replace(chart_config, normalize_equity=normalize_equity)

    equity = getattr(result, "equity", None)
    if equity is None or not equity.timestamps or not equity.values:
        raise ValueError("result does not contain an equity curve")
    if len(equity.timestamps) != len(equity.values):
        raise ValueError("equity timestamps and values must have equal lengths")

    timestamps = list(equity.timestamps)
    member_equity_columns = (
        _portfolio_member_equity_columns(result, timestamps)
        if chart_config.show_member_equity
        else ()
    )
    matplotlib, mdates, plt = _matplotlib()
    strategy_name, currency = _report_metadata(result)
    raw_values = np.asarray(equity.values, dtype=float)
    if chart_config.normalize_equity:
        values = _normalize_curve(raw_values, "portfolio equity")
        member_equity_columns = tuple(
            (label, _normalize_curve(member_values, f"{label} equity"))
            for label, member_values in member_equity_columns
        )
        drawdown = _high_water_mark_drawdown(raw_values, percentage=True)
    else:
        values = raw_values
        drawdown = _high_water_mark_drawdown(values, percentage=False)
    maximum_drawdown = float(np.min(drawdown))
    equity_ylabel = (
        "Cumulative return (%)" if chart_config.normalize_equity
        else f"Equity ({currency})"
    )
    drawdown_ylabel = (
        "Drawdown (%)" if chart_config.normalize_equity
        else f"Drawdown ({currency})"
    )
    drawdown_maximum_label = (
        f"{abs(maximum_drawdown):,.2f}%"
        if chart_config.normalize_equity
        else f"{abs(maximum_drawdown):,.2f} {currency}"
    )

    with matplotlib.rc_context(
        {
            "figure.dpi": dpi,
            "savefig.dpi": dpi,
            "font.family": "DejaVu Sans",
            "axes.grid": True,
            "grid.alpha": 0.25,
        }
    ):
        fig, (equity_axis, drawdown_axis) = plt.subplots(
            2,
            1,
            figsize=(12, 7),
            sharex=True,
            gridspec_kw={"height_ratios": (2.2, 1)},
            constrained_layout=True,
        )
        fig.suptitle(title or f"{strategy_name} — Equity and Drawdown", fontsize=14, fontweight="bold")

        if chart_config.show_member_equity:
            for index, (label, member_values) in enumerate(member_equity_columns):
                equity_axis.plot(
                    timestamps,
                    member_values,
                    color=_MEMBER_EQUITY_COLORS[index % len(_MEMBER_EQUITY_COLORS)],
                    linewidth=0.95,
                    alpha=0.9,
                    label=(
                        f"{label} (allocated return)"
                        if chart_config.normalize_equity
                        else f"{label} (allocated)"
                    ),
                )
            equity_axis.plot(
                timestamps,
                values,
                color="#111827",
                linewidth=1.8,
                label=(
                    "Portfolio return"
                    if chart_config.normalize_equity
                    else "Portfolio (allocated)"
                ),
            )
        else:
            equity_axis.plot(
                timestamps,
                values,
                color="#1f77b4",
                linewidth=1.25,
                label=(
                    f"Cumulative return ({equity.source}, {equity.basis})"
                    if chart_config.normalize_equity
                    else f"Equity ({equity.source}, {equity.basis})"
                ),
            )
        equity_axis.set_ylabel(equity_ylabel)
        equity_axis.legend(loc="upper left", frameon=False)
        equity_axis.set_title("Selected analysis equity curve", loc="left", fontsize=10)

        if chart_config.show_sample_periods:
            period_results = getattr(result, "periods", {}) or {}
            windows = []
            for period_name, period_result in period_results.items():
                window = getattr(period_result, "window", None)
                if window is not None:
                    windows.append((period_name, window))
            windows.sort(key=lambda item: (item[1].start, item[1].end, item[0]))
            colors = {
                "in_sample": chart_config.in_sample_color,
                "out_of_sample": chart_config.out_of_sample_color,
            }
            for period_name, window in windows:
                color = colors.get(period_name, chart_config.in_sample_color)
                for axis in (equity_axis, drawdown_axis):
                    axis.axvspan(window.start, window.end, color=color, alpha=0.12, linewidth=0)
                equity_axis.text(
                    window.start,
                    0.98,
                    period_name.replace("_", " ").title(),
                    transform=equity_axis.get_xaxis_transform(),
                    ha="left",
                    va="top",
                    fontsize=9,
                    color=color,
                )
            if chart_config.show_excluded_periods and windows:
                visible_start = min(timestamps)
                visible_end = max(timestamps)
                previous = visible_start
                for _, window in windows:
                    if previous < window.start:
                        for axis in (equity_axis, drawdown_axis):
                            axis.axvspan(previous, window.start, color=chart_config.excluded_color, alpha=0.08, linewidth=0)
                    previous = max(previous, window.end)
                if previous < visible_end:
                    for axis in (equity_axis, drawdown_axis):
                        axis.axvspan(previous, visible_end, color=chart_config.excluded_color, alpha=0.08, linewidth=0)

        drawdown_axis.fill_between(
            timestamps,
            drawdown,
            0.0,
            color="#d62728",
            alpha=0.28,
            step="post",
            label="Drawdown",
        )
        drawdown_axis.plot(timestamps, drawdown, color="#d62728", linewidth=0.9)
        drawdown_axis.axhline(0.0, color="#444444", linewidth=0.8)
        drawdown_axis.set_ylabel(drawdown_ylabel)
        drawdown_axis.set_xlabel("Report time")
        drawdown_axis.legend(loc="lower left", frameon=False)
        drawdown_axis.set_title(
            f"High-water-mark drawdown | maximum {drawdown_maximum_label}",
            loc="left",
            fontsize=10,
        )
        drawdown_axis.xaxis.set_major_locator(mdates.AutoDateLocator())
        drawdown_axis.xaxis.set_major_formatter(mdates.ConciseDateFormatter(drawdown_axis.xaxis.get_major_locator()))

        output = io.BytesIO()
        fig.savefig(
            output,
            format=image_format.lower(),
            dpi=dpi,
            metadata={"Software": "trade-analyser-tools"},
        )
        plt.close(fig)
    return output.getvalue()


def save_equity_drawdown_chart(
    result: Any,
    destination: str | Path,
    *,
    title: str | None = None,
    image_format: str = "png",
    dpi: int = 140,
    chart_config: ChartConfig | None = None,
    show_sample_periods: bool | None = None,
    show_member_equity: bool | None = None,
    normalize_equity: bool | None = None,
) -> Path:
    """Render and save an equity/drawdown chart, returning its path."""

    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = render_equity_drawdown_chart(
        result,
        title=title,
        image_format=image_format,
        dpi=dpi,
        chart_config=chart_config,
        show_sample_periods=show_sample_periods,
        show_member_equity=show_member_equity,
        normalize_equity=normalize_equity,
    )
    path.write_bytes(data)
    return path



def _monthly_performance_table(source: Any) -> Any:
    """Return the typed monthly-performance table from a result or table input."""

    table = getattr(source, "monthly_performance", source)
    if not hasattr(table, "rows") or not hasattr(table, "month_labels"):
        raise ValueError(
            "source must be an AnalysisResult, PortfolioAnalysisResult, "
            "or MonthlyPerformanceTable"
        )
    if not table.rows:
        raise ValueError("monthly-performance table contains no rows")
    if len(table.month_labels) != 12:
        raise ValueError("monthly-performance table must contain twelve month labels")
    return table


def _monthly_performance_cell(value: float | None) -> tuple[str, str]:
    if value is None or not np.isfinite(float(value)):
        return "—", "missing"
    numeric = float(value)
    if numeric > 0.0:
        tone = "positive"
    elif numeric < 0.0:
        tone = "negative"
    else:
        tone = "neutral"
    return f"{numeric:.2f}%", tone


def render_monthly_performance_table(
    result: Any,
    *,
    title: str | None = None,
    image_format: str = "png",
    chart_config: MonthlyPerformanceTableChartConfig | None = None,
) -> bytes:
    """Render the eager year-by-month return table as a deterministic image.

    ``result`` may be a single-report result, a portfolio result, or the
    already-calculated ``MonthlyPerformanceTable``. The renderer only formats
    table values; it never recalculates analysis.
    """

    if image_format.lower() not in {"png", "svg"}:
        raise ValueError("image_format must be 'png' or 'svg'")
    chart_config = chart_config or MonthlyPerformanceTableChartConfig()
    chart_config.validate()
    table = _monthly_performance_table(result)
    strategy_name, _ = _report_metadata(result)
    headers = ("Year", *table.month_labels, "YTD")
    rows: list[list[str]] = []
    tones: list[list[str]] = []
    for row in table.rows:
        if len(row.monthly_returns_pct) != len(table.month_labels):
            raise ValueError("monthly-performance row does not match month labels")
        formatted = [_monthly_performance_cell(value) for value in row.monthly_returns_pct]
        ytd = _monthly_performance_cell(row.ytd_return_pct)
        rows.append([str(row.year), *(value for value, _ in formatted), ytd[0]])
        tones.append(["neutral", *(tone for _, tone in formatted), ytd[1]])

    matplotlib, _, plt = _matplotlib()
    figure_height = max(2.6, 1.6 + 0.42 * len(rows))
    with matplotlib.rc_context(
        {
            "figure.dpi": chart_config.dpi,
            "savefig.dpi": chart_config.dpi,
            "font.family": "DejaVu Sans",
        }
    ):
        fig, axis = plt.subplots(
            figsize=(14.0, figure_height),
            constrained_layout=True,
        )
        fig.suptitle(
            title or f"{strategy_name} — Monthly Performance (%)",
            fontsize=chart_config.title_size,
            fontweight="bold",
        )
        axis.axis("off")
        table_artist = axis.table(
            cellText=rows,
            colLabels=headers,
            cellLoc="right",
            colLoc="right",
            bbox=(0.01, 0.04, 0.98, 0.82),
            colWidths=(0.08, *(0.07 for _ in range(13))),
        )
        table_artist.auto_set_font_size(False)
        table_artist.set_fontsize(chart_config.font_size)
        table_artist.scale(1.0, 1.35)
        for column_index in range(len(headers)):
            header_cell = table_artist[0, column_index]
            header_cell.set_facecolor(chart_config.header_color)
            header_cell.set_edgecolor(chart_config.border_color)
            header_cell.set_linewidth(0.7)
            header_cell.get_text().set_color(chart_config.header_text_color)
            header_cell.get_text().set_weight("bold")
            header_cell.get_text().set_ha("left" if column_index == 0 else "right")
        tone_colors = {
            "positive": chart_config.positive_color,
            "negative": chart_config.negative_color,
            "neutral": chart_config.neutral_color,
            "missing": chart_config.missing_color,
        }
        for row_index, row_tones in enumerate(tones, start=1):
            for column_index, tone in enumerate(row_tones):
                cell = table_artist[row_index, column_index]
                cell.set_facecolor(
                    "#eeeeee"
                    if tone == "missing"
                    else chart_config.odd_row_color if row_index % 2 else "#ffffff"
                )
                cell.set_edgecolor(chart_config.border_color)
                cell.set_linewidth(0.5)
                cell.get_text().set_color(tone_colors[tone])
                cell.get_text().set_ha("left" if column_index == 0 else "right")
        output = io.BytesIO()
        fig.savefig(
            output,
            format=image_format.lower(),
            dpi=chart_config.dpi,
            metadata={"Software": "trade-analyser-tools"},
        )
        plt.close(fig)
    return output.getvalue()


def save_monthly_performance_table(
    result: Any,
    destination: str | Path,
    *,
    title: str | None = None,
    image_format: str = "png",
    chart_config: MonthlyPerformanceTableChartConfig | None = None,
) -> Path:
    """Render and save the eager monthly-performance table image."""

    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        render_monthly_performance_table(
            result,
            title=title,
            image_format=image_format,
            chart_config=chart_config,
        )
    )
    return path


def _trade_profit_grouping_source(
    source: Any,
    grouping: TradeProfitGrouping | str,
) -> TradeProfitGroupingResult:
    """Resolve an eager grouping result without calculating it."""

    selected = TradeProfitGrouping(grouping)
    if isinstance(source, TradeProfitGroupingResult):
        if source.grouping is not selected:
            raise ValueError(
                f"source contains {source.grouping.value}, not {selected.value}"
            )
        return source
    analysis = getattr(source, "trade_profit", source)
    if isinstance(analysis, TradeProfitAnalysis):
        return analysis.get(selected)
    getter = getattr(analysis, "get", None)
    if callable(getter):
        resolved = getter(selected)
        if isinstance(resolved, TradeProfitGroupingResult):
            return resolved
    raise TypeError(
        "source must be an AnalysisResult, PortfolioAnalysisResult, "
        "TradeProfitAnalysis, or TradeProfitGroupingResult"
    )


def _trade_profit_metadata(source: Any, grouped: TradeProfitGroupingResult) -> tuple[str, str, str]:
    report_source = source
    if not hasattr(report_source, "report") and not hasattr(report_source, "portfolio_report"):
        report_source = getattr(source, "analysis", source)
    strategy_name, currency = _report_metadata(report_source)
    currency = grouped.currency or currency
    timezone = grouped.timezone or "report timezone unavailable"
    return strategy_name, currency, timezone


def _trade_profit_label(grouping: TradeProfitGrouping) -> str:
    return {
        TradeProfitGrouping.OPEN_HOUR: "Opening hour",
        TradeProfitGrouping.CLOSE_HOUR: "Closing hour",
        TradeProfitGrouping.OPEN_DAY_OF_WEEK: "Opening day of week",
        TradeProfitGrouping.CLOSE_DAY_OF_WEEK: "Closing day of week",
    }[grouping]


def _format_trade_profit_money(value: float, currency: str) -> str:
    symbols = {
        "USD": "$",
        "AUD": "A$",
        "NZD": "NZ$",
        "CAD": "C$",
        "EUR": "€",
        "GBP": "£",
        "JPY": "¥",
    }
    symbol = symbols.get(currency.upper())
    sign = "-" if value < 0.0 else ""
    magnitude = abs(value)
    if symbol:
        return f"{sign}{symbol}{magnitude:,.2f}"
    return f"{sign}{magnitude:,.2f} {currency}"


def _format_trade_profit_value(
    value: float | None,
    measure: TradeProfitMeasure,
    currency: str,
) -> str:
    if value is None or not np.isfinite(float(value)):
        return "NA"
    if measure is TradeProfitMeasure.PERCENTAGE_GAIN:
        return f"{float(value):,.2f}%"
    return _format_trade_profit_money(float(value), currency)


def _trade_profit_active_range(grouped: TradeProfitGroupingResult) -> str:
    if grouped.active_start is None or grouped.active_end is None:
        return "no timestamped trades"
    start = grouped.active_start.isoformat(sep=" ", timespec="minutes")
    end = grouped.active_end.isoformat(sep=" ", timespec="minutes")
    return f"{start} to {end}"


def render_trade_profit_bar_chart(
    result: Any,
    *,
    grouping: TradeProfitGrouping | str,
    measure: TradeProfitMeasure | str = TradeProfitMeasure.NET_PROFIT,
    title: str | None = None,
    image_format: str = "png",
    chart_config: TradeProfitBarChartConfig | None = None,
) -> bytes:
    """Render one eager trade-profit grouping as a PNG or SVG bar chart.

    ``result`` may be a single-report result, an allocated portfolio result,
    a member analysis, ``TradeProfitAnalysis``, or one grouping result. The
    renderer formats existing buckets only; it never re-runs analysis.
    """

    if image_format.lower() not in {"png", "svg"}:
        raise ValueError("image_format must be 'png' or 'svg'")
    chart_config = chart_config or TradeProfitBarChartConfig()
    chart_config.validate()
    selected_grouping = TradeProfitGrouping(grouping)
    selected_measure = TradeProfitMeasure(measure)
    grouped = _trade_profit_grouping_source(result, selected_grouping)
    if not grouped.buckets:
        raise ValueError("trade-profit grouping contains no buckets")

    strategy_name, currency, timezone = _trade_profit_metadata(result, grouped)
    labels = [bucket.label for bucket in grouped.buckets]
    raw_values = [bucket.value_for(selected_measure) for bucket in grouped.buckets]
    values = np.asarray([0.0 if value is None else float(value) for value in raw_values], dtype=float)
    colors = [
        (
            chart_config.positive_color
            if value is not None and value > 0.0
            else chart_config.negative_color
            if value is not None and value < 0.0
            else chart_config.zero_color
        )
        for value in raw_values
    ]

    matplotlib, _, plt = _matplotlib()
    width = 14.0 if selected_grouping.is_hourly else 11.0
    with matplotlib.rc_context(
        {
            "figure.dpi": chart_config.dpi,
            "savefig.dpi": chart_config.dpi,
            "font.family": "DejaVu Sans",
            "axes.grid": True,
            "grid.alpha": 0.25,
        }
    ):
        fig, axis = plt.subplots(figsize=(width, 6.8), constrained_layout=True)
        positions = np.arange(len(labels))
        bars = axis.bar(positions, values, color=colors, width=0.78, edgecolor="#ffffff", linewidth=0.5)
        axis.axhline(0.0, color="#374151", linewidth=0.9)
        axis.set_xticks(positions, labels)
        if selected_grouping.is_hourly:
            axis.tick_params(axis="x", labelrotation=45)
            for tick in axis.get_xticklabels():
                tick.set_ha("right")
        axis.set_xlabel(_trade_profit_label(selected_grouping))
        axis.set_ylabel(
            f"Net profit ({currency})"
            if selected_measure is TradeProfitMeasure.NET_PROFIT
            else "Percentage gain (%)"
        )
        axis.set_axisbelow(True)

        finite_values = values[np.isfinite(values)]
        maximum = float(np.max(np.abs(finite_values))) if finite_values.size else 0.0
        padding = maximum * 0.22 if maximum > 0.0 else 1.0
        axis.set_ylim(-maximum - padding, maximum + padding)

        if chart_config.show_value_labels:
            span = max(2.0 * (maximum + padding), 1.0)
            offset = span * 0.012
            for bar, bucket, raw_value in zip(bars, grouped.buckets, raw_values):
                value_label = _format_trade_profit_value(raw_value, selected_measure, currency)
                if chart_config.show_trade_counts:
                    value_label += f"\nn={bucket.trade_count}"
                numeric = 0.0 if raw_value is None else float(raw_value)
                y = numeric + (offset if numeric >= 0.0 else -offset)
                axis.text(
                    bar.get_x() + bar.get_width() / 2.0,
                    y,
                    value_label,
                    ha="center",
                    va="bottom" if numeric >= 0.0 else "top",
                    fontsize=chart_config.font_size,
                    color="#111827",
                )

        measure_label = (
            "Net profit"
            if selected_measure is TradeProfitMeasure.NET_PROFIT
            else "Percentage gain"
        )
        title_text = title or f"{strategy_name} — {measure_label} by {_trade_profit_label(selected_grouping).lower()}"
        fig.suptitle(title_text, fontsize=chart_config.title_size, fontweight="bold")
        warning_count = len(grouped.warnings)
        subtitle = (
            f"{currency} | {timezone} | {grouped.trade_count:,} completed positions"
            f" | active {_trade_profit_active_range(grouped)}"
        )
        if warning_count:
            subtitle += f" | {warning_count} warning(s)"
        axis.set_title(subtitle, loc="left", fontsize=9, color="#4b5563")

        output = io.BytesIO()
        fig.savefig(
            output,
            format=image_format.lower(),
            dpi=chart_config.dpi,
            metadata={"Software": "trade-analyser-tools"},
        )
        plt.close(fig)
    return output.getvalue()


def save_trade_profit_bar_chart(
    result: Any,
    destination: str | Path,
    *,
    grouping: TradeProfitGrouping | str,
    measure: TradeProfitMeasure | str = TradeProfitMeasure.NET_PROFIT,
    title: str | None = None,
    image_format: str = "png",
    chart_config: TradeProfitBarChartConfig | None = None,
) -> Path:
    """Render and save one eager trade-profit bar chart, returning its path."""

    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        render_trade_profit_bar_chart(
            result,
            grouping=grouping,
            measure=measure,
            title=title,
            image_format=image_format,
            chart_config=chart_config,
        )
    )
    return path


def save_trade_profit_bar_charts(
    result: Any,
    destination: str | Path,
    *,
    groupings: tuple[TradeProfitGrouping | str, ...] | None = None,
    measures: tuple[TradeProfitMeasure | str, ...] | None = None,
    image_format: str = "png",
    chart_config: TradeProfitBarChartConfig | None = None,
) -> dict[tuple[TradeProfitGrouping, TradeProfitMeasure], Path]:
    """Save the selected grouped trade-profit chart matrix.

    By default this creates eight separate artifacts: four timestamp/day
    groupings crossed with net-profit and percentage-gain measures.
    """

    if image_format.lower() not in {"png", "svg"}:
        raise ValueError("image_format must be 'png' or 'svg'")
    selected_groupings = tuple(
        TradeProfitGrouping(grouping)
        for grouping in (tuple(TradeProfitGrouping) if groupings is None else groupings)
    )
    selected_measures = tuple(
        TradeProfitMeasure(measure)
        for measure in (tuple(TradeProfitMeasure) if measures is None else measures)
    )
    if not selected_groupings or not selected_measures:
        raise ValueError("at least one grouping and measure are required")
    directory = Path(destination)
    directory.mkdir(parents=True, exist_ok=True)
    paths: dict[tuple[TradeProfitGrouping, TradeProfitMeasure], Path] = {}
    grouping_filename_names = {
        TradeProfitGrouping.OPEN_HOUR: "opening-hour",
        TradeProfitGrouping.CLOSE_HOUR: "closing-hour",
        TradeProfitGrouping.OPEN_DAY_OF_WEEK: "opening-day-of-week",
        TradeProfitGrouping.CLOSE_DAY_OF_WEEK: "closing-day-of-week",
    }
    for selected_grouping in selected_groupings:
        grouping_name = grouping_filename_names[selected_grouping]
        for selected_measure in selected_measures:
            measure_name = selected_measure.value.replace("_", "-")
            path = directory / f"{grouping_name}-{measure_name}.{image_format.lower()}"
            save_trade_profit_bar_chart(
                result,
                path,
                grouping=selected_grouping,
                measure=selected_measure,
                image_format=image_format,
                chart_config=chart_config,
            )
            paths[(selected_grouping, selected_measure)] = path
    return paths


def _monte_carlo_paths(source: Any) -> np.ndarray:
    paths = getattr(source, "equity_paths", None)
    if paths is None:
        paths = getattr(source, "paths", None)
    if paths is None:
        raise ValueError(
            "Monte Carlo paths are not retained; run with "
            "MonteCarloConfig(retain_paths=True)"
        )
    values = np.asarray(paths, dtype=float)
    if values.ndim != 2 or values.shape[0] == 0 or values.shape[1] == 0:
        raise ValueError(
            "Monte Carlo paths are not retained; run with "
            "MonteCarloConfig(retain_paths=True)"
        )
    if not np.isfinite(values).all():
        raise ValueError("Monte Carlo paths must contain only finite values")
    return values


def _drawdown_paths(equity_paths: np.ndarray) -> np.ndarray:
    peaks = np.maximum.accumulate(equity_paths, axis=1)
    return equity_paths - peaks


def _monte_carlo_streak_paths(source: Any) -> tuple[np.ndarray, np.ndarray]:
    winning = getattr(source, "winning_streak_paths", None)
    losing = getattr(source, "losing_streak_paths", None)
    if winning is None or losing is None:
        raise ValueError(
            "Monte Carlo streak paths are not retained; run with "
            "MonteCarloConfig(retain_paths=True)"
        )
    winning_values = np.asarray(winning, dtype=float)
    losing_values = np.asarray(losing, dtype=float)
    if (
        winning_values.ndim != 2
        or losing_values.ndim != 2
        or winning_values.shape != losing_values.shape
        or winning_values.shape[0] == 0
        or winning_values.shape[1] == 0
    ):
        raise ValueError(
            "Monte Carlo streak paths are not retained; run with "
            "MonteCarloConfig(retain_paths=True)"
        )
    if not np.isfinite(winning_values).all() or not np.isfinite(losing_values).all():
        raise ValueError("Monte Carlo streak paths must contain only finite values")
    return winning_values, losing_values


def _render_path_panel(
    axis: Any,
    paths: np.ndarray,
    *,
    chart_config: MonteCarloPathChartConfig,
    ylabel: str,
    title: str,
) -> None:
    x = np.arange(paths.shape[1])
    # Paint bands from widest to narrowest, so the caller's intervals remain
    # readable even when they overlap.
    intervals = sorted(
        chart_config.intervals,
        key=lambda interval: interval.upper_percentile - interval.lower_percentile,
        reverse=True,
    )
    for interval in intervals:
        lower, upper = np.percentile(
            paths,
            [interval.lower_percentile, interval.upper_percentile],
            axis=0,
        )
        axis.fill_between(
            x,
            lower,
            upper,
            color=interval.color,
            alpha=interval.alpha,
            linewidth=0,
            label=interval.display_label,
        )
    for path in paths:
        axis.plot(
            x,
            path,
            color=chart_config.path_color,
            alpha=chart_config.path_alpha,
            linewidth=chart_config.path_linewidth,
        )
    median = np.percentile(paths, 50.0, axis=0)
    axis.plot(
        x,
        median,
        color=chart_config.median_color,
        linewidth=chart_config.median_linewidth,
        label="Median path",
    )
    axis.set_ylabel(ylabel)
    axis.set_title(title, loc="left", fontsize=10)
    axis.set_xlim(0, paths.shape[1] - 1)
    axis.legend(loc="best", frameon=False, fontsize=8)


def render_monte_carlo_paths(
    result: Any,
    *,
    title: str | None = None,
    image_format: str = "png",
    dpi: int = 140,
    chart_config: MonteCarloPathChartConfig | None = None,
) -> bytes:
    """Render retained Monte Carlo equity paths with configurable intervals.

    Each retained simulated path is drawn as a faint line.  Each configured
    percentile interval is calculated across paths at every simulated trade
    step and shaded using its caller-supplied colour/opacity.  The lower panel
    applies the same intervals to high-water-mark drawdown paths when
    ``show_drawdown`` is enabled.  Monte Carlo paths are indexed by simulated
    trade sequence rather than report timestamps because permutation and
    bootstrap deliberately change trade order.
    """

    if image_format.lower() not in {"png", "svg"}:
        raise ValueError("image_format must be 'png' or 'svg'")
    if dpi <= 0:
        raise ValueError("dpi must be positive")
    chart_config = chart_config or MonteCarloPathChartConfig()
    chart_config.validate()
    equity_paths = _monte_carlo_paths(result)
    drawdown_paths = _drawdown_paths(equity_paths)
    method = getattr(getattr(result, "config", None), "method", "Monte Carlo")
    iterations = getattr(result, "iterations", equity_paths.shape[0])
    seed = getattr(getattr(result, "config", None), "seed", None)
    currency = "account currency"
    report = getattr(result, "report", None)
    if report is not None:
        currency = getattr(report, "currency", None) or currency
    winning_streak_paths = losing_streak_paths = None
    if chart_config.show_streaks:
        winning_streak_paths, losing_streak_paths = _monte_carlo_streak_paths(result)
    panel_count = 1 + int(chart_config.show_drawdown) + 2 * int(chart_config.show_streaks)

    matplotlib, _, plt = _matplotlib()
    with matplotlib.rc_context(
        {
            "figure.dpi": dpi,
            "savefig.dpi": dpi,
            "font.family": "DejaVu Sans",
            "axes.grid": True,
            "grid.alpha": 0.25,
        }
    ):
        fig, axes = plt.subplots(
            panel_count,
            1,
            figsize=(13, 5 + 2.4 * (panel_count - 1)),
            sharex=True,
            constrained_layout=True,
            gridspec_kw={"height_ratios": (2.2,) + (1.0,) * (panel_count - 1)},
        )
        axes_array = np.atleast_1d(axes)
        subtitle = f"{method.title()} | {iterations:,} iterations | {equity_paths.shape[0]:,} paths"
        if seed is not None:
            subtitle += f" | seed {seed}"
        fig.suptitle(title or f"Monte Carlo simulated equity paths\n{subtitle}", fontsize=14, fontweight="bold")
        _render_path_panel(
            axes_array[0],
            equity_paths,
            chart_config=chart_config,
            ylabel=f"Equity ({currency})",
            title="Every retained simulated path with configured percentile intervals",
        )
        next_axis = 1
        if chart_config.show_drawdown:
            _render_path_panel(
                axes_array[next_axis],
                drawdown_paths,
                chart_config=chart_config,
                ylabel=f"Drawdown ({currency})",
                title="High-water-mark drawdown paths",
            )
            axes_array[next_axis].axhline(0.0, color="#444444", linewidth=0.8)
            next_axis += 1
        if chart_config.show_streaks:
            _render_path_panel(
                axes_array[next_axis],
                winning_streak_paths,
                chart_config=chart_config,
                ylabel="Winning streak (trades)",
                title="Winning streak paths",
            )
            next_axis += 1
            _render_path_panel(
                axes_array[next_axis],
                losing_streak_paths,
                chart_config=chart_config,
                ylabel="Losing streak (trades)",
                title="Losing streak paths",
            )
        axes_array[-1].set_xlabel("Simulated trade sequence")
        output = io.BytesIO()
        fig.savefig(
            output,
            format=image_format.lower(),
            dpi=dpi,
            metadata={"Software": "trade-analyser-tools"},
        )
        plt.close(fig)
    return output.getvalue()


def save_monte_carlo_paths(
    result: Any,
    destination: str | Path,
    *,
    title: str | None = None,
    image_format: str = "png",
    dpi: int = 140,
    chart_config: MonteCarloPathChartConfig | None = None,
) -> Path:
    """Render and save a Monte Carlo path chart, returning its path."""

    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        render_monte_carlo_paths(
            result,
            title=title,
            image_format=image_format,
            dpi=dpi,
            chart_config=chart_config,
        )
    )
    return path


def _correlation_source(source: Any) -> tuple[AnalysisMatrix, str, int | None]:
    """Resolve a labelled correlation matrix without recalculating it."""

    if isinstance(source, AnalysisMatrix):
        return source, source.value_type, None
    matrix = getattr(source, "matrix", None)
    if not isinstance(matrix, AnalysisMatrix):
        raise TypeError("source must be an AnalysisMatrix or correlation result")
    return matrix, str(getattr(source, "scope", "correlation")), getattr(source, "observations", None)


def render_correlation_heatmap(
    source: Any,
    *,
    title: str | None = None,
    image_format: str = "png",
    dpi: int = 140,
    decimals: int = 2,
) -> bytes:
    """Render a deterministic labelled correlation heat map.

    ``source`` is a ``DailyProfitCorrelationResult`` or ``AnalysisMatrix``.
    The matrix is consumed as-is; this function never recalculates correlation.
    Undefined cells are grey and rendered as ``N/A``.  Display precision is
    configurable, while the source matrix retains its original values.
    """

    if image_format.lower() not in {"png", "svg"}:
        raise ValueError("image_format must be 'png' or 'svg'")
    if dpi <= 0:
        raise ValueError("dpi must be positive")
    if decimals < 0:
        raise ValueError("decimals must be non-negative")
    matrix, scope, observations = _correlation_source(source)
    if matrix.shape[0] != matrix.shape[1]:
        raise ValueError("correlation heat maps require a square matrix")
    if matrix.row_labels != matrix.column_labels:
        raise ValueError("correlation heat maps require identical row and column labels")

    matplotlib, _, plt = _matplotlib()
    values = matrix.to_numpy()
    masked = np.ma.masked_invalid(values)
    cmap = matplotlib.colormaps.get_cmap("RdBu").copy()
    cmap.set_bad("#bdbdbd")
    size = max(5.5, min(12.0, 3.8 + 0.65 * len(matrix.row_labels)))
    subtitle = f"{scope.replace('_', ' ').title()}"
    if observations is not None:
        subtitle += f" | {observations:,} daily observations"

    with matplotlib.rc_context(
        {
            "figure.dpi": dpi,
            "savefig.dpi": dpi,
            "font.family": "DejaVu Sans",
            "axes.grid": False,
        }
    ):
        fig, axis = plt.subplots(figsize=(size, size * 0.88), constrained_layout=True)
        image = axis.imshow(masked, cmap=cmap, vmin=-1.0, vmax=1.0, aspect="equal")
        axis.set_xticks(np.arange(len(matrix.column_labels)), labels=matrix.column_labels, rotation=45, ha="right")
        axis.set_yticks(np.arange(len(matrix.row_labels)), labels=matrix.row_labels)
        axis.set_xlabel("Strategy")
        axis.set_ylabel("Strategy")
        title_text = title or f"Daily profit correlation heat map\n{subtitle}"
        if len(title_text) > 42 and " — " in title_text:
            title_text = title_text.replace(" — ", " —\n", 1)
        axis.set_title(title_text, fontweight="bold", fontsize=12, pad=14)
        colorbar = fig.colorbar(image, ax=axis, fraction=0.046, pad=0.04)
        colorbar.set_label("Correlation")
        for row_index, row in enumerate(matrix.values):
            for column_index, value in enumerate(row):
                # A correlation matrix's diagonal is conventionally 1.0 even
                # when a degenerate series was marked undefined by analysis.
                display_value = 1.0 if row_index == column_index and value is None else value
                if display_value is None:
                    label = "N/A"
                    colour = "#333333"
                else:
                    label = f"{display_value:.{decimals}f}"
                    colour = "white" if abs(float(display_value)) >= 0.5 else "#222222"
                axis.text(column_index, row_index, label, ha="center", va="center", color=colour, fontsize=10)
        axis.set_xticks(np.arange(-0.5, len(matrix.column_labels), 1), minor=True)
        axis.set_yticks(np.arange(-0.5, len(matrix.row_labels), 1), minor=True)
        axis.grid(which="minor", color="white", linestyle="-", linewidth=1.5)
        axis.tick_params(which="minor", bottom=False, left=False)
        output = io.BytesIO()
        fig.savefig(
            output,
            format=image_format.lower(),
            dpi=dpi,
            metadata={"Software": "trade-analyser-tools"},
        )
        plt.close(fig)
    return output.getvalue()


def save_correlation_heatmap(
    source: Any,
    destination: str | Path,
    *,
    title: str | None = None,
    image_format: str = "png",
    dpi: int = 140,
    decimals: int = 2,
) -> Path:
    """Render and save a correlation heat map, returning its path."""

    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        render_correlation_heatmap(
            source,
            title=title,
            image_format=image_format,
            dpi=dpi,
            decimals=decimals,
        )
    )
    return path
