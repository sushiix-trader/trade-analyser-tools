"""Deterministic chart artifacts built from eager analysis results.

Chart rendering is deliberately optional and separate from the analytical
engine.  It consumes the already-selected ``result.equity`` curve, so a chart
never silently recalculates or chooses a different equity basis.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .matrices import AnalysisMatrix


@dataclass(frozen=True)
class ChartConfig:
    """Deterministic visual options for equity/drawdown charts."""

    show_sample_periods: bool = False
    show_excluded_periods: bool = True
    in_sample_color: str = "#4f81bd"
    out_of_sample_color: str = "#f28e2b"
    excluded_color: str = "#9e9e9e"


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


def render_equity_drawdown_chart(
    result: Any,
    *,
    title: str | None = None,
    image_format: str = "png",
    dpi: int = 140,
    chart_config: ChartConfig | None = None,
    show_sample_periods: bool | None = None,
) -> bytes:
    """Render a deterministic PNG/SVG chart containing equity and drawdown.

    The upper panel shows the selected eager-analysis equity curve.  The lower
    panel shows high-water-mark drawdown in account currency, with zero at the
    top and larger losses extending downward.  ``result`` is an
    ``AnalysisResult`` or a compatible ``PortfolioAnalysisResult``.
    """

    if image_format.lower() not in {"png", "svg"}:
        raise ValueError("image_format must be 'png' or 'svg'")
    if dpi <= 0:
        raise ValueError("dpi must be positive")
    chart_config = chart_config or ChartConfig()
    if show_sample_periods is not None:
        chart_config = ChartConfig(
            show_sample_periods=show_sample_periods,
            show_excluded_periods=chart_config.show_excluded_periods,
            in_sample_color=chart_config.in_sample_color,
            out_of_sample_color=chart_config.out_of_sample_color,
            excluded_color=chart_config.excluded_color,
        )

    equity = getattr(result, "equity", None)
    if equity is None or not equity.timestamps or not equity.values:
        raise ValueError("result does not contain an equity curve")
    if len(equity.timestamps) != len(equity.values):
        raise ValueError("equity timestamps and values must have equal lengths")

    matplotlib, mdates, plt = _matplotlib()
    strategy_name, currency = _report_metadata(result)
    timestamps = list(equity.timestamps)
    values = np.asarray(equity.values, dtype=float)
    peaks = np.maximum.accumulate(values)
    drawdown = values - peaks
    maximum_drawdown = float(np.min(drawdown))

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

        equity_axis.plot(
            timestamps,
            values,
            color="#1f77b4",
            linewidth=1.25,
            label=f"Equity ({equity.source}, {equity.basis})",
        )
        equity_axis.set_ylabel(f"Equity ({currency})")
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
        drawdown_axis.set_ylabel(f"Drawdown ({currency})")
        drawdown_axis.set_xlabel("Report time")
        drawdown_axis.legend(loc="lower left", frameon=False)
        drawdown_axis.set_title(
            f"High-water-mark drawdown | maximum {abs(maximum_drawdown):,.2f} {currency}",
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
    )
    path.write_bytes(data)
    return path



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
