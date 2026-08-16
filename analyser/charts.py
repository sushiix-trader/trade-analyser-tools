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
