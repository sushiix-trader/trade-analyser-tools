---
name: mt5-report-analysis
description: Use the canonical analyser API when the user asks to analyze, compare, filter, resize, split into in-sample/out-of-sample periods, combine, correlate, chart, export, cache, or run Monte Carlo on MetaTrader 5 Strategy Tester XML/HTML reports. Trigger for metric questions, portfolio questions, what-if sizing, and how-to guidance for this repository.
---

# MT5 report analysis

Use this repository's `analyser` package as the only user-facing interface for
trading-report work. Translate natural-language requests into its typed API;
do not write a one-off parsing, metric, portfolio, chart, or simulation script.
Keep the workflow deterministic, eager, reproducible, and analysis-only.

## Default workflow

1. Identify the operation: one report, report comparison, filtered report,
   sample-period analysis, portfolio, what-if sizing, chart/export, cache
   retrieval, or Monte Carlo.
2. Accept a path, bytes, or file-like object. Use the public `analyze_file()`
   or `load_report()`/`analyze()` seam as appropriate.
3. Apply transformations through typed configuration and result methods. The
   canonical order is sample-period classification, member-level filtering,
   what-if sizing, and then metrics/curves/portfolio allocation.
4. Return tables or typed result fields, and always surface warnings,
   validation, provenance, and undefined metrics rather than hiding them.
5. Use the existing serializers and chart APIs for artifacts. Add reusable
   package behavior and a regression test when the requested capability does
   not yet exist.

Canonical single-report entry point:

```python
from analyser import AnalysisConfig, analyze_file
result = analyze_file(source, AnalysisConfig())
```

## Question routing

### Single-report questions

For requests such as:

- “Analyze this MT5 report.”
- “Give me the result as a metrics table.”
- “What are the return, CAGR, max drawdown, Sharpe, Calmar, recovery factor,
  profit factor, SQN, win rate, average win, and average loss?”
- “Which was the best or worst month?”
- “Show monthly returns and monthly drawdown.”
- “Compare the MT5-reported metrics with the recalculated metrics.”
- “Which metrics are undefined, and why?”

Use `result.metrics`, `result.monthly`, `result.monthly_drawdown`,
`result.monthly_performance`, `result.balance`, `result.equity`,
`result.validation`, `result.warnings`, and `result.provenance`.

For Sharpe questions, keep the definitions separate:
- `result.metrics.custom_trade_event_sharpe` is the unannualized closed-trade
  event Sharpe.
- `result.metrics.daily_sharpe_ratio` is the daily Sharpe from reconstructed
  calendar end-of-day equity, retaining flat no-trade days.
- `result.metrics.annualized_daily_sharpe_ratio` applies the configured daily
  annualization factor, which defaults to `365.2425` for calendar-day data.
- Configure `SharpeConfig(daily_risk_free_rate=...,
  daily_annualization_factor=...)` when a different daily risk-free rate or
  annualization convention is required.
QuantAnalyzer-style fields are exposed on `result.metrics`, including returns,
drawdowns, return/drawdown ratios, win/loss and payout ratios, gross/average/
largest trade values, streaks, AHPR, daily/monthly/yearly averages,
stagnation, exposure, z-score, and SQN. R-expectancy and bars-per-trade
metrics are `None` with diagnostics unless explicit R/bar inputs exist.

Use the result serializers instead of formatting ad hoc data:

```python
result.to_dict()
result.to_json()          # deterministic JSON
result.to_csv("monthly")
result.to_markdown()
```

### XML/HTML comparison

For “do these XML and HTML reports contain the same trades?” or “what differs
between these reports?”, use:

```python
from analyser import compare_reports
comparison = compare_reports(left, right)
```

Do not analyze both files and manually compare numbers. The comparison is the
canonical XML/HTML equivalence check.

### Filters

For long-only, short-only, date, time-of-day, or named Forex-session questions,
use `result.apply_filters()` and typed predicates:

```python
from datetime import time
from analyser import (
    AllOf, FilterConfig, ForexSession, LongOnly, SessionFilter,
    TimeOfDayFilter,
)

filtered = result.apply_filters(
    AllOf(
        LongOnly(),
        SessionFilter(ForexSession.LONDON),
        TimeOfDayFilter(time(8, 0), time(12, 0), timezone="Europe/London"),
    ),
    FilterConfig(report_timezone="UTC"),
)
```

Available v1 predicates are `LongOnly`, `ShortOnly`,
`OpenDateRangeFilter`, `TimeOfDayFilter`, and named Sydney, Tokyo, London, and
New York `SessionFilter`; compose them with `AllOf`, `AnyOf`, and `Not`.
Boundaries are `[start, end)`. Filters use canonical completed positions and
`open_time`, preserve the original report, reconstruct filtered curves, and
record selection/audit metadata. Chained filters restart from the original
report. The parsed report timezone is authoritative; otherwise temporal
filters require an explicit IANA timezone in `FilterConfig`.

For portfolio members, apply `filters=` and `filter_config=` on each
`PortfolioMember`; filtering happens before allocation.

### In-sample/out-of-sample questions

For requests such as:

- “Use 2011–2020 as in-sample and 2021–2025 as out-of-sample.”
- “Compare IS, OOS, and full-sample performance.”
- “Label the IS and OOS regions on the equity chart.”
- “Suggest periods from the filename, but ask me before activating them.”

Use typed periods, not manual trade slicing:

```python
from datetime import datetime
from analyser import AnalysisConfig, PeriodWindow, SamplePeriodConfig

periods = SamplePeriodConfig(windows={
    "in_sample": PeriodWindow("in_sample", datetime(2011, 1, 1), datetime(2021, 1, 1)),
    "out_of_sample": PeriodWindow("out_of_sample", datetime(2021, 1, 1), datetime(2026, 1, 1)),
})
config = AnalysisConfig(sample_periods=periods)
```

Both named windows are required. Suggestions from `suggest_sample_periods()`
are conservative and require explicit caller confirmation. Periods classify
completed positions by `open_time`, use `[start, end)` boundaries, retain
cross-boundary closes with warnings, and expose
`result.periods["in_sample"]` and `result.periods["out_of_sample"]`.
Use `result.analyze_periods(periods, filters=...)` when filtering is also
requested. Use `ChartConfig(show_sample_periods=True)` with
`save_equity_drawdown_chart()` for labelled period and excluded bands.

### Portfolio questions

For requests such as:

- “Combine these reports into a portfolio.”
- “Use weights of 50%, 30%, and 20%.”
- “Show each strategy separately and the combined portfolio.”
- “Which strategy contributed most?”
- “Create the portfolio equity and drawdown chart.”

Use `PortfolioMember` and `analyze_portfolio()`:

```python
from analyser import PortfolioConfig, PortfolioMember, analyze_portfolio

portfolio = analyze_portfolio(
    [
        PortfolioMember("Strategy A", "Description A", source=source_a, weight=0.6),
        PortfolioMember("Strategy B", "Description B", source=source_b, weight=0.4),
    ],
    PortfolioConfig(portfolio_initial_capital=100_000),
)
```

One report is one strategy. Use static capital-allocation weights, keep
member trades separate, and never net trades across strategies. Retrieve
portfolio metrics and tables from `portfolio.metrics`, `portfolio.monthly`,
`portfolio.monthly_drawdown`, `portfolio.monthly_performance`, and the
labelled portfolio matrices. Keep each member's filters, what-if configuration,
and sample periods on the member. Require compatible currencies and
report/broker timezones; reject incompatible portfolio inputs rather than
silently mixing them. If member period boundaries differ, use the supported
intersection behavior and surface the warning.

### Daily profit correlation

For “calculate profit correlation”, “show a correlation table”, or “create a
heat map”, use the eager portfolio result:

```python
correlation = portfolio.correlations.daily_profit
```

It contains raw daily realized-profit series, capital-scaled allocated series,
a labelled matrix, observation counts, diagnostics, and period-scoped results
in `correlations.by_period`. Dates use broker/report timestamps and align over
overlapping active dates. Undefined cells are `None` with diagnostics. Use
`save_correlation_heatmap(correlation, destination)` for the deterministic
fixed `[-1, 1]` heat-map artifact with two-decimal labels and grey undefined
cells.

### What-if sizing

For requests such as:

- “What if every trade used 0.10 lots?”
- “What if every trade risked 1% of initial capital?”
- “What if every trade risked $100?”
- “Show resized metrics, drawdown, monthly returns, and the equity chart.”
- “Which trades were excluded because they had no stop?”

Use `WhatIfConfig` and return a normal `AnalysisResult`:

```python
from analyser import WhatIfConfig
sized = result.apply_what_if(WhatIfConfig.flat_lot(0.10))
```

The mutually exclusive modes are flat-lot, percentage-risk, and dollar-risk.
What-if sizing occurs after sample-period/filter selection and before portfolio
allocation, scaling profit, swap, and commission consistently. Percentage risk
uses fixed original initial capital, is capped at 100%, and does not compound;
volume is floored to `0.01` lots. Risk sizing requires explicit `InstrumentSpec`
and explicit stops. Missing/invalid stops are excluded with warnings and an
audit; if no eligible trades remain, raise `WhatIfError`. Preserve
`result.source_report`, `result.what_if`, and deterministic provenance.

What-if calculations do not fetch external market, broker, OHLC, or FX datasets
at runtime. The caller must still provide the static instrument metadata needed
for monetary risk conversion. A fixed historical-average tick value is
reproducible but approximate and must retain its source/reference-period
provenance. Resizing scales the report's recorded profit, swap, and commission
linearly; it does not model broker commission tiers, minimum charges, changing
swap schedules, trade-date currency conversion, slippage, spread, or other
nonlinear execution effects. Treat this as a documented what-if limitation,
not an exact broker-account reconstruction.

For portfolios, put `what_if=` on each `PortfolioMember`, not on combined
numbers.

### Charts, cache, and exports

For “create an equity/drawdown chart”, use:

```python
from analyser import save_equity_drawdown_chart
save_equity_drawdown_chart(result, destination)
```

The chart uses the already-calculated selected equity curve and high-water-mark
drawdown; it does not recalculate analysis. For repeated retrieval, use:

```python
from analyser import AnalysisStore
artifact = AnalysisStore("data/analysis-cache").analyze_or_load(source)
result = artifact.result
```

Use `artifact.cache_hit` to report whether eager analysis or cache retrieval
occurred. Use `AnalysisStore.filter_or_load()` or
`analyze_filtered_or_load()` for deterministic filtered retrieval, and
`AnalysisStore.analyze_portfolio_or_load()` for portfolios. Cache keys include
source bytes and configuration.

### Monte Carlo

For “run a deterministic Monte Carlo test”, “permute trade order”, “bootstrap
trades”, or “skip a percentage of trades”, use the public one-strategy API:

```python
from analyser import MonteCarloConfig, run_monte_carlo_file

simulation = run_monte_carlo_file(
    source,
    MonteCarloConfig(iterations=10_000, method="permutation", seed=42),
)
```

Monte Carlo operates on completed-position net profits. Permutation preserves
the historical trade set and changes order; bootstrap is an explicit
alternative. Fixed configuration and seed must produce identical results.
Use `summary()`, aligned result arrays, and `to_json()`.

For a simulated-path visual rather than a histogram, set
`MonteCarloConfig(retain_paths=True, path_count=...)` and use
`save_monte_carlo_paths()` with `MonteCarloPathChartConfig` and one or more
`MonteCarloPathInterval` values. Each retained path is drawn, and configured
percentile bands are calculated at each simulated trade step for both equity and
high-water-mark drawdown. `path_count` is a deterministic evenly-spaced subset
of iterations; it bounds memory and rendering cost. Set `show_streaks=True`
to add winning- and losing-streak panels using the same bands. The simulation
result exposes max consecutive win/loss arrays and retained current-streak
paths. Streaks use completed-position net profit: positive extends wins,
negative extends losses, and zero resets both.

## Contract guardrails

- Accept MT5 single-run HTML/HTM and XML reports, paths, bytes, and file-like
  inputs. Closed positions are the canonical trade unit.
- Reject optimization workbooks and unhydrated Git LFS pointers explicitly.
- Preserve source balance/equity separately from reconstructed curves.
- Keep reported MT5 metrics separate from computed metrics.
- Return `None`/`NA` plus a diagnostic for undefined values; preserve warnings,
  validation, and provenance.
- Keep this strictly analytical: no live execution, trading, or GUI logic.
- Use the package API and typed result model for every user-facing analysis
  request. Add package code and regression tests for new reusable behavior.
- Run `python3 -m unittest discover -s tests -v`, Ruff, and coverage after
  parser, metric, simulation, or transformation changes.
