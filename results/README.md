# Example results

These artifacts were generated through the public `analyser` API from a local
MT5 report that was used only as a structural template. The committed fixture
contains deterministic synthetic data: strategy names, symbols, inputs,
comments, identifiers, timestamps, prices, and P&L were replaced or scrambled.
It is not a real trading result.

Source reports:

- [Sanitized single report](sample_reports/example_strategy_a.html)
- [Sanitized portfolio report B](sample_reports/example_strategy_b.html)
- [Sanitized portfolio report C](sample_reports/example_strategy_c.html)

Single-report outputs:

- [Metrics and monthly Markdown](analysis.md)
- [Full deterministic JSON](analysis.json)
- [Metrics CSV](metrics.csv)
- [Monthly returns CSV](monthly.csv)
- [Monthly drawdown CSV](monthly-drawdown.csv)
- [Year/month/YTD performance CSV](monthly-performance.csv)
- [Equity and drawdown chart](equity-drawdown.png)
- [Long-only result](long-only.md)
- [Flat-lot what-if result](what-if-flat-lot.md)
- [In-sample/out-of-sample result](sample-periods.md)

Portfolio outputs:

- [Portfolio Markdown report](portfolio.md)
- [Portfolio allocated equity CSV](portfolio-equity.csv)
- [Portfolio equity and drawdown chart](portfolio-equity-drawdown.png)
- [Daily profit correlation table](daily-profit-correlation.csv)
- [Daily profit correlation heat map](daily-profit-correlation.png)

Monte Carlo outputs:

- [Monte Carlo p5/p50/p95 summary](monte-carlo-summary.md)
- [Monte Carlo simulated paths](monte-carlo-paths.png)
