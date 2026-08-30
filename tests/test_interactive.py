from __future__ import annotations

import io
import json
import re
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import urlopen

from analyser import (
    AnalysisConfig,
    AnalyzedPortfolioMember,
    InteractiveReportConfig,
    MonteCarloConfig,
    PortfolioConfig,
    PortfolioMember,
    Report,
    Trade,
    TradeSide,
    analyze,
    analyze_portfolio,
    combine_analyses,
    render_interactive_report,
    run_monte_carlo,
    save_interactive_report,
    serve_interactive_report,
)


class InteractiveReportTests(unittest.TestCase):
    def setUp(self) -> None:
        start = datetime(2024, 1, 1, 9, 0)
        trades = [
            self._trade("long-1", start, 100.0, TradeSide.LONG, "secret-comment"),
            self._trade("short-1", start + timedelta(days=1), -50.0, TradeSide.SHORT),
            self._trade("long-2", start + timedelta(days=2), 25.0, TradeSide.LONG),
            self._trade("short-2", start + timedelta(days=3), -10.0, TradeSide.SHORT),
        ]
        self.result = analyze(
            Report(
                trades=trades,
                initial_deposit=1_000.0,
                currency="USD",
                source_file="/private/secret-strategy.html",
                source_format="mt5-html",
                strategy_name="Fixture strategy",
                timezone="UTC",
            ),
            AnalysisConfig(),
        )

    @staticmethod
    def _trade(ticket: str, opened: datetime, profit: float, side: TradeSide, comment: str | None = None) -> Trade:
        return Trade(
            ticket=ticket,
            symbol="TEST",
            side=side,
            volume=1.0,
            open_time=opened,
            close_time=opened + timedelta(hours=2),
            open_price=1.0,
            close_price=1.01,
            profit=profit,
            swap=-1.0,
            commission=-2.0,
            comment=comment,
            magic=123456,
            position_id=f"position-{ticket}",
            deal_ids=(f"deal-{ticket}",),
        )

    @staticmethod
    def _payload(page: str) -> dict:
        match = re.search(
            r'<script id="report-data" type="application/json">(.*?)</script>',
            page,
            re.DOTALL,
        )
        assert match is not None
        return json.loads(match.group(1))

    def test_single_report_is_deterministic_and_contains_direction_variants(self) -> None:
        first = render_interactive_report(self.result)
        second = render_interactive_report(self.result)

        self.assertEqual(first, second)
        self.assertIn("Equity & drawdown", first)
        self.assertIn("Trade analysis", first)
        self.assertIn("Daily profit correlation", first)
        self.assertIn("class=\"brand-logo\"", first)
        self.assertIn("data:image/png;base64,", first)
        self.assertIn('id="editTitleButton"', first)
        self.assertIn('id="reportTitleInput"', first)
        self.assertIn("title: state.title", first)
        self.assertIn('id="valueMode" type="button"', first)
        self.assertNotIn('id="drawdownMode"', first)
        self.assertIn("Values in %", first)
        self.assertIn("class='info-icon'", first)
        self.assertIn("Annualized Sharpe", first)
        self.assertNotIn('>Custom Sharpe<', first)
        self.assertNotIn('>Daily Sharpe<', first)
        self.assertNotIn('>SQN<', first)
        self.assertIn("class='trade-chart'", first)
        self.assertIn("Timing ·", first)
        self.assertIn('"Initial balance"', first)
        self.assertIn("Initial strategy allocation", first)
        self.assertIn("drawdown-axis-label", first)
        self.assertIn(">Equity</text>", first)
        self.assertIn(">Drawdown</text>", first)
        self.assertIn("text-anchor='end'", first)
        self.assertIn('class="monthly-table-wrap" id="monthlyTable"', first)
        self.assertIn('class="monthly-table-wrap" id="monthlyDrawdownTable"', first)
        self.assertIn("monthly-drawdown-table", first)
        self.assertIn("drawdownHeatStyle", first)
        self.assertIn('<div class="section-heading"><h2>Monthly drawdown</h2>', first)
        self.assertIn("@media (max-width: 640px)", first)
        self.assertIn("min-width: 760px", first)
        self.assertIn("Swipe horizontally to view all columns", first)
        self.assertNotIn('id="curveSelect"', first)
        self.assertIn('member-equity-line', first)
        self.assertIn('member-drawdown-line', first)
        self.assertIn("stroke='${item.color}'", first)
        self.assertIn('state.valueMode = "percent"', first)
        self.assertIn('const memberOnlyMoney = showMemberCurves && state.valueMode === "money"', first)
        self.assertIn("if (memberOnlyMoney) {", first)
        self.assertIn("Individual strategy equity curves are unavailable", first)
        self.assertIn('const hoverCurve = hoverItem.curve', first)
        self.assertIn('showMembers: report.kind === "portfolio"', first)
        self.assertIn('params.has("members") ? params.get("members") === "1" : report.kind === "portfolio"', first)
        self.assertNotIn('curveSelect.addEventListener("change"', first)
        self.assertNotIn("/private/secret-strategy.html", first)
        self.assertNotIn("secret-comment", first)
        self.assertNotIn("deal-long-1", first)
        self.assertNotIn("position-long-1", first)

        payload = self._payload(first)
        self.assertEqual(payload["kind"], "single")
        self.assertEqual(set(payload["variants"]), {"all", "long", "short"})
        self.assertEqual(payload["variants"]["all"]["metrics"]["total_trades"], 4)
        self.assertEqual(payload["variants"]["long"]["metrics"]["total_trades"], 2)
        self.assertEqual(payload["variants"]["short"]["metrics"]["total_trades"], 2)
        self.assertNotIn("custom_trade_event_sharpe", payload["variants"]["all"]["metrics"])
        self.assertNotIn("daily_sharpe_ratio", payload["variants"]["all"]["metrics"])
        self.assertNotIn("sqn", payload["variants"]["all"]["metrics"])
        drawdown_table = payload["variants"]["all"]["monthly_drawdown_table"]
        self.assertIn("annual_worst_drawdown_pct", drawdown_table["rows"][0])
        self.assertEqual(drawdown_table["worst_label"], "Worst")
        monthly_drawdowns = [value for value in drawdown_table["rows"][0]["monthly_drawdown_pct"] if value is not None]
        self.assertTrue(monthly_drawdowns)
        self.assertLess(min(monthly_drawdowns), 0)
        self.assertEqual(drawdown_table["rows"][0]["annual_worst_drawdown_pct"], min(monthly_drawdowns))
        self.assertNotIn("ticket", payload["variants"]["all"]["trades"][0])
        self.assertIn("monthly_performance", payload["variants"]["all"])
        self.assertIn("trade_profit", payload["variants"]["all"])

    def test_report_title_can_be_set_by_api_and_is_separate_from_analysis_payload(self) -> None:
        page = render_interactive_report(
            self.result,
            InteractiveReportConfig(title="My review portfolio"),
        )
        self.assertIn("<title>My review portfolio</title>", page)
        self.assertIn('<h1 id="reportTitle">My review portfolio</h1>', page)
        self.assertEqual(self._payload(page)["variants"]["all"]["metrics"]["total_trades"], 4)

    def test_monte_carlo_is_embedded_in_a_dedicated_report_tab(self) -> None:
        simulation = run_monte_carlo(
            self.result.report,
            MonteCarloConfig(
                iterations=8,
                seed=7,
                retain_paths=True,
                path_count=4,
            ),
        )

        first = render_interactive_report(self.result, monte_carlo=simulation)
        second = render_interactive_report(self.result, monte_carlo=simulation)

        self.assertEqual(first, second)
        self.assertIn('href="#monte-carlo"', first)
        self.assertIn('id="monteCarloPanel"', first)
        self.assertIn("Monte Carlo robustness", first)
        self.assertIn("P95 max drawdown", first)
        self.assertIn("monteCarloChart", first)
        self.assertNotIn("id=\"monteCarloMeta\"", first)
        self.assertNotIn("class='mc-config'", first)
        self.assertNotIn("permutation sampling", first)
        payload = self._payload(first)
        monte_carlo = payload["monte_carlo"]
        self.assertIsNotNone(monte_carlo)
        self.assertEqual(monte_carlo["summary"]["iterations"], 8)
        self.assertEqual(monte_carlo["summary"]["trade_count"], 4)
        self.assertEqual(monte_carlo["summary"]["path_count"], 4)
        self.assertEqual(monte_carlo["scope"], "strategy")
        self.assertEqual(len(monte_carlo["equity_paths"]), 4)
        self.assertEqual(len(monte_carlo["winning_streak_paths"]), 4)

    def test_report_without_monte_carlo_keeps_tab_available_with_guidance(self) -> None:
        page = render_interactive_report(self.result)

        self.assertIn('href="#monte-carlo"', page)
        self.assertIn("Monte Carlo was not run for this report", page)

    def test_warnings_and_provenance_are_the_last_section_and_navigation_tab(self) -> None:
        page = render_interactive_report(self.result)
        nav = page.split('<nav class="nav"', 1)[1].split('</nav>', 1)[0]

        self.assertGreater(nav.index('href="#audit"'), nav.index('href="#monte-carlo"'))
        self.assertTrue(nav.rstrip().endswith('href="#audit">Warnings & provenance</a>'))
        self.assertGreater(
            page.index('<section class="section" id="audit">'),
            page.index('<section class="section" id="monte-carlo">'),
        )
        self.assertEqual(
            page.rfind('<section class="section"'),
            page.index('<section class="section" id="audit">'),
        )

    def test_monte_carlo_table_keeps_mobile_scroller_inside_the_panel(self) -> None:
        simulation = run_monte_carlo(
            self.result.report,
            MonteCarloConfig(iterations=8, seed=7, retain_paths=True, path_count=4),
        )
        page = render_interactive_report(self.result, monte_carlo=simulation)
        mobile_css = page.split("@media (max-width: 640px) {", 1)[1].split("</style>", 1)[0]

        self.assertIn(".mc-table { width: 100%; min-width: 0; max-width: 100%; }", mobile_css)
        self.assertIn(".mc-table .data-table { width: max-content; min-width: 720px; }", mobile_css)
        self.assertNotIn(".mc-table { min-width: 720px; }", mobile_css)

    def test_privacy_controls_can_explicitly_include_identifiers_but_not_by_default(self) -> None:
        page = render_interactive_report(
            self.result,
            InteractiveReportConfig(include_trade_identifiers=True, redact_comments=False),
        )
        payload = self._payload(page)
        row = payload["variants"]["all"]["trades"][0]
        self.assertEqual(row["ticket"], "long-1")
        self.assertEqual(row["position_id"], "position-long-1")
        self.assertEqual(row["comment"], "secret-comment")
        self.assertNotIn("<script>alert", page)

    def test_embedded_payload_escapes_markup_before_script_injection(self) -> None:
        report = self.result.report
        report.trades[0] = self._trade(
            "xss",
            datetime(2024, 1, 1, 9),
            1.0,
            TradeSide.LONG,
            "</script><script>alert(1)</script>",
        )
        page = render_interactive_report(
            analyze(report),
            InteractiveReportConfig(redact_comments=False),
        )
        self.assertNotIn("</script><script>alert", page)
        payload = self._payload(page)
        self.assertEqual(payload["variants"]["all"]["trades"][0]["comment"], "</script><script>alert(1)</script>")

    def test_raw_path_bytes_and_file_like_inputs_are_supported(self) -> None:
        report = (
            b"<report><initialDeposit>1000</initialDeposit><currency>USD</currency>"
            b"<timezone>UTC</timezone><position><positionId>one</positionId>"
            b"<symbol>TEST</symbol><type>buy</type><volume>1</volume>"
            b"<openTime>2024.01.01 09:00:00</openTime>"
            b"<closeTime>2024.01.02 09:00:00</closeTime><profit>10</profit>"
            b"</position></report>"
        )
        from_bytes = render_interactive_report(report)
        from_file_like = render_interactive_report(io.BytesIO(report))
        self.assertEqual(self._payload(from_bytes)["kind"], "single")
        self.assertEqual(self._payload(from_file_like)["variants"]["all"]["metrics"]["net_profit"], 10.0)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "report.xml"
            path.write_bytes(report)
            self.assertEqual(self._payload(render_interactive_report(path))["kind"], "single")

    def test_save_and_serve_helpers_return_reusable_artifacts(self) -> None:
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "report.html"
            self.assertEqual(save_interactive_report(self.result, destination), destination)
            self.assertTrue(destination.read_text(encoding="utf-8").startswith("<!doctype html>"))
            server = serve_interactive_report(self.result)
            try:
                with urlopen(server.url, timeout=3) as response:
                    body = response.read().decode("utf-8")
                self.assertEqual(response.status, 200)
                self.assertIn("report-data", body)
            finally:
                server.close()
                server.close()

    def test_portfolio_payload_contains_members_and_daily_correlation(self) -> None:
        right = analyze(
            Report(
                trades=[
                    self._trade("right-1", datetime(2024, 1, 1, 10), 50.0, TradeSide.LONG),
                    self._trade("right-2", datetime(2024, 1, 2, 10), -20.0, TradeSide.SHORT),
                ],
                initial_deposit=1_000.0,
                currency="USD",
                strategy_name="Right strategy",
                timezone="UTC",
            )
        )
        portfolio = combine_analyses(
            [
                AnalyzedPortfolioMember(
                    "left",
                    PortfolioMember("Left strategy", "Left", weight=0.5),
                    self.result,
                ),
                AnalyzedPortfolioMember(
                    "right",
                    PortfolioMember("Right strategy", "Right", weight=0.5),
                    right,
                ),
            ],
            PortfolioConfig(portfolio_initial_capital=2_000.0),
        )
        portfolio_page = render_interactive_report(portfolio)
        self.assertIn("member.analysis?.allocated_equity", portfolio_page)
        self.assertNotIn("member.allocated_equity", portfolio_page)
        payload = self._payload(portfolio_page)
        self.assertEqual(payload["kind"], "portfolio")
        portfolio_view = payload["variants"]["all"]
        self.assertEqual(set(portfolio_view["members"]), {"left", "right"})
        self.assertIn("daily_profit", portfolio_view["correlations"])
        self.assertIn("monthly_drawdown_table", portfolio_view)
        self.assertEqual(portfolio_view["monthly_drawdown_table"]["worst_label"], "Worst")
        self.assertEqual(portfolio_view["metrics"]["total_trades"], 6)
        self.assertEqual(
            payload["variants"]["long"]["members"]["left"]["analysis"]["metrics"]["total_trades"],
            2,
        )
        self.assertEqual(
            payload["variants"]["short"]["members"]["left"]["analysis"]["metrics"]["total_trades"],
            2,
        )

    def test_public_portfolio_entry_point_can_feed_the_renderer(self) -> None:
        with TemporaryDirectory() as directory:
            left = Path(directory) / "left.xml"
            right = Path(directory) / "right.xml"
            left.write_text(
                "<report><initialDeposit>1000</initialDeposit><currency>USD</currency>"
                "<timezone>UTC</timezone><position><positionId>a</positionId><symbol>A</symbol>"
                "<type>buy</type><volume>1</volume><openTime>2024.01.01 09:00:00</openTime>"
                "<closeTime>2024.01.02 09:00:00</closeTime><profit>10</profit></position></report>",
                encoding="utf-8",
            )
            right.write_text(
                "<report><initialDeposit>1000</initialDeposit><currency>USD</currency>"
                "<timezone>UTC</timezone><position><positionId>b</positionId><symbol>B</symbol>"
                "<type>sell</type><volume>1</volume><openTime>2024.01.01 10:00:00</openTime>"
                "<closeTime>2024.01.02 10:00:00</closeTime><profit>20</profit></position></report>",
                encoding="utf-8",
            )
            portfolio = analyze_portfolio(
                [
                    PortfolioMember("A", "A", source=left),
                    PortfolioMember("B", "B", source=right),
                ],
                PortfolioConfig(portfolio_initial_capital=2_000.0),
            )
        self.assertIn("Portfolio", render_interactive_report(portfolio))


if __name__ == "__main__":
    unittest.main()
