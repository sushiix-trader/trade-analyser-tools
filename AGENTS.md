# Agent workflow

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
