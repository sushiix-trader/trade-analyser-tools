"""Deterministic drawdown depth-versus-duration analysis."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol, Sequence

from .diagnostics import Diagnostic
from .serialization import to_primitive


DRAWDOWN_PERCENTILES = (50.0, 90.0, 95.0, 99.0)


class CurveLike(Protocol):
    """The timestamped curve attributes required by :func:`analyze_drawdowns`."""

    timestamps: Sequence[datetime | None]
    values: Sequence[float]
    source: str
    basis: str
    initial_value: float


@dataclass(frozen=True)
class DrawdownDistribution:
    """Sorted values and deterministic percentile summaries for one drawdown axis."""

    unit: str
    values: tuple[float, ...]
    minimum: float | None
    p50: float | None
    p90: float | None
    p95: float | None
    p99: float | None
    maximum: float | None

    @property
    def count(self) -> int:
        return len(self.values)

    @classmethod
    def from_values(cls, values: Sequence[float], unit: str) -> "DrawdownDistribution":
        finite: list[float] = []
        for raw_value in values:
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):
                finite.append(value)
        ordered = tuple(sorted(finite))
        return cls(
            unit=unit,
            values=ordered,
            minimum=ordered[0] if ordered else None,
            p50=_quantile(ordered, 50.0),
            p90=_quantile(ordered, 90.0),
            p95=_quantile(ordered, 95.0),
            p99=_quantile(ordered, 99.0),
            maximum=ordered[-1] if ordered else None,
        )

    def to_dict(self) -> dict[str, object]:
        return to_primitive(self)

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "DrawdownDistribution":
        values = tuple(float(value) for value in payload.get("values", ()))
        return cls(
            unit=str(payload.get("unit", "")),
            values=values,
            minimum=_optional_float(payload.get("minimum")),
            p50=_optional_float(payload.get("p50")),
            p90=_optional_float(payload.get("p90")),
            p95=_optional_float(payload.get("p95")),
            p99=_optional_float(payload.get("p99")),
            maximum=_optional_float(payload.get("maximum")),
        )


@dataclass(frozen=True)
class DrawdownEpisode:
    """One completed or currently open excursion below a local high-water mark."""

    episode_id: int
    status: str
    peak_index: int
    trough_index: int
    end_index: int
    recovery_index: int | None
    peak_time: datetime | None
    trough_time: datetime | None
    recovery_time: datetime | None
    end_time: datetime | None
    peak_value: float
    trough_value: float
    recovery_value: float | None
    end_value: float
    depth_money: float
    depth_percent: float | None
    duration_days: float | None
    duration_periods: int
    depth_percentile: float | None = None
    depth_tail_rarity_percent: float | None = None
    depth_ordinal_rank: float | None = None
    duration_percentile: float | None = None
    duration_tail_rarity_percent: float | None = None
    duration_ordinal_rank: float | None = None

    def to_dict(self) -> dict[str, object]:
        return to_primitive(self)

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "DrawdownEpisode":
        return cls(
            episode_id=int(payload["episode_id"]),
            status=str(payload["status"]),
            peak_index=int(payload["peak_index"]),
            trough_index=int(payload["trough_index"]),
            end_index=int(payload["end_index"]),
            recovery_index=_optional_int(payload.get("recovery_index")),
            peak_time=_optional_datetime(payload.get("peak_time")),
            trough_time=_optional_datetime(payload.get("trough_time")),
            recovery_time=_optional_datetime(payload.get("recovery_time")),
            end_time=_optional_datetime(payload.get("end_time")),
            peak_value=float(payload["peak_value"]),
            trough_value=float(payload["trough_value"]),
            recovery_value=_optional_float(payload.get("recovery_value")),
            end_value=float(payload["end_value"]),
            depth_money=float(payload["depth_money"]),
            depth_percent=_optional_float(payload.get("depth_percent")),
            duration_days=_optional_float(payload.get("duration_days")),
            duration_periods=int(payload["duration_periods"]),
            depth_percentile=_optional_float(payload.get("depth_percentile")),
            depth_tail_rarity_percent=_optional_float(payload.get("depth_tail_rarity_percent")),
            depth_ordinal_rank=_optional_float(payload.get("depth_ordinal_rank")),
            duration_percentile=_optional_float(payload.get("duration_percentile")),
            duration_tail_rarity_percent=_optional_float(payload.get("duration_tail_rarity_percent")),
            duration_ordinal_rank=_optional_float(payload.get("duration_ordinal_rank")),
        )


@dataclass(frozen=True)
class DrawdownAnalysis:
    """Depth and duration distributions extracted from one timestamped curve."""

    curve_source: str
    curve_basis: str
    initial_value: float
    observation_count: int
    start_time: datetime | None
    end_time: datetime | None
    episodes: tuple[DrawdownEpisode, ...]
    depth_distribution: DrawdownDistribution
    depth_money_distribution: DrawdownDistribution
    duration_distribution: DrawdownDistribution
    duration_periods_distribution: DrawdownDistribution
    warnings: tuple[Diagnostic, ...] = ()

    @property
    def completed_episodes(self) -> tuple[DrawdownEpisode, ...]:
        return tuple(episode for episode in self.episodes if episode.status == "completed")

    @property
    def current_episode(self) -> DrawdownEpisode | None:
        return next((episode for episode in self.episodes if episode.status == "open"), None)

    @property
    def completed_episode_count(self) -> int:
        return len(self.completed_episodes)

    def to_dict(self) -> dict[str, object]:
        return to_primitive(self)

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "DrawdownAnalysis":
        return cls(
            curve_source=str(payload.get("curve_source", "")),
            curve_basis=str(payload.get("curve_basis", "")),
            initial_value=float(payload.get("initial_value", 0.0)),
            observation_count=int(payload.get("observation_count", 0)),
            start_time=_optional_datetime(payload.get("start_time")),
            end_time=_optional_datetime(payload.get("end_time")),
            episodes=tuple(
                DrawdownEpisode.from_dict(item)
                for item in payload.get("episodes", ())
            ),
            depth_distribution=DrawdownDistribution.from_dict(
                _mapping(payload.get("depth_distribution"))
            ),
            depth_money_distribution=DrawdownDistribution.from_dict(
                _mapping(payload.get("depth_money_distribution"))
            ),
            duration_distribution=DrawdownDistribution.from_dict(
                _mapping(payload.get("duration_distribution"))
            ),
            duration_periods_distribution=DrawdownDistribution.from_dict(
                _mapping(payload.get("duration_periods_distribution"))
            ),
            warnings=tuple(
                Diagnostic(
                    str(item["code"]),
                    str(item["message"]),
                    str(item.get("severity", "warning")),
                    dict(item.get("context", {})),
                )
                for item in payload.get("warnings", ())
            ),
        )


def analyze_drawdowns(curve: CurveLike | None) -> DrawdownAnalysis:
    """Extract drawdown episodes and rank them against completed history.

    The input curve is consumed exactly as supplied: no interpolation, resampling,
    or randomisation is performed.  Depth values are positive magnitudes.  An
    open episode at the end of the curve is retained in ``episodes`` but is not
    included in any reference distribution.
    """

    warnings: list[Diagnostic] = []
    if curve is None:
        warnings.append(Diagnostic(
            "drawdown_no_curve",
            "Drawdown analysis requires a timestamped equity curve",
        ))
        return _empty_analysis("", "", 0.0, warnings)

    try:
        raw_values = list(curve.values)
        raw_timestamps = list(curve.timestamps)
        curve_source = str(curve.source)
        curve_basis = str(curve.basis)
        initial_value = float(curve.initial_value)
    except (AttributeError, TypeError, ValueError) as error:
        raise TypeError("curve must expose timestamps, values, source, basis, and initial_value") from error

    if not math.isfinite(initial_value):
        initial_value = 0.0
        warnings.append(Diagnostic(
            "drawdown_non_finite_initial_value",
            "The curve initial value was non-finite and was represented as zero",
        ))

    if len(raw_values) == 1 and not raw_timestamps:
        raw_timestamps = [None]
    elif len(raw_values) != len(raw_timestamps):
        warnings.append(Diagnostic(
            "drawdown_curve_length_mismatch",
            "Curve timestamps and values have different lengths; unmatched observations were ignored",
            context={"timestamp_count": len(raw_timestamps), "value_count": len(raw_values)},
        ))
        count = min(len(raw_values), len(raw_timestamps))
        raw_values = raw_values[:count]
        raw_timestamps = raw_timestamps[:count]

    observations: list[_Observation] = []
    for index, (timestamp, raw_value) in enumerate(zip(raw_timestamps, raw_values)):
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            value = math.nan
        if not math.isfinite(value):
            warnings.append(Diagnostic(
                "drawdown_non_finite_observation",
                "A non-finite curve observation was ignored for drawdown analysis",
                context={"index": index},
            ))
            continue
        observations.append(_Observation(index, timestamp, value))

    if not observations:
        warnings.append(Diagnostic(
            "drawdown_no_observations",
            "No finite curve observations are available for drawdown analysis",
        ))
        return _empty_analysis(curve_source, curve_basis, initial_value, warnings)

    previous_timestamp: datetime | None = None
    for observation in observations:
        timestamp = observation.timestamp
        if timestamp is None:
            continue
        if previous_timestamp is not None:
            try:
                if timestamp < previous_timestamp and not any(
                    item.code == "drawdown_timestamps_out_of_order" for item in warnings
                ):
                    warnings.append(Diagnostic(
                        "drawdown_timestamps_out_of_order",
                        "Curve timestamps are not non-decreasing; duration uses the supplied episode endpoints",
                    ))
            except TypeError:
                if not any(
                    item.code == "drawdown_timestamps_incompatible" for item in warnings
                ):
                    warnings.append(Diagnostic(
                        "drawdown_timestamps_incompatible",
                        "Curve timestamps use incompatible values or timezone information",
                    ))
        previous_timestamp = timestamp

    drafts: list[_EpisodeDraft] = []
    peak = observations[0]
    active_peak: _Observation | None = None
    trough: _Observation | None = None
    for observation in observations[1:]:
        if active_peak is None:
            if observation.value >= peak.value:
                peak = observation
            else:
                active_peak = peak
                trough = observation
            continue

        assert trough is not None
        if observation.value < trough.value:
            trough = observation
        if observation.value >= active_peak.value:
            drafts.append(_EpisodeDraft(active_peak, trough, observation, observation))
            peak = observation
            active_peak = None
            trough = None

    if active_peak is not None and trough is not None:
        drafts.append(_EpisodeDraft(active_peak, trough, None, observations[-1]))

    episodes = [
        _episode_from_draft(index + 1, draft, warnings)
        for index, draft in enumerate(drafts)
    ]
    completed = [episode for episode in episodes if episode.status == "completed"]
    depth_reference = [
        episode.depth_percent
        for episode in completed
        if episode.depth_percent is not None
    ]
    depth_money_reference = [episode.depth_money for episode in completed]
    duration_reference = [
        episode.duration_days
        for episode in completed
        if episode.duration_days is not None
    ]
    duration_periods_reference = [float(episode.duration_periods) for episode in completed]

    if not completed:
        warnings.append(Diagnostic(
            "drawdown_no_completed_episodes",
            "No completed drawdown episodes are available for historical distributions",
        ))
    current = next((episode for episode in episodes if episode.status == "open"), None)
    if current is not None and (not depth_reference or not duration_reference):
        warnings.append(Diagnostic(
            "drawdown_current_percentiles_undefined",
            "The current drawdown cannot be percentile-ranked without completed reference episodes",
            context={
                "depth_reference_count": len(depth_reference),
                "duration_reference_count": len(duration_reference),
            },
        ))

    ranked_episodes = tuple(
        _rank_episode(episode, depth_reference, duration_reference)
        for episode in episodes
    )
    return DrawdownAnalysis(
        curve_source=curve_source,
        curve_basis=curve_basis,
        initial_value=initial_value,
        observation_count=len(observations),
        start_time=observations[0].timestamp,
        end_time=observations[-1].timestamp,
        episodes=ranked_episodes,
        depth_distribution=DrawdownDistribution.from_values(depth_reference, "percent"),
        depth_money_distribution=DrawdownDistribution.from_values(depth_money_reference, "money"),
        duration_distribution=DrawdownDistribution.from_values(duration_reference, "days"),
        duration_periods_distribution=DrawdownDistribution.from_values(duration_periods_reference, "periods"),
        warnings=tuple(warnings),
    )


@dataclass(frozen=True)
class _Observation:
    index: int
    timestamp: datetime | None
    value: float


@dataclass(frozen=True)
class _EpisodeDraft:
    peak: _Observation
    trough: _Observation
    recovery: _Observation | None
    end: _Observation


def _episode_from_draft(
    episode_id: int,
    draft: _EpisodeDraft,
    warnings: list[Diagnostic],
) -> DrawdownEpisode:
    depth_money = max(0.0, draft.peak.value - draft.trough.value)
    depth_percent: float | None
    if draft.peak.value > 0:
        depth_percent = depth_money / draft.peak.value * 100.0
    else:
        depth_percent = None
        if not any(item.code == "drawdown_depth_percent_undefined" for item in warnings):
            warnings.append(Diagnostic(
                "drawdown_depth_percent_undefined",
                "Percentage drawdown depth is undefined when a high-water mark is not positive",
            ))
    duration_days = _elapsed_days(draft.peak.timestamp, draft.end.timestamp, warnings)
    return DrawdownEpisode(
        episode_id=episode_id,
        status="completed" if draft.recovery is not None else "open",
        peak_index=draft.peak.index,
        trough_index=draft.trough.index,
        end_index=draft.end.index,
        recovery_index=draft.recovery.index if draft.recovery is not None else None,
        peak_time=draft.peak.timestamp,
        trough_time=draft.trough.timestamp,
        recovery_time=draft.recovery.timestamp if draft.recovery is not None else None,
        end_time=draft.end.timestamp,
        peak_value=draft.peak.value,
        trough_value=draft.trough.value,
        recovery_value=draft.recovery.value if draft.recovery is not None else None,
        end_value=draft.end.value,
        depth_money=depth_money,
        depth_percent=depth_percent,
        duration_days=duration_days,
        duration_periods=max(0, draft.end.index - draft.peak.index),
    )


def _rank_episode(
    episode: DrawdownEpisode,
    depth_reference: Sequence[float],
    duration_reference: Sequence[float],
) -> DrawdownEpisode:
    depth_rank = _rank(episode.depth_percent, depth_reference)
    duration_rank = _rank(episode.duration_days, duration_reference)
    return replace(
        episode,
        depth_percentile=depth_rank[0] if depth_rank else None,
        depth_tail_rarity_percent=depth_rank[1] if depth_rank else None,
        depth_ordinal_rank=depth_rank[2] if depth_rank else None,
        duration_percentile=duration_rank[0] if duration_rank else None,
        duration_tail_rarity_percent=duration_rank[1] if duration_rank else None,
        duration_ordinal_rank=duration_rank[2] if duration_rank else None,
    )


def _rank(
    value: float | None,
    reference: Sequence[float],
) -> tuple[float, float, float] | None:
    if value is None or not reference:
        return None
    less = sum(item < value for item in reference)
    equal = sum(item == value for item in reference)
    midrank = less + (equal + 1) / 2.0
    count = len(reference)
    return (
        min(100.0, max(0.0, midrank / count * 100.0)),
        sum(item > value for item in reference) / count * 100.0,
        midrank,
    )


def _quantile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    position = (len(values) - 1) * percentile / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(values[lower])
    return float(values[lower] + (values[upper] - values[lower]) * (position - lower))


def _elapsed_days(
    start: datetime | None,
    end: datetime | None,
    warnings: list[Diagnostic],
) -> float | None:
    if start is None or end is None:
        if not any(item.code == "drawdown_duration_days_undefined" for item in warnings):
            warnings.append(Diagnostic(
                "drawdown_duration_days_undefined",
                "Elapsed drawdown duration is undefined when an episode timestamp is missing",
            ))
        return None
    try:
        seconds = (end - start).total_seconds()
    except (TypeError, ValueError):
        if not any(item.code == "drawdown_duration_days_undefined" for item in warnings):
            warnings.append(Diagnostic(
                "drawdown_duration_days_undefined",
                "Elapsed drawdown duration is undefined when curve timestamps use incompatible timezone information",
            ))
        return None
    if seconds < 0:
        if not any(item.code == "drawdown_duration_days_undefined" for item in warnings):
            warnings.append(Diagnostic(
                "drawdown_duration_days_undefined",
                "Elapsed drawdown duration is undefined when curve timestamps are out of order",
            ))
        return None
    return seconds / 86400.0


def _empty_analysis(
    source: str,
    basis: str,
    initial_value: float,
    warnings: Sequence[Diagnostic],
) -> DrawdownAnalysis:
    empty = DrawdownDistribution.from_values((), "percent")
    return DrawdownAnalysis(
        curve_source=source,
        curve_basis=basis,
        initial_value=initial_value,
        observation_count=0,
        start_time=None,
        end_time=None,
        episodes=(),
        depth_distribution=empty,
        depth_money_distribution=DrawdownDistribution.from_values((), "money"),
        duration_distribution=DrawdownDistribution.from_values((), "days"),
        duration_periods_distribution=DrawdownDistribution.from_values((), "periods"),
        warnings=tuple(warnings),
    )


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _optional_datetime(value: object) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _optional_float(value: object) -> float | None:
    return None if value is None else float(value)


def _optional_int(value: object) -> int | None:
    return None if value is None else int(value)


__all__ = [
    "DRAWDOWN_PERCENTILES",
    "CurveLike",
    "DrawdownAnalysis",
    "DrawdownDistribution",
    "DrawdownEpisode",
    "analyze_drawdowns",
]
