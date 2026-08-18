"""Eager net-profit groupings for completed MT5 positions.

The analytical values in this module are deliberately independent from the
optional chart layer.  A report is traversed once, and the resulting typed
buckets can be retrieved, serialized, cached, or rendered later without
recalculating the analysis.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Iterable

from .diagnostics import Diagnostic
from .models import Trade
from .serialization import to_primitive


class TradeProfitGrouping(str, Enum):
    """The timestamp dimension used to group canonical net trade profit."""

    OPEN_HOUR = "open_hour"
    CLOSE_HOUR = "close_hour"
    OPEN_DAY_OF_WEEK = "open_day_of_week"
    CLOSE_DAY_OF_WEEK = "close_day_of_week"

    @property
    def uses_close_time(self) -> bool:
        return self in {
            TradeProfitGrouping.CLOSE_HOUR,
            TradeProfitGrouping.CLOSE_DAY_OF_WEEK,
        }

    @property
    def is_hourly(self) -> bool:
        return self in {
            TradeProfitGrouping.OPEN_HOUR,
            TradeProfitGrouping.CLOSE_HOUR,
        }

    @property
    def labels(self) -> tuple[str, ...]:
        if self.is_hourly:
            return tuple(f"{hour:02d}:00" for hour in range(24))
        return (
            "Monday",
            "Tuesday",
            "Wednesday",
            "Thursday",
            "Friday",
            "Saturday",
            "Sunday",
        )


class TradeProfitMeasure(str, Enum):
    """A value that can be selected for a grouped trade-profit chart."""

    NET_PROFIT = "net_profit"
    PERCENTAGE_GAIN = "percentage_gain"


@dataclass(frozen=True)
class TradeProfitConfig:
    """Configuration for eager grouped trade-profit analytics."""

    retain_trade_ids: bool = False


@dataclass(frozen=True)
class TradeProfitBucket:
    """One deterministic bucket of completed-position net-profit results.

    ``gross_profit`` and ``gross_loss`` retain familiar reporting names, but
    they are calculated from the canonical *net* ``Trade.profit`` values.  Raw
    pre-swap/pre-commission gross trade values are never used by this API.
    ``gross_loss`` is negative (or zero), matching the sign of the underlying
    losing net trades.
    """

    label: str
    bucket_index: int
    net_profit: float
    percentage_gain: float | None
    gross_profit: float
    gross_loss: float
    trade_count: int
    winning_trade_count: int
    losing_trade_count: int
    average_trade_profit: float | None
    trade_ids: tuple[str, ...] = ()
    position_ids: tuple[str, ...] = ()

    @property
    def positive_net_profit(self) -> float:
        """Positive sum of canonical net trade profits."""

        return self.gross_profit

    @property
    def negative_net_profit(self) -> float:
        """Negative sum of canonical net trade profits."""

        return self.gross_loss

    def value_for(self, measure: TradeProfitMeasure | str) -> float | None:
        selected = TradeProfitMeasure(measure)
        if selected is TradeProfitMeasure.NET_PROFIT:
            return self.net_profit
        return self.percentage_gain

    def to_dict(self) -> dict[str, Any]:
        return to_primitive(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TradeProfitBucket":
        return cls(
            label=payload["label"],
            bucket_index=int(payload["bucket_index"]),
            net_profit=float(payload["net_profit"]),
            percentage_gain=(
                float(payload["percentage_gain"])
                if payload.get("percentage_gain") is not None
                else None
            ),
            gross_profit=float(payload.get("gross_profit", payload.get("positive_net_profit", 0.0))),
            gross_loss=float(payload.get("gross_loss", payload.get("negative_net_profit", 0.0))),
            trade_count=int(payload.get("trade_count", 0)),
            winning_trade_count=int(payload.get("winning_trade_count", 0)),
            losing_trade_count=int(payload.get("losing_trade_count", 0)),
            average_trade_profit=(
                float(payload["average_trade_profit"])
                if payload.get("average_trade_profit") is not None
                else None
            ),
            trade_ids=tuple(str(value) for value in payload.get("trade_ids", ())),
            position_ids=tuple(str(value) for value in payload.get("position_ids", ())),
        )


@dataclass(frozen=True)
class TradeProfitGroupingResult:
    """All buckets for one grouping dimension."""

    grouping: TradeProfitGrouping
    buckets: tuple[TradeProfitBucket, ...]
    currency: str
    initial_capital: float
    timezone: str | None
    active_start: datetime | None
    active_end: datetime | None
    warnings: tuple[Diagnostic, ...] = ()

    @property
    def trade_count(self) -> int:
        return sum(bucket.trade_count for bucket in self.buckets)

    @property
    def net_profit(self) -> float:
        return float(sum(bucket.net_profit for bucket in self.buckets))

    def bucket(self, label_or_index: str | int) -> TradeProfitBucket:
        if isinstance(label_or_index, int):
            return self.buckets[label_or_index]
        for bucket in self.buckets:
            if bucket.label == label_or_index:
                return bucket
        raise KeyError(label_or_index)

    def to_dict(self) -> dict[str, Any]:
        return to_primitive(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TradeProfitGroupingResult":
        return cls(
            grouping=TradeProfitGrouping(payload["grouping"]),
            buckets=tuple(TradeProfitBucket.from_dict(item) for item in payload.get("buckets", ())),
            currency=payload.get("currency", ""),
            initial_capital=float(payload.get("initial_capital", 0.0)),
            timezone=payload.get("timezone"),
            active_start=_parse_datetime(payload.get("active_start")),
            active_end=_parse_datetime(payload.get("active_end")),
            warnings=tuple(_diagnostic(item) for item in payload.get("warnings", ())),
        )


@dataclass(frozen=True)
class TradeProfitAnalysis:
    """Eager grouped net-profit analytics for one result or portfolio."""

    open_hour: TradeProfitGroupingResult
    close_hour: TradeProfitGroupingResult
    open_day_of_week: TradeProfitGroupingResult
    close_day_of_week: TradeProfitGroupingResult
    config: TradeProfitConfig = field(default_factory=TradeProfitConfig)
    warnings: tuple[Diagnostic, ...] = ()

    @property
    def by_grouping(self) -> dict[TradeProfitGrouping, TradeProfitGroupingResult]:
        return {
            TradeProfitGrouping.OPEN_HOUR: self.open_hour,
            TradeProfitGrouping.CLOSE_HOUR: self.close_hour,
            TradeProfitGrouping.OPEN_DAY_OF_WEEK: self.open_day_of_week,
            TradeProfitGrouping.CLOSE_DAY_OF_WEEK: self.close_day_of_week,
        }

    @property
    def groupings(self) -> tuple[TradeProfitGrouping, ...]:
        return tuple(self.by_grouping)

    def get(self, grouping: TradeProfitGrouping | str) -> TradeProfitGroupingResult:
        return self.by_grouping[TradeProfitGrouping(grouping)]

    def __getitem__(self, grouping: TradeProfitGrouping | str) -> TradeProfitGroupingResult:
        return self.get(grouping)

    def to_dict(self) -> dict[str, Any]:
        return to_primitive(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TradeProfitAnalysis":
        default_config = TradeProfitConfig()
        config_data = payload.get("config", {}) or {}
        config = TradeProfitConfig(
            retain_trade_ids=bool(config_data.get("retain_trade_ids", default_config.retain_trade_ids)),
        )
        results = {
            grouping: TradeProfitGroupingResult.from_dict(
                payload.get(grouping.value, {"grouping": grouping.value})
            )
            for grouping in TradeProfitGrouping
        }
        return cls(
            open_hour=results[TradeProfitGrouping.OPEN_HOUR],
            close_hour=results[TradeProfitGrouping.CLOSE_HOUR],
            open_day_of_week=results[TradeProfitGrouping.OPEN_DAY_OF_WEEK],
            close_day_of_week=results[TradeProfitGrouping.CLOSE_DAY_OF_WEEK],
            config=config,
            warnings=tuple(_diagnostic(item) for item in payload.get("warnings", ())),
        )


@dataclass
class _Accumulator:
    net_profit: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    trade_count: int = 0
    winning_trade_count: int = 0
    losing_trade_count: int = 0
    trade_ids: list[str] | None = None
    position_ids: list[str] | None = None


def _parse_datetime(value: str | datetime | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def _diagnostic(payload: dict[str, Any]) -> Diagnostic:
    return Diagnostic(
        code=payload["code"],
        message=payload["message"],
        severity=payload.get("severity", "warning"),
        context=payload.get("context", {}),
    )


def _timestamp_for(grouping: TradeProfitGrouping, trade: Trade) -> datetime | None:
    return trade.close_time if grouping.uses_close_time else trade.open_time


def _bucket_index(grouping: TradeProfitGrouping, timestamp: datetime) -> int:
    return timestamp.hour if grouping.is_hourly else timestamp.weekday()


def _warning_for_missing(
    grouping: TradeProfitGrouping,
    excluded_count: int,
) -> Diagnostic:
    timestamp_name = "closing" if grouping.uses_close_time else "opening"
    return Diagnostic(
        code=f"trade_profit_missing_{'close' if grouping.uses_close_time else 'open'}_time",
        message=(
            f"{excluded_count} completed position(s) were excluded from the "
            f"{grouping.value} trade-profit grouping because the {timestamp_name} "
            "timestamp was missing"
        ),
        context={
            "grouping": grouping.value,
            "excluded_trade_count": excluded_count,
            "timestamp": "close_time" if grouping.uses_close_time else "open_time",
        },
    )


def _warning_for_inferred(
    grouping: TradeProfitGrouping,
    inferred_count: int,
) -> Diagnostic:
    timestamp_name = "closing" if grouping.uses_close_time else "opening"
    return Diagnostic(
        code=f"trade_profit_inferred_{'close' if grouping.uses_close_time else 'open'}_time",
        message=(
            f"{inferred_count} completed position(s) in the {grouping.value} "
            f"grouping used inferred {timestamp_name} timestamps"
        ),
        context={
            "grouping": grouping.value,
            "inferred_trade_count": inferred_count,
            "timestamp": "close_time" if grouping.uses_close_time else "open_time",
        },
    )


def _make_grouping_result(
    grouping: TradeProfitGrouping,
    accumulators: list[_Accumulator],
    currency: str,
    initial_capital: float,
    timezone: str | None,
    active_start: datetime | None,
    active_end: datetime | None,
    warnings: list[Diagnostic],
    config: TradeProfitConfig,
) -> TradeProfitGroupingResult:
    buckets: list[TradeProfitBucket] = []
    denominator = float(initial_capital)
    for index, (label, accumulator) in enumerate(zip(grouping.labels, accumulators)):
        average = (
            float(accumulator.net_profit / accumulator.trade_count)
            if accumulator.trade_count
            else None
        )
        percentage = (
            float(accumulator.net_profit / denominator * 100.0)
            if denominator > 0.0
            else None
        )
        buckets.append(TradeProfitBucket(
            label=label,
            bucket_index=index,
            net_profit=float(accumulator.net_profit),
            percentage_gain=percentage,
            gross_profit=float(accumulator.gross_profit),
            gross_loss=float(accumulator.gross_loss),
            trade_count=accumulator.trade_count,
            winning_trade_count=accumulator.winning_trade_count,
            losing_trade_count=accumulator.losing_trade_count,
            average_trade_profit=average,
            trade_ids=tuple(accumulator.trade_ids or ()) if config.retain_trade_ids else (),
            position_ids=tuple(accumulator.position_ids or ()) if config.retain_trade_ids else (),
        ))
    return TradeProfitGroupingResult(
        grouping=grouping,
        buckets=tuple(buckets),
        currency=currency,
        initial_capital=float(initial_capital),
        timezone=timezone,
        active_start=active_start,
        active_end=active_end,
        warnings=tuple(warnings),
    )


def build_trade_profit_analysis(
    trades: Iterable[Trade],
    *,
    initial_capital: float,
    currency: str = "",
    timezone: str | None = None,
    config: TradeProfitConfig | None = None,
) -> TradeProfitAnalysis:
    """Build all four grouped net-profit dimensions in one deterministic pass.

    The iterable is consumed in caller-provided order; callers should pass
    ``Report.ordered_trades()`` (as the public analysis pipeline does) when
    deterministic retained identifiers are required.  Grouping uses the
    timestamp as represented by the report/broker; it never silently converts
    it to another timezone.
    """

    config = config or TradeProfitConfig()
    if not isinstance(config, TradeProfitConfig):
        raise TypeError("config must be a TradeProfitConfig")
    all_groupings = tuple(TradeProfitGrouping)
    accumulators = {
        grouping: [
            _Accumulator(
                trade_ids=[] if config.retain_trade_ids else None,
                position_ids=[] if config.retain_trade_ids else None,
            )
            for _ in grouping.labels
        ]
        for grouping in all_groupings
    }
    missing_counts = {grouping: 0 for grouping in all_groupings}
    inferred_counts = {grouping: 0 for grouping in all_groupings}
    active_bounds: dict[TradeProfitGrouping, list[datetime | None]] = {
        grouping: [None, None] for grouping in all_groupings
    }

    for trade in trades:
        # The model's ``profit`` is already the canonical account-currency net
        # result.  In particular, do not replace it with Trade.gross_profit.
        net_profit = float(trade.profit)
        for grouping in all_groupings:
            timestamp = _timestamp_for(grouping, trade)
            if timestamp is None:
                missing_counts[grouping] += 1
                continue
            if not grouping.uses_close_time and trade.open_time_inferred:
                inferred_counts[grouping] += 1
            bounds = active_bounds[grouping]
            bounds[0] = timestamp if bounds[0] is None else min(bounds[0], timestamp)
            bounds[1] = timestamp if bounds[1] is None else max(bounds[1], timestamp)
            accumulator = accumulators[grouping][_bucket_index(grouping, timestamp)]
            accumulator.net_profit += net_profit
            if net_profit > 0.0:
                accumulator.gross_profit += net_profit
                accumulator.winning_trade_count += 1
            elif net_profit < 0.0:
                accumulator.gross_loss += net_profit
                accumulator.losing_trade_count += 1
            accumulator.trade_count += 1
            if accumulator.trade_ids is not None:
                accumulator.trade_ids.append(str(trade.ticket))
            if accumulator.position_ids is not None and trade.position_id is not None:
                accumulator.position_ids.append(str(trade.position_id))

    grouping_results: dict[TradeProfitGrouping, TradeProfitGroupingResult] = {}
    all_warnings: list[Diagnostic] = []
    for grouping in all_groupings:
        warnings: list[Diagnostic] = []
        if missing_counts[grouping]:
            warnings.append(_warning_for_missing(grouping, missing_counts[grouping]))
        if inferred_counts[grouping]:
            warnings.append(_warning_for_inferred(grouping, inferred_counts[grouping]))
        result = _make_grouping_result(
            grouping,
            accumulators[grouping],
            currency,
            initial_capital,
            timezone,
            active_bounds[grouping][0],
            active_bounds[grouping][1],
            warnings,
            config,
        )
        grouping_results[grouping] = result
        all_warnings.extend(warnings)

    return TradeProfitAnalysis(
        open_hour=grouping_results[TradeProfitGrouping.OPEN_HOUR],
        close_hour=grouping_results[TradeProfitGrouping.CLOSE_HOUR],
        open_day_of_week=grouping_results[TradeProfitGrouping.OPEN_DAY_OF_WEEK],
        close_day_of_week=grouping_results[TradeProfitGrouping.CLOSE_DAY_OF_WEEK],
        config=config,
        warnings=tuple(all_warnings),
    )
