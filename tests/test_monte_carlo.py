from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
import unittest

import numpy as np

from analyser.models import Report, Trade, TradeSide
from analyser import (
    DEFAULT_REPORT_MONTE_CARLO_CONFIG,
    MonteCarloPathChartConfig,
    MonteCarloPathInterval,
    load_report,
    render_monte_carlo_paths,
    run_monte_carlo_file,
)
from analyser.simulations import MonteCarloConfig, run_monte_carlo
from analyser.simulations.monte_carlo import _streak_statistics


def make_report(profits: list[float], initial_deposit: float = 100.0) -> Report:
    start = datetime(2024, 1, 1)
    trades = []
    for index, profit in enumerate(profits):
        opened = start + timedelta(days=index)
        trades.append(
            Trade(
                ticket=str(index + 1),
                symbol="TEST_SYMBOL",
                side=TradeSide.LONG,
                volume=1.0,
                open_time=opened,
                close_time=opened + timedelta(hours=1),
                open_price=1.1,
                close_price=1.101,
                profit=profit,
            )
        )
    return Report(trades=trades, initial_deposit=initial_deposit)


class MonteCarloTests(unittest.TestCase):
    def test_complete_report_default_is_reproducible_and_path_bounded(self) -> None:
        config = DEFAULT_REPORT_MONTE_CARLO_CONFIG

        self.assertEqual(config.iterations, 10_000)
        self.assertEqual(config.method, "permutation")
        self.assertEqual(config.seed, 42)
        self.assertTrue(config.retain_paths)
        self.assertEqual(config.path_count, 500)

    def test_permutation_is_deterministic_and_preserves_trade_distribution(self) -> None:
        report = make_report([10.0, -5.0, 20.0, -8.0])
        config = MonteCarloConfig(iterations=100, seed=123)
        left = run_monte_carlo(report, config)
        right = run_monte_carlo(report, config)

        np.testing.assert_array_equal(left.max_drawdowns, right.max_drawdowns)
        np.testing.assert_array_equal(left.net_profits, right.net_profits)
        np.testing.assert_array_equal(left.ruined, right.ruined)
        self.assertTrue(np.all(left.net_profits == 17.0))
        self.assertEqual(left.trade_count, 4)
        self.assertEqual(left.sample_size, 4)
        self.assertEqual(left.to_json(), right.to_json())
        self.assertEqual(left.summary()["net_profit"]["worst"], 17.0)

    def test_bootstrap_is_a_different_explicit_sampling_method(self) -> None:
        report = make_report([10.0, -5.0, 20.0, -8.0])
        result = run_monte_carlo(
            report,
            MonteCarloConfig(iterations=100, method="bootstrap", seed=7),
        )

        self.assertEqual(result.sample_size, 4)
        self.assertGreater(len(set(result.net_profits.tolist())), 1)
        self.assertIn("max_drawdown_pct", result.summary())

    def test_zero_profit_resets_both_streak_paths(self) -> None:
        max_wins, max_losses, winning, losing = _streak_statistics(
            np.asarray([1.0, 2.0, 0.0, -1.0, -2.0])
        )

        self.assertEqual(max_wins, 2)
        self.assertEqual(max_losses, 2)
        np.testing.assert_array_equal(winning, [0, 1, 2, 0, 0, 0])
        np.testing.assert_array_equal(losing, [0, 0, 0, 0, 1, 2])

    def test_winning_and_losing_streaks_are_exposed_with_percentile_summaries(self) -> None:
        result = run_monte_carlo(
            make_report([1.0, 1.0, -1.0, -1.0, 0.0]),
            MonteCarloConfig(
                iterations=8,
                seed=123,
                retain_paths=True,
                path_count=8,
            ),
        )

        self.assertEqual(np.max(result.max_consecutive_wins), 2)
        self.assertEqual(np.max(result.max_consecutive_losses), 2)
        self.assertTrue(np.all(result.max_consecutive_wins <= 2))
        self.assertTrue(np.all(result.max_consecutive_losses <= 2))
        self.assertEqual(result.winning_streak_paths.shape, (8, 6))
        self.assertEqual(result.losing_streak_paths.shape, (8, 6))
        self.assertIn("max_consecutive_wins", result.summary())
        self.assertIn("max_consecutive_losses", result.summary())
        self.assertGreaterEqual(result.summary()["max_consecutive_wins"]["p95"], 1.0)

    def test_retained_paths_are_deterministic_and_evenly_selected(self) -> None:
        report = make_report([10.0, -5.0, 20.0, -8.0])
        config = MonteCarloConfig(
            iterations=10,
            seed=123,
            retain_paths=True,
            path_count=4,
        )
        left = run_monte_carlo(report, config)
        right = run_monte_carlo(report, config)

        np.testing.assert_array_equal(left.path_indices, [0, 3, 6, 9])
        self.assertEqual(left.paths.shape, (4, 5))
        np.testing.assert_array_equal(left.paths, right.paths)
        self.assertEqual(left.path_count, 4)
        self.assertEqual(left.summary()["path_count"], 4)
        self.assertEqual(left.to_json(), right.to_json())

    def test_path_config_requires_retention_and_valid_percentiles(self) -> None:
        with self.assertRaises(ValueError):
            run_monte_carlo(
                make_report([1.0]),
                MonteCarloConfig(iterations=2, path_count=1),
            )
        with self.assertRaises(ValueError):
            run_monte_carlo(
                make_report([1.0]),
                MonteCarloConfig(iterations=2, retain_paths=True, path_count=3),
            )
        with self.assertRaises(ValueError):
            MonteCarloPathInterval(95.0, 5.0).validate()

    def test_path_chart_uses_retained_paths_and_is_deterministic(self) -> None:
        result = run_monte_carlo(
            make_report([10.0, -5.0, 20.0, -8.0]),
            MonteCarloConfig(iterations=12, seed=123, retain_paths=True, path_count=6),
        )
        chart_config = MonteCarloPathChartConfig(
            intervals=(
                MonteCarloPathInterval(10.0, 90.0, color="#123456", label="central 80%"),
                MonteCarloPathInterval(25.0, 75.0, color="#abcdef", alpha=0.3),
            ),
            show_streaks=True,
        )
        first = render_monte_carlo_paths(result, chart_config=chart_config)
        second = render_monte_carlo_paths(result, chart_config=chart_config)
        self.assertTrue(first.startswith(b"\x89PNG"))
        self.assertEqual(first, second)

    def test_path_chart_rejects_result_without_retained_paths(self) -> None:
        result = run_monte_carlo(make_report([1.0]), MonteCarloConfig(iterations=2))
        with self.assertRaises(ValueError):
            render_monte_carlo_paths(result)

    def test_skip_trades_changes_sample_size(self) -> None:
        result = run_monte_carlo(
            make_report([10.0, -5.0, 20.0, -8.0]),
            MonteCarloConfig(iterations=3, skip_trades_pct=50, seed=1),
        )

        self.assertEqual(result.sample_size, 2)
        self.assertTrue(np.all(result.max_consecutive_losses >= 0))

    def test_scalar_options_remain_compatible(self) -> None:
        result = run_monte_carlo(
            make_report([10.0, -5.0]),
            iterations=3,
            skip_trades_pct=50,
            seed=9,
        )

        self.assertEqual(result.iterations, 3)
        self.assertEqual(result.sample_size, 1)

    def test_ruin_is_detected_when_a_path_crosses_threshold(self) -> None:
        # Seed 0 produces [-150, +100]. Final equity is positive, but the path
        # crosses zero and must still be considered ruined.
        result = run_monte_carlo(
            make_report([-150.0, 100.0]),
            MonteCarloConfig(iterations=1, seed=0, ruin_equity=0.0),
        )

        self.assertEqual(result.final_equities[0], 50.0)
        self.assertTrue(result.ruined[0])
        self.assertEqual(result.probability_of_ruin_pct, 100.0)

    def test_empty_report_still_returns_one_row_per_iteration(self) -> None:
        result = run_monte_carlo(
            make_report([]),
            MonteCarloConfig(iterations=4, seed=11),
        )

        self.assertEqual(result.trade_count, 0)
        self.assertEqual(result.sample_size, 0)
        np.testing.assert_array_equal(result.final_equities, [100.0] * 4)
        np.testing.assert_array_equal(result.max_drawdowns, [0.0] * 4)

    def test_invalid_configuration_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            run_monte_carlo(make_report([1.0]), MonteCarloConfig(iterations=0))
        with self.assertRaises(ValueError):
            run_monte_carlo(make_report([1.0]), iterations=0)
        with self.assertRaises(ValueError):
            run_monte_carlo(make_report([1.0]), MonteCarloConfig(method="unknown"))
        with self.assertRaises(ValueError):
            run_monte_carlo(make_report([1.0]), MonteCarloConfig(skip_trades_pct=101))

    def test_file_api_uses_the_same_canonical_parser(self) -> None:
        xml = b"""<?xml version='1.0'?>
        <report><initialDeposit>100</initialDeposit>
        <position><positionId>1</positionId><symbol>TEST_SYMBOL</symbol>
        <type>buy</type><volume>1</volume>
        <openTime>2024.01.01 10:00:00</openTime>
        <closeTime>2024.01.01 11:00:00</closeTime>
        <profit>25</profit></position></report>"""
        result = run_monte_carlo_file(xml, MonteCarloConfig(iterations=2, seed=3))

        self.assertEqual(result.trade_count, 1)
        np.testing.assert_array_equal(result.net_profits, [25.0, 25.0])

    def test_private_report_preserves_net_profit_under_permutation_when_configured(self) -> None:
        configured = os.environ.get("MT5_FIXTURE_REPORT")
        if not configured:
            self.skipTest("set MT5_FIXTURE_REPORT to run the private Monte Carlo smoke test")
        path = Path(configured)
        if not path.exists():
            self.skipTest("configured private MT5 fixture is not available")

        report = load_report(path)
        result = run_monte_carlo(
            report,
            MonteCarloConfig(iterations=25, seed=42),
        )

        self.assertEqual(result.trade_count, len(report.trades))
        np.testing.assert_allclose(result.net_profits, sum(report.profits()), rtol=0, atol=1e-8)


if __name__ == "__main__":
    unittest.main()
