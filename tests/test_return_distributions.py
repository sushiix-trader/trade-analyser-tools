from __future__ import annotations

import json
import re
import unittest
from datetime import datetime, timedelta

from analyser import (
    AnalysisConfig,
    AnalyzedPortfolioMember,
    CurveSeries,
    PortfolioConfig,
    PortfolioMember,
    Report,
    ReturnDistributions,
    Trade,
    TradeSide,
    analyze,
    analyze_return_distribution,
    analyze_return_distributions,
    combine_analyses,
    render_interactive_report,
)


class ReturnDistributionTests(unittest.TestCase):
    @staticmethod
    def curve() -> CurveSeries:
        return CurveSeries(
            timestamps=(
                datetime(2024, 1, 15, 12),
                datetime(2024, 1, 20, 12),
                datetime(2024, 2, 3, 12),
                datetime(2024, 3, 1, 12),
            ),
            values=(100.0, 110.0, 99.0, 108.0),
            source="fixture",
            basis="balance",
            initial_value=100.0,
        )

    @staticmethod
    def trade(ticket: str, close_time: datetime, profit: float) -> Trade:
        return Trade(
            ticket=ticket,
            symbol="TEST",
            side=TradeSide.LONG,
            volume=1.0,
            open_time=close_time - timedelta(hours=1),
            close_time=close_time,
            open_price=1.0,
            close_price=1.01,
            profit=profit,
        )

    def test_monthly_periods_use_initial_baseline_carry_forward_and_partial_labels(self) -> None:
        result = analyze_return_distributions(self.curve())
        monthly = result.monthly

        self.assertEqual([item.period for item in monthly.observations], ["2024-01", "2024-02", "2024-03"])
        self.assertEqual([item.label for item in monthly.observations], [
            "2024-01 (partial)", "2024-02", "2024-03 (partial)",
        ])
        self.assertEqual([item.partial for item in monthly.observations], [True, False, True])
        self.assertAlmostEqual(monthly.observations[0].start_value, 100.0)
        self.assertAlmostEqual(monthly.observations[0].end_value, 110.0)
        self.assertAlmostEqual(monthly.observations[0].return_pct, 10.0)
        self.assertAlmostEqual(monthly.observations[1].return_pct, -10.0)
        self.assertAlmostEqual(monthly.observations[2].return_pct, 108.0 / 99.0 * 100.0 - 100.0)
        self.assertEqual(monthly.observation_count, 3)
        self.assertEqual(monthly.stats.count, 3)
        self.assertEqual(monthly.stats.positive_count, 2)
        self.assertEqual(monthly.stats.negative_count, 1)
        self.assertEqual(monthly.stats.zero_count, 0)

    def test_ranked_observations_are_sorted_and_percentiles_use_linear_interpolation(self) -> None:
        monthly = analyze_return_distribution(self.curve(), "monthly")
        self.assertEqual(
            [item.period for item in monthly.ranked_observations],
            ["2024-02", "2024-03", "2024-01"],
        )
        self.assertEqual([item.rank for item in monthly.ranked_observations], [1, 2, 3])
        values = [item.return_pct for item in monthly.ranked_observations]
        self.assertEqual(values, sorted(values))
        self.assertAlmostEqual(monthly.stats.p5, -8.09090909090909)
        self.assertAlmostEqual(monthly.stats.median, 108.0 / 99.0 * 100.0 - 100.0)
        self.assertAlmostEqual(monthly.stats.p95, 9.90909090909091)
        self.assertAlmostEqual(monthly.stats.mean, sum(values) / 3.0)
        self.assertAlmostEqual(monthly.stats.stddev, (sum((value - monthly.stats.mean) ** 2 for value in values) / 3.0) ** 0.5)

    def test_daily_and_weekly_frequencies_have_calendar_periods_and_integer_ranks(self) -> None:
        daily = analyze_return_distribution(self.curve(), "daily")
        weekly = analyze_return_distribution(self.curve(), "weekly")

        self.assertEqual(daily.frequency, "daily")
        self.assertEqual(weekly.frequency, "weekly")
        self.assertEqual(daily.observations[0].period, "2024-01-15")
        self.assertEqual(daily.observations[-1].period, "2024-03-01")
        self.assertEqual(daily.observation_count, 47)
        self.assertEqual(sum(item.return_pct == 0.0 for item in daily.observations), 44)
        self.assertEqual([item.period for item in weekly.observations], ["2024-W03", "2024-W04", "2024-W05", "2024-W06", "2024-W07", "2024-W08", "2024-W09"])
        self.assertTrue(weekly.observations[0].partial)
        self.assertTrue(weekly.observations[-1].partial)
        self.assertEqual([item.rank for item in daily.ranked_observations], list(range(1, daily.observation_count + 1)))

    def test_no_trade_calendar_periods_are_zero_and_small_sample_warning_is_typed(self) -> None:
        report = Report(
            initial_deposit=1000.0,
            trades=[
                self.trade("one", datetime(2024, 1, 2), 100.0),
                self.trade("two", datetime(2024, 3, 2), 100.0),
            ],
        )
        result = analyze(report)
        monthly = result.return_distributions.monthly

        self.assertEqual([item.period for item in monthly.observations], ["2024-01", "2024-02", "2024-03"])
        self.assertAlmostEqual(monthly.observations[1].return_pct, 0.0)
        self.assertEqual(monthly.stats.zero_count, 1)
        self.assertTrue(any(item.code == "return_distribution_low_observations" for item in monthly.warnings))
        self.assertTrue(any(item.code == "return_distribution_low_observations" for item in result.warnings))

    def test_undefined_zero_baseline_is_retained_but_not_ranked(self) -> None:
        curve = CurveSeries(
            timestamps=(datetime(2024, 1, 1), datetime(2024, 1, 2)),
            values=(0.0, 10.0),
            source="fixture",
            basis="balance",
            initial_value=0.0,
        )
        monthly = analyze_return_distribution(curve, "daily")
        self.assertEqual(monthly.observation_count, 2)
        self.assertEqual(monthly.stats.count, 0)
        self.assertIsNone(monthly.observations[0].return_pct)
        self.assertTrue(any(item.code == "return_distribution_undefined_return" for item in monthly.warnings))

    def test_invalid_frequency_and_empty_curve_are_explicit(self) -> None:
        with self.assertRaises(ValueError):
            analyze_return_distribution(self.curve(), "quarterly")
        empty = analyze_return_distributions(None)
        self.assertIsInstance(empty, ReturnDistributions)
        self.assertEqual(empty.monthly.observation_count, 0)
        self.assertTrue(any(item.code == "return_distribution_no_curve" for item in empty.warnings))

    def test_analysis_and_serialized_result_expose_all_frequencies(self) -> None:
        result = analyze(Report(
            initial_deposit=1000.0,
            trades=[self.trade("one", datetime(2024, 1, 2), 100.0)],
        ), AnalysisConfig())
        restored = type(result).from_dict(result.to_dict())

        self.assertEqual(set(result.return_distributions.as_dict()), {"daily", "weekly", "monthly"})
        self.assertEqual(restored.return_distributions, result.return_distributions)
        self.assertEqual(restored.return_distributions.monthly.stats, result.return_distributions.monthly.stats)

    def test_portfolio_exposes_allocated_combined_and_member_distributions(self) -> None:
        left = analyze(Report(
            initial_deposit=1000.0,
            currency="USD",
            timezone="UTC",
            trades=[self.trade("left", datetime(2024, 1, 2), 100.0)],
        ))
        right = analyze(Report(
            initial_deposit=1000.0,
            currency="USD",
            timezone="UTC",
            trades=[self.trade("right", datetime(2024, 2, 2), -50.0)],
        ))
        portfolio = combine_analyses([
            AnalyzedPortfolioMember("left", PortfolioMember("Left", "left", weight=.5), left),
            AnalyzedPortfolioMember("right", PortfolioMember("Right", "right", weight=.5), right),
        ], PortfolioConfig(portfolio_initial_capital=2000.0))

        self.assertEqual(portfolio.return_distributions.monthly.observation_count, 2)
        self.assertAlmostEqual(portfolio.return_distributions.monthly.observations[0].return_pct, 5.0)
        self.assertAlmostEqual(portfolio.return_distributions.monthly.observations[1].return_pct, -2.5 / 1.05)
        self.assertEqual(len(portfolio.members[0].allocated_return_distributions.monthly.observations), 1)
        self.assertEqual(len(portfolio.members[1].raw_return_distributions.monthly.observations), 1)
        restored = type(portfolio).from_dict(portfolio.to_dict())
        self.assertEqual(restored.return_distributions, portfolio.return_distributions)

    def test_interactive_payload_contains_return_distribution_tab_and_frequency_data(self) -> None:
        result = analyze(Report(
            initial_deposit=1000.0,
            currency="USD",
            timezone="UTC",
            strategy_name="Fixture",
            trades=[self.trade("one", datetime(2024, 1, 2), 100.0)],
        ))
        page = render_interactive_report(result)
        match = re.search(r'<script id="report-data" type="application/json">(.*?)</script>', page, re.DOTALL)
        self.assertIsNotNone(match)
        payload = json.loads(match.group(1))

        self.assertIn('href="#return-distributions"', page)
        self.assertIn('id="returnFrequency"', page)
        self.assertIn("Ranked period returns", page)
        self.assertIn("Return histogram", page)
        self.assertIn("function returnHistogramSvg(data)", page)
        self.assertIn("const binCount = Math.min(12, Math.max(1, observations.length));", page)
        self.assertIn("Periods (count)", page)
        self.assertIn("percentile-line", page)
        self.assertIn("Vertical red dotted lines mark P5, median, and P95", page)
        self.assertIn('const markerSpecs = [["P5", stats.p5, 5], ["Median", stats.median, 50], ["P95", stats.p95, 95]];', page)
        self.assertIn("const rank = 1 + (observations.length - 1) * percentile / 100;", page)
        self.assertEqual(page.count('role="tabpanel"'), 11)
        distributions = payload["variants"]["all"]["return_distributions"]
        self.assertEqual(set(distributions), {"monthly", "weekly", "daily"})
        self.assertIn("p5", distributions["monthly"]["stats"])
        self.assertIn("p95", distributions["monthly"]["stats"])
        self.assertNotIn("observations", distributions["monthly"])
        self.assertIn("ranked_observations", distributions["monthly"])
        self.assertEqual(
            set(distributions["monthly"]["ranked_observations"][0]),
            {"period", "label", "return_pct", "has_curve_observation", "rank"},
        )


if __name__ == "__main__":
    unittest.main()
