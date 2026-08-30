from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from analyser import MonteCarloConfig
from gui.workflow import GuiRunConfig, run_analysis


GUI_HTML = """<!doctype html><html><body>
<table>
<tr><td>Strategy Tester Report</td></tr>
<tr><td>Expert:</td><td>gui-fixture</td></tr>
<tr><td>Initial Deposit:</td><td>1 000.00</td></tr>
<tr><td>Total Net Profit:</td><td>71.00</td></tr>
<tr><td>Total Trades:</td><td>2</td></tr>
</table>
<table>
<tr><td>Deals</td></tr>
<tr><td>Time</td><td>Deal</td><td>Symbol</td><td>Type</td><td>Direction</td><td>Volume</td><td>Price</td><td>Order</td><td>Commission</td><td>Swap</td><td>Profit</td><td>Balance</td><td>Comment</td></tr>
<tr><td>2024.01.02 10:00:00</td><td>1</td><td>GUI_SYMBOL</td><td>buy</td><td>in</td><td>1</td><td>1.1000</td><td>1</td><td>-2</td><td>0</td><td>0</td><td>998</td><td>entry</td></tr>
<tr><td>2024.01.03 10:00:00</td><td>2</td><td>GUI_SYMBOL</td><td>sell</td><td>out</td><td>1</td><td>1.1100</td><td>2</td><td>0</td><td>0</td><td>50</td><td>1048</td><td>exit</td></tr>
<tr><td>2024.02.02 10:00:00</td><td>3</td><td>GUI_SYMBOL</td><td>sell</td><td>in</td><td>1</td><td>1.1100</td><td>3</td><td>-2</td><td>0</td><td>0</td><td>1046</td><td>entry</td></tr>
<tr><td>2024.02.03 10:00:00</td><td>4</td><td>GUI_SYMBOL</td><td>buy</td><td>out</td><td>1</td><td>1.1000</td><td>4</td><td>0</td><td>0</td><td>25</td><td>1071</td><td>exit</td></tr>
</table></body></html>"""


class GuiWorkflowTests(unittest.TestCase):
    def test_single_report_workflow_writes_report_serializers_and_monte_carlo_outputs(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "fixture.html"
            source.write_text(GUI_HTML, encoding="utf-8")

            run = run_analysis(
                GuiRunConfig(
                    source=source,
                    output_dir=root / "outputs",
                    monte_carlo=MonteCarloConfig(iterations=6, seed=123),
                    generate_monte_carlo_chart=True,
                )
            )

            self.assertEqual(run.report_path.name, "fixture-interactive-report.html")
            self.assertTrue(run.report_path.exists())
            report_html = run.report_path.read_text(encoding="utf-8")
            self.assertIn("<!doctype html>", report_html.lower())
            self.assertIn('href="#monte-carlo"', report_html)
            self.assertIn("Monte Carlo robustness", report_html)
            self.assertIn("P95 max drawdown", report_html)
            self.assertTrue(run.analysis_json_path.exists())
            self.assertTrue(run.analysis_markdown_path.exists())
            self.assertEqual(json.loads(run.analysis_json_path.read_text()), run.analysis_result.to_dict())
            self.assertIsNotNone(run.monte_carlo_result)
            self.assertEqual(run.monte_carlo_result.iterations, 6)
            self.assertTrue(run.monte_carlo_summary_path.exists())
            self.assertTrue(run.monte_carlo_json_path.exists())
            self.assertEqual(
                json.loads(run.monte_carlo_json_path.read_text()) ["config"]["seed"],
                123,
            )

            if importlib.util.find_spec("matplotlib"):
                self.assertIsNotNone(run.equity_chart_path)
                self.assertTrue(run.equity_chart_path.exists())
                self.assertIsNotNone(run.monte_carlo_chart_path)
                self.assertTrue(run.monte_carlo_chart_path.exists())

    def test_default_workflow_includes_complete_monte_carlo_report(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "fixture.xml"
            source.write_text(
                """<?xml version='1.0'?><report><initialDeposit>100</initialDeposit>
                <position><positionId>1</positionId><symbol>GUI_SYMBOL</symbol>
                <type>buy</type><volume>1</volume>
                <openTime>2024.01.01 10:00:00</openTime>
                <closeTime>2024.01.01 11:00:00</closeTime>
                <profit>25</profit></position></report>""",
                encoding="utf-8",
            )

            run = run_analysis(GuiRunConfig(source=source, output_dir=root / "outputs"))

            self.assertIsNotNone(run.monte_carlo_result)
            self.assertEqual(run.monte_carlo_result.iterations, 10_000)
            self.assertEqual(run.monte_carlo_result.config.method, "permutation")
            self.assertEqual(run.monte_carlo_result.config.seed, 42)
            self.assertTrue(run.monte_carlo_result.config.retain_paths)
            self.assertEqual(run.monte_carlo_result.config.path_count, 500)
            self.assertTrue(run.monte_carlo_summary_path.exists())
            self.assertTrue(run.monte_carlo_json_path.exists())
            self.assertTrue(run.report_path.exists())
            report_html = run.report_path.read_text(encoding="utf-8")
            self.assertIn("Monte Carlo robustness", report_html)
            self.assertIn("P95 max drawdown", report_html)
            self.assertIn("Drawdown depth", report_html)

    def test_monte_carlo_can_be_explicitly_disabled(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "fixture.xml"
            source.write_text(
                """<?xml version='1.0'?><report><initialDeposit>100</initialDeposit>
                <position><positionId>1</positionId><symbol>GUI_SYMBOL</symbol>
                <type>buy</type><volume>1</volume>
                <openTime>2024.01.01 10:00:00</openTime>
                <closeTime>2024.01.01 11:00:00</closeTime>
                <profit>25</profit></position></report>""",
                encoding="utf-8",
            )

            run = run_analysis(
                GuiRunConfig(
                    source=source,
                    output_dir=root / "outputs",
                    monte_carlo=None,
                )
            )

            self.assertIsNone(run.monte_carlo_result)
            self.assertIsNone(run.monte_carlo_summary_path)
            self.assertIsNone(run.monte_carlo_json_path)
            self.assertIsNone(run.monte_carlo_chart_path)
            self.assertTrue(run.report_path.exists())
            self.assertIn("Monte Carlo was not run for this report", run.report_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
