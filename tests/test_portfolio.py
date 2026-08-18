from __future__ import annotations

import importlib.util
import os

import numpy as np
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from analyser import (
    AnalysisStore,
    AnalyzedPortfolioMember,
    ChartConfig,
    MonthlyPerformanceTableChartConfig,
    PortfolioConfig,
    PortfolioMember,
    analyze,
    analyze_portfolio,
    combine_analyses,
    load_report,
    LongOnly,
    render_equity_drawdown_chart,
    render_monthly_performance_table,
    save_monthly_performance_table,
    ShortOnly,
)
from analyser.charts import _high_water_mark_drawdown
from analyser.errors import CurrencyMismatchError, DuplicatePortfolioMemberError, TimezoneMismatchError
from analyser.models import AccountPoint


def xml_report(currency: str, positions: list[tuple[str, str, float]], deposit: float = 1000.0) -> bytes:
    rows = "".join(
        f"""<position><positionId>{ticket}</positionId><symbol>TEST_SYMBOL</symbol>
        <type>buy</type><volume>1</volume>
        <openTime>{opened}</openTime><closeTime>{closed}</closeTime>
        <profit>{profit}</profit></position>"""
        for ticket, opened, closed, profit in positions
    )
    return f"<report><initialDeposit>{deposit}</initialDeposit><currency>{currency}</currency>{rows}</report>".encode()


A = xml_report(
    "USD",
    [
        ("a1", "2024.01.01 10:00:00", "2024.01.02 10:00:00", 100.0),
        ("a2", "2024.02.01 10:00:00", "2024.02.02 10:00:00", -50.0),
    ],
)
B = xml_report(
    "USD",
    [
        ("b1", "2024.01.03 10:00:00", "2024.01.04 10:00:00", 200.0),
        ("b2", "2024.03.01 10:00:00", "2024.03.02 10:00:00", -100.0),
    ],
)


def members() -> list[PortfolioMember]:
    return [
        PortfolioMember("Strategy A", "Trend test", weight=0.6, source=A),
        PortfolioMember("Strategy B", "Mean reversion test", weight=0.4, source=B),
    ]


class PortfolioTests(unittest.TestCase):
    def test_combines_allocated_curves_and_trade_metrics(self) -> None:
        result = analyze_portfolio(
            members(),
            PortfolioConfig(portfolio_initial_capital=1000.0),
        )

        self.assertEqual(result.metrics.net_profit, 70.0)
        self.assertAlmostEqual(result.metrics.average_win, 70.0)
        self.assertAlmostEqual(result.metrics.average_loss, -35.0)
        self.assertEqual(len(result.portfolio_report.trades), 4)
        self.assertEqual(
            {trade.strategy_id for trade in result.portfolio_report.trades},
            {member.member_key for member in result.members},
        )
        self.assertEqual(result.equity_matrix.column_labels, ("Strategy A", "Strategy B", "PORTFOLIO"))
        self.assertEqual(result.raw_equity_matrix.shape, result.equity_matrix.shape)
        self.assertEqual(result.allocated_monthly_return_matrix.shape[1], 3)
        self.assertEqual(result.correlation_matrix.shape, (2, 2))
        self.assertEqual(result.monthly[0].pnl, 140.0)
        self.assertEqual(result.monthly[-1].pnl, -40.0)
        self.assertTrue(any(w.code == "member_active_period_differs" for w in result.warnings))

    @unittest.skipUnless(importlib.util.find_spec("matplotlib"), "matplotlib is not installed")
    def test_portfolio_chart_optionally_overlays_allocated_member_equity(self) -> None:
        result = analyze_portfolio(
            members(),
            PortfolioConfig(portfolio_initial_capital=1000.0),
        )

        portfolio_only = render_equity_drawdown_chart(result, title="Portfolio")
        with_members = render_equity_drawdown_chart(
            result,
            title="Portfolio with members",
            show_member_equity=True,
        )
        configured = render_equity_drawdown_chart(
            result,
            title="Portfolio with members",
            chart_config=ChartConfig(show_member_equity=True),
        )
        normalized = render_equity_drawdown_chart(
            result,
            title="Normalized portfolio with members",
            show_member_equity=True,
            normalize_equity=True,
        )
        normalized_configured = render_equity_drawdown_chart(
            result,
            title="Normalized portfolio with members",
            chart_config=ChartConfig(
                show_member_equity=True,
                normalize_equity=True,
            ),
        )

        self.assertTrue(portfolio_only.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertTrue(with_members.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertTrue(normalized.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(with_members, configured)
        self.assertEqual(normalized, normalized_configured)
        self.assertNotEqual(portfolio_only, with_members)
        self.assertNotEqual(with_members, normalized)

    @unittest.skipUnless(importlib.util.find_spec("matplotlib"), "matplotlib is not installed")
    def test_monthly_performance_table_image_is_deterministic_and_public(self) -> None:
        result = analyze_portfolio(
            members(),
            PortfolioConfig(portfolio_initial_capital=1000.0),
        )
        config = MonthlyPerformanceTableChartConfig()

        rendered = render_monthly_performance_table(
            result,
            title="Monthly returns",
            chart_config=config,
        )
        rendered_from_table = render_monthly_performance_table(
            result.monthly_performance,
            title="Monthly returns",
            chart_config=config,
        )

        self.assertTrue(rendered.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(rendered, rendered_from_table)
        with TemporaryDirectory() as directory:
            destination = save_monthly_performance_table(
                result,
                Path(directory) / "monthly-performance.png",
                title="Monthly returns",
                chart_config=config,
            )
            self.assertEqual(destination.read_bytes(), rendered)

    def test_normalized_chart_drawdown_uses_peak_relative_denominator(self) -> None:
        equity = np.asarray([100.0, 200.0, 150.0])

        drawdown = _high_water_mark_drawdown(equity, percentage=True)

        self.assertEqual(tuple(drawdown), (0.0, 0.0, -25.0))

    def test_currency_chart_drawdown_remains_a_money_difference(self) -> None:
        equity = np.asarray([100.0, 200.0, 150.0])

        drawdown = _high_water_mark_drawdown(equity, percentage=False)

        self.assertEqual(tuple(drawdown), (0.0, 0.0, -50.0))

    def test_member_filters_apply_before_portfolio_allocation(self) -> None:
        filtered_members = [
            PortfolioMember("Strategy A", "Long-only A", weight=0.6, source=A, filters=LongOnly()),
            PortfolioMember("Strategy B", "Short-only B", weight=0.4, source=B, filters=ShortOnly()),
        ]
        result = analyze_portfolio(
            filtered_members,
            PortfolioConfig(portfolio_initial_capital=1000.0),
        )
        self.assertEqual(result.members[0].analysis.selection.selected_trade_count, 2)
        self.assertEqual(result.members[1].analysis.selection.selected_trade_count, 0)
        self.assertEqual(len(result.portfolio_report.trades), 2)
        self.assertEqual(result.metrics.net_profit, 30.0)

    def test_raw_and_allocated_contribution_matrices_are_separate(self) -> None:
        result = analyze_portfolio(members(), PortfolioConfig(portfolio_initial_capital=1000.0))

        raw = result.raw_monthly_contribution_matrix.values
        allocated = result.allocated_monthly_contribution_matrix.values
        self.assertEqual(raw[0], (100.0, 200.0, 300.0))
        self.assertEqual(allocated[0], (60.0, 80.0, 140.0))
        self.assertNotEqual(raw, allocated)

    def test_weight_and_metadata_updates_reuse_member_analyses(self) -> None:
        result = analyze_portfolio(members(), PortfolioConfig(portfolio_initial_capital=1000.0))
        weights = {member.member_key: 0.5 for member in result.members}
        reweighted = result.with_weights(weights)
        renamed = reweighted.with_member_metadata(
            result.members[0].member_key,
            strategy_name="Updated A",
            description="Updated description",
        )

        self.assertEqual(reweighted.metrics.net_profit, 75.0)
        self.assertEqual(renamed.members[0].strategy_name, "Updated A")
        self.assertEqual(renamed.members[0].description, "Updated description")
        self.assertEqual(renamed.members[0].analysis.to_json(), result.members[0].analysis.to_json())

    def test_currency_mismatch_is_a_hard_error(self) -> None:
        other = PortfolioMember("Strategy B", "Other currency", source=xml_report("AUD", []))
        with self.assertRaises(CurrencyMismatchError):
            analyze_portfolio([members()[0], other])

    def test_duplicate_sources_and_names_are_rejected(self) -> None:
        with self.assertRaises(DuplicatePortfolioMemberError):
            analyze_portfolio([
                PortfolioMember("A", "one", source=A),
                PortfolioMember("B", "two", source=A),
            ])
        with self.assertRaises(DuplicatePortfolioMemberError):
            analyze_portfolio([
                PortfolioMember("A", "one", source=A),
                PortfolioMember("a", "two", source=B),
            ])

    def test_weights_are_normalized_and_invalid_weights_rejected(self) -> None:
        result = analyze_portfolio([
            PortfolioMember("A", "one", weight=3, source=A),
            PortfolioMember("B", "two", weight=2, source=B),
        ])
        self.assertEqual(result.normalized_weights[result.members[0].member_key], 0.6)
        with self.assertRaises(ValueError):
            analyze_portfolio([
                PortfolioMember("A", "one", weight=-1, source=A),
                PortfolioMember("B", "two", weight=1, source=B),
            ])

    def test_timezone_mismatch_is_validated(self) -> None:
        first = load_report(A)
        second = load_report(B)
        first.timezone = "UTC"
        second.timezone = "Australia/Brisbane"
        prepared = [
            AnalyzedPortfolioMember("a", PortfolioMember("A", "one", source=A), analyze(first)),
            AnalyzedPortfolioMember("b", PortfolioMember("B", "two", source=B), analyze(second)),
        ]
        with self.assertRaises(TimezoneMismatchError):
            combine_analyses(prepared)

    def test_source_curve_can_be_selected(self) -> None:
        first = load_report(A)
        second = load_report(B)
        for report in (first, second):
            report.source_balance_points = [
                AccountPoint(datetime(2024, 1, 1), balance=report.initial_deposit),
                AccountPoint(datetime(2024, 3, 3), balance=report.initial_deposit + sum(report.profits())),
            ]
        prepared = [
            AnalyzedPortfolioMember("a", PortfolioMember("A", "one", source=A), analyze(first)),
            AnalyzedPortfolioMember("b", PortfolioMember("B", "two", source=B), analyze(second)),
        ]
        result = combine_analyses(prepared, PortfolioConfig(primary_curve="source"))
        self.assertEqual(result.equity.basis, "balance")
        self.assertIsNotNone(result.source_balance)

    def test_filtered_portfolio_cache_round_trip(self) -> None:
        filtered_members = [
            PortfolioMember("Strategy A", "Long-only A", weight=0.6, source=A, filters=LongOnly()),
            PortfolioMember("Strategy B", "Short-only B", weight=0.4, source=B, filters=ShortOnly()),
        ]
        config = PortfolioConfig(portfolio_initial_capital=1000.0)
        with TemporaryDirectory() as directory:
            store = AnalysisStore(directory)
            first = store.analyze_portfolio_or_load(filtered_members, config)
            second = store.analyze_portfolio_or_load(filtered_members, config)
            self.assertFalse(first.cache_hit)
            self.assertTrue(second.cache_hit)
            self.assertEqual(first.result.to_json(), second.result.to_json())
            self.assertEqual(second.result.members[0].analysis.selection.selected_trade_count, 2)

    def test_serialization_and_portfolio_cache_round_trip(self) -> None:
        config = PortfolioConfig(portfolio_initial_capital=1000.0)
        result = analyze_portfolio(members(), config)
        restored = result.from_dict(result.to_dict())
        self.assertEqual(result.to_json(), restored.to_json())
        self.assertIn("PORTFOLIO", result.to_csv("equity"))
        self.assertIn("Portfolio Trade Analysis", result.to_markdown())

        with TemporaryDirectory() as directory:
            store = AnalysisStore(directory)
            first = store.analyze_portfolio_or_load(members(), config)
            second = store.analyze_portfolio_or_load(members(), config)
            self.assertFalse(first.cache_hit)
            self.assertTrue(second.cache_hit)
            self.assertEqual(first.result.to_json(), second.result.to_json())
            self.assertTrue(Path(second.path).exists())

    def test_private_reports_combine_without_trade_netting_when_configured(self) -> None:
        first = os.environ.get("MT5_FIXTURE_REPORT_A")
        second = os.environ.get("MT5_FIXTURE_REPORT_B")
        if not first or not second:
            self.skipTest(
                "set MT5_FIXTURE_REPORT_A and MT5_FIXTURE_REPORT_B to run the private portfolio smoke test"
            )
        paths = [Path(first), Path(second)]
        if not all(path.exists() for path in paths):
            self.skipTest("configured private MT5 fixtures are not available")

        result = analyze_portfolio([
            PortfolioMember("Private fixture A", "Local smoke test A", source=paths[0], weight=0.5),
            PortfolioMember("Private fixture B", "Local smoke test B", source=paths[1], weight=0.5),
        ])

        member_trade_count = sum(
            member.analysis.metrics.total_trades for member in result.members
        )
        self.assertEqual(result.metrics.total_trades, member_trade_count)
        self.assertEqual(len(result.portfolio_report.trades), member_trade_count)


if __name__ == "__main__":
    unittest.main()
