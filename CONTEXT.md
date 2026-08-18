# Trade analysis context

This context defines the domain language for deterministic analysis of single-run
MetaTrader 5 strategy reports and their derived portfolio views.

## Agent-first framework

The repository is designed as an **Agent-first** framework. Natural-language
questions should resolve to the public typed `analyser` API and its eager result
models. Reusable analysis, serialization, caching, and chart behavior belongs in
the package; user-facing workflows must not depend on one-off calculation
scripts. Any future GUI or remote delivery layer should consume the same
deterministic results.

## Report and trade language

**Canonical report**:
A normalized single-run MetaTrader 5 report representing one strategy, with its
completed positions, account metadata, and any source account observations.
_Avoid_: optimization workbook, raw export

**Completed position**:
A position that has been fully closed and is therefore eligible for the
canonical trade stream. Its `open_time` is the authoritative timestamp for
sample-period and trade-filter classification; its net result includes profit,
swap, and commission.
_Avoid_: order, partial close, deal

**Analysis view**:
A deterministic full-sample or named-period result calculated from a canonical
report or a selected/resized trade stream.
_Avoid_: snapshot, dashboard

**Trade transformation**:
A deterministic operation that changes which completed positions or position
sizes participate in an analysis view, such as a filter or what-if sizing.
_Avoid_: live modification, execution

**Interactive report**:
A deterministic, self-contained HTML presentation of one eager single-report or
portfolio result. It is a retrieval/export layer over the typed API; it does not
parse MT5 markup or implement a competing metric engine in the browser.
_Avoid_: live dashboard, trading terminal

## Period language

**In-sample period**:
The explicitly named historical window used for strategy development or
selection.
_Avoid_: training data

**Out-of-sample period**:
The explicitly named historical window held out from development for evaluation.
_Avoid_: test data

## Portfolio language

**Portfolio member**:
One canonical report treated as one independently identified strategy within a
portfolio. Member trades are never netted with another member's trades.
_Avoid_: leg, combined trade
