from __future__ import annotations

import importlib.util
import unittest
from datetime import datetime
from tempfile import TemporaryDirectory
from pathlib import Path

from analyser import (
    AnalysisConfig,
    PortfolioConfig,
    PortfolioMember,
    Report,
    Trade,
    TradeProfitConfig,
    TradeProfitGrouping,
    TradeProfitMeasure,
    TradeSide,
    analyze,
    analyze_portfolio,
    build_trade_profit_analysis,
    render_trade_profit_bar_chart,
    save_trade_profit_bar_charts,
)


def trade(
    ticket: str,
    opened: datetime,
    closed: datetime | None,
    profit: float,
    *,
    side: TradeSide = TradeSide.LONG,
    position_id: str | None = None,
    swap: float = 0.0,
    commission: float = 0.0,
) -> Trade:
    return Trade(
        ticket=ticket,
        symbol="TEST",
        side=side,
        volume=1.0,
        open_time=opened,
        close_time=closed,
        open_price=1.0,
        close_price=1.0,
        profit=profit,
        swap=swap,
        commission=commission,
        position_id=position_id,
    )


def report(name: str, trades: list[Trade], deposit: float = 1000.0) -> Report:
    return Report(
        trades=trades,
        initial_deposit=deposit,
        currency="USD",
        strategy_name=name,
        timezone="UTC",
    )


class TradeProfitAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.trades = [
            trade(
                "t1",
                datetime(2024, 1, 1, 9, 0),
                datetime(2024, 1, 2, 10, 0),
                100.0,
                position_id="p1",
                swap=-5.0,
                commission=-10.0,
            ),
            trade(
                "t2",
                datetime(2024, 1, 1, 10, 0),
                datetime(2024, 1, 3, 11, 0),
                -40.0,
                position_id="p2",
            ),
            trade(
                "t3",
                datetime(2024, 1, 2, 9, 0),
                datetime(2024, 1, 5, 12, 0),
                20.0,
                position_id="p3",
            ),
            trade(
                "t4",
                datetime(2024, 1, 3, 23, 0),
                datetime(2024, 1, 4, 0, 0),
                -10.0,
                position_id="p4",
            ),
        ]
        self.result = analyze(
            report("Grouped strategy", self.trades),
            AnalysisConfig(trade_profit=TradeProfitConfig(retain_trade_ids=True)),
        )

    def test_all_four_dimensions_are_eager_and_use_canonical_net_profit(self) -> None:
        grouped = self.result.trade_profit
        self.assertEqual(len(grouped.open_hour.buckets), 24)
        self.assertEqual(len(grouped.open_day_of_week.buckets), 7)
        self.assertEqual(grouped.open_hour.bucket("09:00").net_profit, 120.0)
        self.assertEqual(grouped.open_hour.bucket("10:00").net_profit, -40.0)
        self.assertEqual(grouped.open_hour.bucket("23:00").net_profit, -10.0)
        self.assertEqual(grouped.close_day_of_week.bucket("Tuesday").net_profit, 100.0)
        self.assertEqual(grouped.close_day_of_week.bucket("Wednesday").net_profit, -40.0)
        self.assertEqual(grouped.close_day_of_week.bucket("Thursday").net_profit, -10.0)
        self.assertEqual(grouped.close_day_of_week.bucket("Friday").net_profit, 20.0)
        self.assertEqual(grouped.open_hour.bucket("09:00").percentage_gain, 12.0)
        self.assertEqual(grouped.open_hour.bucket("09:00").gross_profit, 120.0)
        # The raw Trade.gross_profit for t1 is 115.00; grouped analytics must
        # never use it and must sum the canonical net profit of 100.00 instead.
        self.assertEqual(grouped.open_hour.bucket("09:00").gross_loss, 0.0)
        self.assertEqual(grouped.open_hour.bucket("09:00").trade_ids, ("t1", "t3"))
        self.assertEqual(grouped.open_hour.bucket("09:00").position_ids, ("p1", "p3"))
        self.assertEqual(grouped.open_hour.bucket("08:00").average_trade_profit, None)

    def test_missing_timestamp_only_excludes_affected_dimensions_and_warns(self) -> None:
        missing_close = trade(
            "missing-close",
            datetime(2024, 1, 8, 8, 0),
            None,
            25.0,
        )
        grouped = build_trade_profit_analysis(
            [missing_close],
            initial_capital=1000.0,
            currency="USD",
            timezone="UTC",
        )
        self.assertEqual(grouped.open_hour.bucket("08:00").trade_count, 1)
        self.assertEqual(grouped.close_hour.trade_count, 0)
        self.assertTrue(any(w.code == "trade_profit_missing_close_time" for w in grouped.warnings))
        self.assertTrue(any(w.code == "trade_profit_missing_close_time" for w in grouped.close_hour.warnings))
        self.assertFalse(any(w.code == "trade_profit_missing_open_time" for w in grouped.warnings))

    def test_result_serialization_preserves_eager_groupings(self) -> None:
        restored = type(self.result).from_dict(self.result.to_dict())
        self.assertEqual(self.result.to_json(), restored.to_json())
        self.assertEqual(restored.trade_profit.config.retain_trade_ids, True)
        self.assertEqual(
            restored.trade_profit.open_hour.bucket("09:00").trade_ids,
            ("t1", "t3"),
        )


class PortfolioTradeProfitTests(unittest.TestCase):
    def test_allocated_portfolio_and_raw_member_groupings_are_separate(self) -> None:
        with TemporaryDirectory() as directory:
            # The portfolio API is intentionally exercised through its public
            # source-based entry point, rather than manually combining numbers.
            left_path = Path(directory) / "left.xml"
            right_path = Path(directory) / "right.xml"
            left_path.write_text(
                "<report><initialDeposit>1000</initialDeposit><currency>USD</currency>"
                "<position><positionId>left</positionId><symbol>TEST</symbol><type>buy</type>"
                "<volume>1</volume><openTime>2024.01.01 09:00:00</openTime>"
                "<closeTime>2024.01.02 09:00:00</closeTime><profit>100</profit></position></report>",
                encoding="utf-8",
            )
            right_path.write_text(
                "<report><initialDeposit>1000</initialDeposit><currency>USD</currency>"
                "<position><positionId>right</positionId><symbol>TEST</symbol><type>buy</type>"
                "<volume>1</volume><openTime>2024.01.01 10:00:00</openTime>"
                "<closeTime>2024.01.02 10:00:00</closeTime><profit>200</profit></position></report>",
                encoding="utf-8",
            )
            result = analyze_portfolio(
                [
                    PortfolioMember("Left", "left", weight=0.5, source=left_path),
                    PortfolioMember("Right", "right", weight=0.5, source=right_path),
                ],
                PortfolioConfig(portfolio_initial_capital=1000.0),
            )
        self.assertEqual(result.trade_profit.open_hour.bucket("09:00").net_profit, 50.0)
        self.assertEqual(result.trade_profit.open_hour.bucket("10:00").net_profit, 100.0)
        self.assertEqual(result.trade_profit.open_hour.bucket("09:00").percentage_gain, 5.0)
        self.assertEqual(result.raw_trade_profit["Left"].open_hour.bucket("09:00").net_profit, 100.0)
        self.assertEqual(result.raw_trade_profit["Right"].open_hour.bucket("10:00").net_profit, 200.0)
        restored = type(result).from_dict(result.to_dict())
        self.assertEqual(result.to_json(), restored.to_json())


@unittest.skipUnless(importlib.util.find_spec("matplotlib"), "matplotlib is not installed")
class TradeProfitChartTests(unittest.TestCase):
    def test_bar_chart_is_deterministic_and_convenience_api_writes_eight_artifacts(self) -> None:
        result = analyze(report("Chart strategy", [
            trade("one", datetime(2024, 1, 1, 8), datetime(2024, 1, 2, 9), 50.0),
            trade("two", datetime(2024, 1, 1, 9), datetime(2024, 1, 2, 10), -25.0),
        ]))
        first = render_trade_profit_bar_chart(
            result,
            grouping=TradeProfitGrouping.OPEN_HOUR,
            measure=TradeProfitMeasure.NET_PROFIT,
        )
        second = render_trade_profit_bar_chart(
            result,
            grouping=TradeProfitGrouping.OPEN_HOUR,
            measure=TradeProfitMeasure.NET_PROFIT,
        )
        self.assertTrue(first.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(first, second)
        with TemporaryDirectory() as directory:
            artifacts = save_trade_profit_bar_charts(result, directory)
            self.assertEqual(len(artifacts), 8)
            self.assertTrue((Path(directory) / "opening-hour-net-profit.png").is_file())
            self.assertTrue((Path(directory) / "closing-day-of-week-percentage-gain.png").is_file())


if __name__ == "__main__":
    unittest.main()
