# Local MT5 report fixtures

This directory is reserved as a local placeholder for non-submitted
MetaTrader 5 report fixtures. Report payloads, manifests, account identifiers,
strategy names, and performance results are intentionally excluded from the
repository. For the strongest separation, keep private reports in a directory
outside the repository and point the optional tests at them with environment
variables.

The repository ignores local XML/HTML report payloads, and the repository
safety audit rejects raw payloads found here. Do not stage them. For private
local experiments, prefer an external directory such as:

```text
/path/ outside/this/repository/valid_html/
/path/ outside/this/repository/rejected_optimization_xml/
```

The optional real-report regression tests do not require committed fixtures.
They accept private paths through environment variables such as
`MT5_FIXTURE_REPORT`, `MT5_FIXTURE_ROOT`, `MT5_FIXTURE_REPORT_A`, and
`MT5_FIXTURE_REPORT_B`.
