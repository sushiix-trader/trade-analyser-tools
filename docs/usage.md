# API Usage Guide

This guide contains installation and workflow details that are kept out of
the human-oriented project README. The public `analyser` API remains the
canonical interface for every workflow shown here.

## Install

```bash
pip install -e .

# Optional chart support
pip install -e ".[charts]"
```

## Quick start: analyse one report

```python
from analyser import AnalysisConfig, analyze_file

result = analyze_file("tester_report.htm", AnalysisConfig())

print(result.metrics.net_profit)
print(result.metrics.total_profit_pct)
print(result.metrics.profit_factor)
print(result.metrics.recovery_factor)
print(result.metrics.calmar_ratio)
print(result.metrics.custom_trade_event_sharpe)
print(result.metrics.daily_sharpe_ratio)
print(result.metrics.annualized_daily_sharpe_ratio)

for month in result.monthly:
    print(month.period, month.pnl, month.return_on_starting_equity)

# QuantAnalyzer-style year × Jan-Dec × YTD table.
for row in result.monthly_performance.rows:
    print(row.year, row.monthly_returns_pct, row.ytd_return_pct)
```

The API accepts a filesystem path, bytes, or a file-like object. It accepts
single-run MT5 `.htm`, `.html`, and XML reports. Completed closed positions are
the canonical trade unit. Optimization workbooks and unhydrated Git LFS
pointers are rejected explicitly.

Every analysis is eager and deterministic. Values can be retrieved later from
`result.metrics`, `result.monthly`, `result.monthly_drawdown`,
`result.monthly_performance`, `result.balance`, `result.equity`,
`result.warnings`, and `result.provenance`.

## Filter a strategy

Filters select whole completed positions using their **open time**, then all
metrics and curves are recalculated from the selected trades.

```python
from analyser import LongOnly

long_only = result.apply_filters(LongOnly())

print(long_only.metrics)
print(long_only.selection.selected_trade_keys)
print(long_only.selection.excluded_by_filter)
```

Available filters include:

- `LongOnly` and `ShortOnly`
- `OpenDateRangeFilter`
- `TimeOfDayFilter`
- Named Sydney, Tokyo, London, and New York `SessionFilter`
- `AllOf`, `AnyOf`, and `Not` composition

Temporal filters use the report timezone when available. If timestamps are
naive and no report timezone is present, provide an explicit IANA timezone with
`FilterConfig(report_timezone="...")`.

Filtering does not mutate the original result. Chained filters are re-evaluated
from the original report, so a trade cannot accidentally be filtered twice.

## What-if trade sizing

What-if sizing returns another normal `AnalysisResult`. The resized metrics,
monthly tables, drawdowns, equity charts, sample-period analysis, and portfolio
correlations can all be used by the rest of the API.

### Flat lots

```python
from analyser import WhatIfConfig

flat_lot = long_only.apply_what_if(
    WhatIfConfig.flat_lot(0.10)
)
```

### Percentage or dollar risk

```python
from analyser import InstrumentSpec, WhatIfConfig

spec = InstrumentSpec(
    symbol="EURUSD",
    tick_size=0.00001,
    tick_value=1.0,
    account_currency="USD",
)

risk_sized = long_only.apply_what_if(
    WhatIfConfig.percent_risk(
        1.0,
        initial_capital=100_000,
        instrument_spec=spec,
    )
)
```

The sizing modes—flat lot, percentage risk, and dollar risk—are mutually
exclusive. Risk sizing uses explicit entry and stop-loss prices and floors
volume to `0.01` lots. Missing or invalid stops are excluded with warnings and
an audit; if no eligible trades remain, the API raises `WhatIfError`.

What-if calculations do not fetch external datasets at runtime. Risk-based
sizing still requires static instrument metadata, and a historical-average tick
value is deterministic but approximate. Profit, swap, and commission are scaled
linearly from the report; broker-specific commission tiers, changing swap
schedules, trade-date FX conversion, spread, slippage, and other nonlinear
execution effects are not modelled.

## Build a portfolio

One report represents one strategy. Portfolio members remain separate and
trades are never netted. Weights are static capital allocations set at the
start of the portfolio.

```python
from analyser import PortfolioConfig, PortfolioMember, analyze_portfolio

portfolio = analyze_portfolio(
    [
        PortfolioMember(
            "Strategy A",
            "Long-only breakout",
            source="strategy_a.htm",
            weight=0.40,
            filters=LongOnly(),
        ),
        PortfolioMember(
            "Strategy B",
            "Mean-reversion strategy",
            source="strategy_b.xml",
            weight=0.35,
        ),
        PortfolioMember(
            "Strategy C",
            "Session strategy",
            source="strategy_c.htm",
            weight=0.25,
        ),
    ],
    PortfolioConfig(portfolio_initial_capital=100_000),
)

print(portfolio.metrics)
print(portfolio.monthly)
print(portfolio.monthly_drawdown)
print(portfolio.equity_matrix)
```

Portfolio results expose:

- Portfolio-level metrics and curves
- Per-strategy metrics and allocated curves
- Monthly return and contribution matrices
- Daily profit correlation and covariance
- Warnings for differing active periods

Reports must use a compatible currency and timezone. Filters, sample periods,
and what-if sizing belong on each `PortfolioMember` and are applied before
portfolio allocation.

### Daily profit correlation

```python
correlation = portfolio.correlations.daily_profit

print(correlation.matrix)
print(correlation.series)
print(correlation.allocated_series)
print(correlation.observations)
```

The correlation is based on daily realized net profit, aligned over the
strategies’ overlapping active dates. Undefined cells are returned as `None`
with diagnostics.

## In-sample and out-of-sample analysis

Both named periods are required before sample-period analysis is enabled.
Completed positions are classified by their open time using `[start, end)`
boundaries.

```python
from datetime import datetime
from analyser import AnalysisConfig, PeriodWindow, SamplePeriodConfig, analyze_file

periods = SamplePeriodConfig(
    windows={
        "in_sample": PeriodWindow(
            "in_sample", datetime(2011, 1, 1), datetime(2021, 1, 1)
        ),
        "out_of_sample": PeriodWindow(
            "out_of_sample", datetime(2021, 1, 1), datetime(2026, 1, 1)
        ),
    }
)

result = analyze_file(
    "tester_report.htm",
    AnalysisConfig(sample_periods=periods),
)

print(result.periods["in_sample"].metrics)
print(result.periods["out_of_sample"].metrics)
```

Use `suggest_sample_periods(source)` for conservative filename/folder
suggestions. Suggestions require explicit caller confirmation and are never
activated automatically.

## Monte Carlo estimates

Monte Carlo is an optional simulation over completed-position net profits. It
is separate from the primary report analysis and is deterministic for a fixed
configuration and seed.

```python
from analyser import MonteCarloConfig, run_monte_carlo_file

simulation = run_monte_carlo_file(
    "tester_report.htm",
    MonteCarloConfig(
        iterations=10_000,
        method="permutation",  # or "bootstrap"
        seed=42,
    ),
)

summary = simulation.summary()
print(summary["net_profit"])       # p5, p50, p95, mean, worst
print(summary["max_drawdown_money"])
print(summary["max_consecutive_wins"])
print(summary["max_consecutive_losses"])
```

- `permutation` preserves every historical trade and changes only its order.
- `bootstrap` samples historical outcomes with replacement.
- `p5` and `p95` provide the central 90% simulated interval, often described
  as the 5th-to-95th-percentile or 90% Monte Carlo range.
- Streak distributions are available for winning and losing streaks.

For simulated path charts, retain a bounded set of paths and use
`save_monte_carlo_paths()` with configurable percentile bands.

## Charts, exports, and caching

```python
from analyser import (
    AnalysisStore,
    save_correlation_heatmap,
    save_equity_drawdown_chart,
)

save_equity_drawdown_chart(result, "equity-drawdown.png")
save_correlation_heatmap(
    portfolio.correlations.daily_profit,
    "daily-profit-correlation.png",
)

print(result.to_json())
print(result.to_markdown())
print(result.to_csv("monthly"))
```

The equity chart contains equity and high-water-mark drawdown. The correlation
heat map uses the already-calculated matrix and displays undefined cells as
`N/A`.

For repeated retrieval, use the deterministic local store:

```python
store = AnalysisStore("data/analysis-cache")
artifact = store.analyze_or_load("tester_report.htm")

print(artifact.cache_hit)
print(artifact.result.metrics)
```

The cache key includes the report bytes and analysis configuration. Changed
reports or configurations create new artifacts rather than reusing stale
results.
