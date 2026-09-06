"""Calendar-period return distributions derived from a timestamped account curve."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Protocol, Sequence

from .diagnostics import Diagnostic
from .serialization import to_primitive
from .statistics import linear_quantile


RETURN_DISTRIBUTION_FREQUENCIES = ("monthly", "weekly", "daily")
RETURN_DISTRIBUTION_WARNING_THRESHOLD = 30


class CurveLike(Protocol):
    """The timestamped curve attributes required by return analysis."""

    timestamps: Sequence[datetime | None]
    values: Sequence[float]
    source: str
    basis: str
    initial_value: float


@dataclass(frozen=True)
class ReturnDistributionObservation:
    """One calendar-period return observation.

    ``return_pct`` is a percentage rather than a fraction.  ``period_end`` is
    the exclusive calendar boundary.  A missing ``return_pct`` is retained in
    the observations for auditability but is excluded from percentile and
    count statistics when the starting value is zero or otherwise undefined.
    """

    period: str
    label: str
    period_start: datetime
    period_end: datetime
    start_value: float
    end_value: float
    return_pct: float | None
    partial: bool
    has_curve_observation: bool
    rank: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return to_primitive(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ReturnDistributionObservation":
        return cls(
            period=str(payload["period"]),
            label=str(payload.get("label", payload["period"])),
            period_start=_datetime(payload.get("period_start")),
            period_end=_datetime(payload.get("period_end")),
            start_value=float(payload["start_value"]),
            end_value=float(payload["end_value"]),
            return_pct=_optional_float(payload.get("return_pct")),
            partial=bool(payload.get("partial", False)),
            has_curve_observation=bool(payload.get("has_curve_observation", True)),
            rank=_optional_int(payload.get("rank")),
        )


@dataclass(frozen=True)
class ReturnDistributionStats:
    """Descriptive statistics for the finite period returns."""

    count: int
    minimum: float | None
    p5: float | None
    median: float | None
    p95: float | None
    mean: float | None
    maximum: float | None
    stddev: float | None
    positive_count: int
    positive_pct: float | None
    negative_count: int
    negative_pct: float | None
    zero_count: int
    zero_pct: float | None

    @classmethod
    def from_values(cls, values: Sequence[float]) -> "ReturnDistributionStats":
        finite = tuple(float(value) for value in values if math.isfinite(float(value)))
        count = len(finite)
        if not count:
            return cls(
                count=0,
                minimum=None,
                p5=None,
                median=None,
                p95=None,
                mean=None,
                maximum=None,
                stddev=None,
                positive_count=0,
                positive_pct=None,
                negative_count=0,
                negative_pct=None,
                zero_count=0,
                zero_pct=None,
            )
        ordered = tuple(sorted(finite))
        mean = sum(ordered) / count
        stddev = math.sqrt(sum((value - mean) ** 2 for value in ordered) / count)
        positive_count = sum(value > 0.0 for value in ordered)
        negative_count = sum(value < 0.0 for value in ordered)
        zero_count = count - positive_count - negative_count
        return cls(
            count=count,
            minimum=ordered[0],
            p5=linear_quantile(ordered, 5.0),
            median=linear_quantile(ordered, 50.0),
            p95=linear_quantile(ordered, 95.0),
            mean=float(mean),
            maximum=ordered[-1],
            stddev=float(stddev),
            positive_count=positive_count,
            positive_pct=positive_count / count * 100.0,
            negative_count=negative_count,
            negative_pct=negative_count / count * 100.0,
            zero_count=zero_count,
            zero_pct=zero_count / count * 100.0,
        )

    def to_dict(self) -> dict[str, Any]:
        return to_primitive(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ReturnDistributionStats":
        return cls(
            count=int(payload.get("count", 0)),
            minimum=_optional_float(payload.get("minimum")),
            p5=_optional_float(payload.get("p5")),
            median=_optional_float(payload.get("median")),
            p95=_optional_float(payload.get("p95")),
            mean=_optional_float(payload.get("mean")),
            maximum=_optional_float(payload.get("maximum")),
            stddev=_optional_float(payload.get("stddev")),
            positive_count=int(payload.get("positive_count", 0)),
            positive_pct=_optional_float(payload.get("positive_pct")),
            negative_count=int(payload.get("negative_count", 0)),
            negative_pct=_optional_float(payload.get("negative_pct")),
            zero_count=int(payload.get("zero_count", 0)),
            zero_pct=_optional_float(payload.get("zero_pct")),
        )


@dataclass(frozen=True)
class ReturnDistribution:
    """One monthly, weekly, or daily return distribution."""

    frequency: str
    curve_source: str
    curve_basis: str
    initial_value: float
    observation_count: int
    start_time: datetime | None
    end_time: datetime | None
    observations: tuple[ReturnDistributionObservation, ...]
    ranked_observations: tuple[ReturnDistributionObservation, ...]
    stats: ReturnDistributionStats
    warnings: tuple[Diagnostic, ...] = ()

    @property
    def count(self) -> int:
        """Number of calendar periods represented, including flat periods."""

        return self.observation_count

    @property
    def values(self) -> tuple[float, ...]:
        """Finite returns in ascending chart order."""

        return tuple(
            float(item.return_pct)
            for item in self.ranked_observations
            if item.return_pct is not None and math.isfinite(float(item.return_pct))
        )

    def to_dict(self) -> dict[str, Any]:
        return to_primitive(self)

    def to_interactive_dict(self) -> dict[str, Any]:
        """Return the compact redacted shape needed by the HTML chart.

        The typed result keeps both chronological and ranked observations with
        complete period boundaries. The self-contained browser only needs the
        ranked bar labels, values, and observation flags, so it deliberately
        omits the duplicate chronological list and unused boundary fields.
        """

        return {
            "frequency": self.frequency,
            "curve_source": self.curve_source,
            "curve_basis": self.curve_basis,
            "initial_value": self.initial_value,
            "observation_count": self.observation_count,
            "start_time": to_primitive(self.start_time),
            "end_time": to_primitive(self.end_time),
            "ranked_observations": [
                {
                    "period": item.period,
                    "label": item.label,
                    "return_pct": item.return_pct,
                    "has_curve_observation": item.has_curve_observation,
                    "rank": item.rank,
                }
                for item in self.ranked_observations
            ],
            "stats": self.stats.to_dict(),
            "warnings": [warning.to_dict() for warning in self.warnings],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ReturnDistribution":
        observations = tuple(
            ReturnDistributionObservation.from_dict(item)
            for item in payload.get("observations", ())
        )
        ranked_payload = payload.get("ranked_observations")
        ranked = tuple(
            ReturnDistributionObservation.from_dict(item)
            for item in ranked_payload
        ) if ranked_payload is not None else _rank_observations(observations)
        return cls(
            frequency=str(payload.get("frequency", "")),
            curve_source=str(payload.get("curve_source", "")),
            curve_basis=str(payload.get("curve_basis", "")),
            initial_value=float(payload.get("initial_value", 0.0)),
            observation_count=int(payload.get("observation_count", len(observations))),
            start_time=_optional_datetime(payload.get("start_time")),
            end_time=_optional_datetime(payload.get("end_time")),
            observations=observations,
            ranked_observations=ranked,
            stats=ReturnDistributionStats.from_dict(payload.get("stats", {})),
            warnings=tuple(_diagnostic(item) for item in payload.get("warnings", ())),
        )


@dataclass(frozen=True)
class ReturnDistributions:
    """All supported calendar frequencies for one selected account curve."""

    monthly: ReturnDistribution
    weekly: ReturnDistribution
    daily: ReturnDistribution
    warnings: tuple[Diagnostic, ...] = ()

    def get(self, frequency: str) -> ReturnDistribution:
        if frequency not in RETURN_DISTRIBUTION_FREQUENCIES:
            raise ValueError(
                f"frequency must be one of {list(RETURN_DISTRIBUTION_FREQUENCIES)}"
            )
        return getattr(self, frequency)

    def as_dict(self) -> dict[str, dict[str, Any]]:
        return {
            "monthly": self.monthly.to_dict(),
            "weekly": self.weekly.to_dict(),
            "daily": self.daily.to_dict(),
        }

    def with_curve_metadata(self, source: str, basis: str) -> "ReturnDistributions":
        """Return the same observations labelled with a transformed curve."""

        return replace(
            self,
            monthly=replace(self.monthly, curve_source=source, curve_basis=basis),
            weekly=replace(self.weekly, curve_source=source, curve_basis=basis),
            daily=replace(self.daily, curve_source=source, curve_basis=basis),
        )

    def to_dict(self) -> dict[str, Any]:
        # Container diagnostics are already represented in result.warnings;
        # keeping the serialized surface frequency-keyed makes the API easy to
        # consume. The renderer uses to_interactive_dict() for its compact shape.
        return self.as_dict()

    def to_interactive_dict(self) -> dict[str, dict[str, Any]]:
        """Return compact frequency-keyed data for the self-contained report."""

        return {
            "monthly": self.monthly.to_interactive_dict(),
            "weekly": self.weekly.to_interactive_dict(),
            "daily": self.daily.to_interactive_dict(),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ReturnDistributions":
        distributions = {
            frequency: ReturnDistribution.from_dict(payload.get(frequency, {}))
            for frequency in RETURN_DISTRIBUTION_FREQUENCIES
        }
        warnings = tuple(_diagnostic(item) for item in payload.get("warnings", ()))
        if not warnings:
            warnings = tuple(
                warning
                for frequency in RETURN_DISTRIBUTION_FREQUENCIES
                for warning in distributions[frequency].warnings
            )
        return cls(
            monthly=distributions["monthly"],
            weekly=distributions["weekly"],
            daily=distributions["daily"],
            warnings=warnings,
        )


def analyze_return_distribution(
    curve: CurveLike | None,
    frequency: str,
) -> ReturnDistribution:
    """Calculate calendar-period percentage returns from one account curve.

    Returns use the simple percentage formula ``(end / start - 1) * 100``.
    The first period starts from the curve's initial value; periods between the
    first and last curve observations are carried forward and retained as flat
    zero-return observations.  The first and last calendar periods are labelled
    partial when the supplied curve does not span their full calendar bounds.
    """

    if frequency not in RETURN_DISTRIBUTION_FREQUENCIES:
        raise ValueError(
            f"frequency must be one of {list(RETURN_DISTRIBUTION_FREQUENCIES)}"
        )
    warnings: list[Diagnostic] = []
    if curve is None:
        warnings.append(Diagnostic(
            "return_distribution_no_curve",
            "Return distribution analysis requires a timestamped account curve",
            context={"frequency": frequency},
        ))
        return _empty_distribution(frequency, warnings)

    try:
        raw_timestamps = list(curve.timestamps)
        raw_values = list(curve.values)
        curve_source = str(curve.source)
        curve_basis = str(curve.basis)
        initial_value = float(curve.initial_value)
    except (AttributeError, TypeError, ValueError) as error:
        raise TypeError(
            "curve must expose timestamps, values, source, basis, and initial_value"
        ) from error

    if not math.isfinite(initial_value):
        initial_value = 0.0
        warnings.append(Diagnostic(
            "return_distribution_non_finite_initial_value",
            "The curve initial value was non-finite and was represented as zero",
            context={"frequency": frequency},
        ))

    if len(raw_timestamps) != len(raw_values):
        warnings.append(Diagnostic(
            "return_distribution_curve_length_mismatch",
            "Curve timestamps and values have different lengths; unmatched observations were ignored",
            context={
                "frequency": frequency,
                "timestamp_count": len(raw_timestamps),
                "value_count": len(raw_values),
            },
        ))
    count = min(len(raw_timestamps), len(raw_values))
    observations: list[tuple[datetime, float, int]] = []
    for index, (timestamp, raw_value) in enumerate(zip(raw_timestamps[:count], raw_values[:count])):
        if not isinstance(timestamp, datetime):
            warnings.append(Diagnostic(
                "return_distribution_invalid_timestamp",
                "A curve observation without a datetime timestamp was ignored",
                context={"frequency": frequency, "index": index},
            ))
            continue
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            value = math.nan
        if not math.isfinite(value):
            warnings.append(Diagnostic(
                "return_distribution_non_finite_observation",
                "A non-finite curve observation was ignored for return distribution analysis",
                context={"frequency": frequency, "index": index},
            ))
            continue
        observations.append((timestamp, value, index))

    if not observations:
        warnings.append(Diagnostic(
            "return_distribution_no_observations",
            "No finite timestamped curve observations are available for return distribution analysis",
            context={"frequency": frequency},
        ))
        return _empty_distribution(frequency, warnings, curve_source, curve_basis, initial_value)

    try:
        observations.sort(key=lambda item: (item[0], item[2]))
    except TypeError:
        warnings.append(Diagnostic(
            "return_distribution_incompatible_timestamps",
            "Curve timestamps use incompatible timezone information; no return periods were generated",
            context={"frequency": frequency},
        ))
        return _empty_distribution(frequency, warnings, curve_source, curve_basis, initial_value)

    first_timestamp = observations[0][0]
    last_timestamp = observations[-1][0]
    first_period = _period_start(first_timestamp, frequency)
    final_period = _period_start(last_timestamp, frequency)
    current_period = first_period
    previous_value = initial_value
    period_observations: list[ReturnDistributionObservation] = []
    observation_index = 0
    while current_period <= final_period:
        next_period = _next_period(current_period, frequency)
        bucket: list[tuple[datetime, float, int]] = []
        while observation_index < len(observations):
            timestamp, value, source_index = observations[observation_index]
            try:
                before_next = timestamp < next_period
            except TypeError:
                warnings.append(Diagnostic(
                    "return_distribution_incompatible_timestamps",
                    "Curve timestamps use incompatible timezone information; no return periods were generated",
                    context={"frequency": frequency},
                ))
                return _empty_distribution(frequency, warnings, curve_source, curve_basis, initial_value)
            if timestamp < current_period:
                observation_index += 1
                continue
            if not before_next:
                break
            bucket.append((timestamp, value, source_index))
            observation_index += 1

        end_value = bucket[-1][1] if bucket else previous_value
        return_pct: float | None
        if previous_value == 0.0:
            return_pct = None
            warnings.append(Diagnostic(
                "return_distribution_undefined_return",
                "A period return is undefined when its starting curve value is zero",
                context={"frequency": frequency, "period": _period_label(current_period, frequency)},
            ))
        else:
            return_pct = float((end_value / previous_value - 1.0) * 100.0)
        is_first = current_period == first_period
        is_last = current_period == final_period
        partial = (
            (is_first and first_timestamp > current_period)
            or (is_last and last_timestamp < next_period)
        )
        period = _period_label(current_period, frequency)
        period_observations.append(ReturnDistributionObservation(
            period=period,
            label=f"{period} (partial)" if partial else period,
            period_start=current_period,
            period_end=next_period,
            start_value=float(previous_value),
            end_value=float(end_value),
            return_pct=return_pct,
            partial=partial,
            has_curve_observation=bool(bucket),
            rank=None,
        ))
        previous_value = end_value
        current_period = next_period

    ranked = _rank_observations(tuple(period_observations))
    values = [item.return_pct for item in period_observations if item.return_pct is not None]
    valid_values = [float(value) for value in values if math.isfinite(float(value))]
    if valid_values and len(valid_values) < RETURN_DISTRIBUTION_WARNING_THRESHOLD:
        warnings.append(Diagnostic(
            "return_distribution_low_observations",
            "Return distribution has fewer than the recommended 30 finite period observations",
            context={
                "frequency": frequency,
                "observation_count": len(period_observations),
                "finite_observation_count": len(valid_values),
                "recommended_observation_count": RETURN_DISTRIBUTION_WARNING_THRESHOLD,
            },
        ))
    elif not valid_values:
        warnings.append(Diagnostic(
            "return_distribution_low_observations",
            "Return distribution has no finite period observations",
            context={
                "frequency": frequency,
                "observation_count": len(period_observations),
                "finite_observation_count": 0,
                "recommended_observation_count": RETURN_DISTRIBUTION_WARNING_THRESHOLD,
            },
        ))

    return ReturnDistribution(
        frequency=frequency,
        curve_source=curve_source,
        curve_basis=curve_basis,
        initial_value=initial_value,
        observation_count=len(period_observations),
        start_time=first_timestamp,
        end_time=last_timestamp,
        observations=tuple(period_observations),
        ranked_observations=ranked,
        stats=ReturnDistributionStats.from_values(valid_values),
        warnings=tuple(warnings),
    )


def analyze_return_distributions(curve: CurveLike | None) -> ReturnDistributions:
    """Calculate monthly, weekly, and daily distributions for one curve."""

    distributions = {
        frequency: analyze_return_distribution(curve, frequency)
        for frequency in RETURN_DISTRIBUTION_FREQUENCIES
    }
    warnings = tuple(
        warning
        for frequency in RETURN_DISTRIBUTION_FREQUENCIES
        for warning in distributions[frequency].warnings
    )
    return ReturnDistributions(
        monthly=distributions["monthly"],
        weekly=distributions["weekly"],
        daily=distributions["daily"],
        warnings=warnings,
    )


def _rank_observations(
    observations: Sequence[ReturnDistributionObservation],
) -> tuple[ReturnDistributionObservation, ...]:
    finite = [
        item for item in observations
        if item.return_pct is not None and math.isfinite(float(item.return_pct))
    ]
    undefined = [
        item for item in observations
        if item.return_pct is None or not math.isfinite(float(item.return_pct))
    ]
    ordered = sorted(finite, key=lambda item: (float(item.return_pct), item.period))
    ranked = [replace(item, rank=index + 1) for index, item in enumerate(ordered)]
    return tuple(ranked + undefined)


def _empty_distribution(
    frequency: str,
    warnings: Sequence[Diagnostic],
    curve_source: str = "",
    curve_basis: str = "",
    initial_value: float = 0.0,
) -> ReturnDistribution:
    return ReturnDistribution(
        frequency=frequency,
        curve_source=curve_source,
        curve_basis=curve_basis,
        initial_value=initial_value,
        observation_count=0,
        start_time=None,
        end_time=None,
        observations=(),
        ranked_observations=(),
        stats=ReturnDistributionStats.from_values(()),
        warnings=tuple(warnings),
    )


def _period_start(timestamp: datetime, frequency: str) -> datetime:
    midnight = timestamp.replace(hour=0, minute=0, second=0, microsecond=0)
    if frequency == "daily":
        return midnight
    if frequency == "weekly":
        return midnight - timedelta(days=timestamp.weekday())
    return midnight.replace(day=1)


def _next_period(period_start: datetime, frequency: str) -> datetime:
    if frequency == "daily":
        return period_start + timedelta(days=1)
    if frequency == "weekly":
        return period_start + timedelta(days=7)
    if period_start.month == 12:
        return period_start.replace(year=period_start.year + 1, month=1)
    return period_start.replace(month=period_start.month + 1)


def _period_label(period_start: datetime, frequency: str) -> str:
    if frequency == "daily":
        return period_start.strftime("%Y-%m-%d")
    if frequency == "weekly":
        iso = period_start.date().isocalendar()
        return f"{iso.year:04d}-W{iso.week:02d}"
    return period_start.strftime("%Y-%m")


def _datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if value is None:
        raise ValueError("serialized return distribution observation is missing a datetime")
    return datetime.fromisoformat(str(value))


def _optional_datetime(value: object) -> datetime | None:
    return None if value is None else _datetime(value)


def _optional_float(value: object) -> float | None:
    return None if value is None else float(value)


def _optional_int(value: object) -> int | None:
    return None if value is None else int(value)


def _diagnostic(value: object) -> Diagnostic:
    if not isinstance(value, dict):
        return Diagnostic("return_distribution_serialization_warning", str(value))
    return Diagnostic(
        str(value.get("code", "return_distribution_warning")),
        str(value.get("message", "Return distribution warning")),
        str(value.get("severity", "warning")),
        dict(value.get("context", {})),
    )


__all__ = [
    "RETURN_DISTRIBUTION_FREQUENCIES",
    "RETURN_DISTRIBUTION_WARNING_THRESHOLD",
    "ReturnDistribution",
    "ReturnDistributionObservation",
    "ReturnDistributionStats",
    "ReturnDistributions",
    "analyze_return_distribution",
    "analyze_return_distributions",
]
