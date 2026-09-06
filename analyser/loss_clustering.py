"""Losing-trade equity path and inter-loss gap clustering analysis."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Sequence

from .diagnostics import Diagnostic
from .drawdown import DrawdownAnalysis, analyze_drawdowns
from .equity import CurveSeries
from .models import Report, Trade
from .serialization import to_primitive
from .statistics import linear_quantile


DEFAULT_GAP_THRESHOLDS_DAYS: tuple[float, ...] = (1.0, 7.0)
DEFAULT_GAP_HISTOGRAM_BIN_WIDTH_DAYS: float = 1.0


@dataclass(frozen=True)
class LossClusteringConfig:
    """Optional thresholds and equal-width histogram binning for inter-loss gaps."""

    gap_thresholds_days: tuple[float, ...] = DEFAULT_GAP_THRESHOLDS_DAYS
    gap_histogram_bin_width_days: float = DEFAULT_GAP_HISTOGRAM_BIN_WIDTH_DAYS

    def __post_init__(self) -> None:
        if any(not math.isfinite(value) or value <= 0 for value in self.gap_thresholds_days):
            raise ValueError("gap_thresholds_days must contain positive finite values")
        width = float(self.gap_histogram_bin_width_days)
        if not math.isfinite(width) or width <= 0:
            raise ValueError("gap_histogram_bin_width_days must be a positive finite value")


@dataclass(frozen=True)
class LossEvent:
    """One completed losing position in close-time order."""

    index: int
    ticket: str
    close_time: datetime
    profit: float
    magnitude: float
    gap_days_since_previous: float | None
    consecutive_loss_streak: int
    side: str


@dataclass(frozen=True)
class LossGapPoint:
    """One close→close inter-loss gap observation."""

    gap_days: float
    magnitude: float
    close_time: datetime
    ticket: str
    consecutive_loss_streak: int


@dataclass(frozen=True)
class LossGapHistogramBin:
    """One equal-width histogram bin on the continuous days axis."""

    left: float
    right: float
    count: int


@dataclass(frozen=True)
class LossGapHistogram:
    """Equal-width histogram of close→close days between consecutive losses."""

    bins: tuple[LossGapHistogramBin, ...]
    sample_count: int
    bin_width_days: float = DEFAULT_GAP_HISTOGRAM_BIN_WIDTH_DAYS


@dataclass(frozen=True)
class LossClusteringSummary:
    loss_count: int
    total_loss_money: float
    mean_gap_days: float | None
    median_gap_days: float | None
    p95_gap_days: float | None
    max_consecutive_losses: int
    loss_only_max_drawdown_money: float | None
    loss_only_max_drawdown_pct: float | None
    share_gaps_below_days: dict[str, float | None] = field(default_factory=dict)


@dataclass(frozen=True)
class LossClusteringResult:
    """Eager losing-trade accumulation and gap clustering payload.

    ``curve`` is a balance-basis series that starts at the report initial deposit
    and steps only on completed losing closes (winners are omitted).  Charts and
    the interactive Losses tab plot that series against close time, with a
    linear start→end reference for constant loss-rate-over-time comparison.
    """

    events: tuple[LossEvent, ...]
    gap_points: tuple[LossGapPoint, ...]
    gap_histogram: LossGapHistogram
    curve: CurveSeries
    drawdown_analysis: DrawdownAnalysis
    summary: LossClusteringSummary
    warnings: tuple[Diagnostic, ...] = ()
    config: LossClusteringConfig = field(default_factory=LossClusteringConfig)

    def to_dict(self) -> dict[str, Any]:
        return to_primitive(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "LossClusteringResult":
        config_data = payload.get("config") or {}
        thresholds = tuple(config_data.get("gap_thresholds_days", DEFAULT_GAP_THRESHOLDS_DAYS))
        bin_width = float(
            config_data.get(
                "gap_histogram_bin_width_days",
                DEFAULT_GAP_HISTOGRAM_BIN_WIDTH_DAYS,
            )
        )
        config = LossClusteringConfig(
            gap_thresholds_days=thresholds,
            gap_histogram_bin_width_days=bin_width,
        )
        events = tuple(
            LossEvent(
                index=int(item["index"]),
                ticket=str(item["ticket"]),
                close_time=datetime.fromisoformat(item["close_time"]),
                profit=float(item["profit"]),
                magnitude=float(item["magnitude"]),
                gap_days_since_previous=_optional_float(item.get("gap_days_since_previous")),
                consecutive_loss_streak=int(item["consecutive_loss_streak"]),
                side=str(item.get("side", "")),
            )
            for item in payload.get("events", ())
        )
        gap_points = tuple(
            LossGapPoint(
                gap_days=float(item["gap_days"]),
                magnitude=float(item["magnitude"]),
                close_time=datetime.fromisoformat(item["close_time"]),
                ticket=str(item["ticket"]),
                consecutive_loss_streak=int(item["consecutive_loss_streak"]),
            )
            for item in payload.get("gap_points", ())
        )
        histogram_data = payload.get("gap_histogram")
        if isinstance(histogram_data, dict):
            gap_histogram = LossGapHistogram(
                bins=tuple(
                    LossGapHistogramBin(
                        left=float(item["left"]),
                        right=float(item["right"]),
                        count=int(item["count"]),
                    )
                    for item in histogram_data.get("bins", ())
                ),
                sample_count=int(histogram_data.get("sample_count", 0)),
                bin_width_days=float(
                    histogram_data.get("bin_width_days", config.gap_histogram_bin_width_days)
                ),
            )
        else:
            gap_histogram = build_gap_histogram(
                [point.gap_days for point in gap_points],
                bin_width_days=config.gap_histogram_bin_width_days,
            )
        curve_data = payload["curve"]
        curve = CurveSeries(
            timestamps=tuple(datetime.fromisoformat(value) for value in curve_data["timestamps"]),
            values=tuple(float(value) for value in curve_data["values"]),
            source=str(curve_data["source"]),
            basis=str(curve_data["basis"]),
            initial_value=float(curve_data["initial_value"]),
        )
        drawdown_data = payload.get("drawdown_analysis")
        drawdown = (
            DrawdownAnalysis.from_dict(drawdown_data)
            if isinstance(drawdown_data, dict)
            else analyze_drawdowns(curve)
        )
        summary_data = payload.get("summary") or {}
        summary = LossClusteringSummary(
            loss_count=int(summary_data.get("loss_count", 0)),
            total_loss_money=float(summary_data.get("total_loss_money", 0.0)),
            mean_gap_days=_optional_float(summary_data.get("mean_gap_days")),
            median_gap_days=_optional_float(summary_data.get("median_gap_days")),
            p95_gap_days=_optional_float(summary_data.get("p95_gap_days")),
            max_consecutive_losses=int(summary_data.get("max_consecutive_losses", 0)),
            loss_only_max_drawdown_money=_optional_float(
                summary_data.get("loss_only_max_drawdown_money")
            ),
            loss_only_max_drawdown_pct=_optional_float(
                summary_data.get("loss_only_max_drawdown_pct")
            ),
            share_gaps_below_days={
                str(key): _optional_float(value)
                for key, value in dict(summary_data.get("share_gaps_below_days", {})).items()
            },
        )
        warnings = tuple(
            Diagnostic(
                str(item["code"]),
                str(item["message"]),
                str(item.get("severity", "warning")),
                dict(item.get("context", {})),
            )
            if isinstance(item, dict)
            else item
            for item in payload.get("warnings", ())
        )
        return cls(
            events=events,
            gap_points=gap_points,
            gap_histogram=gap_histogram,
            curve=curve,
            drawdown_analysis=drawdown,
            summary=summary,
            warnings=warnings,
            config=config,
        )


def build_loss_clustering(
    report: Report,
    *,
    initial_capital: float | None = None,
    config: LossClusteringConfig | None = None,
    trades: Sequence[Trade] | None = None,
) -> LossClusteringResult:
    """Build loss-only equity and inter-loss gap statistics from completed trades."""

    config = config or LossClusteringConfig()
    capital = float(report.initial_deposit if initial_capital is None else initial_capital)
    if not math.isfinite(capital):
        raise ValueError("initial_capital must be finite")
    ordered = list(trades) if trades is not None else report.ordered_trades()
    losses = [trade for trade in ordered if trade.close_time is not None and trade.profit < 0.0]
    losses.sort(key=lambda trade: trade.sort_key)

    warnings: list[Diagnostic] = []
    if not losses:
        warnings.append(Diagnostic(
            "loss_clustering_no_losses",
            "No completed losing trades are available for loss clustering",
            "warning",
            {},
        ))
        empty_curve = CurveSeries(
            timestamps=(),
            values=(capital,),
            source="loss_only_closed_positions",
            basis="balance",
            initial_value=capital,
        )
        drawdown = analyze_drawdowns(empty_curve)
        summary = LossClusteringSummary(
            loss_count=0,
            total_loss_money=0.0,
            mean_gap_days=None,
            median_gap_days=None,
            p95_gap_days=None,
            max_consecutive_losses=0,
            loss_only_max_drawdown_money=drawdown.depth_money_distribution.maximum,
            loss_only_max_drawdown_pct=drawdown.depth_distribution.maximum,
            share_gaps_below_days={
                _threshold_key(value): None for value in config.gap_thresholds_days
            },
        )
        return LossClusteringResult(
            events=(),
            gap_points=(),
            gap_histogram=build_gap_histogram(
                (),
                bin_width_days=config.gap_histogram_bin_width_days,
            ),
            curve=empty_curve,
            drawdown_analysis=drawdown,
            summary=summary,
            warnings=tuple(warnings),
            config=config,
        )

    # Consecutive-loss streaks in full trade order (winners break streaks).
    streak_by_key: dict[tuple[str, datetime], int] = {}
    current_streak = 0
    for trade in ordered:
        if trade.close_time is None:
            continue
        if trade.profit < 0.0:
            current_streak += 1
            streak_by_key[(trade.ticket, trade.close_time)] = current_streak
        elif trade.profit > 0.0:
            current_streak = 0

    events: list[LossEvent] = []
    gap_points: list[LossGapPoint] = []
    gaps: list[float] = []
    previous_close: datetime | None = None

    candidates = [trade.open_time or trade.close_time for trade in losses]
    candidates = [value for value in candidates if value is not None]
    baseline = min(candidates) if candidates else losses[0].close_time
    assert baseline is not None

    timestamps: list[datetime] = [baseline]
    values: list[float] = [capital]
    running = capital

    for index, trade in enumerate(losses):
        close_time = trade.close_time
        assert close_time is not None
        gap: float | None = None
        if previous_close is not None:
            gap = (close_time - previous_close).total_seconds() / 86400.0
            gaps.append(gap)
        streak = streak_by_key.get((trade.ticket, close_time), 1)
        magnitude = abs(float(trade.profit))
        side = trade.side.value if hasattr(trade.side, "value") else str(trade.side)
        events.append(LossEvent(
            index=index,
            ticket=str(trade.ticket),
            close_time=close_time,
            profit=float(trade.profit),
            magnitude=magnitude,
            gap_days_since_previous=gap,
            consecutive_loss_streak=streak,
            side=side,
        ))
        if gap is not None:
            gap_points.append(LossGapPoint(
                gap_days=gap,
                magnitude=magnitude,
                close_time=close_time,
                ticket=str(trade.ticket),
                consecutive_loss_streak=streak,
            ))
        running += float(trade.profit)
        step_time = close_time
        if step_time <= timestamps[-1]:
            step_time = timestamps[-1] + timedelta(microseconds=1)
        timestamps.append(step_time)
        values.append(running)
        previous_close = close_time

    curve = CurveSeries(
        timestamps=tuple(timestamps),
        values=tuple(values),
        source="loss_only_closed_positions",
        basis="balance",
        initial_value=capital,
    )
    drawdown = analyze_drawdowns(curve)
    warnings.extend(drawdown.warnings)

    ordered_gaps = sorted(gaps)
    mean_gap = (sum(gaps) / len(gaps)) if gaps else None
    median_gap = linear_quantile(ordered_gaps, 50.0) if ordered_gaps else None
    p95_gap = linear_quantile(ordered_gaps, 95.0) if ordered_gaps else None
    share: dict[str, float | None] = {}
    for threshold in config.gap_thresholds_days:
        key = _threshold_key(threshold)
        if not gaps:
            share[key] = None
        else:
            share[key] = sum(1 for gap in gaps if gap < threshold) / len(gaps)

    max_streak = max((event.consecutive_loss_streak for event in events), default=0)
    loss_only_dd_money = drawdown.depth_money_distribution.maximum
    loss_only_dd_pct = drawdown.depth_distribution.maximum
    if loss_only_dd_money is None and drawdown.current_episode is not None:
        loss_only_dd_money = drawdown.current_episode.depth_money
    if loss_only_dd_pct is None and drawdown.current_episode is not None:
        loss_only_dd_pct = drawdown.current_episode.depth_percent
    summary = LossClusteringSummary(
        loss_count=len(events),
        total_loss_money=sum(event.magnitude for event in events),
        mean_gap_days=mean_gap,
        median_gap_days=median_gap,
        p95_gap_days=p95_gap,
        max_consecutive_losses=max_streak,
        loss_only_max_drawdown_money=loss_only_dd_money,
        loss_only_max_drawdown_pct=loss_only_dd_pct,
        share_gaps_below_days=share,
    )
    return LossClusteringResult(
        events=tuple(events),
        gap_points=tuple(gap_points),
        gap_histogram=build_gap_histogram(
            gaps,
            bin_width_days=config.gap_histogram_bin_width_days,
        ),
        curve=curve,
        drawdown_analysis=drawdown,
        summary=summary,
        warnings=tuple(warnings),
        config=config,
    )


def build_gap_histogram(
    gaps: Sequence[float],
    *,
    bin_width_days: float = DEFAULT_GAP_HISTOGRAM_BIN_WIDTH_DAYS,
) -> LossGapHistogram:
    """Bin close→close inter-loss gaps into equal-width day bins.

    Bins are half-open ``[left, right)`` on a continuous days axis, except the
    final bin which is closed on the right so the maximum gap is retained.
    """

    width = float(bin_width_days)
    if not math.isfinite(width) or width <= 0:
        raise ValueError("bin_width_days must be a positive finite value")
    samples = [
        float(gap)
        for gap in gaps
        if math.isfinite(float(gap)) and float(gap) >= 0.0
    ]
    if not samples:
        return LossGapHistogram(bins=(), sample_count=0, bin_width_days=width)

    max_gap = max(samples)
    bin_count = max(1, int(math.ceil(max_gap / width)))
    if bin_count * width < max_gap:
        bin_count += 1
    edges = [index * width for index in range(bin_count + 1)]
    counts = [0] * bin_count
    for gap in samples:
        if gap >= edges[-1]:
            counts[-1] += 1
            continue
        index = min(bin_count - 1, int(gap // width))
        counts[index] += 1
    bins = tuple(
        LossGapHistogramBin(
            left=edges[index],
            right=edges[index + 1],
            count=counts[index],
        )
        for index in range(bin_count)
    )
    return LossGapHistogram(
        bins=bins,
        sample_count=len(samples),
        bin_width_days=width,
    )


def _threshold_key(days: float) -> str:
    if float(days).is_integer():
        return f"lt_{int(days)}d"
    return f"lt_{days}d"


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)
