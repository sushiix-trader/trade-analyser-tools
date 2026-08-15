"""Canonical models used by the report parsers and analysis engine.

The parser layer normalises MT5 HTML and XML into the same objects.  The
analysis layer deliberately works on completed positions rather than raw
orders, while retaining position/deal identifiers for auditability.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class TradeSide(str, Enum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


@dataclass(frozen=True)
class AccountPoint:
    """An account observation exported by MT5.

    Most Strategy Tester HTML reports expose a balance series, not a complete
    floating-equity series.  ``equity`` is therefore optional and is never
    inferred to be present merely because ``balance`` is present.
    """

    timestamp: datetime
    balance: float | None = None
    equity: float | None = None
    source_id: str | None = None


@dataclass(frozen=True)
class Trade:
    """One completed, closed MT5 position.

    ``profit`` is the canonical net result in account currency and includes
    the separately retained swap and commission components.
    """

    ticket: str
    symbol: str
    side: TradeSide
    volume: float
    open_time: datetime | None
    close_time: datetime | None
    open_price: float | None
    close_price: float | None
    profit: float
    swap: float = 0.0
    commission: float = 0.0
    sl: float | None = None
    tp: float | None = None
    comment: str | None = None
    magic: int | None = None
    position_id: str | None = None
    deal_ids: tuple[str, ...] = ()
    strategy_id: str | None = None
    source_report_hash: str | None = None
    allocation_scale: float | None = None
    bars: int | None = None
    r_multiple: float | None = None
    open_time_inferred: bool = False

    @property
    def gross_profit(self) -> float:
        """Profit before swap and commission."""

        return self.profit - self.swap - self.commission

    @property
    def net_profit(self) -> float:
        return self.profit

    @property
    def duration_seconds(self) -> float | None:
        if self.open_time is None or self.close_time is None:
            return None
        return (self.close_time - self.open_time).total_seconds()

    @property
    def is_win(self) -> bool:
        return self.profit > 0.0

    @property
    def is_loss(self) -> bool:
        return self.profit < 0.0

    @property
    def sort_key(self) -> tuple[Any, ...]:
        return (
            self.close_time or datetime.min,
            self.open_time or datetime.min,
            self.strategy_id or "",
            self.position_id or self.ticket,
            self.ticket,
        )


@dataclass
class Report:
    """A parsed single-run MT5 Strategy Tester report."""

    trades: list[Trade] = field(default_factory=list)
    initial_deposit: float = 0.0
    currency: str = ""
    broker: str = ""
    leverage: str = ""
    source_file: str = ""
    source_format: str = ""  # mt5-xml | mt5-html
    strategy_name: str = ""
    server: str = ""
    timezone: str | None = None
    source_balance_points: list[AccountPoint] = field(default_factory=list)
    source_equity_points: list[AccountPoint] = field(default_factory=list)
    reported_metrics: dict[str, float | str | None] = field(default_factory=dict)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def n_trades(self) -> int:
        return len(self.trades)

    def ordered_trades(self) -> list[Trade]:
        return sorted(self.trades, key=lambda trade: trade.sort_key)

    def profits(self) -> list[float]:
        return [trade.profit for trade in self.ordered_trades()]

    def symbols(self) -> list[str]:
        return sorted({t.symbol for t in self.trades if t.symbol})
