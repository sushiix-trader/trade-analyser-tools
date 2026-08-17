# Trade Analyser Tools

A fast, deterministic Python library for analysing MetaTrader 5 Strategy
Tester reports. It is an analysis platform only: there is no live execution,
order placement, or GUI in the current release.

## Questions this API can answer

- **“What if we take only the long trades from this strategy?”**
- **“What does the portfolio of three strategies look like?”**
- **“What are the 95% Monte Carlo estimates of returns?”**
- **“What are the monthly returns and monthly drawdowns?”**
- **“What are the profit factor, recovery factor, Calmar ratio, and Sharpe?”**
- **“What if every trade risks 1% of a $100,000 account?”**
- **“How correlated are the strategies’ daily profits?”**
- **“How did the strategy perform in-sample versus out-of-sample?”**
- **“Can I compare an XML report with its HTML equivalent?”**

## Inputs

The platform currently accepts single-run MetaTrader 5 Strategy Tester reports:

- MT5 HTML reports: `.htm` and `.html`
- MT5 XML reports: `.xml`
- Filesystem paths, bytes, and file-like objects

HTML and XML reports are normalized into the same canonical completed-position
model. Use `compare_reports()` when you need to verify that two files represent
the same report. Optimization workbooks, account-history exports outside the
supported report format, and unhydrated Git LFS pointers are rejected.

## Outputs

The API returns typed, eager, deterministic results containing everything needed
for downstream retrieval, reporting, charting, and portfolio work:

- Net profit, total return, CAGR, annual/monthly/daily averages — see the
  [metrics example](results/analysis.md)
- Profit factor, recovery factor, return/drawdown ratio, Calmar, MAR, and
  gain-to-pain metrics — see the [metrics CSV](results/metrics.csv)
- Sharpe variants, Sortino, SQN, expectancy, payoff ratio, win rate, average
  win/loss, largest trades, streaks, stagnation, exposure, and drawdowns — see
  the [full JSON result](results/analysis.json)
- Monthly returns — [monthly CSV](results/monthly.csv); monthly drawdown —
  [drawdown CSV](results/monthly-drawdown.csv); year × Jan-Dec × compounded
  YTD — [performance CSV](results/monthly-performance.csv)
- Reconstructed balance/equity curves and equity-plus-drawdown charts — see the
  [equity/drawdown chart](results/equity-drawdown.png)
- Long-only, short-only, date, time-of-day, and named Forex-session analyses —
  see the [long-only example](results/long-only.md)
- Flat-lot, percentage-risk, and dollar-risk what-if analyses with sizing audits
  — see the [flat-lot example](results/what-if-flat-lot.md)
- In-sample, out-of-sample, and full-sample results — see the
  [sample-period example](results/sample-periods.md)
- Multi-strategy portfolio metrics and allocated curves — see the
  [portfolio report](results/portfolio.md) and [portfolio chart](results/portfolio-equity-drawdown.png)
- Portfolio return/contribution matrices, covariance, and daily profit
  correlation — see the [correlation table](results/daily-profit-correlation.csv)
  and [correlation heat map](results/daily-profit-correlation.png)
- Deterministic Monte Carlo distributions for returns, drawdown, ruin, and
  winning/losing streaks, including p5, p50, and p95 summaries — see the
  [Monte Carlo summary](results/monte-carlo-summary.md) and [path chart](results/monte-carlo-paths.png)
- JSON, Markdown, CSV, validation, warnings, provenance, and deterministic
  local-cache artifacts — see the [example results folder](results/README.md)

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

## How to view outputs remotely via Telegram

Telegram delivery is a transport layer around the analyser; it does not change
or recalculate the analysis. The analyser creates the message text and chart or
report files, and the Telegram Bot API delivers them to your private chat or
group. The repository does not currently include a built-in Telegram sender.

### 1. Create a Telegram bot

1. Open Telegram and start a chat with **@BotFather**.
2. Send `/newbot`.
3. Choose a display name and a unique username ending in `bot`.
4. Copy the bot token returned by BotFather.
5. Treat the token like a password. Do not put it in source code, README files,
   reports, screenshots, or Git history.

### 2. Set the credentials as environment variables

Set these in the shell or service that runs the analysis:

```bash
export TELEGRAM_BOT_TOKEN="paste-the-token-here"
export TELEGRAM_CHAT_ID="your-chat-or-group-id"
```

Using environment variables keeps the token out of the repository. If you use a
local `.env` file instead, add `.env` to `.gitignore` before creating it and
never commit the file.

### 3. Find the destination chat ID

1. Open the new bot in Telegram.
2. Send it `/start` from the account or group that should receive reports.
3. Query the bot updates:

```bash
curl -sS \
  "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates"
```

4. Find the `chat.id` value in the response and set it as
   `TELEGRAM_CHAT_ID`.

For a group, add the bot to the group first and send a message in that group.
Group IDs are commonly negative numbers. Keep the complete value, including the
minus sign.

### 4. Test the connection

```bash
curl --fail-with-body -sS \
  "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe"

curl --fail-with-body -sS -X POST \
  "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
  --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
  --data-urlencode "text=Trade analyser Telegram connection is working."
```

Both commands should return JSON containing `"ok":true`.

### 5. Create analyser outputs

Use the canonical analyser API first. Do not recalculate metrics in the Telegram
transport layer:

```python
from analyser import AnalysisConfig, analyze_file, save_equity_drawdown_chart

result = analyze_file("tester_report.htm", AnalysisConfig())
save_equity_drawdown_chart(result, "/tmp/equity-drawdown.png")

message = result.to_markdown()
with open("/tmp/analysis.md", "w", encoding="utf-8") as output:
    output.write(message)
```

The same pattern applies to portfolio correlation heat maps and Monte Carlo path
charts: generate them with `save_correlation_heatmap()` or
`save_monte_carlo_paths()` and then deliver the resulting files.

### 6. Send a message and charts

Telegram messages are limited in length, so send a concise summary as a message
and the complete Markdown/JSON report as a document:

```bash
curl --fail-with-body -sS -X POST \
  "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
  --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
  --data-urlencode "text=Analysis complete. Equity chart and full report attached."

curl --fail-with-body -sS -X POST \
  "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendPhoto" \
  -F "chat_id=${TELEGRAM_CHAT_ID}" \
  -F "photo=@/tmp/equity-drawdown.png" \
  -F "caption=Equity and drawdown"

curl --fail-with-body -sS -X POST \
  "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendDocument" \
  -F "chat_id=${TELEGRAM_CHAT_ID}" \
  -F "document=@/tmp/analysis.md" \
  -F "caption=Full deterministic analysis report"
```

For a portfolio, send the equity/drawdown chart, correlation heat map, and
portfolio Markdown report in the same way. For Monte Carlo, send the path chart
and the summary report. The Telegram layer should preserve the analysis
configuration, warnings, and provenance alongside the files so the result can
be reproduced later.

### Security checklist

- Keep `TELEGRAM_BOT_TOKEN` outside the repository and deployment logs.
- Never print the token in diagnostic output.
- Restrict the bot to a private chat or controlled group.
- Revoke and regenerate the token with BotFather if it is exposed.
- Do not send reports containing confidential strategy names or data to a chat
  that is not controlled by you.
- Telegram delivery is for viewing analysis outputs only; it does not enable
  live trading or execution.

## Metric conventions

- `profit_factor` = gross profit / absolute gross loss.
- `recovery_factor` = net profit / maximum drawdown in money.
- `return_drawdown_ratio` = total return percentage / maximum drawdown percentage.
- `calmar_ratio` = CAGR / maximum drawdown percentage.
- `custom_trade_event_sharpe` is an unannualized Sharpe calculated from
  completed-trade events.
- `daily_sharpe_ratio` uses reconstructed calendar end-of-day equity and keeps
  flat no-trade days.
- `annualized_daily_sharpe_ratio` applies the configured daily annualization
  factor, defaulting to `365.2425` for the calendar-day series.
- Bars-per-trade and R-expectancy metrics are `None` with diagnostics unless
  the required optional inputs are supplied.
- Reported MT5 metrics remain separate from metrics recalculated by this API.

Undefined values are returned as `None`/JSON `null` with a structured warning
rather than being guessed or silently converted to infinity.

## Development

Run the test suite with:

```bash
python3 -m unittest discover -s tests -v
```

The full agent workflow and canonical API routing instructions are in
[`.agents/skills/mt5-report-analysis/SKILL.md`](.agents/skills/mt5-report-analysis/SKILL.md).
