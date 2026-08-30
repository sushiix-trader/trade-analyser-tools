from __future__ import annotations

from datetime import datetime
from tempfile import TemporaryDirectory
import unittest

from analyser import (
    AnalysisConfig,
    AnalysisStore,
    InstrumentSpec,
    PortfolioConfig,
    PortfolioMember,
    PeriodWindow,
    SamplePeriodConfig,
    WhatIfConfig,
    LongOnly,
    analyze,
    analyze_portfolio,
    render_correlation_heatmap,
)
from analyser.errors import WhatIfConfigurationError, WhatIfError
from analyser.models import Report, Trade, TradeSide


def make_trade(
    ticket: str,
    profit: float,
    *,
    volume: float = 1.0,
    sl: float | None = 1.0900,
    side: TradeSide = TradeSide.LONG,
    swap: float = 0.0,
    commission: float = 0.0,
) -> Trade:
    return Trade(
        ticket=ticket,
        position_id=ticket,
        symbol="TEST_SYMBOL",
        side=side,
        volume=volume,
        open_time=datetime(2024, 1, 1, 10, 0),
        close_time=datetime(2024, 1, 2, 10, 0),
        open_price=1.1000,
        close_price=1.1100 if profit > 0 else 1.0950,
        profit=profit,
        swap=swap,
        commission=commission,
        sl=sl,
    )


def make_report(*trades: Trade) -> Report:
    return Report(
        trades=list(trades),
        initial_deposit=10_000.0,
        currency="USD",
        timezone=None,
    )


def xml_source(ticket: str, profit: float, volume: float = 1.0) -> bytes:
    return f"""<report><initialDeposit>10000</initialDeposit><currency>USD</currency>
    <position><positionId>{ticket}</positionId><symbol>TEST_SYMBOL</symbol><type>buy</type>
    <volume>{volume}</volume><openTime>2024.01.01 10:00:00</openTime>
    <closeTime>2024.01.02 10:00:00</closeTime><openPrice>1.1</openPrice>
    <closePrice>1.11</closePrice><profit>{profit}</profit></position></report>""".encode()


SPEC = InstrumentSpec(
    symbol="TEST_SYMBOL",
    tick_size=0.0001,
    tick_value=10.0,
    account_currency="USD",
)


def sample_periods() -> SamplePeriodConfig:
    return SamplePeriodConfig(
        windows={
            "in_sample": PeriodWindow("in_sample", datetime(2024, 1, 1), datetime(2024, 2, 1)),
            "out_of_sample": PeriodWindow("out_of_sample", datetime(2024, 2, 1), datetime(2024, 3, 1)),
        }
    )


class WhatIfTests(unittest.TestCase):
    def test_flat_lot_replaces_volume_and_scales_all_trade_components(self) -> None:
        result = analyze(
            make_report(make_trade("flat", 120.0, volume=2.0, swap=-5.0, commission=-15.0)),
            AnalysisConfig(what_if=WhatIfConfig.flat_lot(0.50)),
        )

        trade = result.report.trades[0]
        self.assertEqual(trade.volume, 0.50)
        self.assertEqual(trade.profit, 30.0)
        self.assertEqual(trade.swap, -1.25)
        self.assertEqual(trade.commission, -3.75)
        self.assertEqual(result.metrics.net_profit, 30.0)
        self.assertEqual(result.what_if.transformed_trade_count, 1)
        self.assertEqual(result.what_if.audits[0].status, "sized")

    def test_percent_risk_uses_explicit_stop_and_floors_to_one_cent_lot(self) -> None:
        result = analyze(
            make_report(make_trade("risk", 200.0, volume=2.0)),
            AnalysisConfig(
                what_if=WhatIfConfig.percent_risk(
                    1.0,
                    initial_capital=10_000.0,
                    instrument_spec=SPEC,
                )
            ),
        )

        trade = result.report.trades[0]
        # $100 desired risk / ($1,000 risk per lot) = 0.10 lot.
        self.assertEqual(trade.volume, 0.1)
        self.assertEqual(trade.profit, 10.0)
        self.assertEqual(result.what_if.audits[0].risk_source, "explicit_stop")
        self.assertAlmostEqual(result.what_if.audits[0].calculated_risk_amount, 100.0)

    def test_historical_average_tick_value_is_explicitly_warned_and_serialized(self) -> None:
        average_spec = InstrumentSpec(
            symbol="TEST_SYMBOL",
            tick_size=0.0001,
            tick_value=10.0,
            account_currency="USD",
            tick_value_basis=InstrumentSpec.HISTORICAL_AVERAGE,
            tick_value_source="Example annual USD conversion averages",
            tick_value_reference_period="2023-01-01/2026-01-01",
        )
        result = analyze(
            make_report(make_trade("average", 200.0, volume=2.0)),
            AnalysisConfig(
                what_if=WhatIfConfig.percent_risk(
                    1.0,
                    initial_capital=10_000.0,
                    instrument_spec=average_spec,
                )
            ),
        )

        self.assertIn(
            "what_if_historical_average_tick_value",
            [warning.code for warning in result.warnings],
        )
        payload = result.what_if.to_dict()
        self.assertEqual(
            payload["config"]["instrument_spec"]["tick_value_basis"],
            "historical_average",
        )
        self.assertEqual(
            payload["config"]["instrument_spec"]["tick_value_reference_period"],
            "2023-01-01/2026-01-01",
        )

    def test_historical_average_tick_value_requires_provenance(self) -> None:
        with self.assertRaises(WhatIfConfigurationError):
            WhatIfConfig.percent_risk(
                1.0,
                instrument_spec=InstrumentSpec(
                    symbol="TEST_SYMBOL",
                    tick_size=0.0001,
                    tick_value=10.0,
                    account_currency="USD",
                    tick_value_basis=InstrumentSpec.HISTORICAL_AVERAGE,
                ),
            )

    def test_missing_stop_is_excluded_and_warned_but_other_trades_continue(self) -> None:
        result = analyze(
            make_report(
                make_trade("eligible", 100.0),
                make_trade("missing", 100.0, sl=None),
            ),
            AnalysisConfig(
                what_if=WhatIfConfig.dollar_risk(
                    100.0,
                    instrument_spec=SPEC,
                )
            ),
        )

        self.assertEqual([trade.ticket for trade in result.report.trades], ["eligible"])
        self.assertEqual(result.what_if.excluded_trade_count, 1)
        self.assertTrue(any(item.code == "what_if_missing_stop_excluded" for item in result.warnings))
        self.assertEqual(result.what_if.audits[1].status, "excluded")

    def test_no_eligible_risk_trades_raises(self) -> None:
        with self.assertRaises(WhatIfError):
            analyze(
                make_report(make_trade("missing", 100.0, sl=None)),
                AnalysisConfig(what_if=WhatIfConfig.dollar_risk(100.0, instrument_spec=SPEC)),
            )

    def test_percent_risk_above_one_hundred_percent_is_capped(self) -> None:
        config = WhatIfConfig.percent_risk(100.01, instrument_spec=SPEC)
        self.assertEqual(config.value, 100.0)

    def test_transformed_result_round_trips_and_original_result_is_unchanged(self) -> None:
        original = analyze(make_report(make_trade("round", 100.0, volume=2.0)))
        transformed = original.apply_what_if(WhatIfConfig.flat_lot(0.50))
        restored = type(transformed).from_dict(transformed.to_dict())

        self.assertEqual(original.report.trades[0].volume, 2.0)
        self.assertEqual(transformed.report.trades[0].volume, 0.50)
        self.assertEqual(transformed.to_json(), restored.to_json())
        self.assertIsNotNone(restored.source_report)
        self.assertEqual(restored.source_report.trades[0].volume, 2.0)

    def test_filter_after_what_if_restarts_from_original_and_does_not_double_size(self) -> None:
        report = make_report(
            make_trade("long", 100.0, volume=2.0, side=TradeSide.LONG),
            make_trade("short", 100.0, volume=2.0, side=TradeSide.SHORT, sl=1.1100),
        )
        result = analyze(report, AnalysisConfig(what_if=WhatIfConfig.flat_lot(0.50)))
        filtered = result.apply_filters(LongOnly())

        self.assertEqual(filtered.report.trades[0].volume, 0.50)
        self.assertEqual(filtered.metrics.net_profit, 25.0)

    def test_sample_periods_are_built_from_transformed_trades(self) -> None:
        oos_trade = Trade(
            **{
                **make_trade("oos", 100.0, volume=2.0).__dict__,
                "open_time": datetime(2024, 2, 2, 10, 0),
                "close_time": datetime(2024, 2, 3, 10, 0),
            }
        )
        result = analyze(
            make_report(
                make_trade("is", 100.0, volume=2.0),
                oos_trade,
            ),
            AnalysisConfig(
                sample_periods=sample_periods(),
                what_if=WhatIfConfig.flat_lot(0.50),
            ),
        )

        self.assertEqual(result.periods["in_sample"].analysis.report.trades[0].volume, 0.50)
        self.assertEqual(result.periods["out_of_sample"].analysis.report.trades[0].volume, 0.50)

    def test_applying_what_if_to_period_result_rebuilds_periods_once(self) -> None:
        oos_trade = Trade(
            **{
                **make_trade("oos", 100.0, volume=2.0).__dict__,
                "open_time": datetime(2024, 2, 2, 10, 0),
                "close_time": datetime(2024, 2, 3, 10, 0),
            }
        )
        result = analyze(
            make_report(
                make_trade("is", 100.0, volume=2.0),
                oos_trade,
            ),
            AnalysisConfig(sample_periods=sample_periods()),
        )
        transformed = result.apply_what_if(WhatIfConfig.flat_lot(0.50))

        self.assertEqual(transformed.periods["in_sample"].analysis.report.trades[0].volume, 0.50)
        self.assertEqual(transformed.periods["out_of_sample"].analysis.report.trades[0].volume, 0.50)

    def test_composed_transformations_are_deterministic_and_restart_from_source(self) -> None:
        oos_trade = Trade(
            **{
                **make_trade("oos", 50.0, volume=2.0).__dict__,
                "open_time": datetime(2024, 2, 2, 10, 0),
                "close_time": datetime(2024, 2, 3, 10, 0),
            }
        )
        report = make_report(
            make_trade("is", 100.0, volume=2.0),
            oos_trade,
        )
        sizing = WhatIfConfig.flat_lot(0.50)
        first = analyze(report, AnalysisConfig(sample_periods=sample_periods()))
        composed = first.apply_filters(LongOnly()).apply_what_if(sizing)
        equivalent = analyze(report).analyze_periods(
            sample_periods(), filters=LongOnly()
        ).apply_what_if(sizing)

        self.assertEqual(composed.to_json(), equivalent.to_json())
        self.assertEqual(composed.source_report.trades[0].volume, 2.0)
        self.assertEqual(composed.report.trades[0].volume, 0.50)
        self.assertEqual(composed.periods["in_sample"].analysis.report.trades[0].volume, 0.50)
        self.assertEqual(composed.periods["out_of_sample"].analysis.report.trades[0].volume, 0.50)

    def test_empty_period_remains_empty_when_full_result_is_sized(self) -> None:
        result = analyze(
            make_report(make_trade("is", 100.0, volume=2.0)),
            AnalysisConfig(
                sample_periods=sample_periods(),
                what_if=WhatIfConfig.flat_lot(0.50),
            ),
        )

        self.assertEqual(result.periods["out_of_sample"].analysis.metrics.total_trades, 0)
        self.assertEqual(result.periods["out_of_sample"].analysis.what_if.transformed_trade_count, 0)

    def test_period_risk_sizing_uses_original_capital_and_preserves_provenance(self) -> None:
        oos_trade = Trade(
            **{
                **make_trade("oos", 50.0, volume=2.0).__dict__,
                "open_time": datetime(2024, 2, 2, 10, 0),
                "close_time": datetime(2024, 2, 3, 10, 0),
            }
        )
        result = analyze(
            make_report(make_trade("is", 100.0, volume=2.0), oos_trade),
            AnalysisConfig(
                sample_periods=sample_periods(),
                what_if=WhatIfConfig.percent_risk(10.0, instrument_spec=SPEC),
            ),
        )

        out_of_sample = result.periods["out_of_sample"].analysis
        self.assertEqual(out_of_sample.what_if.capital_base, 10_000.0)
        self.assertEqual(out_of_sample.report.trades[0].volume, 1.0)
        self.assertIsNotNone(out_of_sample.source_report)
        self.assertEqual(out_of_sample.source_report.trades[0].volume, 2.0)
        self.assertEqual(out_of_sample.provenance["what_if"]["capital_base"], 10_000.0)

    def test_composed_period_filter_diagnostics_do_not_duplicate(self) -> None:
        oos_trade = Trade(
            **{
                **make_trade("oos", 50.0, volume=2.0).__dict__,
                "open_time": datetime(2024, 2, 2, 10, 0),
                "close_time": datetime(2024, 2, 3, 10, 0),
            }
        )
        result = analyze(
            make_report(make_trade("is", 100.0, volume=2.0), oos_trade),
            AnalysisConfig(sample_periods=sample_periods()),
        ).apply_filters(LongOnly())

        top_codes = [warning.code for warning in result.warnings]
        self.assertEqual(len(top_codes), len(set(top_codes)))
        self.assertNotIn("undefined_sharpe", top_codes)
        for period in result.periods.values():
            period_codes = [warning.code for warning in period.warnings]
            self.assertEqual(len(period_codes), len(set(period_codes)))

    def test_what_if_cache_key_and_round_trip_are_deterministic(self) -> None:
        source = xml_source("cached", 100.0, volume=2.0)
        config = AnalysisConfig(what_if=WhatIfConfig.flat_lot(0.50))
        with TemporaryDirectory() as directory:
            store = AnalysisStore(directory)
            first = store.analyze_or_load(source, config)
            second = store.analyze_or_load(source, config)

        self.assertFalse(first.cache_hit)
        self.assertTrue(second.cache_hit)
        self.assertEqual(first.result.to_json(), second.result.to_json())
        self.assertEqual(second.result.what_if.config.mode, "flat_lot")

    def test_portfolio_member_what_if_is_applied_before_allocation(self) -> None:
        first = xml_source("a", 100.0, volume=2.0)
        second = xml_source("b", 100.0, volume=1.0)
        result = analyze_portfolio(
            [
                PortfolioMember(
                    "A",
                    "flat member",
                    source=first,
                    weight=0.5,
                    what_if=WhatIfConfig.flat_lot(0.50),
                ),
                PortfolioMember("B", "unchanged member", source=second, weight=0.5),
            ],
            PortfolioConfig(),
        )

        self.assertEqual(result.members[0].analysis.report.trades[0].volume, 0.50)
        self.assertEqual(result.members[0].analysis.metrics.net_profit, 25.0)
        self.assertEqual(result.members[1].analysis.metrics.net_profit, 100.0)


class CorrelationHeatmapTests(unittest.TestCase):
    def test_heatmap_is_deterministic_and_uses_two_decimal_annotations(self) -> None:
        first = xml_source("a1", 10.0)
        second = xml_source("b1", 5.0)
        portfolio = analyze_portfolio(
            [
                PortfolioMember("A", "A", source=first),
                PortfolioMember("B", "B", source=second),
            ],
            PortfolioConfig(),
        )

        rendered = render_correlation_heatmap(portfolio.correlations.daily_profit)
        rendered_again = render_correlation_heatmap(portfolio.correlations.daily_profit)
        weekly = render_correlation_heatmap(
            portfolio.correlations.weekly_profit,
            image_format="svg",
        )
        self.assertTrue(rendered.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(rendered, rendered_again)
        self.assertIn(b"Weekly profit correlation heat map", weekly)


if __name__ == "__main__":
    unittest.main()
