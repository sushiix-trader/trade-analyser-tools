# Agent workflow

## Query routing

For natural-language questions about what this platform can do or how to use it,
read `.agents/skills/mt5-report-analysis/SKILL.md` before acting. It maps
common report-analysis questions to the canonical typed `analyser` API.

## Trading-report work

Use the `analyser` package as the canonical trading-report interface.

### Standard path

For one MT5 Strategy Tester report, use the eager platform API:

```python
from analyser import AnalysisConfig, analyze_file
result = analyze_file(source, AnalysisConfig())
```

Use `load_report()` followed by `analyze()` when parsing and analysis need to
be separate. Retrieve calculated values from `AnalysisResult`:

- `result.metrics`
- `result.monthly`
- `result.monthly_drawdown`
- `result.monthly_performance` (year × Jan-Dec + compounded YTD table)
- `result.balance`
- `result.equity`
- `result.validation`
- `result.warnings`
- `result.provenance`
- `result.what_if` when a deterministic trade re-sizing mode is enabled
- `result.periods["in_sample"]` and `result.periods["out_of_sample"]` when an explicit `SamplePeriodConfig` is enabled
- `result.daily_profit` for normalized realized daily net-profit points

Use `compare_reports(left, right)` for XML/HTML canonical equivalence. Use the
serializers on `AnalysisResult` for JSON, CSV, or Markdown output. For a
visual artifact, use `save_equity_drawdown_chart(result, destination)` from the
optional chart API; it renders the selected equity curve and high-water-mark
drawdown without recalculating analysis. The
QuantAnalyzer-style fields are on `result.metrics`, including total profit
percentage, return/drawdown ratios, win/loss and payout ratios, gross/average/
largest trade percentages, streak averages, AHPR, daily/monthly/yearly
averages, stagnation, exposure, z-score, and SQN. R-expectancy and bars per
trade are calculated only when the input supplies explicit R/bar values.

For explicit in-sample/out-of-sample work, use the typed period seam rather
than manually slicing trades:

```python
from datetime import datetime
from analyser import AnalysisConfig, PeriodWindow, SamplePeriodConfig, analyze_file

periods = SamplePeriodConfig(
    windows={
        "in_sample": PeriodWindow("in_sample", datetime(2011, 1, 1), datetime(2021, 1, 1)),
        "out_of_sample": PeriodWindow("out_of_sample", datetime(2021, 1, 1), datetime(2026, 1, 1)),
    }
)
result = analyze_file(source, AnalysisConfig(sample_periods=periods))
period_result = result.periods["out_of_sample"]
```

Use `suggest_sample_periods(source)` for conservative folder/filename
suggestions. Suggestions require explicit caller confirmation and are never
activated automatically. Sample periods use `[start, end)` boundaries and
classify completed positions by `open_time`; cross-boundary closes are retained
with a warning. `result.analyze_periods(periods, filters=...)` applies period
classification before member-level trade filters. Use `ChartConfig(show_sample_periods=True)`
with `save_equity_drawdown_chart()` for labelled in-sample, out-of-sample, and
excluded bands.

For multi-report work, use the typed portfolio seam rather than combining
numbers in a custom script:

```python
from analyser import PortfolioConfig, PortfolioMember, analyze_portfolio

result = analyze_portfolio(
    [
        PortfolioMember("Strategy A", "Description A", source=source_a, weight=0.6),
        PortfolioMember("Strategy B", "Description B", source=source_b, weight=0.4),
    ],
    PortfolioConfig(portfolio_initial_capital=100_000),
)
```

Use `result.metrics`, `result.monthly`, `result.monthly_drawdown`,
`result.monthly_performance`, and the labelled matrices on
`PortfolioAnalysisResult`. Do not net member trades or
silently combine reports with different currencies/timezones. Use
`AnalysisStore.analyze_portfolio_or_load()` for cached portfolio retrieval.

Set `sample_periods=` on each `PortfolioMember` to keep period definitions at
the individual-report level. Compatible named periods are available through
`result.periods`; differing member boundaries use their intersection and emit a
warning. Daily realized-profit correlation is eager at
`result.correlations.daily_profit`, with raw `series`, capital-scaled
`allocated_series`, labelled `matrix`, observation count, and period-scoped
results in `result.correlations.by_period`. Undefined correlation cells are
`None` with diagnostics. Use
`save_correlation_heatmap(result.correlations.daily_profit, destination)` for
the deterministic table-like visual artifact. The heat map uses a fixed `[-1, 1]`
scale, two-decimal display labels, and grey undefined cells without
recalculating the canonical matrix.

For repeated report retrieval, use the package cache rather than writing a
custom persistence script:

```python
from analyser import AnalysisStore

artifact = AnalysisStore("data/analysis-cache").analyze_or_load(source)
result = artifact.result
```

`artifact.cache_hit` indicates whether eager analysis was performed during that
call. The cache key is deterministic for the report bytes and analysis
configuration.

For trade filtering, use the typed filter API rather than a custom script:

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

What-if re-sizing is also a typed platform operation, never a custom script:

```python
from analyser import WhatIfConfig

sized = result.apply_what_if(WhatIfConfig.flat_lot(0.10))
```

Use `AnalysisConfig(what_if=...)` for eager sizing during file analysis.
Risk-based sizing requires an explicit `InstrumentSpec` and explicit stop loss;
missing/invalid stops are excluded with warnings, and the transformed result
retains `source_report`, `what_if.audits`, and deterministic provenance. Flat
lot, percentage-risk, and dollar-risk modes are mutually exclusive. What-if
sizing occurs after filters/sample-period selection and before portfolio
allocation. It scales profit, swap, and commission consistently.

Filtering uses canonical completed positions and their `open_time`; it never
nets trades or mutates the original `AnalysisResult`. Available v1 predicates
are `LongOnly`, `ShortOnly`, `OpenDateRangeFilter`, `TimeOfDayFilter`, and
named Sydney/Tokyo/London/New York `SessionFilter`. Compose them with `AllOf`,
`AnyOf`, or `Not`. Boundaries are `[start, end)`. The parsed report timezone is
authoritative; otherwise temporal filters require an explicit IANA timezone in
`FilterConfig`. Filtered curves are reconstructed from the selected trades,
source curves are not reused, and selection/audit metadata is available through
`result.selection`. Chain filters on an `AnalysisResult` safely: the package
re-evaluates the original report rather than a previously filtered subset.

Use `filters=` and `filter_config=` on each `PortfolioMember` to filter before
allocation. Use `AnalysisStore.filter_or_load()` or
`AnalysisStore.analyze_filtered_or_load()` for deterministic filtered-result
retrieval. Filter specifications/configuration and the source report hash are
part of the cache key.

For Monte Carlo robustness work, use the public simulation API rather than a
custom randomisation script:

```python
from analyser import MonteCarloConfig, run_monte_carlo_file

simulation = run_monte_carlo_file(
    source,
    MonteCarloConfig(iterations=10_000, method="permutation", seed=42),
)
```

Monte Carlo operates on completed-position net profits. The permutation
method preserves every historical trade and changes only its order; bootstrap
sampling is an explicit alternative. Results are deterministic for a fixed
configuration and expose `summary()`, aligned result arrays, and `to_json()`.

### Implementation rule

When a user asks for trading-report or metric analysis, extend or call the
public `analyser` API and its typed result model. Put reusable behavior in the
package and add a regression test under `tests/`. Keep exploratory work in
notebooks or tests only when it is needed to validate a package change; the
user-facing workflow must remain the package API.

### Analytical contract

- Accept single-run MT5 HTML/HTM and XML reports.
- Treat completed closed positions as the canonical trade unit.
- Preserve source balance/equity and always calculate the reconstructed curve.
- Keep reported MT5 metrics separate from computed metrics.
- Preserve deterministic configuration, warnings, validation, and provenance.
- Represent undefined metrics as `None`/JSON `null` with a diagnostic.
- Reject optimization workbooks and unhydrated Git LFS pointers explicitly.
- Keep simulations and GUI work separate from the v1 eager analytics path.
- Treat one report as one strategy in portfolio work; never net member trades.
- Require common currency and timezone before portfolio aggregation.
- Use static capital-allocation weights and the typed portfolio matrices.
- Apply member filters before portfolio allocation; never combine filtered
  numbers by hand or net member trades.
- Use explicit IANA/report timezone context for temporal filters and emit
  warnings when open times are missing/inferred or configuration conflicts with
  the report timezone.
- Require both named `in_sample` and `out_of_sample` windows before activating
  sample-period analysis; use conservative suggestions only after explicit
  caller confirmation.
- Classify sample periods by completed-position `open_time`, preserve
  segment-relative opening capital, and warn on cross-boundary closes or trades
  outside named windows.
- Use typed what-if sizing only; keep flat-lot, percentage-risk, and
  dollar-risk modes mutually exclusive and deterministic.
- Require explicit stops and instrument metadata for risk-based sizing; exclude
  unsupported trades with warnings and retain a per-trade sizing audit.
- Use broker/report timestamps for daily realized-profit correlation, align
  strategy series over overlapping active dates, expose raw and allocated
  series, and return `None` plus diagnostics for undefined cells.

### Verification

Run the full suite after changes:

```bash
python3 -m unittest discover -s tests -v
```

For measured coverage, use the project environment:

```bash
.venv/bin/coverage run -m unittest discover -s tests
.venv/bin/coverage report -m
```

A parser, metric, or simulation change is complete only when synthetic tests,
relevant real local-report regressions, and coverage verification pass.
