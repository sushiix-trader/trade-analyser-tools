from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from pathlib import Path

from analyser import (
    AnalyzedPortfolioMember,
    CurveSeries,
    DrawdownDistribution,
    PortfolioConfig,
    PortfolioMember,
    Report,
    Trade,
    TradeSide,
    analyze,
    analyze_drawdowns,
    analyze_file,
    combine_analyses,
)
from analyser.filters import LongOnly


class DrawdownAnalysisTests(unittest.TestCase):
    @staticmethod
    def curve(values: list[float], *, timestamps: list[datetime | None] | None = None) -> CurveSeries:
        if timestamps is None:
            start = datetime(2024, 1, 1)
            timestamps = [start + timedelta(days=index) for index in range(len(values))]
        return CurveSeries(
            tuple(timestamps),
            tuple(values),
            "fixture_curve",
            "equity",
            float(values[0]) if values else 0.0,
        )

    def test_extracts_completed_and_open_episodes_and_excludes_open_from_reference(self) -> None:
        result = analyze_drawdowns(self.curve([100, 110, 90, 115, 105, 112, 108, 107]))

        self.assertEqual(result.observation_count, 8)
        self.assertEqual(result.completed_episode_count, 1)
        self.assertEqual(result.current_episode.status, "open")
        completed, current = result.episodes
        self.assertEqual((completed.peak_index, completed.trough_index, completed.recovery_index), (1, 2, 3))
        self.assertEqual((current.peak_index, current.trough_index, current.recovery_index), (3, 4, None))
        self.assertAlmostEqual(completed.depth_money, 20.0)
        self.assertAlmostEqual(completed.depth_percent, 18.1818181818)
        self.assertEqual(completed.duration_periods, 2)
        self.assertAlmostEqual(completed.duration_days, 2.0)
        self.assertAlmostEqual(current.depth_money, 10.0)
        self.assertAlmostEqual(current.duration_days, 4.0)
        self.assertEqual(result.depth_money_distribution.values, (20.0,))
        self.assertEqual(result.duration_distribution.values, (2.0,))
        self.assertAlmostEqual(current.depth_percentile, 50.0)
        self.assertAlmostEqual(current.depth_tail_rarity_percent, 100.0)
        self.assertAlmostEqual(current.depth_ordinal_rank, 0.5)

    def test_percentiles_use_linear_interpolation_and_ties_use_midranks(self) -> None:
        distribution = DrawdownDistribution.from_values([1.0, 2.0, 3.0, 4.0], "percent")
        self.assertAlmostEqual(distribution.p50, 2.5)
        self.assertAlmostEqual(distribution.p90, 3.7)
        self.assertAlmostEqual(distribution.p95, 3.85)
        self.assertAlmostEqual(distribution.p99, 3.97)

        result = analyze_drawdowns(self.curve([100, 90, 100, 90, 100, 80, 100]))
        first, second, third = result.completed_episodes
        self.assertAlmostEqual(first.depth_percentile, 50.0)
        self.assertAlmostEqual(first.depth_tail_rarity_percent, 100 / 3)
        self.assertAlmostEqual(first.depth_ordinal_rank, 1.5)
        self.assertAlmostEqual(third.depth_percentile, 100.0)
        self.assertAlmostEqual(third.depth_tail_rarity_percent, 0.0)
        self.assertAlmostEqual(third.depth_ordinal_rank, 3.0)
        self.assertAlmostEqual(second.duration_percentile, 66.6666666667)

        above_history = analyze_drawdowns(self.curve([100, 90, 100, 80, 100, 70]))
        current = above_history.current_episode
        self.assertIsNotNone(current)
        self.assertEqual(current.depth_percentile, 100.0)
        self.assertEqual(current.depth_tail_rarity_percent, 0.0)

    def test_duplicate_timestamps_are_preserved_and_observed_periods_are_counted(self) -> None:
        timestamp = datetime(2024, 1, 1)
        result = analyze_drawdowns(self.curve(
            [100, 110, 90, 110],
            timestamps=[timestamp, timestamp, timestamp + timedelta(days=1), timestamp + timedelta(days=1)],
        ))

        self.assertEqual(len(result.episodes), 1)
        episode = result.episodes[0]
        self.assertEqual(episode.status, "completed")
        self.assertEqual(episode.duration_periods, 2)
        self.assertAlmostEqual(episode.duration_days, 1.0)
        self.assertEqual(episode.peak_time, timestamp)
        self.assertEqual(episode.recovery_time, timestamp + timedelta(days=1))

    def test_nonpositive_high_water_mark_keeps_money_depth_but_nulls_percentage(self) -> None:
        result = analyze_drawdowns(self.curve([-1, -2, -1], timestamps=[None, None, None]))

        episode = result.episodes[0]
        self.assertEqual(episode.status, "completed")
        self.assertEqual(episode.depth_money, 1.0)
        self.assertIsNone(episode.depth_percent)
        self.assertIsNone(episode.duration_days)
        self.assertIn("drawdown_depth_percent_undefined", {item.code for item in result.warnings})
        self.assertIn("drawdown_duration_days_undefined", {item.code for item in result.warnings})
        self.assertEqual(result.depth_distribution.values, ())
        self.assertEqual(result.depth_money_distribution.values, (1.0,))

    def test_out_of_order_timestamps_emit_a_diagnostic_without_sorting_the_curve(self) -> None:
        start = datetime(2024, 1, 1)
        result = analyze_drawdowns(self.curve(
            [100, 90, 100],
            timestamps=[start + timedelta(days=2), start, start + timedelta(days=3)],
        ))

        self.assertEqual(result.episodes[0].peak_index, 0)
        self.assertEqual(result.episodes[0].recovery_index, 2)
        self.assertAlmostEqual(result.episodes[0].duration_days, 1.0)
        self.assertIn("drawdown_timestamps_out_of_order", {item.code for item in result.warnings})

    def test_monotonic_curve_is_not_currently_underwater(self) -> None:
        result = analyze_drawdowns(self.curve([100, 101, 102]))

        self.assertIsNone(result.current_episode)
        self.assertEqual(result.completed_episodes, ())
        self.assertEqual(result.depth_distribution.count, 0)
        self.assertIn("drawdown_no_completed_episodes", {item.code for item in result.warnings})

    def test_result_round_trip_backward_compatibility_and_exports(self) -> None:
        report = Report(
            initial_deposit=1000.0,
            trades=[
                Trade("one", "TEST", TradeSide.LONG, 1.0, datetime(2024, 1, 1), datetime(2024, 1, 2), 1, 1, 100.0),
                Trade("two", "TEST", TradeSide.LONG, 1.0, datetime(2024, 1, 2), datetime(2024, 1, 3), 1, 1, -50.0),
            ],
        )
        result = analyze(report)
        restored = type(result).from_dict(result.to_dict())
        self.assertEqual(restored.drawdown_analysis, result.drawdown_analysis)
        self.assertIn("episodes", result.drawdown_analysis.to_dict())
        self.assertIn("depth_percent", result.to_csv("drawdown_summary"))
        self.assertIn("episode_id", result.to_csv("drawdown_episodes"))
        self.assertIn("Drawdown depth × duration", result.to_markdown())

        legacy_payload = result.to_dict()
        del legacy_payload["drawdown_analysis"]
        restored_legacy = type(result).from_dict(legacy_payload)
        self.assertEqual(restored_legacy.drawdown_analysis, result.drawdown_analysis)

    def test_filters_recompute_drawdown_and_update_curve_basis(self) -> None:
        report = Report(
            initial_deposit=1000.0,
            trades=[
                Trade("one", "TEST", TradeSide.LONG, 1.0, datetime(2024, 1, 1), datetime(2024, 1, 2), 1, 1, 100.0),
                Trade("two", "TEST", TradeSide.SHORT, 1.0, datetime(2024, 1, 2), datetime(2024, 1, 3), 1, 1, -50.0),
                Trade("three", "TEST", TradeSide.LONG, 1.0, datetime(2024, 1, 3), datetime(2024, 1, 4), 1, 1, 25.0),
            ],
        )
        filtered = analyze(report).apply_filters(LongOnly())

        self.assertEqual(filtered.drawdown_analysis.curve_source, "filtered_reconstructed_closed_positions")
        self.assertEqual(filtered.drawdown_analysis.curve_basis, "balance")
        self.assertIsNone(filtered.drawdown_analysis.current_episode)
        self.assertNotEqual(filtered.drawdown_analysis, analyze(report).drawdown_analysis)

    def test_committed_synthetic_fixture_has_thirty_six_completed_episodes(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "results"
            / "sample_reports"
            / "synthetic_drawdown_36_episodes.html"
        )
        result = analyze_file(source)

        self.assertEqual(result.report.source_format, "mt5-html")
        self.assertEqual(len(result.report.trades), 144)
        self.assertEqual(result.drawdown_analysis.completed_episode_count, 36)
        self.assertIsNone(result.drawdown_analysis.current_episode)
        self.assertEqual(result.drawdown_analysis.depth_distribution.count, 36)
        self.assertEqual(result.drawdown_analysis.duration_distribution.count, 36)
        self.assertEqual(
            set(result.drawdown_analysis.duration_periods_distribution.values),
            {2.0, 3.0, 4.0},
        )
        self.assertEqual(result.validation.status, "match")

    def test_portfolio_exposes_raw_and_allocated_member_drawdowns_and_allocated_portfolio(self) -> None:
        left = analyze(Report(
            initial_deposit=1000.0,
            currency="USD",
            timezone="UTC",
            trades=[
                Trade("left-1", "TEST", TradeSide.LONG, 1.0, datetime(2024, 1, 1), datetime(2024, 1, 2), 1, 1, 100.0),
                Trade("left-2", "TEST", TradeSide.LONG, 1.0, datetime(2024, 1, 2), datetime(2024, 1, 3), 1, 1, -50.0),
                Trade("left-3", "TEST", TradeSide.LONG, 1.0, datetime(2024, 1, 3), datetime(2024, 1, 4), 1, 1, 100.0),
            ],
        ))
        right = analyze(Report(
            initial_deposit=1000.0,
            currency="USD",
            timezone="UTC",
            trades=[
                Trade("right-1", "TEST", TradeSide.LONG, 1.0, datetime(2024, 1, 1), datetime(2024, 1, 2), 1, 1, 20.0),
            ],
        ))
        portfolio = combine_analyses([
            AnalyzedPortfolioMember("left", PortfolioMember("Left", "Left"), left),
            AnalyzedPortfolioMember("right", PortfolioMember("Right", "Right"), right),
        ], PortfolioConfig(portfolio_initial_capital=2000.0))

        member = portfolio.members[0]
        self.assertEqual(member.raw_drawdown_analysis.curve_source, "reconstructed_closed_positions")
        self.assertTrue(member.allocated_drawdown_analysis.curve_source.startswith("allocated:"))
        self.assertEqual(portfolio.drawdown_analysis.curve_source, "portfolio_allocated_member_curves")
        self.assertEqual(portfolio.to_dict()["members"][0]["allocated_drawdown_analysis"]["curve_source"], member.allocated_drawdown_analysis.curve_source)
        self.assertIn("episode_id", portfolio.to_csv("drawdown_episodes"))

        legacy_payload = portfolio.to_dict()
        del legacy_payload["drawdown_analysis"]
        for item in legacy_payload["members"]:
            del item["raw_drawdown_analysis"]
            del item["allocated_drawdown_analysis"]
        restored = type(portfolio).from_dict(legacy_payload)
        self.assertEqual(restored.drawdown_analysis, portfolio.drawdown_analysis)
        self.assertEqual(restored.members[0].raw_drawdown_analysis, member.raw_drawdown_analysis)


if __name__ == "__main__":
    unittest.main()
