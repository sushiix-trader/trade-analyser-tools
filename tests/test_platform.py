from __future__ import annotations

import os
import io
import unittest
import importlib.util
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from analyser import (
    AnalysisConfig,
    Report,
    Trade,
    TradeSide,
    UnsupportedReportError,
    UnhydratedInputError,
    analyze,
    analyze_file,
    compare_reports,
    load_report,
    render_equity_drawdown_chart,
    save_equity_drawdown_chart,
)


MINIMAL_HTML = """<!doctype html><html><body>
<table>
<tr><td>Strategy Tester Report</td></tr>
<tr><td>Expert:</td><td>fixture</td></tr>
<tr><td>Initial Deposit:</td><td>1 000.00</td></tr>
<tr><td>Total Net Profit:</td><td>71.00</td></tr>
<tr><td>Total Trades:</td><td>2</td></tr>
</table>
<table>
<tr><td>Deals</td></tr>
<tr><td>Time</td><td>Deal</td><td>Symbol</td><td>Type</td><td>Direction</td><td>Volume</td><td>Price</td><td>Order</td><td>Commission</td><td>Swap</td><td>Profit</td><td>Balance</td><td>Comment</td></tr>
<tr><td>2024.01.02 10:00:00</td><td>1</td><td>TEST_SYMBOL</td><td>buy</td><td>in</td><td>1</td><td>1.1000</td><td>1</td><td>-2</td><td>0</td><td>0</td><td>998</td><td>entry</td></tr>
<tr><td>2024.01.03 10:00:00</td><td>2</td><td>TEST_SYMBOL</td><td>sell</td><td>out</td><td>1</td><td>1.1100</td><td>2</td><td>0</td><td>0</td><td>50</td><td>1048</td><td>exit</td></tr>
<tr><td>2024.02.02 10:00:00</td><td>3</td><td>TEST_SYMBOL</td><td>sell</td><td>in</td><td>1</td><td>1.1100</td><td>3</td><td>-2</td><td>0</td><td>0</td><td>1046</td><td>entry</td></tr>
<tr><td>2024.02.03 10:00:00</td><td>4</td><td>TEST_SYMBOL</td><td>buy</td><td>out</td><td>1</td><td>1.1000</td><td>4</td><td>0</td><td>0</td><td>25</td><td>1071</td><td>exit</td></tr>
</table></body></html>"""


class PlatformTests(unittest.TestCase):
    def test_utf16_html_and_eager_analysis(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.htm"
            path.write_bytes(MINIMAL_HTML.encode("utf-16"))
            result = analyze_file(path)
        self.assertEqual(len(result.report.trades), 2)
        self.assertAlmostEqual(result.metrics.net_profit, 71.0)
        self.assertEqual(result.metrics.total_trades, 2)
        self.assertEqual([row.period for row in result.monthly], ["2024-01", "2024-02"])
        self.assertAlmostEqual(result.monthly[0].pnl, 48.0)
        self.assertAlmostEqual(result.monthly[1].pnl, 23.0)
        self.assertEqual(result.validation.status, "match")
        self.assertEqual(result.to_json(), result.to_json())

    def test_bytes_and_file_like_inputs(self) -> None:
        data = MINIMAL_HTML.encode("utf-8")
        self.assertEqual(len(load_report(data).trades), 2)
        self.assertEqual(len(load_report(io.BytesIO(data)).trades), 2)

    def test_custom_sharpe_is_configurable(self) -> None:
        report = Report(
            initial_deposit=1000,
            trades=[
                Trade("1", "X", TradeSide.LONG, 1, datetime(2024, 1, 1), datetime(2024, 1, 2), 1, 1, 100),
                Trade("2", "X", TradeSide.LONG, 1, datetime(2024, 1, 3), datetime(2024, 1, 4), 1, 1, -50),
                Trade("3", "X", TradeSide.LONG, 1, datetime(2024, 1, 5), datetime(2024, 1, 6), 1, 1, 25),
            ],
        )
        result = analyze(report, AnalysisConfig())
        self.assertIsNotNone(result.metrics.custom_trade_event_sharpe)
        self.assertEqual(result.metrics.custom_trade_event_sharpe, result.metrics.sharpe_ratio)

    def test_lfs_pointers_are_rejected(self) -> None:
        with self.assertRaises(UnhydratedInputError):
            load_report(b"version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 10\n")

    def test_optimization_workbook_is_rejected(self) -> None:
        workbook = b"<?xml version='1.0'?><optimization><pass>1</pass><result>100</result></optimization>"
        with self.assertRaises(UnsupportedReportError):
            load_report(workbook)

    def test_report_comparison(self) -> None:
        left = load_report(MINIMAL_HTML.encode("utf-8"))
        right = load_report(MINIMAL_HTML.encode("utf-8"))
        comparison = compare_reports(left, right)
        self.assertTrue(comparison.equivalent)
        self.assertEqual(comparison.left_trade_count, 2)


    @unittest.skipUnless(importlib.util.find_spec("matplotlib"), "matplotlib is not installed")
    def test_equity_drawdown_chart_is_deterministic_and_contains_both_series(self) -> None:
        result = analyze_file(MINIMAL_HTML.encode("utf-8"))
        first = render_equity_drawdown_chart(result, title="Fixture equity")
        second = render_equity_drawdown_chart(result, title="Fixture equity")
        self.assertTrue(first.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(first, second)
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "equity-drawdown.png"
            saved = save_equity_drawdown_chart(result, destination, title="Fixture equity")
            self.assertEqual(saved, destination)
            self.assertEqual(destination.read_bytes(), first)

    def test_private_real_fixture_when_configured(self) -> None:
        configured = os.environ.get("MT5_FIXTURE_REPORT")
        if not configured:
            self.skipTest("set MT5_FIXTURE_REPORT to run the private real-report smoke test")
        path = Path(configured)
        if not path.exists():
            self.skipTest("configured private MT5 fixture is not available")
        result = analyze_file(path)
        self.assertGreaterEqual(result.metrics.total_trades, 0)
        self.assertIsNotNone(result.metrics.net_profit)
        self.assertIn("YTD", result.to_csv("monthly_performance"))
        self.assertEqual(result.validation.status, "match")


if __name__ == "__main__":
    unittest.main()
