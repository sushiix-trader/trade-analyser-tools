from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import io
import unittest

from analyser import (
    AllOf,
    AnyOf,
    AnalysisStore,
    FilterConfig,
    ForexSession,
    LongOnly,
    Not,
    OpenDateRangeFilter,
    Report,
    SessionFilter,
    ShortOnly,
    TimeOfDayFilter,
    Trade,
    TradeSide,
    analyze,
)
from analyser.errors import FilterConfigurationError


def trade(
    ticket: str,
    opened: str,
    profit: float,
    side: TradeSide,
    *,
    open_time_inferred: bool = False,
) -> Trade:
    opened_at = datetime.fromisoformat(opened) if opened else None
    closed_at = opened_at + timedelta(hours=1) if opened_at else datetime(2024, 1, 2, 12)
    return Trade(
        ticket=ticket,
        position_id=ticket,
        symbol="TEST_SYMBOL_A",
        side=side,
        volume=1.0,
        open_time=opened_at,
        close_time=closed_at,
        open_price=1.0,
        close_price=1.0,
        profit=profit,
        open_time_inferred=open_time_inferred,
    )


def report(*trades: Trade, timezone: str | None = "UTC") -> Report:
    return Report(
        initial_deposit=1000.0,
        currency="USD",
        timezone=timezone,
        reported_metrics={"totalnetprofit": sum(item.profit for item in trades)},
        trades=list(trades),
    )


class FilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = report(
            trade("long-09", "2024-01-02T09:00:00", 100.0, TradeSide.LONG),
            trade("short-13", "2024-01-02T13:00:00", -50.0, TradeSide.SHORT),
            trade("long-22", "2024-01-02T22:00:00", 25.0, TradeSide.LONG),
        )
        self.result = analyze(self.source)

    def test_long_only_recalculates_full_result_and_records_selection(self) -> None:
        filtered = self.result.apply_filters(LongOnly())

        self.assertEqual([item.ticket for item in filtered.report.trades], ["long-09", "long-22"])
        self.assertEqual(filtered.metrics.net_profit, 125.0)
        self.assertEqual(filtered.selection.source_trade_count, 3)
        self.assertEqual(filtered.selection.selected_trade_count, 2)
        self.assertEqual(filtered.selection.excluded_trade_keys, ("short-13",))
        self.assertEqual(filtered.selection.excluded_by_filter, {"long_only": 1})
        self.assertEqual(filtered.equity.source, "filtered_reconstructed_closed_positions")
        self.assertIsNone(filtered.source_equity)
        self.assertEqual(filtered.reported_metrics, {})
        self.assertEqual(filtered.validation.status, "not_applicable")
        self.assertEqual(filtered.source_reported_metrics, self.source.reported_metrics)

    def test_open_date_range_is_inclusive_exclusive_and_needs_no_timezone(self) -> None:
        filtered = self.result.apply_filters(
            OpenDateRangeFilter(date(2024, 1, 2), date(2024, 1, 3)),
            FilterConfig(),
        )
        self.assertEqual(filtered.selection.selected_trade_count, 3)

    def test_time_of_day_and_named_sessions_convert_from_report_timezone(self) -> None:
        self.assertEqual(SessionFilter("new york").session, ForexSession.NEW_YORK)
        london = self.result.apply_filters(SessionFilter(ForexSession.LONDON))
        self.assertEqual(london.selection.selected_trade_keys, ("long-09", "short-13"))

        london_morning = self.result.apply_filters(
            TimeOfDayFilter(time(9, 0), time(10, 0), timezone="Europe/London")
        )
        self.assertEqual(london_morning.selection.selected_trade_keys, ("long-09",))

    def test_overnight_windows_and_composition_are_supported(self) -> None:
        overnight = self.result.apply_filters(TimeOfDayFilter(time(22, 0), time(2, 0)))
        self.assertEqual(overnight.selection.selected_trade_keys, ("long-22",))

        not_long = self.result.apply_filters(Not(LongOnly()))
        self.assertEqual(not_long.selection.selected_trade_keys, ("short-13",))

        any_side = self.result.apply_filters(AnyOf(LongOnly(), ShortOnly()))
        self.assertEqual(any_side.selection.selected_trade_count, 3)

        london_longs = self.result.apply_filters(
            AllOf(LongOnly(), SessionFilter(ForexSession.LONDON))
        )
        self.assertEqual(london_longs.selection.selected_trade_keys, ("long-09",))

    def test_report_timezone_takes_precedence_with_a_warning(self) -> None:
        report_with_zone = report(
            trade("1", "2024-01-02T09:00:00", 1.0, TradeSide.LONG),
            timezone="Australia/Brisbane",
        )
        filtered = analyze(report_with_zone).apply_filters(
            TimeOfDayFilter(time(8, 0), time(10, 0)),
            FilterConfig(report_timezone="UTC"),
        )
        self.assertEqual(filtered.selection.selected_trade_count, 1)
        self.assertTrue(any(item.code == "filter_config_timezone_overridden" for item in filtered.warnings))

    def test_invalid_filter_configuration_is_rejected(self) -> None:
        with self.assertRaises(FilterConfigurationError):
            FilterConfig("")
        with self.assertRaises(FilterConfigurationError):
            OpenDateRangeFilter("2024-01-01", "2024-01-02")
        with self.assertRaises(FilterConfigurationError):
            TimeOfDayFilter(datetime(2024, 1, 1), time(1, 0))
        with self.assertRaises(FilterConfigurationError):
            AllOf(LongOnly(), object())
        with self.assertRaises(FilterConfigurationError):
            Not(object())

    def test_timezone_aware_datetime_range_uses_boundary_timezone(self) -> None:
        report_zone = report(
            trade("report-zone", "2024-01-02T00:00:00+00:00", 1.0, TradeSide.LONG),
            timezone="America/New_York",
        )
        report_zone_filtered = analyze(report_zone).apply_filters(
            OpenDateRangeFilter(
                datetime(2024, 1, 1, 18, 0),
                datetime(2024, 1, 1, 20, 0),
            )
        )
        self.assertEqual(report_zone_filtered.selection.selected_trade_keys, ("report-zone",))

        aware = report(
            trade("utc", "2024-01-02T00:00:00+00:00", 1.0, TradeSide.LONG),
            timezone=None,
        )
        filtered = analyze(aware).apply_filters(
            OpenDateRangeFilter(
                datetime.fromisoformat("2024-01-01T19:00:00-05:00"),
                datetime.fromisoformat("2024-01-01T20:00:00-05:00"),
            )
        )
        self.assertEqual(filtered.selection.selected_trade_keys, ("utc",))

    def test_session_filter_requires_source_timezone_for_naive_report_timestamps(self) -> None:
        no_timezone = analyze(
            report(
                trade("1", "2024-01-02T09:00:00", 1.0, TradeSide.LONG),
                timezone=None,
            )
        )
        with self.assertRaises(FilterConfigurationError):
            no_timezone.apply_filters(SessionFilter(ForexSession.LONDON))

        configured = no_timezone.apply_filters(
            SessionFilter(ForexSession.LONDON),
            FilterConfig(report_timezone="UTC"),
        )
        self.assertEqual(configured.selection.selected_trade_count, 1)

    def test_missing_open_time_is_excluded_with_a_warning(self) -> None:
        missing = analyze(
            report(
                trade("missing", "", 10.0, TradeSide.LONG, open_time_inferred=True),
                trade("valid", "2024-01-02T09:00:00", 20.0, TradeSide.LONG),
            )
        )
        filtered = missing.apply_filters(TimeOfDayFilter(time(8, 0), time(10, 0)))
        self.assertEqual(filtered.selection.selected_trade_keys, ("valid",))
        self.assertTrue(any(item.code == "filter_missing_open_time" for item in filtered.warnings))

    def test_empty_selection_is_valid_and_warns(self) -> None:
        filtered = self.result.apply_filters(ShortOnly())
        empty = filtered.apply_filters(LongOnly())
        self.assertEqual(empty.selection.selected_trade_count, 0)
        self.assertEqual(empty.monthly, ())
        self.assertIsNone(empty.metrics.custom_trade_event_sharpe)
        self.assertTrue(any(item.code == "no_trades_selected" for item in empty.warnings))

    def test_chaining_restarts_from_original_report(self) -> None:
        long_only = self.result.apply_filters(LongOnly())
        chained = long_only.apply_filters(TimeOfDayFilter(time(21, 0), time(23, 0)))
        self.assertEqual(chained.selection.source_trade_count, 3)
        self.assertEqual(chained.selection.selected_trade_keys, ("long-22",))
        self.assertEqual(chained.metrics.net_profit, 25.0)
        self.assertIsNotNone(chained.source_report)
        self.assertEqual(chained.validation.checks["source_validation"]["status"], "match")

    def test_filtered_result_serializes_and_cache_retrieves(self) -> None:
        filtered = self.result.apply_filters(LongOnly())
        restored = type(filtered).from_dict(filtered.to_dict())
        self.assertEqual(filtered.to_json(), restored.to_json())

        with TemporaryDirectory() as directory:
            store = AnalysisStore(directory)
            first = store.filter_or_load(self.result, LongOnly())
            second = store.filter_or_load(self.result, LongOnly())
            self.assertFalse(first.cache_hit)
            self.assertTrue(second.cache_hit)
            self.assertEqual(first.result.to_json(), second.result.to_json())
            self.assertTrue(Path(second.path).exists())

            first_from_source = store.analyze_filtered_or_load(
                io.BytesIO(
                    b"<report><initialDeposit>1000</initialDeposit><currency>USD</currency>"
                    b"<position><positionId>1</positionId><symbol>X</symbol><type>buy</type>"
                    b"<openTime>2024.01.01 10:00:00</openTime><closeTime>2024.01.02 10:00:00</closeTime>"
                    b"<profit>5</profit></position></report>"
                ),
                LongOnly(),
            )
            second_from_source = store.analyze_filtered_or_load(
                io.BytesIO(
                    b"<report><initialDeposit>1000</initialDeposit><currency>USD</currency>"
                    b"<position><positionId>1</positionId><symbol>X</symbol><type>buy</type>"
                    b"<openTime>2024.01.01 10:00:00</openTime><closeTime>2024.01.02 10:00:00</closeTime>"
                    b"<profit>5</profit></position></report>"
                ),
                LongOnly(),
            )
            self.assertFalse(first_from_source.cache_hit)
            self.assertTrue(second_from_source.cache_hit)

    def test_private_real_report_supports_side_filter_when_configured(self) -> None:
        configured = os.environ.get("MT5_FIXTURE_REPORT")
        if not configured:
            self.skipTest("set MT5_FIXTURE_REPORT to run the private filter smoke test")
        path = Path(configured)
        if not path.exists():
            self.skipTest("configured private MT5 fixture is not available")
        filtered = analyze(__import__("analyser").load_report(path)).apply_filters(LongOnly())
        self.assertLessEqual(filtered.selection.selected_trade_count, filtered.selection.source_trade_count)
        self.assertEqual(filtered.validation.status, "not_applicable")


if __name__ == "__main__":
    unittest.main()
