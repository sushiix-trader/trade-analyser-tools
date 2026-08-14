# Local MT5 report fixtures

This directory is reserved for local, non-submitted MetaTrader 5 report
fixtures. Report payloads, manifests, account identifiers, strategy names,
and performance results are intentionally excluded from the repository.

The repository ignores local XML/HTML report payloads. For private local
experiments, place them under one of these directories:

```text
data/mt5_reports/valid_html/
data/mt5_reports/rejected_optimization_xml/
```

The optional real-report regression tests do not require committed fixtures.
They accept private paths through environment variables such as
`MT5_FIXTURE_REPORT`, `MT5_FIXTURE_ROOT`, `MT5_FIXTURE_REPORT_A`, and
`MT5_FIXTURE_REPORT_B`.
