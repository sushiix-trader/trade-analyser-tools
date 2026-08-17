from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from analyser import AnalysisConfig, Report, SharpeConfig, Trade, TradeSide, analyze
from analyser.models import AccountPoint


def trade(
    ticket: str,
    close: datetime,
    profit: float,
    *,
    symbol: str = "X",
    bars: int | None = None,
    r_multiple: float | None = None,
) -> Trade:
    return Trade(
        ticket=ticket,
        symbol=symbol,
        side=TradeSide.LONG,
        volume=1.0,
        open_time=close - timedelta(hours=1),
        close_time=close,
        open_price=100.0,
        close_price=101.0,
        profit=profit,
        bars=bars,
        r_multiple=r_multiple,
    )


class MetricEdgeTests(unittest.TestCase):
    def test_hand_calculated_profit_and_drawdown(self) -> None:
        report = Report(
            initial_deposit=1000.0,
            trades=[
                trade("1", datetime(2024, 1, 2), 100.0),
                trade("2", datetime(2024, 1, 3), -50.0),
                trade("3", datetime(2024, 1, 4), 25.0),
            ],
        )
        result = analyze(report)
        self.assertAlmostEqual(result.metrics.net_profit, 75.0)
        self.assertAlmostEqual(result.metrics.final_balance, 1075.0)
        self.assertAlmostEqual(result.metrics.max_drawdown_money, 50.0)
        self.assertAlmostEqual(result.metrics.max_drawdown_pct, 4.5454545, places=5)
        self.assertAlmostEqual(result.metrics.profit_factor, 2.5)
        self.assertAlmostEqual(result.metrics.recovery_factor, 1.5)
        self.assertEqual(result.validation.status, "match")

    def test_undefined_metrics_are_none_and_diagnostic(self) -> None:
        result = analyze(Report(initial_deposit=1000.0))
        self.assertIsNone(result.metrics.custom_trade_event_sharpe)
        self.assertIsNone(result.metrics.calmar_ratio)
        codes = {warning.code for warning in result.warnings}
        self.assertIn("undefined_sharpe", codes)
        self.assertIn("undefined_calmar", codes)
        self.assertEqual(result.monthly, ())

    def test_source_equity_is_preferred_when_complete(self) -> None:
        report = Report(
            initial_deposit=1000.0,
            trades=[trade("1", datetime(2024, 1, 2), 100.0)],
            source_equity_points=[
                AccountPoint(datetime(2024, 1, 1), equity=1000.0),
                AccountPoint(datetime(2024, 1, 2), equity=1200.0),
            ],
        )
        result = analyze(report)
        self.assertEqual(result.equity.source, "source_report")
        self.assertEqual(result.equity.basis, "equity")
        self.assertEqual(result.metrics.final_equity, 1200.0)
        self.assertIsNotNone(result.source_equity)

    def test_months_without_trades_are_present(self) -> None:
        report = Report(
            initial_deposit=1000.0,
            trades=[
                trade("1", datetime(2024, 1, 2), 100.0),
                trade("2", datetime(2024, 3, 2), 100.0),
            ],
        )
        result = analyze(report)
        self.assertEqual([row.period for row in result.monthly], ["2024-01", "2024-02", "2024-03"])
        self.assertEqual(result.monthly[1].pnl, 0.0)
        self.assertEqual(result.monthly[1].trade_count, 0)

    def test_sharpe_configuration_changes_result_deterministically(self) -> None:
        report = Report(
            initial_deposit=1000.0,
            trades=[
                trade("1", datetime(2024, 1, 2), 100.0),
                trade("2", datetime(2024, 1, 3), -50.0),
                trade("3", datetime(2024, 1, 4), 25.0),
            ],
        )
        base = analyze(report)
        annualized = analyze(report, AnalysisConfig(sharpe=SharpeConfig(annualization_factor=252)))
        self.assertNotEqual(base.metrics.custom_trade_event_sharpe, annualized.metrics.custom_trade_event_sharpe)
        self.assertEqual(base.to_json(), analyze(report).to_json())

    def test_daily_and_annualized_daily_sharpe_use_calendar_end_of_day_returns(self) -> None:
        report = Report(
            initial_deposit=1000.0,
            trades=[
                trade("1", datetime(2024, 1, 2), 100.0),
                trade("2", datetime(2024, 1, 4), -50.0),
            ],
        )
        result = analyze(report)

        # Jan 1: initial flat day, Jan 2: +10%, Jan 3: flat,
        # Jan 4: -50 / 1100.
        expected_returns = [0.0, 0.10, 0.0, -50.0 / 1100.0]
        expected_daily = sum(expected_returns) / 4.0
        expected_std = (
            sum((value - expected_daily) ** 2 for value in expected_returns) / 4.0
        ) ** 0.5
        expected_daily_sharpe = expected_daily / expected_std

        self.assertEqual(result.metrics.daily_sharpe_observations, 4)
        self.assertAlmostEqual(result.metrics.daily_sharpe_ratio, expected_daily_sharpe)
        self.assertAlmostEqual(
            result.metrics.annualized_daily_sharpe_ratio,
            expected_daily_sharpe * (365.2425 ** 0.5),
        )
        self.assertEqual(result.metrics.daily_sharpe_annualization_factor, 365.2425)

    def test_quant_analyzer_style_monthly_table_compounds_ytd(self) -> None:
        report = Report(
            initial_deposit=1000.0,
            trades=[
                trade("1", datetime(2024, 1, 2), 100.0),
                trade("2", datetime(2024, 2, 2), -50.0),
                trade("3", datetime(2024, 3, 2), 25.0),
            ],
        )
        result = analyze(report)
        row = result.monthly_performance.row(2024)
        self.assertEqual(result.monthly_performance.month_labels[:3], ("Jan", "Feb", "Mar"))
        self.assertAlmostEqual(row.monthly_returns_pct[0], 10.0)
        self.assertAlmostEqual(row.monthly_returns_pct[1], -50.0 / 1100.0 * 100.0)
        self.assertAlmostEqual(row.monthly_returns_pct[2], 25.0 / 1050.0 * 100.0)
        self.assertAlmostEqual(row.ytd_return_pct, 7.5)
        self.assertIsNone(row.monthly_returns_pct[3])
        self.assertIn("YTD", result.to_csv("monthly_performance"))

    def test_screenshot_style_closed_position_metrics_are_exposed(self) -> None:
        report = Report(
            initial_deposit=1000.0,
            trades=[
                trade("1", datetime(2024, 1, 2), 100.0),
                trade("2", datetime(2024, 1, 3), -50.0),
                trade("3", datetime(2024, 1, 4), 25.0),
                trade("4", datetime(2024, 1, 5), -25.0),
            ],
        )
        result = analyze(report)
        metrics = result.metrics
        self.assertEqual(metrics.total_profit_pct, 5.0)
        self.assertEqual(metrics.wins_losses_ratio, 1.0)
        self.assertEqual(metrics.gross_profit_pct, 12.5)
        self.assertEqual(metrics.gross_loss_pct, -7.5)
        self.assertEqual(metrics.average_trade_pct, 1.25)
        self.assertEqual(metrics.largest_win_pct, 10.0)
        self.assertEqual(metrics.largest_loss_pct, -5.0)
        self.assertEqual(metrics.average_consecutive_wins, 1.0)
        self.assertEqual(metrics.average_consecutive_losses, 1.0)
        self.assertAlmostEqual(metrics.return_drawdown_ratio, 1.1)
        self.assertIsNotNone(metrics.z_score)
        self.assertIsNotNone(metrics.z_probability_pct)
        self.assertIsNotNone(metrics.sqn_score)
        self.assertIsNotNone(metrics.max_stagnation_days)
        self.assertEqual(metrics.max_position_exposure, 1)
        self.assertEqual(metrics.max_lots_exposure, 1.0)

    def test_optional_bars_and_r_metrics_are_computed_when_supplied(self) -> None:
        report = Report(
            initial_deposit=1000.0,
            trades=[
                trade("1", datetime(2024, 1, 2), 100.0, bars=4, r_multiple=1.0),
                trade("2", datetime(2024, 1, 3), -50.0, bars=8, r_multiple=-0.5),
                trade("3", datetime(2024, 1, 4), 25.0, bars=12, r_multiple=0.25),
            ],
        )
        metrics = analyze(report).metrics
        self.assertAlmostEqual(metrics.average_bars_in_trade, 8.0)
        self.assertAlmostEqual(metrics.average_bars_in_wins, 8.0)
        self.assertAlmostEqual(metrics.average_bars_in_losses, 8.0)
        self.assertAlmostEqual(metrics.r_expectancy, 0.25)
        self.assertAlmostEqual(metrics.r_expectancy_score, 0.25 * (3.0 ** 0.5))

    def test_unavailable_screenshot_metrics_are_explicitly_diagnostic(self) -> None:
        result = analyze(Report(initial_deposit=1000.0))
        self.assertIsNone(result.metrics.cancelled_expired_trades)
        self.assertIsNone(result.metrics.average_bars_in_trade)
        self.assertIsNone(result.metrics.r_expectancy)
        codes = {warning.code for warning in result.warnings}
        self.assertIn("undefined_cancelled_expired", codes)
        self.assertIn("undefined_bars_metrics", codes)
        self.assertIn("undefined_r_metrics", codes)

    def test_positions_exposure_counts_overlapping_closed_positions(self) -> None:
        report = Report(
            initial_deposit=1000.0,
            trades=[
                Trade(
                    "1", "X", TradeSide.LONG, 1.0,
                    datetime(2024, 1, 1), datetime(2024, 1, 3), 1, 1, 100,
                ),
                Trade(
                    "2", "X", TradeSide.LONG, 0.5,
                    datetime(2024, 1, 2), datetime(2024, 1, 4), 1, 1, 50,
                ),
            ],
        )
        metrics = analyze(report).metrics
        self.assertEqual(metrics.max_position_exposure, 2)
        self.assertAlmostEqual(metrics.max_lots_exposure, 1.5)

    def test_extended_result_round_trips_through_json_payload(self) -> None:
        report = Report(
            initial_deposit=1000.0,
            trades=[trade("1", datetime(2024, 1, 2), 100.0)],
        )
        result = analyze(report)
        restored = type(result).from_dict(result.to_dict())
        self.assertEqual(restored.monthly_performance, result.monthly_performance)
        self.assertEqual(restored.metrics.to_dict(), result.metrics.to_dict())

    def test_symbol_breakdown(self) -> None:
        report = Report(
            initial_deposit=1000.0,
            trades=[trade("1", datetime(2024, 1, 2), 100.0, symbol="A"), trade("2", datetime(2024, 1, 3), -25.0, symbol="B")],
        )
        result = analyze(report)
        self.assertEqual(result.by_symbol["A"]["net_profit"], 100.0)
        self.assertEqual(result.by_symbol["B"]["losing_trades"], 1)


if __name__ == "__main__":
    unittest.main()
