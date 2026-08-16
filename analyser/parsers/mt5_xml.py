"""Parser for supported single-run MetaTrader 5 XML reports."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import defaultdict, deque

from ..diagnostics import Diagnostic, add_diagnostic
from ..errors import UnsupportedReportError
from ..models import Report
from ..parsing_utils import parse_number
from .base import ReportParser, normalise_trade

_TRADE_TAGS = {"position", "deal", "trade", "transaction"}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _fields(element: ET.Element) -> dict[str, str]:
    result = {
        _local(key): value for key, value in element.attrib.items() if value is not None
    }
    for child in element:
        value = (child.text or "").strip()
        if value:
            result[_local(child.tag)] = value
    return result


def _looks_like_workbook(root: ET.Element, source: str) -> bool:
    tags = {_local(element.tag) for element in root.iter()}
    text = source.lower()
    # A single-run report must expose position/deal timing or explicit
    # position rows.  Optimization workbooks generally have pass/result rows.
    has_trades = bool(tags & {"position", "deal", "trade", "transaction"})
    has_workbook_shape = (
        "reportoptimizer" in text
        or ("pass" in tags and "result" in tags and not has_trades)
        or ("optimization" in text and not has_trades)
    )
    return has_workbook_shape


class MT5XmlParser(ReportParser):
    def parse(self, source: str) -> Report:
        try:
            root = ET.fromstring(source)
        except ET.ParseError as exc:
            raise ValueError(f"Invalid MT5 XML: {exc}") from exc
        if _looks_like_workbook(root, source):
            raise UnsupportedReportError(
                "MT5 optimization workbooks are not accepted as single-run reports"
            )

        report = Report(source_format="mt5-xml")
        diagnostics: list[Diagnostic] = []
        rows: list[dict[str, str]] = []
        for element in root.iter():
            tag = _local(element.tag)
            row = _fields(element)
            if tag in _TRADE_TAGS or {"profit", "time"}.issubset(row):
                rows.append(row)
            key = _local(element.tag)
            value = (element.text or "").strip()
            if not value:
                continue
            normalized = re.sub(r"[^a-z0-9]+", "", key)
            if normalized in {"initialdeposit", "initialbalance", "deposit"}:
                parsed = parse_number(value)
                if parsed is not None and parsed > 0 and not report.initial_deposit:
                    report.initial_deposit = parsed
            elif normalized in {"currency", "broker", "company", "server", "leverage", "expert", "strategy", "name"}:
                target = {
                    "expert": "strategy_name", "strategy": "strategy_name", "name": "strategy_name",
                    "company": "broker",
                }.get(normalized, normalized)
                if not getattr(report, target, ""):
                    setattr(report, target, value)

        position_rows = [row for row in rows if _is_position_row(row)]
        if position_rows:
            for row in position_rows:
                trade = normalise_trade(row, diagnostics)
                if trade is not None:
                    report.trades.append(trade)
        else:
            _pair_deals(rows, report, diagnostics)
        report.warnings = [diagnostic.to_dict() for diagnostic in diagnostics]
        return report


def _is_position_row(row: dict[str, str]) -> bool:
    keys = {re.sub(r"[^a-z0-9]+", "", key.lower()) for key in row}
    return bool({"opentime", "closetime"}.issubset(keys)) and (
        "positionid" in keys or "ticket" in keys or "profit" in keys
    )


def _pair_deals(
    rows: list[dict[str, str]],
    report: Report,
    diagnostics: list[Diagnostic],
) -> None:
    active: dict[tuple[str, str], deque[dict[str, str]]] = defaultdict(deque)
    for row in rows:
        normalized = {re.sub(r"[^a-z0-9]+", "", k.lower()): v for k, v in row.items()}
        symbol = normalized.get("symbol", "")
        action = normalized.get("direction", "").lower()
        kind = normalized.get("type", "").lower()
        if kind in {"balance", "credit", "charge"}:
            continue
        if action in {"in", "entry", "open"}:
            active[(symbol, kind)].append(normalized)
            continue
        if action not in {"out", "exit", "close"}:
            continue
        entry_side = "buy" if kind == "sell" else "sell" if kind == "buy" else ""
        queue = active[(symbol, entry_side)]
        if not queue:
            add_diagnostic(
                diagnostics,
                "unmatched_exit_deal",
                "Skipped an XML exit deal without a matching entry",
                context={"symbol": symbol, "deal_id": normalized.get("deal")},
            )
            continue
        entry = queue.popleft()
        fields = {
            "ticket": entry.get("order") or entry.get("deal") or normalized.get("deal"),
            "position_id": entry.get("positionid") or entry.get("position") or entry.get("order"),
            "deal_ids": ",".join(x for x in (entry.get("deal"), normalized.get("deal")) if x),
            "symbol": symbol,
            "side": entry_side,
            "volume": entry.get("volume") or normalized.get("volume"),
            "open_time": entry.get("time") or entry.get("opentime"),
            "close_time": normalized.get("time") or normalized.get("closetime"),
            "open_price": entry.get("price") or entry.get("openprice"),
            "close_price": normalized.get("price") or normalized.get("closeprice"),
            "profit": normalized.get("profit"),
            "swap": (parse_number(entry.get("swap")) or 0.0)
            + (parse_number(normalized.get("swap")) or 0.0),
            "commission": (parse_number(entry.get("commission")) or 0.0)
            + (parse_number(normalized.get("commission")) or 0.0),
            "comment": entry.get("comment") or normalized.get("comment"),
        }
        trade = normalise_trade(fields, diagnostics)
        if trade is not None:
            report.trades.append(trade)

    for (symbol, side), queue in active.items():
        for entry in queue:
            add_diagnostic(
                diagnostics,
                "unclosed_position",
                "Ignored an XML entry deal without a matching completed exit",
                context={"symbol": symbol, "side": side, "deal_id": entry.get("deal")},
            )
