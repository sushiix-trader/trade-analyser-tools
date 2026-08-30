"""Generate the deterministic synthetic drawdown example in ``results/``.

The source fixture is deliberately MT5-shaped HTML rather than a separate CSV
format so it exercises the same public parsing path as a Strategy Tester
report.  The generated report is rendered only through the canonical
``analyze_file`` and ``save_interactive_report`` APIs.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
import sys

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from analyser import (  # noqa: E402
    AnalysisConfig,
    DEFAULT_REPORT_MONTE_CARLO_CONFIG,
    InteractiveReportConfig,
    analyze_file,
    run_monte_carlo,
    save_interactive_report,
)


INITIAL_DEPOSIT = 100_000.0
EPISODE_COUNT = 36
SOURCE_NAME = "synthetic_drawdown_36_episodes.html"
REPORT_NAME = "synthetic-drawdown-report.html"
MARKDOWN_NAME = "synthetic-drawdown-analysis.md"
SUMMARY_NAME = "synthetic-drawdown-summary.csv"
EPISODES_NAME = "synthetic-drawdown-episodes.csv"


@dataclass(frozen=True)
class SyntheticPosition:
    """One deterministic completed position for the source fixture."""

    ticket: int
    side: str
    volume: float
    open_time: datetime
    close_time: datetime
    open_price: float
    close_price: float
    profit: float
    comment: str


def build_positions(variant: int = 0) -> tuple[SyntheticPosition, ...]:
    """Build 36 recovered drawdown cycles with varied depth and duration.

    ``variant=0`` preserves the original single-report fixture exactly. Other
    deterministic variants are used by the synthetic portfolio example so each
    member has distinct source bytes and trade outcomes.
    """

    if variant < 0:
        raise ValueError("variant must be non-negative")
    positions: list[SyntheticPosition] = []
    cursor = datetime(2021, 1, 4, 9, 0) + timedelta(days=variant)
    equity = INITIAL_DEPOSIT
    ticket = 200_000

    for episode in range(EPISODE_COUNT):
        side = "buy" if (episode + variant) % 2 == 0 else "sell"
        volume = round(0.10 + (((episode * 3) + variant) % 5) * 0.10, 2)
        lead_profit = float(180 + ((episode + variant) % 8) * 35 + ((episode + variant) // 8) * 20)
        peak_value = round(equity + lead_profit, 2)

        depth_percent = 0.0020 + ((episode + variant) % 9) * 0.00035 + ((episode + variant) // 12) * 0.00025
        if (episode + variant) in {11, 23, 34}:
            depth_percent += 0.0040
        depth_money = round(peak_value * depth_percent, 2)
        leg_count = 1 + episode % 3
        leg_weights = {
            1: (1.0,),
            2: (0.55, 0.45),
            3: (0.45, 0.32, 0.23),
        }[leg_count]
        loss_legs: list[float] = []
        remaining_depth = depth_money
        for leg_index, weight in enumerate(leg_weights):
            loss = (
                remaining_depth
                if leg_index == len(leg_weights) - 1
                else round(depth_money * weight, 2)
            )
            loss_legs.append(loss)
            remaining_depth = round(remaining_depth - loss, 2)

        recovery_bonus = float(60 + ((episode + variant) % 5) * 25)
        recovery_profit = round(depth_money + recovery_bonus, 2)
        stages = [("new high-water mark", lead_profit, 1 + (episode % 3))]
        stages.extend(
            (
                f"drawdown leg {leg_index + 1}",
                -loss,
                1 + (((episode + variant) * (leg_index + 2)) % (4 + leg_index * 3)),
            )
            for leg_index, loss in enumerate(loss_legs)
        )
        stages.append(
            ("recovery above high-water mark", recovery_profit, 2 + (((episode + variant) * 5) % 18))
        )

        for stage_index, (comment, profit, holding_days) in enumerate(stages):
            open_time = cursor
            close_time = open_time + timedelta(days=holding_days, hours=6)
            open_price = round(
                1.04500 + (episode % 10) * 0.00300 + stage_index * 0.00035,
                5,
            )
            price_delta = profit / (100_000.0 * volume)
            direction = 1.0 if side == "buy" else -1.0
            close_price = round(open_price + direction * price_delta, 5)
            positions.append(
                SyntheticPosition(
                    ticket=ticket,
                    side=side,
                    volume=volume,
                    open_time=open_time,
                    close_time=close_time,
                    open_price=open_price,
                    close_price=close_price,
                    profit=float(profit),
                    comment=f"Episode {episode + 1:02d}: {comment}",
                )
            )
            ticket += 1
            cursor = close_time + timedelta(hours=12)
            equity = round(equity + profit, 2)

    return tuple(positions)


def _format_mt5_time(value: datetime) -> str:
    return value.strftime("%Y.%m.%d %H:%M:%S")


def render_source_report(
    positions: tuple[SyntheticPosition, ...],
    *,
    title: str = "Synthetic Drawdown Demonstrator",
    expert: str = "Synthetic Drawdown Demonstrator",
    symbol: str = "SYNTH_DD",
    purpose: str | None = None,
) -> str:
    """Render a compact MT5-style completed-position HTML report."""

    if not positions:
        raise ValueError("at least one synthetic position is required")
    if purpose is None:
        purpose = f"Deterministic fixture with {EPISODE_COUNT} completed drawdown episodes"
    start = positions[0].open_time.strftime("%Y.%m.%d")
    end = positions[-1].close_time.strftime("%Y.%m.%d")
    rows = []
    for position in positions:
        cells = (
            position.ticket,
            _format_mt5_time(position.open_time),
            _format_mt5_time(position.close_time),
            symbol,
            position.side,
            f"{position.volume:.2f}",
            f"{position.open_price:.5f}",
            f"{position.close_price:.5f}",
            f"{position.profit:.2f}",
            "0.00",
            "0.00",
            position.comment,
        )
        rows.append("<tr>" + "".join(f"<td>{escape(str(cell))}</td>" for cell in cells) + "</tr>")

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>{escape(title)}</title>
</head>
<body>
<table>
  <tr><td colspan="2"><b>Strategy Tester Report</b></td></tr>
  <tr><td>Expert:</td><td><b>{escape(expert)}</b></td></tr>
  <tr><td>Symbol:</td><td><b>{escape(symbol)}</b></td></tr>
  <tr><td>Period:</td><td><b>M30 ({start} - {end})</b></td></tr>
  <tr><td>Server:</td><td><b>Example Broker</b></td></tr>
  <tr><td>Currency:</td><td><b>USD</b></td></tr>
  <tr><td>Initial Deposit:</td><td><b>{INITIAL_DEPOSIT:,.2f}</b></td></tr>
  <tr><td>Purpose:</td><td><b>{escape(purpose)}</b></td></tr>
</table>

<table>
  <tr><td colspan="12"><b>Positions</b></td></tr>
  <tr>
    <th>Ticket</th><th>Open Time</th><th>Close Time</th><th>Symbol</th>
    <th>Type</th><th>Volume</th><th>Open Price</th><th>Close Price</th>
    <th>Profit</th><th>Swap</th><th>Commission</th><th>Comment</th>
  </tr>
  {"".join(rows)}
</table>
</body>
</html>
"""


def generate(output_dir: Path) -> dict[str, Path]:
    """Write the fixture and all user-facing example artifacts."""

    output_dir.mkdir(parents=True, exist_ok=True)
    source_dir = output_dir / "sample_reports"
    source_dir.mkdir(parents=True, exist_ok=True)
    positions = build_positions()
    source_path = source_dir / SOURCE_NAME
    source_path.write_text(render_source_report(positions), encoding="utf-8")

    result = analyze_file(source_path, AnalysisConfig())
    completed_count = result.drawdown_analysis.completed_episode_count
    if completed_count != EPISODE_COUNT:
        raise RuntimeError(
            f"expected {EPISODE_COUNT} completed drawdown episodes, got {completed_count}"
        )
    if len(result.report.trades) != len(positions):
        raise RuntimeError(
            f"expected {len(positions)} parsed positions, got {len(result.report.trades)}"
        )
    monte_carlo = run_monte_carlo(
        result.report,
        DEFAULT_REPORT_MONTE_CARLO_CONFIG,
    )

    outputs = {
        "source": source_path,
        "report": output_dir / REPORT_NAME,
        "markdown": output_dir / MARKDOWN_NAME,
        "summary": output_dir / SUMMARY_NAME,
        "episodes": output_dir / EPISODES_NAME,
    }
    save_interactive_report(
        result,
        outputs["report"],
        config=InteractiveReportConfig(
            title="Synthetic Drawdown Demonstrator",
            description=(
                "A deterministic MT5-style example containing 36 completed drawdown "
                "episodes for exploring depth × duration distributions."
            ),
            table_page_size=25,
        ),
        monte_carlo=monte_carlo,
    )
    outputs["markdown"].write_text(result.to_markdown(), encoding="utf-8")
    outputs["summary"].write_text(result.to_csv("drawdown_summary"), encoding="utf-8")
    outputs["episodes"].write_text(result.to_csv("drawdown_episodes"), encoding="utf-8")
    return outputs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPOSITORY_ROOT / "results",
        help="directory for the source fixture and generated example outputs",
    )
    args = parser.parse_args()
    outputs = generate(args.output_dir)
    for label, path in outputs.items():
        print(f"{label}: {path}")


if __name__ == "__main__":
    main()
