from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from analyser import AnalysisConfig, Report, SharpeConfig, Trade, TradeSide, analyze
from analyser.models import AccountPoint


def trade(ticket: str, close: datetime, profit: float, *, symbol: str = "X") -> Trade:
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
