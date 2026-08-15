"""Deterministic selection of canonical completed-position trades.

Filters are intentionally separate from metric calculation.  A filter selects
whole canonical :class:`~analyser.models.Trade` objects using their open time;
the existing analysis engine then recalculates every metric and curve from the
selected trades.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from enum import Enum
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .diagnostics import Diagnostic
from .errors import FilterConfigurationError
from .models import Report, Trade, TradeSide
from .serialization import deterministic_json, to_primitive


class ForexSession(str, Enum):
    """Named local-time windows for the four v1 forex sessions."""

    SYDNEY = "sydney"
    TOKYO = "tokyo"
    LONDON = "london"
    NEW_YORK = "new_york"


_SESSION_WINDOWS: dict[ForexSession, tuple[time, time, str]] = {
    ForexSession.SYDNEY: (time(8, 0), time(17, 0), "Australia/Sydney"),
    ForexSession.TOKYO: (time(9, 0), time(18, 0), "Asia/Tokyo"),
    ForexSession.LONDON: (time(8, 0), time(17, 0), "Europe/London"),
    ForexSession.NEW_YORK: (time(8, 0), time(17, 0), "America/New_York"),
}


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, TypeError, ValueError) as exc:
        raise FilterConfigurationError(f"unknown IANA timezone: {name!r}") from exc


def _time_value(value: time) -> tuple[int, int, int, int]:
    return value.hour, value.minute, value.second, value.microsecond


def _window_contains(value: time, start: time, end: time) -> bool:
    value_key = _time_value(value)
    start_key = _time_value(start)
    end_key = _time_value(end)
    if start_key < end_key:
        return start_key <= value_key < end_key
    if start_key > end_key:
        return value_key >= start_key or value_key < end_key
    raise FilterConfigurationError("a time window must have distinct start and end times")


def _serialize_temporal(value: date | datetime) -> str:
    return value.isoformat()


@dataclass(frozen=True)
class FilterConfig:
    """Context required to interpret report timestamps deterministically."""

    report_timezone: str | None = None

    def __post_init__(self) -> None:
        if self.report_timezone is not None:
            _zone(self.report_timezone)

    def resolve_report_timezone(self, report: Report) -> str | None:
        return report.timezone or self.report_timezone

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FilterContext:
    report_timezone: str | None
    report_zone: ZoneInfo | None

    @classmethod
    def for_report(cls, report: Report, config: FilterConfig) -> "FilterContext":
        timezone = config.resolve_report_timezone(report)
        return cls(timezone, _zone(timezone) if timezone else None)

    def local_time(self, timestamp: datetime, target_timezone: str | None) -> time:
        if timestamp.tzinfo is None:
            if self.report_zone is None:
                raise FilterConfigurationError(
                    "a report timezone is required for time-of-day and session filters "
                    "when report timestamps are naive"
                )
            timestamp = timestamp.replace(tzinfo=self.report_zone)
        if target_timezone:
            timestamp = timestamp.astimezone(_zone(target_timezone))
        return timestamp.timetz().replace(tzinfo=None)

    def report_datetime(self, timestamp: datetime) -> datetime:
        if timestamp.tzinfo is None:
            if self.report_zone is None:
                return timestamp
            return timestamp.replace(tzinfo=self.report_zone)
        return timestamp.astimezone(self.report_zone) if self.report_zone else timestamp


@dataclass(frozen=True)
class FilterEvaluation:
    matched: bool
    reasons: tuple[str, ...] = ()


class TradeFilter(ABC):
    """One immutable predicate over a canonical completed position."""

    @property
    @abstractmethod
    def code(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def evaluate(self, trade: Trade, context: FilterContext) -> FilterEvaluation:
        raise NotImplementedError

    @abstractmethod
    def to_dict(self) -> dict[str, Any]:
        raise NotImplementedError

    def fingerprint(self) -> str:
        return hashlib.sha256(deterministic_json(self.to_dict()).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LongOnly(TradeFilter):
    @property
    def code(self) -> str:
        return "long_only"

    def evaluate(self, trade: Trade, context: FilterContext) -> FilterEvaluation:
        if trade.side is TradeSide.LONG:
            return FilterEvaluation(True)
        return FilterEvaluation(False, (self.code,))

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.code}


@dataclass(frozen=True)
class ShortOnly(TradeFilter):
    @property
    def code(self) -> str:
        return "short_only"

    def evaluate(self, trade: Trade, context: FilterContext) -> FilterEvaluation:
        if trade.side is TradeSide.SHORT:
            return FilterEvaluation(True)
        return FilterEvaluation(False, (self.code,))

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.code}


@dataclass(frozen=True)
class OpenDateRangeFilter(TradeFilter):
    """Select trades whose open time is in ``[start, end)``."""

    start: date | datetime
    end: date | datetime

    def __post_init__(self) -> None:
        valid_types = (date, datetime)
        if not isinstance(self.start, valid_types) or not isinstance(self.end, valid_types):
            raise FilterConfigurationError("OpenDateRangeFilter boundaries must be date or datetime values")
        if type(self.start) is not type(self.end):
            raise FilterConfigurationError("OpenDateRangeFilter start and end must have the same type")
        if self.start >= self.end:
            raise FilterConfigurationError("OpenDateRangeFilter requires start < end")
        if isinstance(self.start, datetime):
            if (self.start.tzinfo is None) != (self.end.tzinfo is None):
                raise FilterConfigurationError("date-range datetime boundaries must both be naive or both timezone-aware")

    @property
    def code(self) -> str:
        return "open_date_range"

    def evaluate(self, trade: Trade, context: FilterContext) -> FilterEvaluation:
        if trade.open_time is None or trade.open_time_inferred:
            return FilterEvaluation(False, (self.code,))
        timestamp = trade.open_time
        if isinstance(self.start, datetime):
            start = self.start
            end = self.end
            if start.tzinfo is None:
                # Naive datetime boundaries are local to the report timezone.
                # If no report zone exists, an aware timestamp keeps its own
                # embedded timezone before comparison by wall-clock value.
                timestamp = context.report_datetime(timestamp).replace(tzinfo=None)
            else:
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(
                        tzinfo=context.report_zone or start.tzinfo
                    )
                timestamp = timestamp.astimezone(start.tzinfo)
            matched = start <= timestamp < end
        else:
            matched = self.start <= context.report_datetime(timestamp).date() < self.end
        return FilterEvaluation(matched, (self.code,) if not matched else ())

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.code,
            "start": _serialize_temporal(self.start),
            "end": _serialize_temporal(self.end),
            "value_type": "datetime" if isinstance(self.start, datetime) else "date",
        }


@dataclass(frozen=True)
class TimeOfDayFilter(TradeFilter):
    """Select trades opened during a local-time window."""

    start: time
    end: time
    timezone: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.start, time) or not isinstance(self.end, time):
            raise FilterConfigurationError("TimeOfDayFilter boundaries must be time values")
        if self.start.tzinfo is not None or self.end.tzinfo is not None:
            raise FilterConfigurationError("TimeOfDayFilter boundaries must be timezone-naive times")
        if _time_value(self.start) == _time_value(self.end):
            raise FilterConfigurationError("TimeOfDayFilter requires distinct start and end times")
        if self.timezone is not None:
            _zone(self.timezone)

    @property
    def code(self) -> str:
        return "time_of_day"

    def evaluate(self, trade: Trade, context: FilterContext) -> FilterEvaluation:
        if trade.open_time is None or trade.open_time_inferred:
            return FilterEvaluation(False, (self.code,))
        value = context.local_time(trade.open_time, self.timezone or context.report_timezone)
        matched = _window_contains(value, self.start, self.end)
        return FilterEvaluation(matched, (self.code,) if not matched else ())

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.code,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "timezone": self.timezone,
        }


@dataclass(frozen=True)
class SessionFilter(TradeFilter):
    """Select trades opened during one named forex session."""

    session: ForexSession | str

    def __post_init__(self) -> None:
        try:
            if isinstance(self.session, ForexSession):
                session = self.session
            else:
                normalized = str(self.session).strip().lower().replace("-", "_").replace(" ", "_")
                session = ForexSession(normalized)
        except ValueError as exc:
            raise FilterConfigurationError(
                f"unknown forex session {self.session!r}; expected one of "
                f"{', '.join(item.value for item in ForexSession)}"
            ) from exc
        object.__setattr__(self, "session", session)

    @property
    def code(self) -> str:
        return f"session_{self.session.value}"

    def evaluate(self, trade: Trade, context: FilterContext) -> FilterEvaluation:
        if trade.open_time is None or trade.open_time_inferred:
            return FilterEvaluation(False, (self.code,))
        start, end, timezone = _SESSION_WINDOWS[self.session]
        value = context.local_time(trade.open_time, timezone)
        matched = _window_contains(value, start, end)
        return FilterEvaluation(matched, (self.code,) if not matched else ())

    def to_dict(self) -> dict[str, Any]:
        start, end, timezone = _SESSION_WINDOWS[self.session]
        return {
            "type": "session",
            "session": self.session.value,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "timezone": timezone,
        }


def _flatten_filters(filters: tuple[TradeFilter | Iterable[TradeFilter], ...]) -> tuple[TradeFilter, ...]:
    flattened: list[TradeFilter] = []
    for item in filters:
        if isinstance(item, TradeFilter):
            flattened.append(item)
            continue
        try:
            nested = tuple(item)
        except TypeError as exc:
            raise FilterConfigurationError("filter compositions accept TradeFilter objects") from exc
        if not all(isinstance(child, TradeFilter) for child in nested):
            raise FilterConfigurationError("filter compositions accept TradeFilter objects")
        flattened.extend(nested)
    return tuple(flattened)


@dataclass(frozen=True)
class AllOf(TradeFilter):
    filters: tuple[TradeFilter, ...]

    def __init__(self, *filters: TradeFilter | Iterable[TradeFilter]):
        object.__setattr__(self, "filters", _flatten_filters(filters))

    @property
    def code(self) -> str:
        return "all_of"

    def evaluate(self, trade: Trade, context: FilterContext) -> FilterEvaluation:
        evaluations = [item.evaluate(trade, context) for item in self.filters]
        reasons = tuple(dict.fromkeys(reason for evaluation in evaluations for reason in evaluation.reasons))
        return FilterEvaluation(all(evaluation.matched for evaluation in evaluations), reasons)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.code, "filters": [item.to_dict() for item in self.filters]}


@dataclass(frozen=True)
class AnyOf(TradeFilter):
    filters: tuple[TradeFilter, ...]

    def __init__(self, *filters: TradeFilter | Iterable[TradeFilter]):
        object.__setattr__(self, "filters", _flatten_filters(filters))

    @property
    def code(self) -> str:
        return "any_of"

    def evaluate(self, trade: Trade, context: FilterContext) -> FilterEvaluation:
        evaluations = [item.evaluate(trade, context) for item in self.filters]
        if any(evaluation.matched for evaluation in evaluations):
            return FilterEvaluation(True)
        reasons = tuple(dict.fromkeys(reason for evaluation in evaluations for reason in evaluation.reasons))
        return FilterEvaluation(False, reasons)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.code, "filters": [item.to_dict() for item in self.filters]}


@dataclass(frozen=True)
class Not(TradeFilter):
    filter: TradeFilter

    def __post_init__(self) -> None:
        if not isinstance(self.filter, TradeFilter):
            raise FilterConfigurationError("Not accepts one TradeFilter object")

    @property
    def code(self) -> str:
        return "not"

    def evaluate(self, trade: Trade, context: FilterContext) -> FilterEvaluation:
        evaluation = self.filter.evaluate(trade, context)
        return FilterEvaluation(False, (self.code,)) if evaluation.matched else FilterEvaluation(True)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.code, "filter": self.filter.to_dict()}


@dataclass(frozen=True)
class TradeSelectionRecord:
    ticket: str
    position_id: str | None
    selected: bool
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class TradeSelection:
    source_trade_count: int
    selected_trade_count: int
    excluded_trade_count: int
    records: tuple[TradeSelectionRecord, ...]
    excluded_by_filter: dict[str, int]
    filter_spec: dict[str, Any]
    filter_config: dict[str, Any]

    @property
    def selected_trade_keys(self) -> tuple[str, ...]:
        return tuple(record.ticket for record in self.records if record.selected)

    @property
    def excluded_trade_keys(self) -> tuple[str, ...]:
        return tuple(record.ticket for record in self.records if not record.selected)

    def to_dict(self) -> dict[str, Any]:
        return to_primitive(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TradeSelection":
        return cls(
            source_trade_count=payload["source_trade_count"],
            selected_trade_count=payload["selected_trade_count"],
            excluded_trade_count=payload["excluded_trade_count"],
            records=tuple(
                TradeSelectionRecord(
                    ticket=item["ticket"],
                    position_id=item.get("position_id"),
                    selected=item["selected"],
                    reasons=tuple(item.get("reasons", ())),
                )
                for item in payload.get("records", [])
            ),
            excluded_by_filter=payload.get("excluded_by_filter", {}),
            filter_spec=payload.get("filter_spec", {}),
            filter_config=payload.get("filter_config", {}),
        )


def filter_from_dict(payload: dict[str, Any]) -> TradeFilter:
    kind = payload.get("type")
    if kind == "long_only":
        return LongOnly()
    if kind == "short_only":
        return ShortOnly()
    if kind == "open_date_range":
        parser = datetime.fromisoformat if payload.get("value_type") == "datetime" else date.fromisoformat
        return OpenDateRangeFilter(parser(payload["start"]), parser(payload["end"]))
    if kind == "time_of_day":
        return TimeOfDayFilter(time.fromisoformat(payload["start"]), time.fromisoformat(payload["end"]), payload.get("timezone"))
    if kind == "session":
        return SessionFilter(payload["session"])
    if kind == "all_of":
        return AllOf(filter_from_dict(item) for item in payload.get("filters", []))
    if kind == "any_of":
        return AnyOf(filter_from_dict(item) for item in payload.get("filters", []))
    if kind == "not":
        return Not(filter_from_dict(payload["filter"]))
    raise FilterConfigurationError(f"unknown trade filter type: {kind!r}")


def _requires_timezone(filter_spec: TradeFilter) -> bool:
    if isinstance(filter_spec, (TimeOfDayFilter, SessionFilter)):
        return True
    if isinstance(filter_spec, OpenDateRangeFilter):
        return isinstance(filter_spec.start, datetime) and (
            filter_spec.start.tzinfo is not None or filter_spec.end.tzinfo is not None
        )
    if isinstance(filter_spec, (AllOf, AnyOf)):
        return any(_requires_timezone(item) for item in filter_spec.filters)
    if isinstance(filter_spec, Not):
        return _requires_timezone(filter_spec.filter)
    return False


def select_trades(
    report: Report,
    filter_spec: TradeFilter,
    config: FilterConfig | None = None,
) -> tuple[list[Trade], TradeSelection, tuple[Diagnostic, ...]]:
    """Select whole completed positions and return audit metadata/diagnostics."""

    if not isinstance(filter_spec, TradeFilter):
        raise TypeError("filter_spec must be a TradeFilter")
    if config is None:
        config = FilterConfig()
    if not isinstance(config, FilterConfig):
        raise TypeError("config must be a FilterConfig")
    context = FilterContext.for_report(report, config)
    ordered = report.ordered_trades()
    if _requires_timezone(filter_spec) and context.report_zone is None:
        if any(trade.open_time is not None and trade.open_time.tzinfo is None for trade in ordered):
            raise FilterConfigurationError(
                "report_timezone is required for open-time filters when report timestamps are naive"
            )

    selected: list[Trade] = []
    records: list[TradeSelectionRecord] = []
    excluded_by_filter: dict[str, int] = {}
    missing_open_time = 0
    for trade in ordered:
        evaluation = filter_spec.evaluate(trade, context)
        if evaluation.matched:
            selected.append(trade)
        else:
            if any(reason in {"open_date_range", "time_of_day"} or reason.startswith("session_") for reason in evaluation.reasons):
                if trade.open_time is None or trade.open_time_inferred:
                    missing_open_time += 1
            for reason in evaluation.reasons:
                excluded_by_filter[reason] = excluded_by_filter.get(reason, 0) + 1
        records.append(TradeSelectionRecord(
            ticket=trade.ticket,
            position_id=trade.position_id,
            selected=evaluation.matched,
            reasons=evaluation.reasons,
        ))
    diagnostics: list[Diagnostic] = []
    if _requires_timezone(filter_spec) and report.timezone and config.report_timezone and report.timezone != config.report_timezone:
        diagnostics.append(Diagnostic(
            "filter_config_timezone_overridden",
            "The report timezone took precedence over FilterConfig.report_timezone",
            context={"report_timezone": report.timezone, "configured_timezone": config.report_timezone},
        ))
    if missing_open_time:
        diagnostics.append(Diagnostic(
            "filter_missing_open_time",
            "Trades missing a trustworthy open time were excluded from the temporal filter",
            context={"count": missing_open_time},
        ))
    if not selected:
        diagnostics.append(Diagnostic(
            "no_trades_selected",
            "The trade filters selected no completed positions",
            context={"source_trade_count": len(ordered)},
        ))
    selection = TradeSelection(
        source_trade_count=len(ordered),
        selected_trade_count=len(selected),
        excluded_trade_count=len(ordered) - len(selected),
        records=tuple(records),
        excluded_by_filter=dict(sorted(excluded_by_filter.items())),
        filter_spec=filter_spec.to_dict(),
        filter_config=config.to_dict(),
    )
    return selected, selection, tuple(diagnostics)


def filter_fingerprint(filter_spec: TradeFilter, config: FilterConfig | None = None) -> str:
    config = config or FilterConfig()
    descriptor = {"filter_spec": filter_spec.to_dict(), "filter_config": config.to_dict(), "version": "2"}
    return hashlib.sha256(deterministic_json(descriptor).encode("utf-8")).hexdigest()
