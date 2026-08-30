from __future__ import annotations

from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from analyser import (
    AnalysisConfig,
    ChartConfig,
    PeriodWindow,
    PortfolioConfig,
    PortfolioMember,
    SamplePeriodConfig,
    SamplePeriodConfigurationError,
    analyze,
    render_equity_drawdown_chart,
    suggest_sample_periods,
)
from analyser.models import Report, Trade, TradeSide


def make_trade(ticket: str, opened: str, closed: str, profit: float) -> Trade:
    return Trade(
        ticket=ticket,
        position_id=ticket,
        symbol="TEST_SYMBOL",
        side=TradeSide.LONG,
        volume=1.0,
        open_time=datetime.fromisoformat(opened),
        close_time=datetime.fromisoformat(closed),
        open_price=1.0,
        close_price=1.0,
        profit=profit,
    )


def make_report(*trades: Trade) -> Report:
    return Report(
        trades=list(trades),
        initial_deposit=1000.0,
        currency="USD",
        timezone=None,
    )


def periods() -> SamplePeriodConfig:
    return SamplePeriodConfig(
        windows={
            "in_sample": PeriodWindow("in_sample", datetime(2024, 1, 1), datetime(2024, 2, 1)),
            "out_of_sample": PeriodWindow("out_of_sample", datetime(2024, 2, 1), datetime(2024, 3, 1)),
        }
    )


class SamplePeriodTests(unittest.TestCase):
    def test_period_analysis_is_eager_and_segment_relative(self) -> None:
        result = analyze(
            make_report(
                make_trade("is-1", "2024-01-02T10:00:00", "2024-01-03T10:00:00", 100.0),
                make_trade("is-2", "2024-01-10T10:00:00", "2024-01-11T10:00:00", -50.0),
                make_trade("oos-1", "2024-02-02T10:00:00", "2024-02-03T10:00:00", 25.0),
            ),
            AnalysisConfig(sample_periods=periods()),
        )

        self.assertEqual(set(result.periods), {"in_sample", "out_of_sample"})
        self.assertEqual(result.periods["in_sample"].metrics.total_trades, 2)
        self.assertEqual(result.periods["in_sample"].metrics.net_profit, 50.0)
        self.assertEqual(result.periods["out_of_sample"].metrics.total_trades, 1)
        self.assertEqual(result.periods["out_of_sample"].metrics.initial_deposit, 1050.0)
        self.assertAlmostEqual(result.periods["out_of_sample"].metrics.total_profit_pct, 25.0 / 1050.0 * 100)

    def test_cross_boundary_trade_is_retained_and_warned(self) -> None:
        result = analyze(
            make_report(
                make_trade("cross", "2024-01-31T10:00:00", "2024-02-02T10:00:00", 40.0),
                make_trade("oos", "2024-02-03T10:00:00", "2024-02-04T10:00:00", 10.0),
            ),
            AnalysisConfig(sample_periods=periods()),
        )
        period = result.periods["in_sample"]
        self.assertEqual(period.metrics.total_trades, 1)
        self.assertTrue(any(item.code == "sample_period_cross_boundary_trade" for item in period.warnings))

    def test_period_config_requires_both_reserved_windows(self) -> None:
        with self.assertRaises(SamplePeriodConfigurationError):
            SamplePeriodConfig(
                windows={
                    "in_sample": PeriodWindow("in_sample", datetime(2024, 1, 1), datetime(2024, 2, 1)),
                }
            )

    def test_folder_suggestions_are_conservative_and_not_automatically_active(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "is_2000-2010" / "oos_2010-2020" / "report.htm"
            path.parent.mkdir(parents=True)
            path.write_text("not parsed", encoding="utf-8")
            suggestions = suggest_sample_periods(path)
        self.assertIsNotNone(suggestions.in_sample)
        self.assertIsNotNone(suggestions.out_of_sample)
        self.assertEqual(suggestions.in_sample.window.source, "inferred")
        self.assertIsNone(suggestions.accepted)

    def test_period_aware_chart_adds_deterministic_overlays(self) -> None:
        result = analyze(
            make_report(
                make_trade("is", "2024-01-02T10:00:00", "2024-01-03T10:00:00", 10.0),
                make_trade("oos", "2024-02-02T10:00:00", "2024-02-03T10:00:00", 20.0),
            ),
            AnalysisConfig(sample_periods=periods()),
        )
        first = render_equity_drawdown_chart(
            result,
            title="periods",
            chart_config=ChartConfig(show_sample_periods=True),
        )
        second = render_equity_drawdown_chart(
            result,
            title="periods",
            chart_config=ChartConfig(show_sample_periods=True),
        )
        self.assertTrue(first.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(first, second)


class DailyProfitCorrelationTests(unittest.TestCase):
    def test_portfolio_eagerly_exposes_daily_profit_correlation(self) -> None:
        first = make_report(
            make_trade("a1", "2024-01-01T10:00:00", "2024-01-02T10:00:00", 100.0),
            make_trade("a2", "2024-01-02T10:00:00", "2024-01-03T10:00:00", -50.0),
            make_trade("a3", "2024-02-01T10:00:00", "2024-02-02T10:00:00", 20.0),
        )
        second = make_report(
            make_trade("b1", "2024-01-01T11:00:00", "2024-01-02T11:00:00", 50.0),
            make_trade("b2", "2024-01-02T11:00:00", "2024-01-03T11:00:00", -25.0),
            make_trade("b3", "2024-02-01T11:00:00", "2024-02-02T11:00:00", 10.0),
        )
        from analyser.portfolio import AnalyzedPortfolioMember, combine_analyses
        prepared = [
            AnalyzedPortfolioMember("a", PortfolioMember("A", "A"), analyze(first)),
            AnalyzedPortfolioMember("b", PortfolioMember("B", "B"), analyze(second)),
        ]
        result = combine_analyses(prepared, PortfolioConfig())
        daily = result.correlations.daily_profit
        self.assertEqual(daily.frequency, "daily")
        self.assertEqual(daily.matrix.row_labels, ("A", "B"))
        self.assertEqual(daily.observations, 3)
        self.assertAlmostEqual(daily.matrix.values[0][1], 1.0)
        self.assertEqual(daily.series["A"][0].profit, 100.0)
        self.assertEqual(result.daily_profit_correlation, daily.matrix)

        weekly = result.correlations.weekly_profit
        self.assertIsNotNone(weekly)
        assert weekly is not None
        self.assertEqual(weekly.frequency, "weekly")
        self.assertEqual(weekly.observations, 2)
        self.assertEqual(weekly.included_dates[0].isoformat(), "2024-01-01")
        self.assertEqual(weekly.series["A"][0].profit, 50.0)
        self.assertAlmostEqual(weekly.matrix.values[0][1], 1.0)
        self.assertEqual(result.weekly_profit_correlation, weekly)

    def test_period_correlations_are_exposed(self) -> None:
        p = periods()
        first = analyze(
            make_report(
                make_trade("a1", "2024-01-01T10:00:00", "2024-01-02T10:00:00", 100.0),
                make_trade("a2", "2024-02-02T10:00:00", "2024-02-03T10:00:00", 20.0),
            ),
            AnalysisConfig(sample_periods=p),
        )
        second = analyze(
            make_report(
                make_trade("b1", "2024-01-01T11:00:00", "2024-01-02T11:00:00", 50.0),
                make_trade("b2", "2024-02-02T11:00:00", "2024-02-03T11:00:00", 10.0),
            ),
            AnalysisConfig(sample_periods=p),
        )
        from analyser.portfolio import AnalyzedPortfolioMember, combine_analyses
        result = combine_analyses(
            [
                AnalyzedPortfolioMember("a", PortfolioMember("A", "A", sample_periods=p), first),
                AnalyzedPortfolioMember("b", PortfolioMember("B", "B", sample_periods=p), second),
            ],
            PortfolioConfig(),
        )
        self.assertIn("in_sample", result.periods)
        self.assertIn("out_of_sample", result.correlations.by_period)
        self.assertIn("out_of_sample", result.correlations.weekly_by_period)
        self.assertIsNotNone(result.periods["out_of_sample"].daily_profit_correlation)
        self.assertIsNotNone(result.periods["out_of_sample"].weekly_profit_correlation)


if __name__ == "__main__":
    unittest.main()

class PeriodCompositionAndSerializationTests(unittest.TestCase):
    def test_sample_periods_then_filter_preserves_segment_starting_capital(self) -> None:
        from analyser import LongOnly

        report = make_report(
            make_trade("long", "2024-01-02T10:00:00", "2024-01-03T10:00:00", 100.0),
            make_trade("short", "2024-01-10T10:00:00", "2024-01-11T10:00:00", -50.0),
            make_trade("oos", "2024-02-02T10:00:00", "2024-02-03T10:00:00", 25.0),
        )
        # Make the second trade short without changing the test helper's other fields.
        report.trades[1] = Trade(
            **{**report.trades[1].__dict__, "side": TradeSide.SHORT}
        )
        result = analyze(report).analyze_periods(periods(), filters=LongOnly())
        self.assertEqual(result.periods["in_sample"].metrics.total_trades, 1)
        self.assertEqual(result.periods["in_sample"].metrics.initial_deposit, 1000.0)
        self.assertEqual(result.periods["out_of_sample"].metrics.initial_deposit, 1050.0)

    def test_period_result_round_trip_is_deterministic(self) -> None:
        result = analyze(
            make_report(
                make_trade("is", "2024-01-02T10:00:00", "2024-01-03T10:00:00", 10.0),
                make_trade("oos", "2024-02-02T10:00:00", "2024-02-03T10:00:00", 20.0),
            ),
            AnalysisConfig(sample_periods=periods()),
        )
        restored = type(result).from_dict(result.to_dict())
        self.assertEqual(result.to_json(), restored.to_json())
        self.assertEqual(restored.periods["in_sample"].window.name, "in_sample")

    def test_undefined_daily_correlation_is_null_with_warning(self) -> None:
        from analyser.portfolio import AnalyzedPortfolioMember, combine_analyses

        first = analyze(make_report(make_trade("a", "2024-01-01T10:00:00", "2024-01-02T10:00:00", 1.0)))
        second = analyze(make_report(make_trade("b", "2024-01-01T11:00:00", "2024-01-02T11:00:00", 2.0)))
        result = combine_analyses(
            [
                AnalyzedPortfolioMember("a", PortfolioMember("A", "A"), first),
                AnalyzedPortfolioMember("b", PortfolioMember("B", "B"), second),
            ],
            PortfolioConfig(),
        )
        self.assertIsNone(result.daily_profit_correlation.values[0][1])
        self.assertTrue(any(item.code == "insufficient_daily_profit_observations" for item in result.warnings))


if __name__ == "__main__":
    unittest.main()

class PortfolioPeriodAlignmentTests(unittest.TestCase):
    def test_member_period_mismatch_warns_and_uses_intersection(self) -> None:
        from analyser.portfolio import AnalyzedPortfolioMember, combine_analyses

        first_periods = periods()
        second_periods = SamplePeriodConfig(
            windows={
                "in_sample": PeriodWindow("in_sample", datetime(2024, 1, 15), datetime(2024, 2, 1)),
                "out_of_sample": PeriodWindow("out_of_sample", datetime(2024, 2, 1), datetime(2024, 3, 15)),
            }
        )
        first = analyze(
            make_report(
                make_trade("a1", "2024-01-20T10:00:00", "2024-01-21T10:00:00", 10.0),
                make_trade("a2", "2024-02-10T10:00:00", "2024-02-11T10:00:00", 20.0),
            ),
            AnalysisConfig(sample_periods=first_periods),
        )
        second = analyze(
            make_report(
                make_trade("b1", "2024-01-20T11:00:00", "2024-01-21T11:00:00", 5.0),
                make_trade("b2", "2024-02-10T11:00:00", "2024-02-11T11:00:00", 10.0),
            ),
            AnalysisConfig(sample_periods=second_periods),
        )
        result = combine_analyses(
            [
                AnalyzedPortfolioMember("a", PortfolioMember("A", "A", sample_periods=first_periods), first),
                AnalyzedPortfolioMember("b", PortfolioMember("B", "B", sample_periods=second_periods), second),
            ],
            PortfolioConfig(),
        )
        self.assertEqual(result.periods["in_sample"].window.start, datetime(2024, 1, 15))
        self.assertTrue(any(item.code == "portfolio_sample_period_differs" for item in result.warnings))


if __name__ == "__main__":
    unittest.main()

class PeriodCacheTests(unittest.TestCase):
    def test_period_configuration_participates_in_single_report_cache(self) -> None:
        from analyser import AnalysisStore

        data = (
            b"<report><initialDeposit>1000</initialDeposit><currency>USD</currency>"
            b"<position><positionId>1</positionId><symbol>X</symbol><type>buy</type>"
            b"<openTime>2024.01.02 10:00:00</openTime><closeTime>2024.01.03 10:00:00</closeTime>"
            b"<profit>5</profit></position></report>"
        )
        with TemporaryDirectory() as directory:
            store = AnalysisStore(directory)
            first = store.analyze_or_load(data, AnalysisConfig(sample_periods=periods()))
            second = store.analyze_or_load(data, AnalysisConfig(sample_periods=periods()))
        self.assertFalse(first.cache_hit)
        self.assertTrue(second.cache_hit)
        self.assertEqual(first.result.to_json(), second.result.to_json())


if __name__ == "__main__":
    unittest.main()
