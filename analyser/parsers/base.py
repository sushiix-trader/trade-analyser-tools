"""Parser interface and shared position normalisation."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from ..diagnostics import Diagnostic, add_diagnostic
from ..models import Report, Trade, TradeSide
from ..parsing_utils import parse_datetime, parse_int, parse_number


def _key(value: object) -> str:
    return "".join(ch for ch in str(value).lower() if ch.isalnum())


def normalise_trade(
    fields: dict[str, Any],
    diagnostics: list[Diagnostic] | None = None,
    *,
    emit_warnings: bool = False,
) -> Trade | None:
    """Build a canonical completed position from flexible field names."""

    diagnostics = diagnostics if diagnostics is not None else []
    lowered = {_key(k): v for k, v in fields.items() if k is not None}

    def pick(*keys: str) -> Any:
        for key in keys:
            value = lowered.get(_key(key))
            if value is not None and str(value).strip() != "":
                return value
        return None

    side_raw = str(pick("side", "position_side", "direction", "type") or "").lower()
    if side_raw in {"buy", "long", "inbuy", "1"}:
        side = TradeSide.LONG
    elif side_raw in {"sell", "short", "insell", "-1"}:
        side = TradeSide.SHORT
    else:
        side = TradeSide.FLAT

    close_time = parse_datetime(pick("close_time", "closetime", "close", "exit_time"))
    open_time = parse_datetime(pick("open_time", "opentime", "open", "entry_time"))
    open_time_inferred = open_time is None and close_time is not None
    ticket = pick("ticket", "position_id", "positionid", "id", "order")
    symbol = pick("symbol", "instrument", "instr")
    net_profit_raw = pick("net_profit", "netprofit", "result")
    profit_raw = net_profit_raw if net_profit_raw is not None else pick("profit", "pnl")
    profit = parse_number(profit_raw)
    swap = parse_number(pick("swap")) or 0.0
    commission = parse_number(pick("commission", "fee", "commission_fee")) or 0.0

    missing: list[str] = []
    if close_time is None:
        missing.append("close_time")
    if ticket is None:
        missing.append("ticket/position_id")
    if symbol is None:
        missing.append("symbol")
    if profit is None:
        missing.append("profit")
    if missing:
        add_diagnostic(
            diagnostics,
            "missing_required_trade_fields",
            "Skipped a position because required fields were missing",
            context={"fields": missing, "raw": {str(k): str(v) for k, v in fields.items()}},
            emit=emit_warnings,
        )
        return None

    if side is TradeSide.FLAT:
        add_diagnostic(
            diagnostics,
            "unknown_trade_side",
            "Position direction was not recognized; stored as flat",
            context={"ticket": str(ticket), "value": side_raw},
            emit=emit_warnings,
        )

    if open_time is None:
        open_time = close_time
        add_diagnostic(
            diagnostics,
            "missing_open_time",
            "Open time was missing; close time was used for the position",
            context={"ticket": str(ticket)},
            emit=emit_warnings,
        )

    deal_ids_raw = pick("deal_ids", "dealids", "deal_id", "deal")
    deal_ids = tuple(
        part.strip() for part in str(deal_ids_raw).replace(";", ",").split(",") if part.strip()
    ) if deal_ids_raw is not None else ()

    return Trade(
        ticket=str(ticket),
        position_id=str(pick("position_id", "positionid") or ticket),
        deal_ids=deal_ids,
        symbol=str(symbol),
        side=side,
        volume=parse_number(pick("volume", "lots", "size")) or 0.0,
        open_time=open_time,
        close_time=close_time,
        open_price=parse_number(pick("open_price", "openprice", "price_open")),
        close_price=parse_number(pick("close_price", "closeprice", "price_close")),
        # Deal/position rows normally expose gross profit plus costs.  If the
        # source explicitly supplied net_profit, preserve it as already net.
        profit=float(profit) if net_profit_raw is not None else float(profit) + swap + commission,
        swap=swap,
        commission=commission,
        sl=parse_number(pick("sl", "stop_loss", "stoploss")),
        tp=parse_number(pick("tp", "take_profit", "takeprofit")),
        comment=str(pick("comment") or "") or None,
        magic=parse_int(pick("magic", "magic_number", "magicnumber")),
        bars=parse_int(pick("bars", "bars_in_trade", "bar_count")),
        r_multiple=parse_number(pick("r", "r_multiple", "r_multiple_result", "risk_multiple")),
        open_time_inferred=open_time_inferred,
    )


class ReportParser(ABC):
    """Base class for a single-run report parser."""

    @abstractmethod
    def parse(self, source: str) -> Report:
        raise NotImplementedError

    def parse_file(self, path: str | Path) -> Report:
        from ..parsing_utils import decode_report_bytes

        report = self.parse(decode_report_bytes(Path(path).read_bytes()))
        report.source_file = str(path)
        return report
