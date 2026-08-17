"""Parser for MT5 Strategy Tester HTML reports.

MT5 exports the HTML as UTF-16 and places several logical sections in one
large table.  In particular, the useful completed-position data is usually in
the ``Deals`` section, where entry/exit deals can be paired deterministically.
"""

from __future__ import annotations

import re
from collections import defaultdict, deque
from typing import Any

from lxml import html as lxml_html

from ..diagnostics import Diagnostic, add_diagnostic
from ..models import AccountPoint, Report
from ..parsing_utils import parse_datetime, parse_number
from .base import ReportParser, normalise_trade


def _text(cell: Any) -> str:
    return re.sub(r"\s+", " ", " ".join(cell.itertext()).strip()).strip()


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _row_cells(row: Any) -> list[str]:
    return [_text(cell) for cell in row.xpath("./td|./th")]


def _is_deals_header(cells: list[str]) -> bool:
    normalized = {_norm(cell) for cell in cells}
    return {"time", "deal", "direction", "profit"}.issubset(normalized)


def _is_position_header(cells: list[str]) -> bool:
    normalized = {_norm(cell) for cell in cells}
    return (
        {"opentime", "closetime", "profit"}.issubset(normalized)
        or {"time", "symbol", "profit"}.issubset(normalized)
    )


def _is_orders_header(cells: list[str]) -> bool:
    normalized = {_norm(cell) for cell in cells}
    return {"opentime", "order", "symbol", "type", "sl", "tp"}.issubset(normalized)


def _parse_summary_value(label: str, value: str) -> float | str | None:
    parsed = parse_number(value)
    return parsed if parsed is not None else (value.strip() or None)


class MT5HtmlParser(ReportParser):
    def parse(self, source: str) -> Report:
        document = lxml_html.fromstring(source)
        report = Report(source_format="mt5-html")
        diagnostics: list[Diagnostic] = []
        tables = document.xpath("//table")
        if not tables:
            add_diagnostic(diagnostics, "no_tables", "No HTML tables were found")
            report.warnings = [d.to_dict() for d in diagnostics]
            return report

        self._extract_metadata(tables[0], report)
        order_stops: dict[tuple[str, str], dict[str, float | None]] = {}
        order_section = self._find_section(tables, _is_orders_header)
        if order_section is not None:
            table, header_index, header = order_section
            order_rows = self._read_rows(table, header_index, header)
            order_stops = self._parse_order_levels(order_rows)

        deal_section = self._find_section(tables, _is_deals_header)
        if deal_section is not None:
            table, header_index, header = deal_section
            rows = self._read_rows(table, header_index, header)
            self._parse_deals(rows, report, diagnostics, order_stops=order_stops)
        else:
            position_section = self._find_section(tables, _is_position_header)
            if position_section is not None:
                table, header_index, header = position_section
                rows = self._read_rows(table, header_index, header)
                self._parse_positions(rows, report, diagnostics)
            else:
                add_diagnostic(
                    diagnostics,
                    "trade_section_not_found",
                    "Could not find an MT5 Deals or completed-position table",
                )

        report.warnings = [d.to_dict() for d in diagnostics]
        return report

    @staticmethod
    def _find_section(
        tables: list[Any], predicate,
    ) -> tuple[Any, int, dict[str, int]] | None:
        for table in tables:
            rows = table.xpath(".//tr")
            for index, row in enumerate(rows):
                cells = _row_cells(row)
                if not predicate(cells):
                    continue
                mapping = {_norm(value): col for col, value in enumerate(cells)}
                return table, index, mapping
        return None

    @staticmethod
    def _read_rows(
        table: Any,
        header_index: int,
        header: dict[str, int],
    ) -> list[dict[str, str]]:
        rows = table.xpath(".//tr")
        result: list[dict[str, str]] = []
        for row in rows[header_index + 1 :]:
            cells = _row_cells(row)
            if not cells or not any(cells):
                if result:
                    break
                continue
            time_index = header.get("time")
            if time_index is not None and (time_index >= len(cells) or parse_datetime(cells[time_index]) is None):
                break
            # Another one-cell section heading or another header ends this
            # logical section.
            if len(cells) <= 2 and not parse_datetime(cells[0]):
                break
            if _is_deals_header(cells) or _is_position_header(cells):
                break
            fields: dict[str, str] = {}
            for name, index in header.items():
                if index < len(cells) and cells[index] != "":
                    fields[name] = cells[index]
            if fields:
                result.append(fields)
        return result

    @staticmethod
    def _extract_metadata(table: Any, report: Report) -> None:
        rows = table.xpath(".//tr")
        # Report title/header text.
        all_text = " ".join(cell for row in rows[:3] for cell in _row_cells(row))
        if all_text:
            report.metadata["title"] = all_text
            for cell in (cell for row in rows[:3] for cell in _row_cells(row)):
                build_match = re.search(r"^(.+?)\s*\(Build\s+[^)]+\)", cell, re.I)
                if build_match:
                    report.server = build_match.group(1).strip()
                    break
        for row in rows:
            cells = _row_cells(row)
            if len(cells) < 2:
                continue
            for i in range(0, len(cells) - 1, 2):
                label = cells[i].strip().rstrip(":")
                value = cells[i + 1].strip()
                key = _norm(label)
                if key in {"expert", "strategy", "ea"}:
                    report.strategy_name = report.strategy_name or value
                elif key == "symbol":
                    report.metadata.setdefault("symbol", value)
                elif key == "period":
                    report.metadata["period"] = value
                    match = re.search(
                        r"(\d{4}[./]\d{2}[./]\d{2})\s*-\s*(\d{4}[./]\d{2}[./]\d{2})",
                        value,
                    )
                    if match:
                        report.metadata["test_start"] = match.group(1)
                        report.metadata["test_end"] = match.group(2)
                elif key in {"initialdeposit", "initialbalance"}:
                    parsed = parse_number(value)
                    if parsed is not None:
                        report.initial_deposit = parsed
                elif key in {"currency", "leverage", "server"}:
                    setattr(report, key, value)
                elif key in {"broker", "company"}:
                    report.broker = report.broker or value
                elif key in {
                    "totalnetprofit", "grossprofit", "grossloss", "profitfactor",
                    "recoveryfactor", "sharperatio", "sortinoratio", "expectedpayoff",
                    "totaltrades", "totaldeals", "balance", "equity",
                } or "drawdown" in key:
                    report.reported_metrics[key] = _parse_summary_value(label, value)
                    if "drawdown" in key:
                        # Preserve the displayed percentage and amount when
                        # both are present, e.g. ``12 960.04 (8.21%)``.
                        numbers = re.findall(r"[-+]?\d[\d\s,]*(?:\.\d+)?", value)
                        parsed_numbers = [parse_number(n) for n in numbers]
                        report.reported_metrics[key + "_values"] = [
                            n for n in parsed_numbers if n is not None
                        ]
        if not report.broker and report.server:
            report.broker = report.server

    @staticmethod
    def _parse_order_levels(
        rows: list[dict[str, str]],
    ) -> dict[tuple[str, str], dict[str, float | None]]:
        """Index opening-order risk levels for completed-position hydration.

        MT5 HTML reports put the explicit S/L and T/P on the opening order,
        while the canonical completed position is reconstructed from the
        Deals section.  Keep the order table as supporting evidence and join
        it by symbol/order id rather than treating orders as trades.
        """

        levels: dict[tuple[str, str], dict[str, float | None]] = {}
        for row in rows:
            symbol = row.get("symbol") or ""
            order_id = row.get("order") or ""
            if not symbol or not order_id:
                continue
            sl = parse_number(row.get("sl"))
            tp = parse_number(row.get("tp"))
            if sl is None and tp is None:
                continue
            levels[(symbol, order_id)] = {"sl": sl, "tp": tp}
        return levels

    @staticmethod
    def _parse_deals(
        rows: list[dict[str, str]],
        report: Report,
        diagnostics: list[Diagnostic],
        *,
        order_stops: dict[tuple[str, str], dict[str, float | None]] | None = None,
    ) -> None:
        order_stops = order_stops or {}
        active: dict[tuple[str, str], deque[dict[str, str]]] = defaultdict(deque)
        for row in rows:
            timestamp = parse_datetime(row.get("time"))
            deal_id = row.get("deal") or row.get("id")
            balance = parse_number(row.get("balance"))
            if timestamp is not None and balance is not None:
                report.source_balance_points.append(
                    AccountPoint(timestamp, balance=balance, source_id=deal_id)
                )
            action = (row.get("direction") or "").lower()
            kind = (row.get("type") or "").lower()
            symbol = row.get("symbol") or ""
            if kind in {"balance", "credit", "charge"}:
                continue
            if action == "in":
                key = (symbol, kind)
                active[key].append(row)
                continue
            if action != "out":
                add_diagnostic(
                    diagnostics,
                    "unknown_deal_direction",
                    "Skipped a deal with an unknown direction",
                    context={"deal_id": deal_id, "direction": action},
                )
                continue

            entry_side = "buy" if kind == "sell" else "sell" if kind == "buy" else ""
            key = (symbol, entry_side)
            if not active[key]:
                add_diagnostic(
                    diagnostics,
                    "unmatched_exit_deal",
                    "Skipped an exit deal without a matching entry",
                    context={"deal_id": deal_id, "symbol": symbol},
                )
                continue
            entry = active[key].popleft()
            entry_order = entry.get("order") or entry.get("deal")
            order_levels = order_stops.get((symbol, entry_order), {})
            fields = {
                "ticket": entry_order or deal_id,
                "position_id": entry.get("position_id") or entry.get("position") or entry.get("order") or entry.get("deal"),
                "deal_ids": ",".join(
                    value for value in (entry.get("deal"), deal_id) if value
                ),
                "symbol": symbol,
                "side": entry_side,
                "volume": entry.get("volume") or row.get("volume"),
                "open_time": entry.get("time"),
                "close_time": row.get("time"),
                "open_price": entry.get("price"),
                "close_price": row.get("price"),
                "sl": order_levels.get("sl"),
                "tp": order_levels.get("tp"),
                "profit": row.get("profit"),
                "swap": (parse_number(entry.get("swap")) or 0.0)
                + (parse_number(row.get("swap")) or 0.0),
                "commission": (parse_number(entry.get("commission")) or 0.0)
                + (parse_number(row.get("commission")) or 0.0),
                "comment": entry.get("comment") or row.get("comment"),
            }
            trade = normalise_trade(fields, diagnostics)
            if trade is not None:
                report.trades.append(trade)

        for (symbol, side), pending in active.items():
            for entry in pending:
                add_diagnostic(
                    diagnostics,
                    "unclosed_position",
                    "Ignored an entry deal without a matching completed exit",
                    context={"symbol": symbol, "side": side, "deal_id": entry.get("deal")},
                )

    @staticmethod
    def _parse_positions(
        rows: list[dict[str, str]],
        report: Report,
        diagnostics: list[Diagnostic],
    ) -> None:
        for row in rows:
            trade = normalise_trade(row, diagnostics)
            if trade is not None:
                report.trades.append(trade)
