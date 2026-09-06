"""Tests for losing-trade equity and inter-loss gap clustering."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from analyser import (
    LosingOnly,
    Report,
    Trade,
    TradeSide,
    analyze,
    build_loss_clustering,
    render_interactive_report,
    render_loss_gap_histogram,
    render_loss_only_equity_chart,
    save_loss_gap_histogram,
    save_loss_only_equity_chart,
)
from analyser.filters import filter_from_dict
from analyser.loss_clustering import LossClusteringResult


def trade(
    ticket: str,
    opened: str,
    profit: float,
    side: TradeSide = TradeSide.LONG,
    *,
    close_offset_hours: float = 1.0,
) -> Trade:
    opened_at = datetime.fromisoformat(opened)
    closed_at = opened_at + timedelta(hours=close_offset_hours)
    return Trade(
        ticket=ticket,
        position_id=ticket,
        symbol="TEST",
        side=side,
        volume=1.0,
        open_time=opened_at,
        close_time=closed_at,
        open_price=1.0,
        close_price=1.0,
        profit=profit,
    )


def report(*trades: Trade) -> Report:
    return Report(
        initial_deposit=10_000.0,
        currency="USD",
        timezone="UTC",
        reported_metrics={"totalnetprofit": sum(item.profit for item in trades)},
        trades=list(trades),
    )


class LosingOnlyFilterTests(unittest.TestCase):
    def test_losing_only_selects_strict_losses(self) -> None:
        source = report(
            trade("w1", "2024-01-01T10:00:00", 100.0),
            trade("l1", "2024-01-02T10:00:00", -40.0),
            trade("be", "2024-01-03T10:00:00", 0.0),
            trade("l2", "2024-01-04T10:00:00", -10.0, TradeSide.SHORT),
        )
        filtered = analyze(source).apply_filters(LosingOnly())
        self.assertEqual([item.ticket for item in filtered.report.trades], ["l1", "l2"])
        self.assertEqual(filtered.metrics.net_profit, -50.0)

    def test_filter_from_dict_round_trip(self) -> None:
        restored = filter_from_dict({"type": "losing_only"})
        self.assertIsInstance(restored, LosingOnly)
        self.assertEqual(restored.to_dict(), {"type": "losing_only"})


class LossClusteringTests(unittest.TestCase):
    def test_known_sequence_curve_gaps_and_streak(self) -> None:
        source = report(
            trade("w1", "2024-01-01T10:00:00", 200.0),
            trade("l1", "2024-01-02T10:00:00", -50.0),
            trade("l2", "2024-01-04T10:00:00", -30.0),  # +2 days after l1 close
            trade("w2", "2024-01-05T10:00:00", 80.0),
            trade("l3", "2024-01-06T10:00:00", -20.0),
        )
        result = analyze(source)
        clustering = result.loss_clustering
        assert clustering is not None

        self.assertEqual(clustering.summary.loss_count, 3)
        self.assertEqual(clustering.summary.total_loss_money, 100.0)
        self.assertEqual(clustering.summary.max_consecutive_losses, 2)
        self.assertAlmostEqual(clustering.summary.mean_gap_days or 0.0, 2.0)
        self.assertAlmostEqual(clustering.summary.median_gap_days or 0.0, 2.0)

        # Baseline + three loss steps.
        self.assertEqual(len(clustering.curve.values), 4)
        self.assertEqual(clustering.curve.values[0], 10_000.0)
        self.assertEqual(clustering.curve.values[1], 9_950.0)
        self.assertEqual(clustering.curve.values[2], 9_920.0)
        self.assertEqual(clustering.curve.values[3], 9_900.0)

        self.assertEqual(len(clustering.gap_points), 2)
        self.assertAlmostEqual(clustering.gap_points[0].gap_days, 2.0)
        self.assertAlmostEqual(clustering.gap_points[0].magnitude, 30.0)
        self.assertAlmostEqual(clustering.gap_points[1].gap_days, 2.0)
        self.assertAlmostEqual(clustering.gap_points[1].magnitude, 20.0)

        self.assertEqual(clustering.events[0].gap_days_since_previous, None)
        self.assertEqual(clustering.events[0].consecutive_loss_streak, 1)
        self.assertEqual(clustering.events[1].consecutive_loss_streak, 2)
        self.assertEqual(clustering.events[2].consecutive_loss_streak, 1)

        self.assertIn("lt_1d", clustering.summary.share_gaps_below_days)
        self.assertIn("lt_7d", clustering.summary.share_gaps_below_days)
        self.assertEqual(clustering.summary.share_gaps_below_days["lt_1d"], 0.0)
        self.assertEqual(clustering.summary.share_gaps_below_days["lt_7d"], 1.0)
        self.assertEqual(clustering.gap_histogram.sample_count, 2)
        self.assertEqual(sum(bin.count for bin in clustering.gap_histogram.bins), 2)
        # Both gaps are exactly 2 days → land in the final closed [1, 2] bin.
        one_to_two = next(bin for bin in clustering.gap_histogram.bins if bin.left == 1.0)
        self.assertEqual(one_to_two.count, 2)
        self.assertEqual(one_to_two.right, 2.0)

    def test_same_timestamp_losses_preserve_sort_key_order(self) -> None:
        close = datetime(2024, 2, 1, 12, 0, 0)
        earlier_open = close - timedelta(hours=2)
        later_open = close - timedelta(hours=1)
        source = Report(
            initial_deposit=1_000.0,
            currency="USD",
            timezone="UTC",
            trades=[
                Trade(
                    ticket="b",
                    position_id="b",
                    symbol="TEST",
                    side=TradeSide.LONG,
                    volume=1.0,
                    open_time=later_open,
                    close_time=close,
                    open_price=1.0,
                    close_price=1.0,
                    profit=-20.0,
                ),
                Trade(
                    ticket="a",
                    position_id="a",
                    symbol="TEST",
                    side=TradeSide.LONG,
                    volume=1.0,
                    open_time=earlier_open,
                    close_time=close,
                    open_price=1.0,
                    close_price=1.0,
                    profit=-10.0,
                ),
            ],
        )
        clustering = build_loss_clustering(source)
        self.assertEqual([event.ticket for event in clustering.events], ["a", "b"])
        self.assertEqual(clustering.curve.values[-1], 970.0)

    def test_no_losses_empty_curve_and_warning(self) -> None:
        source = report(trade("w1", "2024-01-01T10:00:00", 50.0))
        clustering = build_loss_clustering(source)
        self.assertEqual(clustering.summary.loss_count, 0)
        self.assertEqual(clustering.events, ())
        self.assertEqual(clustering.gap_points, ())
        self.assertEqual(clustering.curve.values, (10_000.0,))
        self.assertTrue(any(item.code == "loss_clustering_no_losses" for item in clustering.warnings))

    def test_break_even_excluded_from_losses(self) -> None:
        source = report(
            trade("be", "2024-01-01T10:00:00", 0.0),
            trade("l1", "2024-01-02T10:00:00", -5.0),
        )
        clustering = build_loss_clustering(source)
        self.assertEqual(clustering.summary.loss_count, 1)
        self.assertEqual(clustering.events[0].ticket, "l1")

    def test_serialization_round_trip(self) -> None:
        source = report(
            trade("l1", "2024-01-01T10:00:00", -10.0),
            trade("l2", "2024-01-03T10:00:00", -15.0),
        )
        original = build_loss_clustering(source)
        restored = LossClusteringResult.from_dict(original.to_dict())
        self.assertEqual(restored.summary.loss_count, 2)
        self.assertEqual(restored.curve.values, original.curve.values)
        self.assertEqual(len(restored.gap_points), 1)
        self.assertEqual(restored.gap_histogram.sample_count, original.gap_histogram.sample_count)

    def test_chart_smoke(self) -> None:
        result = analyze(report(
            trade("l1", "2024-01-01T10:00:00", -10.0),
            trade("l2", "2024-01-03T10:00:00", -15.0),
        ))
        equity_png = render_loss_only_equity_chart(result)
        hist_png = render_loss_gap_histogram(result)
        self.assertTrue(equity_png.startswith(b"\x89PNG"))
        self.assertTrue(hist_png.startswith(b"\x89PNG"))
        with TemporaryDirectory() as tmp:
            equity_path = save_loss_only_equity_chart(result, Path(tmp) / "loss-equity.png")
            hist_path = save_loss_gap_histogram(result, Path(tmp) / "loss-gaps.png")
            self.assertTrue(equity_path.exists())
            self.assertTrue(hist_path.exists())

    def test_interactive_payload_contains_loss_clustering(self) -> None:
        import json
        import re

        result = analyze(report(
            trade("l1", "2024-01-01T10:00:00", -10.0),
            trade("l2", "2024-01-03T10:00:00", -25.0),
        ))
        page = render_interactive_report(result)
        self.assertIn('data-tab="losses"', page)
        self.assertIn('id="losses"', page)
        self.assertIn("loss_clustering", page)
        self.assertIn("renderLosses", page)
        self.assertIn("close time", page)
        self.assertIn("histogram", page.lower())
        self.assertIn("constant loss rate", page)
        self.assertIn("Frequency", page)
        self.assertIn("class='grid'", page)
        match = re.search(
            r'<script id="report-data" type="application/json">(.*?)</script>',
            page,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(match)
        payload = json.loads(match.group(1))
        clustering = payload["variants"]["all"]["loss_clustering"]
        self.assertEqual(clustering["summary"]["loss_count"], 2)
        self.assertEqual(clustering["summary"]["total_loss_money"], 35.0)
        self.assertEqual(clustering["gap_histogram"]["sample_count"], 1)


if __name__ == "__main__":
    unittest.main()
