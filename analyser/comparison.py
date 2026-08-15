"""Canonical XML/HTML equivalence checks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import Report, Trade


@dataclass(frozen=True)
class TradeMismatch:
    index: int
    field: str
    left: Any
    right: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "field": self.field,
            "left": self.left,
            "right": self.right,
        }


@dataclass(frozen=True)
class ReportComparison:
    equivalent: bool
    left_trade_count: int
    right_trade_count: int
    mismatches: tuple[TradeMismatch, ...] = ()
    metadata_mismatches: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "equivalent": self.equivalent,
            "left_trade_count": self.left_trade_count,
            "right_trade_count": self.right_trade_count,
            "mismatches": [mismatch.to_dict() for mismatch in self.mismatches],
            "metadata_mismatches": list(self.metadata_mismatches),
        }


def _trade_fields(trade: Trade) -> dict[str, Any]:
    return {
        "ticket": trade.ticket,
        "position_id": trade.position_id,
        "deal_ids": trade.deal_ids,
        "symbol": trade.symbol,
        "side": trade.side.value,
        "volume": trade.volume,
        "open_time": trade.open_time,
        "open_time_inferred": trade.open_time_inferred,
        "close_time": trade.close_time,
        "open_price": trade.open_price,
        "close_price": trade.close_price,
        "profit": trade.profit,
        "swap": trade.swap,
        "commission": trade.commission,
        "sl": trade.sl,
        "tp": trade.tp,
        "comment": trade.comment,
        "magic": trade.magic,
    }


def compare_reports(
    left: Report,
    right: Report,
    *,
    numeric_tolerance: float = 1e-8,
    max_mismatches: int = 100,
) -> ReportComparison:
    left_trades = left.ordered_trades()
    right_trades = right.ordered_trades()
    mismatches: list[TradeMismatch] = []
    for index, (left_trade, right_trade) in enumerate(zip(left_trades, right_trades)):
        left_fields = _trade_fields(left_trade)
        right_fields = _trade_fields(right_trade)
        for field, left_value in left_fields.items():
            right_value = right_fields[field]
            if isinstance(left_value, float) and isinstance(right_value, float):
                same = abs(left_value - right_value) <= numeric_tolerance
            else:
                same = left_value == right_value
            if not same:
                mismatches.append(TradeMismatch(index, field, left_value, right_value))
                if len(mismatches) >= max_mismatches:
                    break
        if len(mismatches) >= max_mismatches:
            break
    metadata_mismatches: list[dict[str, Any]] = []
    for field in ("initial_deposit", "currency", "strategy_name", "server"):
        left_value = getattr(left, field)
        right_value = getattr(right, field)
        if left_value and right_value and left_value != right_value:
            metadata_mismatches.append({"field": field, "left": left_value, "right": right_value})
    equivalent = (
        len(left_trades) == len(right_trades)
        and not mismatches
        and not metadata_mismatches
    )
    return ReportComparison(
        equivalent=equivalent,
        left_trade_count=len(left_trades),
        right_trade_count=len(right_trades),
        mismatches=tuple(mismatches),
        metadata_mismatches=tuple(metadata_mismatches),
    )
