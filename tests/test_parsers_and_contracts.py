from __future__ import annotations

import os
import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from analyser import (
    AnalysisConfig,
    ReportParseError,
    UnsupportedReportError,
    UnhydratedInputError,
    analyze_file,
    compare_reports,
    load_report,
)


VALID_XML = b'''<?xml version="1.0"?>
<report>
  <initialDeposit>1000</initialDeposit>
  <currency>USD</currency>
  <position>
    <positionId>42</positionId><dealIds>10,11</dealIds>
    <symbol>TEST_SYMBOL</symbol><type>buy</type><volume>1</volume>
    <openTime>2024.01.01 10:00:00</openTime>
    <closeTime>2024.01.02 10:00:00</closeTime>
    <openPrice>1.1000</openPrice><closePrice>1.1100</closePrice>
    <profit>100</profit><swap>-1</swap><commission>-2</commission>
    <bars>10</bars><r>1.5</r>
  </position>
</report>'''


INVALID_XML = b"<report><position>"

DEALS_ONLY_XML = b'''<?xml version="1.0"?>
<report><initialDeposit>1000</initialDeposit>
<deal><deal>1</deal><symbol>TEST_SYMBOL</symbol><type>buy</type><direction>in</direction><volume>1</volume><time>2024.01.01 10:00:00</time><price>1.1</price><commission>-2</commission></deal>
<deal><deal>2</deal><symbol>TEST_SYMBOL</symbol><type>sell</type><direction>out</direction><volume>1</volume><time>2024.01.02 10:00:00</time><price>1.11</price><profit>100</profit></deal></report>'''

ORDERS_AND_DEALS_HTML = b"""
<html><body><table>
<tr><td>Initial Deposit:</td><td>1000 USD</td><td>Currency:</td><td>USD</td></tr>
<tr><th>Orders</th></tr>
<tr><th>Open Time</th><th>Order</th><th>Symbol</th><th>Type</th><th>Volume</th><th>Price</th><th>S / L</th><th>T / P</th><th>Time</th><th>State</th><th>Comment</th></tr>
<tr><td>2024.01.01 10:00:00</td><td>2</td><td>TEST_SYMBOL</td><td>buy</td><td>1</td><td>1.1000</td><td>1.0900</td><td>1.1200</td><td>2024.01.01 10:00:00</td><td>filled</td><td>entry</td></tr>
<tr><td>2024.01.02 10:00:00</td><td>3</td><td>TEST_SYMBOL</td><td>sell</td><td>1</td><td>1.1100</td><td></td><td></td><td>2024.01.02 10:00:00</td><td>filled</td><td>tp</td></tr>
<tr><th>Deals</th></tr>
<tr><th>Time</th><th>Deal</th><th>Symbol</th><th>Type</th><th>Direction</th><th>Volume</th><th>Price</th><th>Order</th><th>Commission</th><th>Swap</th><th>Profit</th><th>Balance</th></tr>
<tr><td>2024.01.01 10:00:00</td><td>2</td><td>TEST_SYMBOL</td><td>buy</td><td>in</td><td>1</td><td>1.1000</td><td>2</td><td>0</td><td>0</td><td>0</td><td>1000</td></tr>
<tr><td>2024.01.02 10:00:00</td><td>3</td><td>TEST_SYMBOL</td><td>sell</td><td>out</td><td>1</td><td>1.1100</td><td>3</td><td>-2</td><td>-1</td><td>100</td><td>1097</td></tr>
</table></body></html>
"""


class ParserContractTests(unittest.TestCase):
    def test_valid_single_run_xml(self) -> None:
        report = load_report(VALID_XML)
        self.assertEqual(report.source_format, "mt5-xml")
        self.assertEqual(report.initial_deposit, 1000.0)
        self.assertEqual(len(report.trades), 1)
        self.assertEqual(report.trades[0].deal_ids, ("10", "11"))
        self.assertEqual(report.trades[0].profit, 97.0)
        self.assertEqual(report.trades[0].bars, 10)
        self.assertEqual(report.trades[0].r_multiple, 1.5)

    def test_deals_only_xml_are_paired_into_a_closed_position(self) -> None:
        report = load_report(DEALS_ONLY_XML)
        self.assertEqual(len(report.trades), 1)
        self.assertEqual(report.trades[0].deal_ids, ("1", "2"))
        self.assertEqual(report.trades[0].profit, 98.0)

    def test_html_deals_hydrate_explicit_stop_from_opening_order(self) -> None:
        report = load_report(ORDERS_AND_DEALS_HTML)
        self.assertEqual(len(report.trades), 1)
        self.assertEqual(report.trades[0].ticket, "2")
        self.assertEqual(report.trades[0].sl, 1.09)
        self.assertEqual(report.trades[0].tp, 1.12)
        self.assertEqual(report.trades[0].profit, 97.0)

    def test_malformed_xml_has_public_parse_error(self) -> None:
        with self.assertRaises(ReportParseError):
            load_report(INVALID_XML)

    def test_bytes_file_like_and_path_are_equivalent(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "report.xml"
            path.write_bytes(VALID_XML)
            path_report = load_report(path)
        bytes_report = load_report(VALID_XML)
        stream_report = load_report(io.BytesIO(VALID_XML))
        self.assertTrue(compare_reports(path_report, bytes_report).equivalent)
        self.assertTrue(compare_reports(bytes_report, stream_report).equivalent)

    def test_report_comparison_identifies_a_field(self) -> None:
        left = load_report(VALID_XML)
        right = load_report(VALID_XML.replace(b"<profit>100</profit>", b"<profit>101</profit>"))
        comparison = compare_reports(left, right)
        self.assertFalse(comparison.equivalent)
        self.assertEqual(comparison.mismatches[0].field, "profit")

    def test_unhydrated_pointer_is_never_parsed(self) -> None:
        pointer = b"version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 10\n"
        with self.assertRaises(UnhydratedInputError):
            load_report(pointer)

    def test_optimization_xml_is_rejected(self) -> None:
        workbook = b"<?xml version='1.0'?><optimization><pass>1</pass><result>12</result></optimization>"
        with self.assertRaises(UnsupportedReportError):
            load_report(workbook)

    def test_serializers_are_available(self) -> None:
        report = load_report(VALID_XML)
        result = analyze_file(VALID_XML, AnalysisConfig())
        self.assertIn('"metrics"', result.to_json())
        self.assertIn("period", result.to_csv("monthly"))
        self.assertIn("# Trade Analysis", result.to_markdown())
        self.assertEqual(report.n_trades, 1)

    def test_private_html_smoke_report_when_configured(self) -> None:
        configured_root = os.environ.get("MT5_FIXTURE_ROOT")
        if not configured_root:
            self.skipTest("set MT5_FIXTURE_ROOT to run the private HTML smoke test")
        root = Path(configured_root)
        available = sorted(root.rglob("*.htm")) if root.exists() else []
        if not available:
            self.skipTest("configured private MT5 fixture directory has no HTML reports")
        result = analyze_file(available[0])
        self.assertGreaterEqual(result.metrics.total_trades, 0)
        self.assertEqual(result.validation.status, "match")


class AnalysisStoreTests(unittest.TestCase):
    def test_analysis_store_round_trips_without_recalculation(self) -> None:
        from analyser import AnalysisStore

        with TemporaryDirectory() as directory:
            store = AnalysisStore(directory)
            first = store.analyze_or_load(VALID_XML)
            self.assertFalse(first.cache_hit)
            self.assertTrue(first.path.exists())
            second = store.analyze_or_load(VALID_XML)
            self.assertTrue(second.cache_hit)
            self.assertEqual(first.key, second.key)
            self.assertEqual(first.result.to_json(), second.result.to_json())


if __name__ == "__main__":
    unittest.main()
