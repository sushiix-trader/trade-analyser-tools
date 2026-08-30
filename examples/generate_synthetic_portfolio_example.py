"""Generate a deterministic two-strategy synthetic portfolio example.

The two source fixtures are deliberately MT5-shaped HTML reports so the
portfolio exercises the same public parsing, analysis, aggregation, and
interactive-rendering paths as user-supplied Strategy Tester reports. The
portfolio is equal-weighted at 50% / 50% and is not a real trading result.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

# The example scripts are intentionally runnable directly from the repository
# root. The first import works for ``python3 examples/...``; the fallback keeps
# importing this generator from a package/context also convenient.
try:  # noqa: E402
    from generate_synthetic_drawdown_example import (  # type: ignore[import-not-found]
        EPISODE_COUNT,
        build_positions,
        render_source_report,
    )
except ModuleNotFoundError:  # pragma: no cover - only used by package imports
    from examples.generate_synthetic_drawdown_example import (  # type: ignore[no-redef]
        EPISODE_COUNT,
        build_positions,
        render_source_report,
    )

from analyser import (  # noqa: E402
    PortfolioConfig,
    PortfolioMember,
    InteractiveReportConfig,
    analyze_portfolio,
    save_interactive_report,
)


PORTFOLIO_CAPITAL = 100_000.0
WEIGHT = 0.5
REPORT_NAME = "synthetic-portfolio-report.html"
MARKDOWN_NAME = "synthetic-portfolio-analysis.md"
SUMMARY_NAME = "synthetic-portfolio-drawdown-summary.csv"
EPISODES_NAME = "synthetic-portfolio-drawdown-episodes.csv"

MEMBERS = (
    {
        "key": "a",
        "source_name": "synthetic_portfolio_strategy_a.html",
        "strategy_name": "Synthetic Strategy A",
        "description": "Equal-weight synthetic drawdown strategy A",
        "symbol": "SYNTH_A",
        "variant": 0,
    },
    {
        "key": "b",
        "source_name": "synthetic_portfolio_strategy_b.html",
        "strategy_name": "Synthetic Strategy B",
        "description": "Equal-weight synthetic drawdown strategy B",
        "symbol": "SYNTH_B",
        "variant": 1,
    },
)


def generate(output_dir: Path) -> dict[str, Path]:
    """Write two source fixtures and the canonical portfolio artifacts."""

    output_dir.mkdir(parents=True, exist_ok=True)
    source_dir = output_dir / "sample_reports"
    source_dir.mkdir(parents=True, exist_ok=True)

    source_paths: dict[str, Path] = {}
    portfolio_members: list[PortfolioMember] = []
    for member in MEMBERS:
        positions = build_positions(member["variant"])
        source_path = source_dir / member["source_name"]
        source_path.write_text(
            render_source_report(
                positions,
                title=f"{member['strategy_name']} Drawdown Demonstrator",
                expert=member["strategy_name"],
                symbol=member["symbol"],
                purpose=(
                    f"Deterministic portfolio fixture with {EPISODE_COUNT} "
                    f"completed drawdown episodes"
                ),
            ),
            encoding="utf-8",
        )
        source_paths[member["key"]] = source_path
        portfolio_members.append(
            PortfolioMember(
                strategy_name=member["strategy_name"],
                description=member["description"],
                weight=WEIGHT,
                source=source_path,
            )
        )

    portfolio = analyze_portfolio(
        portfolio_members,
        PortfolioConfig(portfolio_initial_capital=PORTFOLIO_CAPITAL),
    )
    expected_weights = {member["strategy_name"]: WEIGHT for member in MEMBERS}
    for member in portfolio.members:
        if member.normalized_weight != WEIGHT:
            raise RuntimeError(
                f"expected {WEIGHT:.0%} weight for {member.strategy_name}, "
                f"got {member.normalized_weight:.6f}"
            )
        if member.raw_drawdown_analysis.completed_episode_count != EPISODE_COUNT:
            raise RuntimeError(
                f"expected {EPISODE_COUNT} episodes for {member.strategy_name}, "
                f"got {member.raw_drawdown_analysis.completed_episode_count}"
            )
    if portfolio.normalized_weights != {
        member.member_key: WEIGHT for member in portfolio.members
    }:
        raise RuntimeError(
            f"unexpected normalized portfolio weights: {portfolio.normalized_weights}"
        )
    if set(expected_weights) != {member.strategy_name for member in portfolio.members}:
        raise RuntimeError("portfolio members do not match the expected synthetic pair")
    if portfolio.drawdown_analysis.completed_episode_count < 1:
        raise RuntimeError("expected the allocated portfolio to contain drawdown episodes")

    outputs = {
        "strategy_a_source": source_paths["a"],
        "strategy_b_source": source_paths["b"],
        "report": output_dir / REPORT_NAME,
        "markdown": output_dir / MARKDOWN_NAME,
        "summary": output_dir / SUMMARY_NAME,
        "episodes": output_dir / EPISODES_NAME,
    }
    save_interactive_report(
        portfolio,
        outputs["report"],
        config=InteractiveReportConfig(
            title="Synthetic Equal-Weight Portfolio",
            description=(
                "A deterministic 50/50 portfolio of two synthetic strategies "
                "for exploring portfolio drawdown distributions."
            ),
            table_page_size=25,
        ),
    )
    outputs["markdown"].write_text(portfolio.to_markdown(), encoding="utf-8")
    outputs["summary"].write_text(
        portfolio.to_csv("drawdown_summary"),
        encoding="utf-8",
    )
    outputs["episodes"].write_text(
        portfolio.to_csv("drawdown_episodes"),
        encoding="utf-8",
    )
    return outputs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPOSITORY_ROOT / "results",
        help="directory for source fixtures and generated portfolio outputs",
    )
    args = parser.parse_args()
    outputs = generate(args.output_dir)
    for label, path in outputs.items():
        print(f"{label}: {path}")


if __name__ == "__main__":
    main()
