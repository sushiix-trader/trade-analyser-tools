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
print(result.drawdown_analysis.depth_distribution.p95)
print(result.drawdown_analysis.duration_distribution.p95)

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
`result.monthly_performance`, `result.drawdown_analysis`, `result.balance`,
`result.equity`, `result.warnings`, and `result.provenance`.

## Limitations and assumptions

### Report and trade scope

- The supported inputs are single-run MT5 `.htm`, `.html`, and XML reports.
  Optimization workbooks, unhydrated Git LFS pointers, and unsupported account
  history exports are rejected.
- Completed closed positions are the canonical trade unit. Open positions,
  partial/unmatched deals, and fields that cannot be normalized may be omitted
  with diagnostics.
- External cash flows are not modeled. The source balance/equity is preserved,
  while the reconstructed curve is calculated from the canonical completed
  positions.
- M1/OHLC data is not currently an input source. R-expectancy and bars-per-trade
  metrics remain undefined unless the report supplies explicit R/bar values.

### Currency and timezone

Reports do **not** have to be in USD. A single report may use another account
currency, such as AUD or EUR, and its money metrics remain denominated in that
report currency. USD is the convention for your current backtest workflow, not
a hard-coded parser requirement.

Portfolio members must have one common, non-empty account currency and compatible
report timezones. The portfolio API does not perform FX conversion, so mixing a
USD report with an AUD report raises an error rather than producing a misleading
combined result. For risk-based what-if sizing, `InstrumentSpec.tick_value` must
be expressed in the report account currency and explicit instrument metadata
and stops are required.

### What-if and simulation scope

- Flat-lot, percentage-risk, and dollar-risk modes are deterministic
  transformations of completed-trade results; they are not broker execution
  replays.
- What-if sizing does not model historical FX conversion, slippage, spread
  changes, margin, commission tiers, or nonlinear swap schedules. Historical
  average tick values are approximate and must retain provenance.
- Monte Carlo randomizes completed-trade net-profit order or samples trades. It
  does not simulate future tick paths, market microstructure, or execution
  latency. The standard portfolio report permutes the allocated aggregate
  completed-position stream; it is not a joint member-level market simulation.
- Analysis is eager and in-memory by design. Very large reports can require
  substantial memory; multiprocessing and live execution are out of scope.

### Known security limitation for future hardening

The current XML path uses the standard-library XML parser and reads the complete
input before parsing. External-entity payloads are rejected in the current
runtime, but hostile XML can still target parser/resource exhaustion, and there
is no configurable maximum input size. Treat report inputs as trusted/local and
do not expose this parser directly as a public hostile-upload service. Future
hardening should use a hardened XML parser such as `defusedxml`, enforce an input
size limit, and add malicious-XML regression tests.

## Generate one complete interactive webpage

The standard report workflow turns one MT5 HTML/XML report into one
self-contained webpage containing the full analysis: metrics, equity and
high-water-mark drawdown, drawdown depth × duration, monthly tables, trade
analysis, and deterministic Monte Carlo robustness. The page is generated from
the eager typed result, so the browser is a retrieval and presentation layer
rather than a second metric implementation. Its section navigation behaves as
true in-page tabs: selecting a tab hides the other panels instead of making the
user flick through one long page, while the complete report remains in the same
HTML file. The selected tab and other view controls are kept in the URL fragment
for reloads and shareable links. The Monte Carlo tab appears near the end of the
report, immediately before the final Warnings & provenance section, with
percentile summaries and retained simulated paths.

```python
from analyser import (
    AnalysisConfig,
    DEFAULT_REPORT_MONTE_CARLO_CONFIG,
    InteractiveReportConfig,
    analyze_file,
    run_monte_carlo,
    save_interactive_report,
)

# Analyse eagerly; drawdown is calculated as part of this result.
result = analyze_file("tester_report.htm", AnalysisConfig())
simulation = run_monte_carlo(
    result.report,
    DEFAULT_REPORT_MONTE_CARLO_CONFIG,
)
save_interactive_report(
    result,
    "results/tester-report.html",
    InteractiveReportConfig(
        title="Tester report review",
        include_trade_table=True,
        table_page_size=50,
    ),
    monte_carlo=simulation,
)
```

`DEFAULT_REPORT_MONTE_CARLO_CONFIG` uses 10,000 permutation iterations, seed
42, and 500 retained paths. For a portfolio, analyse with
`analyze_portfolio()`, run the same configuration on
`portfolio.portfolio_report`, and pass that simulation with the portfolio result
to `save_interactive_report()`.

The lower-level renderer can still be used when a caller deliberately wants a
report without a simulation: `render_interactive_report(result)` leaves the
Monte Carlo tab available and displays a request-to-regenerate message. It does
not silently start an expensive simulation. For a local preview,
`serve_interactive_report(result, monte_carlo=simulation)` returns immediately
and binds to localhost; call `server.close()` when finished.

The default dark-blue page contains:

- the Strategy Analyser logo and an overview grid with returns, drawdown,
  annualized Sharpe, Calmar, recovery factor, profit factor, win rate,
  average win/loss, expectancy, and streaks, with an `(i)` definition icon on
  every displayed metric;
- Long + Short, Long only, and Short only controls that update every downstream
  section; for portfolios, filtering is performed per strategy before
  allocation and recombination;
- a normalized SVG equity/drawdown chart on a white background. Click the
  `Values in %`/`Values in $` button to switch both axes together. The equity
  panel marks and labels the initial balance. Member strategy traces are dotted,
  drawdown traces use the matching strategy colours, and enabling member curves
  starts the view in percentage mode. The chart also includes hover tooltips,
  sample-period bands, and an optional portfolio-only toggle;
- grouped trade analysis by opening/closing hour or day of week, with the
  selected profit measure on the y-axis and timing buckets on the x-axis;
- a deterministic **Drawdown** tab after Equity. It extracts completed and
  currently open high-water-mark drawdown episodes from the selected curve,
  reports depth and duration distributions at P50/P90/P95/P99, ranks each
  episode with ascending percentile, strict-tail rarity, and neutral ordinal rank,
  and shows a depth × elapsed-duration scatter plot plus separate per-episode
  distribution bars sorted by value, with vertical P5, median, and P95 markers.
  The diagrams stack vertically and fit the available width on narrow screens
  instead of requiring horizontal scrolling;
- a light-shaded, full-width year × Jan–Dec × YTD monthly return table with a
  separate maximum-intramonth-drawdown table stacked directly underneath it.
  Drawdown values are displayed as negative percentages, every defined cell is
  red with darker red indicating a larger drawdown, and the table has a
  `Worst` annual column containing the year's most negative monthly value. The
  drawdown table has no YTD column; it fits within the desktop layout and becomes
  a swipeable table on narrow screens.
- an **Edit name** control in the browser. The title is presentation metadata,
  so users can rename the report without changing any analysis. The edited
  name is stored in the URL fragment, survives reloads, is included in JSON
  exports, and is copied by **Copy view link**;
- a filterable, sortable, paginated completed-position table; and
- a daily/weekly realized-profit correlation table for portfolios, plus warnings,
  validation, provenance, and deterministic CSV/JSON/SVG/PNG exports.
- a Monte Carlo tab near the end with probability of ruin, P5/median/P95/mean/worst
  distributions for returns, equity, drawdown, and streaks, plus a retained-path
  percentile chart. The standard complete workflow supplies the deterministic
  simulation and retained paths; the low-level renderer can be used without it
  only when the caller deliberately opts out.

The Monte Carlo tab describes the all-trades simulation supplied by the caller;
changing the report's long/short or portfolio-member view does not rerun it.

Single-report correlation is intentionally shown as “not applicable”. The page
embeds only canonical normalized analysis data and completed positions. Original
MT5 markup, comments, magic numbers, raw deal IDs, position IDs, and source
paths are excluded by default. Use
`InteractiveReportConfig(include_trade_identifiers=True)` or
`redact_comments=False` only when explicitly required. Rendering does not start
a server or make network requests; `serve_interactive_report()` is an explicit
localhost-only convenience for previewing the generated page.

The same result can be rendered repeatedly with byte-for-byte identical HTML
for the same input and configuration. Use the package cache before rendering
when repeated eager analysis retrieval is important:

```python
from analyser import (
    AnalysisStore,
    DEFAULT_REPORT_MONTE_CARLO_CONFIG,
    run_monte_carlo,
    save_interactive_report,
)

artifact = AnalysisStore("data/analysis-cache").analyze_or_load("tester_report.htm")
simulation = run_monte_carlo(
    artifact.result.report,
    DEFAULT_REPORT_MONTE_CARLO_CONFIG,
)
save_interactive_report(
    artifact.result,
    "results/cached-report.html",
    monte_carlo=simulation,
)
```

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

## Drawdown depth × duration

The drawdown API implements the descriptive depth-versus-duration framework from
the Obsidian trading notes. It consumes the selected primary curve exactly as
supplied: no interpolation, resampling, or Monte Carlo randomisation is used.
Each episode starts at the high-water point before the decline, ends at the first
later observation that recovers that high-water value, or remains `open` when the
curve ends underwater. Open episodes are retained and ranked when a completed
history exists, but are excluded from the historical reference distributions.

```python
from analyser import AnalysisConfig, analyze_file

result = analyze_file("tester_report.htm", AnalysisConfig())
drawdowns = result.drawdown_analysis

print(drawdowns.completed_episode_count)
print(drawdowns.current_episode)
print(drawdowns.depth_distribution.p50)
print(drawdowns.depth_distribution.p95)
print(drawdowns.duration_distribution.p95)

for episode in drawdowns.episodes:
    print(
        episode.status,
        episode.depth_percent,       # positive typed magnitude
        episode.duration_days,
        episode.depth_percentile,
        episode.depth_tail_rarity_percent,
        episode.duration_percentile,
    )
```

Percentage and money depth are positive magnitudes in the typed result; the
interactive and Markdown report views display depth as a negative decline.
`duration_days` is exact elapsed time between the peak and recovery/current end;
`duration_periods` counts supplied curve observation transitions, preserving
duplicate timestamps. The fixed v1 historical percentiles use deterministic
linear interpolation. `to_csv("drawdown_summary")` and
`to_csv("drawdown_episodes")` provide separate export sections; the same data is
included in JSON, Markdown, and interactive HTML reports.

For portfolios, `portfolio.drawdown_analysis` is the allocated portfolio curve.
Each `PortfolioMemberResult` also exposes clearly labelled
`raw_drawdown_analysis` and `allocated_drawdown_analysis`; member analyses and
portfolio curves are never pooled into one reference distribution.

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
- Daily and weekly profit correlation and covariance
- Warnings for differing active periods

Reports must use a compatible currency and timezone. Filters, sample periods,
and what-if sizing belong on each `PortfolioMember` and are applied before
portfolio allocation.

### Daily and weekly profit correlation

```python
daily = portfolio.correlations.daily_profit
weekly = portfolio.correlations.weekly_profit

print(daily.matrix)
print(weekly.matrix)
print(weekly.series)
print(weekly.allocated_series)
print(weekly.observations)
```

Daily correlation uses canonical realized net profit aligned over the strategies’
overlapping active dates. Weekly correlation starts with that aligned daily
series and sums each strategy into Monday–Sunday calendar weeks using the report
timezone. Undefined cells are returned as `None` with diagnostics. The
interactive portfolio report exposes the same choice through its Daily/Weekly
frequency selector.

### Trade-profit bar charts

Grouped trade-profit analytics are calculated eagerly as part of every single
report and portfolio result. They use the canonical closed-position
`Trade.profit` value, which is already the report's net result including swap
and commission. They never substitute the raw gross trade value.

```python
from analyser import (
    TradeProfitGrouping,
    TradeProfitMeasure,
    save_trade_profit_bar_chart,
    save_trade_profit_bar_charts,
)

# Retrieve the typed data without rendering a chart.
opening_hours = portfolio.trade_profit.open_hour
for bucket in opening_hours.buckets:
    print(
        bucket.label,
        bucket.net_profit,
        bucket.percentage_gain,
        bucket.trade_count,
    )

# Money and percentage are separate chart artifacts.
save_trade_profit_bar_chart(
    portfolio,
    "opening-hour-net-profit.png",
    grouping=TradeProfitGrouping.OPEN_HOUR,
    measure=TradeProfitMeasure.NET_PROFIT,
)
save_trade_profit_bar_chart(
    portfolio,
    "opening-hour-percentage-gain.png",
    grouping=TradeProfitGrouping.OPEN_HOUR,
    measure=TradeProfitMeasure.PERCENTAGE_GAIN,
)

# Or create all 8 deterministic grouping/measure combinations.
paths = save_trade_profit_bar_charts(portfolio, "trade-profit-bars")
```

The available groupings are opening hour, closing hour, opening day of week,
and closing day of week. Hours use the report/broker timestamp as represented
by the parsed report (00:00 through 23:00); days run Monday through Sunday.
Cross-date positions contribute to their opening and closing dimensions
independently. Percentage gain is a bucket's net profit divided by the original
report deposit, or by total portfolio initial capital for the allocated
portfolio view. Empty buckets remain in the result with zero net profit and
zero counts.

For a portfolio, `portfolio.trade_profit` is the combined capital-allocated
view. `portfolio.raw_trade_profit` contains the unallocated member views by
strategy name, and `portfolio.members[0].analysis.trade_profit` exposes the
corresponding member analysis. Missing timestamps generate structured warnings
and exclude only the affected grouping; the rest of the analysis remains
usable. `TradeProfitConfig(retain_trade_ids=True)` can be supplied through
`AnalysisConfig` when deterministic ticket/position ID audit lists are needed.

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

The Monte Carlo API is an optional standalone capability, but it is included by
default in the complete interactive report workflow unless the user explicitly
asks to skip it. It operates over completed-position net profits, is separate
from the primary eager metrics, and is deterministic for a fixed configuration
and seed.

```python
from analyser import (
    DEFAULT_REPORT_MONTE_CARLO_CONFIG,
    MonteCarloConfig,
    run_monte_carlo,
    run_monte_carlo_file,
)

# Preferred after eager analysis: no second report parse.
simulation = run_monte_carlo(result.report, DEFAULT_REPORT_MONTE_CARLO_CONFIG)

# Standalone raw-input form, or use a custom configuration.
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
    save_monthly_performance_table,
    save_trade_profit_bar_charts,
)

save_equity_drawdown_chart(result, "equity-drawdown.png")
save_equity_drawdown_chart(
    portfolio,
    "portfolio-with-strategies.png",
    show_member_equity=True,
)
save_equity_drawdown_chart(
    portfolio,
    "portfolio-with-strategies-normalized.png",
    show_member_equity=True,
    normalize_equity=True,
)
save_correlation_heatmap(
    portfolio.correlations.daily_profit,
    "daily-profit-correlation.png",
)
save_correlation_heatmap(
    portfolio.correlations.weekly_profit,
    "weekly-profit-correlation.png",
)
save_monthly_performance_table(
    portfolio,
    "monthly-performance.png",
)
save_trade_profit_bar_charts(portfolio, "trade-profit-bars")

print(result.to_json())
print(result.to_markdown())
print(result.to_csv("monthly"))
```

The equity chart contains equity and high-water-mark drawdown. By default, a
portfolio chart shows only the combined portfolio. Pass
`show_member_equity=True` (or use `ChartConfig(show_member_equity=True)`) to
overlay each strategy's capital-allocated equity curve; the drawdown panel
remains the combined portfolio drawdown. Pass `normalize_equity=True` to
rebase every displayed curve to its own opening capital: the upper panel then
shows cumulative return from `0.00%` and the lower panel shows peak-relative percentage
high-water-mark drawdown, matching `result.metrics.max_drawdown_pct`. This is the recommended comparison view when
strategies have unequal allocations. The correlation heat map uses the
already-calculated matrix and displays undefined cells as `N/A`. The
monthly-performance table image consumes `portfolio.monthly_performance`, uses
two-decimal percentage labels, colors positive/negative returns, and shows
undefined months as `—`; it does not recalculate analysis.

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
