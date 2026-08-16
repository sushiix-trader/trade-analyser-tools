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
