# Trade Analyser Tools

A fast, deterministic Python analysis engine for MetaTrader 5 single-run
Strategy Tester reports. The project is intentionally platform-first; a GUI
is a later consumer of the same result objects.

## Supported inputs

- MetaTrader 5 Strategy Tester `.htm` / `.html`
- MetaTrader 5 single-run XML reports when they expose completed positions or
  pairable deals
- Filesystem paths, bytes, and binary file-like objects

Optimization workbooks are rejected with `UnsupportedReportError`. Git LFS
pointer files are rejected with `UnhydratedInputError` rather than being
mistaken for report payloads.

## Quick start

```python
from analyser import AnalysisConfig, analyze_file

result = analyze_file("tester_report.htm", AnalysisConfig())

print(result.metrics.net_profit)
print(result.metrics.custom_trade_event_sharpe)
print(result.metrics.calmar_ratio)
print(result.metrics.recovery_factor)
print(result.metrics.return_drawdown_ratio)
print(result.metrics.sqn_score)

for month in result.monthly:
    print(month.period, month.pnl, month.return_on_starting_equity)

# QuantAnalyzer-style year x month table. Values are percentages; YTD is compounded.
for row in result.monthly_performance.rows:
    print(row.year, row.monthly_returns_pct, row.ytd_return_pct)

payload = result.to_dict()
json_text = result.to_json()  # deterministic JSON
markdown = result.to_markdown()

# Optional chart artifact: selected equity plus high-water-mark drawdown.
from analyser import save_equity_drawdown_chart
save_equity_drawdown_chart(result, "equity-drawdown.png")
```

For repeated retrieval, use the optional deterministic local result store. The
first call performs the eager parse and analysis; later calls with the same
report bytes and configuration load the already-calculated result instead of
recalculating it:

```python
from analyser import AnalysisStore

store = AnalysisStore("data/analysis-cache")
artifact = store.analyze_or_load("tester_report.htm")

print(artifact.cache_hit)       # False on first load, True on a cache hit
print(artifact.key)             # content/config-derived artifact key
print(artifact.result.monthly)  # already-calculated values
```

The cache key includes the input SHA-256, analysis configuration, parser
version, and package version. The store writes JSON atomically, so a changed
report or configuration creates a new artifact rather than reusing stale
results.

## Trade filters and what-if analysis

Filtering is a typed platform operation, not a custom post-processing script.
It is applied to each report's canonical completed positions before metrics,
curves, monthly tables, or portfolio allocation are calculated:

```python
from datetime import time
from analyser import (
    AllOf,
    FilterConfig,
    ForexSession,
    LongOnly,
    SessionFilter,
    TimeOfDayFilter,
)

filtered = result.apply_filters(
    AllOf(
        LongOnly(),
        SessionFilter(ForexSession.LONDON),
        TimeOfDayFilter(time(8, 0), time(12, 0), timezone="Europe/London"),
    ),
    # Use this only when the report has naive MT5 server timestamps and its
    # timezone is not already present on the parsed Report.
    FilterConfig(report_timezone="Australia/Brisbane"),
)

print(filtered.metrics)
print(filtered.monthly)
print(filtered.monthly_drawdown)
print(filtered.selection.selected_trade_keys)
print(filtered.selection.excluded_by_filter)
```

The v1 filter set is `LongOnly`, `ShortOnly`, `OpenDateRangeFilter`,
`TimeOfDayFilter`, and named `SessionFilter` for Sydney, Tokyo, London, and
New York. `AllOf`, `AnyOf`, and `Not` compose them. Time windows use
`[start, end)` boundaries; overnight windows are supported. The source
report's IANA timezone is authoritative when present. Otherwise, temporal
filters require an explicit `FilterConfig(report_timezone=...)`; the machine's
local timezone is never guessed. Named sessions convert timestamps into their
own IANA/DST-aware timezone. Missing or inferred open times are excluded from
temporal filters and produce a warning.

Filtered results retain the original initial deposit, preserve the original
report and reported MT5 metrics separately, and reconstruct balance/equity from
selected closed positions. Source account curves are not claimed to be
filterable, so filtered validation is `not_applicable` and the result records
its filter specification, configuration, selected/excluded trade audit, and
deterministic provenance. Chaining filters always re-evaluates the original
report, preventing accidental filtering of an already-filtered subset.

### What-if trade sizing

What-if sizing is a deterministic transformation of canonical completed
positions. It returns a normal `AnalysisResult`, so the transformed metrics,
monthly returns, drawdown, equity chart, sample-period analysis, portfolio
allocation, and daily-profit correlations can all be consumed by the rest of
the platform. Exactly one sizing mode may be selected:

```python
from analyser import AnalysisConfig, InstrumentSpec, WhatIfConfig, analyze_file

# Flat lot sizing: replace the original volume on every completed position.
flat = analyze_file(
    "tester_report.htm",
    AnalysisConfig(what_if=WhatIfConfig.flat_lot(0.10)),
)

# Risk sizing: explicit stop losses and static instrument metadata are required.
spec = InstrumentSpec(
    symbol="TEST_SYMBOL",
    tick_size=0.00001,
    tick_value=1.0,
    account_currency="USD",
)
risk_sized = analyze_file(
    "tester_report.htm",
    AnalysisConfig(
        what_if=WhatIfConfig.percent_risk(
            1.0,
            initial_capital=100_000,
            instrument_spec=spec,
        )
    ),
)

print(risk_sized.metrics)
print(risk_sized.what_if.audits)
print(risk_sized.what_if.warnings)
```

`WhatIfConfig.dollar_risk(amount=...)` is the fixed-dollar alternative.
Percentage risk is based on fixed initial capital, capped at 100%, and does not
compound. Risk sizing uses the explicit stop distance and floors calculated
volume to `0.01` lots. Trades without explicit stops, invalid stops, or sizes
below the lot precision are excluded with diagnostics; if no eligible trades
remain, a `WhatIfError` is raised. Profit, swap, and commission are scaled by
the effective/original volume ratio. The original report remains available at
`result.source_report`, and `result.what_if` contains the per-trade sizing
audit and excluded-trade diagnostics.

An existing result can be transformed without mutating it:

```python
resized = result.apply_what_if(WhatIfConfig.flat_lot(0.10))
```

For portfolios, provide `what_if=` on each `PortfolioMember`. Member-level
what-if sizing occurs before static portfolio capital allocation.

Member-level filters can be supplied before portfolio aggregation:

```python
from analyser import PortfolioMember

PortfolioMember(
    "London longs",
    "London-session long positions",
    source="tester_report.htm",
    weight=0.5,
    filters=AllOf(LongOnly(), SessionFilter(ForexSession.LONDON)),
    filter_config=FilterConfig(report_timezone="UTC"),
)
```

Use `AnalysisStore.filter_or_load(result, filter_spec, filter_config)` when a
base result is already available, or
`AnalysisStore.analyze_filtered_or_load(source, filter_spec, ...)` for a single
call. Filter fingerprints and the source report hash are part of the cache
key, so retrieval is deterministic and does not silently reuse another filter
selection. Monte Carlo can consume the resulting filtered analysis through its
public API later; no simulation is performed by filtering.

## Sample periods and daily profit correlation

In-sample and out-of-sample windows are explicit analytical metadata on each
report. Both canonical windows are required before period analysis is enabled;
periods use `[start, end)` boundaries and classify completed positions by their
`open_time`. A position that crosses a boundary remains assigned to its open
period and produces a structured warning.

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
print(result.periods["out_of_sample"].monthly_performance)
print(result.periods["out_of_sample"].warnings)
```

Folder and filename metadata can produce conservative suggestions, but they are
never activated automatically:

```python
from analyser import suggest_sample_periods

suggestions = suggest_sample_periods("reports/in-sample/out-of-sample/report.htm")
periods = suggestions.accept()  # explicit caller confirmation
```

Period analysis is eager. The segment opening balance is retained so period
returns and drawdowns are segment-relative, while the full report remains
available for reconciliation. Use `analyze_periods()` when a trade filter must
be applied after sample-period classification:

```python
from analyser import LongOnly

filtered = result.analyze_periods(periods, filters=LongOnly())
```

Enable labelled overlays on the existing equity/drawdown chart without changing
the default chart:

```python
from analyser import ChartConfig, save_equity_drawdown_chart

save_equity_drawdown_chart(
    result,
    "equity-drawdown-periods.png",
    chart_config=ChartConfig(
        show_sample_periods=True,
        show_excluded_periods=True,
    ),
)
```

For portfolios, period definitions belong to individual members. Compatible
named periods are combined over their effective intersection; differing member
boundaries produce a warning:

```python
from analyser import PortfolioMember, analyze_portfolio

portfolio = analyze_portfolio([
    PortfolioMember("Strategy A", "A", source="a.htm", weight=0.5, sample_periods=periods),
    PortfolioMember("Strategy B", "B", source="b.htm", weight=0.5, sample_periods=periods),
])

print(portfolio.periods["out_of_sample"].metrics)
```

Multi-strategy portfolios also eagerly calculate daily realized net-profit
correlation over overlapping active dates:

```python
correlation = portfolio.correlations.daily_profit
print(correlation.matrix)       # labelled strategy × strategy matrix
print(correlation.series)       # raw daily net-profit points
print(correlation.allocated_series)
print(correlation.observations)
print(portfolio.correlations.by_period["out_of_sample"].matrix)
```

The canonical correlation series uses each completed position's normalized net
profit on its `close_time` date. Missing strategy days inside the active overlap
are zero-filled; periods with insufficient observations or zero variance return
`None` matrix cells and structured warnings. Raw and allocated daily series are
both exposed. Correlation configuration and period definitions participate in
deterministic serialization and caching.

Render the labelled numeric matrix as a deterministic heat map without
recalculating it:

```python
from analyser import save_correlation_heatmap

save_correlation_heatmap(
    portfolio.correlations.daily_profit,
    "daily-profit-correlation.png",
    decimals=2,
)
```

The heat map uses a fixed `[-1, 1]` scale, blue for positive correlation, red
for negative correlation, neutral colour near zero, and grey `N/A` cells for
undefined values. Display values are rounded to two decimals; the canonical
matrix and CSV retain their analytical values.

## Monte Carlo robustness test

Monte Carlo is an optional simulation over the canonical closed-position net
profit sequence. It is separate from the reported MT5 metrics and does not
invent intrabar floating equity. The default permutation method keeps every
historical trade once and randomises its order:

```python
from analyser import MonteCarloConfig, run_monte_carlo_file

simulation = run_monte_carlo_file(
    "tester_report.htm",
    MonteCarloConfig(
        iterations=10_000,
        method="permutation",  # or "bootstrap"
        skip_trades_pct=0.0,
        ruin_equity=0.0,
        seed=42,
    ),
)

print(simulation.summary())
print(simulation.max_drawdowns)
print(simulation.probability_of_ruin_pct)
```

`permutation` measures sequence/order risk while preserving the observed
trade distribution. `bootstrap` samples outcomes with replacement and varies
the realised distribution as well. `skip_trades_pct` is an explicit stress
option. Every result includes aligned per-iteration arrays, a deterministic
JSON serializer, and the seed/configuration used to produce it.

To render simulated paths instead of a histogram, opt into retaining a deterministic
subset of paths and pass percentile intervals to the chart API:

```python
from analyser import (
    MonteCarloConfig,
    MonteCarloPathChartConfig,
    MonteCarloPathInterval,
    run_monte_carlo_file,
    save_monte_carlo_paths,
)

simulation = run_monte_carlo_file(
    "tester_report.htm",
    MonteCarloConfig(
        iterations=10_000,
        method="bootstrap",
        seed=42,
        retain_paths=True,
        path_count=500,
    ),
)
save_monte_carlo_paths(
    simulation,
    "monte-carlo-paths.png",
    chart_config=MonteCarloPathChartConfig(
        intervals=(
            MonteCarloPathInterval(5, 95, color="#4f81bd", alpha=0.16),
            MonteCarloPathInterval(25, 75, color="#1f77b4", alpha=0.24),
        )
    ),
)
```

Each retained path is drawn as a faint line. The supplied intervals are
calculated at every simulated trade step and highlighted on the equity and
drawdown panels. Set `show_streaks=True` to add winning-streak and
losing-streak path panels using the same intervals. The result also exposes
`max_consecutive_wins` and `max_consecutive_losses` arrays plus their percentile
summaries. Streaks use completed-position net profit: positive extends wins,
negative extends losses, and zero resets both. Monte Carlo paths use
trade-sequence steps rather than report timestamps because permutation and
bootstrap change the trade order.

Parsing and analysis are separate when needed:

```python
from analyser import analyze, load_report

report = load_report(report_bytes)
result = analyze(report)
```

## Portfolio analysis

One single-run MT5 report represents one strategy. Multiple reports can be
combined without netting their trades:

```python
from analyser import (
    PortfolioConfig,
    PortfolioMember,
    analyze_portfolio,
)

portfolio = analyze_portfolio(
    [
        PortfolioMember(
            strategy_name="Strategy A",
            description="Generic strategy A",
            source="trend.htm",
            weight=0.60,
        ),
        PortfolioMember(
            strategy_name="Strategy B",
            description="Generic strategy B",
            source="mean-reversion.xml",
            weight=0.40,
        ),
    ],
    PortfolioConfig(portfolio_initial_capital=100_000),
)

print(portfolio.metrics)
print(portfolio.monthly)
print(portfolio.monthly_drawdown)
print(portfolio.allocated_monthly_return_matrix)
print(portfolio.correlation_matrix)
```

Weights are static capital allocations from the portfolio start, not
rebalanced weights. The portfolio uses the union of member time ranges,
retains both raw and allocated matrices, preserves each tagged trade, and
recalculates portfolio-level metrics from the aggregate allocated curve. The
same account currency and report timezone are required. Different active
periods produce structured warnings. Use `with_weights()` or
`with_member_metadata()` to create a new deterministic result without
reparsing the member reports.

Portfolio results are cached through the same store:

```python
from analyser import AnalysisStore

artifact = AnalysisStore("data/analysis-cache").analyze_portfolio_or_load(
    members,
    PortfolioConfig(portfolio_initial_capital=100_000),
)
portfolio = artifact.result
```

Available portfolio matrix serializers include `raw_equity`, `equity`,
`raw_monthly_returns`, `allocated_monthly_returns`,
`raw_monthly_contributions`, `allocated_monthly_contributions`,
`correlation`, `covariance`, and `daily_profit_correlation`.

Chart rendering is an optional dependency (`pip install -e ".[charts]"`).
`save_equity_drawdown_chart()` uses the already-calculated `result.equity`
curve and renders equity and high-water-mark drawdown together.

The complete major analysis is eager. Reading `result.metrics`,
`result.monthly`, or `result.monthly_drawdown` later only retrieves
already-calculated values.

## v1 analytical contract

- Completed closed positions are the unit of analysis.
- XML and HTML normalize into the same canonical `Trade` model.
- Position/deal identifiers are retained for auditability.
- Net P&L is profit + swap + commission.
- Source equity is preferred when a complete source-equity series exists.
- Reconstructed closed-position balance is always calculated as a deterministic
  fallback and comparison curve.
- Balance and equity remain separate concepts.
- Monthly output includes zero-trade months and exposes raw P&L, return on
  starting equity, return on initial capital, and cumulative return.
- `monthly_performance` exposes a year × Jan-Dec × YTD matrix. Monthly values
  are percentages and YTD compounds the active months; inactive edge months
  are `None`.
- `Metrics` also exposes screenshot-style closed-position fields: total profit
  percentage, return/drawdown ratios, wins/losses and payout ratios,
  gross/average/largest trade percentages, consecutive-run averages, AHPR,
  calendar averages, stagnation, exposure, z-score, and SQN.
- Metrics requiring optional source data are explicit: bars-per-trade requires
  bars, R-expectancy requires a per-trade R model, and cancelled/expired
  counts are outside the closed-position contract. Such values are `None` and
  have a warning diagnostic rather than being guessed.
- Monthly drawdown exposes both global-high-water-mark and month-contained
  peak-to-trough views.
- The primary computed Sharpe is explicitly a configurable custom trade-event
  Sharpe. It defaults to simple returns, zero risk-free rate, population
  standard deviation, and no annualization.
- Undefined metrics serialize as `null` and produce structured diagnostics.
- Reported MT5 metrics and computed metrics remain separate.
- Trade filters operate on open time, support typed side/date/time/session
  predicates and `AllOf`/`AnyOf`/`Not` composition, and run before portfolio
  aggregation.
- Filtered analyses preserve the original deposit, reconstruct curves from
  selected closed positions, retain an auditable selection record, and mark
  source-curve validation as not applicable.
- Results include deterministic provenance, validation, warnings, and input
  fingerprints.

## Verification

The project uses the standard library `unittest` suite:

```bash
python3 -m unittest discover -s tests -v
```

Measured coverage for the active analyser and Monte Carlo implementation is
enforced at 80% through `pyproject.toml`. Legacy money-management and what-if
helpers remain outside this threshold until they are promoted into the active
platform API:

```bash
.venv/bin/coverage run -m unittest discover -s tests
.venv/bin/coverage report -m
```

Local report payloads are not included in this repository. Provide private
fixtures through the environment variables documented in the test suite when
running optional real-report regressions.
