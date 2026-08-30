"""Deterministic, self-contained interactive HTML reports.

The interactive report is deliberately a presentation seam over the eager
``analyser`` result model.  It never embeds the original MT5 markup and never
re-parses a report in the browser.  A raw report is parsed and analysed through
the canonical API, then the browser receives a redacted, normalized payload
containing the already-calculated directional variants and portfolio members.
"""

from __future__ import annotations

import base64
import html
import http.server
import json
import math
import threading
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .analysis import MONTH_LABELS, AnalysisResult, analyze_file
from .config import AnalysisConfig
from .diagnostics import Diagnostic
from .load import InputSource
from .filters import LongOnly, ShortOnly, TradeFilter
from .models import Trade
from .portfolio import (
    AnalyzedPortfolioMember,
    PortfolioAnalysisResult,
    PortfolioMember,
    combine_analyses,
)
from .serialization import to_primitive
from .simulations import MonteCarloResult
from .trade_profit import TradeProfitAnalysis, TradeProfitGrouping


_HIDDEN_INTERACTIVE_METRICS = frozenset({
    "custom_trade_event_sharpe",
    "daily_sharpe_ratio",
    "sqn",
})


@dataclass(frozen=True)
class InteractiveReportConfig:
    """Presentation and privacy controls for one generated HTML report.

    The defaults are safe for sharing the generated page: original report
    markup, comments, magic numbers, deal IDs, and source paths are not
    embedded.  The report remains deterministic because no creation timestamp
    or random identifier is added to the document.
    """

    title: str | None = None
    description: str | None = None
    include_trade_table: bool = True
    include_trade_identifiers: bool = False
    redact_comments: bool = True
    table_page_size: int = 50
    theme: str = "dark-blue"

    def __post_init__(self) -> None:
        if self.title is not None and not isinstance(self.title, str):
            raise TypeError("title must be a string or None")
        if self.description is not None and not isinstance(self.description, str):
            raise TypeError("description must be a string or None")
        if not isinstance(self.table_page_size, int) or self.table_page_size <= 0:
            raise ValueError("table_page_size must be a positive integer")
        if self.theme != "dark-blue":
            raise ValueError("theme must be 'dark-blue' in the current release")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class InteractiveReportServer:
    """A small localhost HTTP server for a generated report.

    The server is intentionally explicit and non-blocking.  Call ``close``
    when the page is no longer needed.  It serves only the generated HTML and
    binds to localhost by default.
    """

    def __init__(self, server: http.server.ThreadingHTTPServer, thread: threading.Thread):
        self._server = server
        self._thread = thread
        host, port = server.server_address[:2]
        display_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
        self.url = f"http://{display_host}:{port}/"
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2.0)

    def __enter__(self) -> "InteractiveReportServer":
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.close()


def render_interactive_report(
    source: AnalysisResult | PortfolioAnalysisResult | InputSource,
    config: InteractiveReportConfig | None = None,
    *,
    analysis_config: AnalysisConfig | None = None,
    monte_carlo: MonteCarloResult | None = None,
) -> str:
    """Return one deterministic, self-contained interactive report page.

    ``source`` may be an already eager single/portfolio result, or one MT5
    report path, bytes object, or file-like object.  Portfolio construction is
    intentionally performed through ``analyze_portfolio``/``combine_analyses``
    before this renderer is called; the renderer does not accept a collection
    of raw reports because one raw report maps to one strategy.

    ``monte_carlo`` may contain a simulation already computed by
    :func:`analyser.run_monte_carlo`.  It is embedded as a separate report tab;
    the browser only presents its typed summary and retained paths.
    """

    config = config or InteractiveReportConfig()
    result = _ensure_result(source, analysis_config)
    payload = _build_payload(result, config, monte_carlo)
    title = config.title or _default_title(result)
    description = config.description or _default_description(result)
    embedded_payload = _embed_json(payload)
    embedded_config = _embed_json(config.to_dict())
    return _render_html(
        title=title,
        description=description,
        payload_json=embedded_payload,
        config_json=embedded_config,
        logo_data_uri=_logo_data_uri(),
    )


def save_interactive_report(
    source: AnalysisResult | PortfolioAnalysisResult | InputSource,
    destination: str | Path,
    config: InteractiveReportConfig | None = None,
    *,
    analysis_config: AnalysisConfig | None = None,
    monte_carlo: MonteCarloResult | None = None,
) -> Path:
    """Generate and write one self-contained interactive HTML page."""

    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_interactive_report(
            source,
            config,
            analysis_config=analysis_config,
            monte_carlo=monte_carlo,
        ),
        encoding="utf-8",
    )
    return path


def serve_interactive_report(
    source: AnalysisResult | PortfolioAnalysisResult | InputSource,
    config: InteractiveReportConfig | None = None,
    *,
    analysis_config: AnalysisConfig | None = None,
    monte_carlo: MonteCarloResult | None = None,
    host: str = "127.0.0.1",
    port: int = 0,
) -> InteractiveReportServer:
    """Serve one generated report on a local, non-blocking HTTP server."""

    if not host:
        raise ValueError("host must be non-empty")
    page = render_interactive_report(
        source,
        config,
        analysis_config=analysis_config,
        monte_carlo=monte_carlo,
    )
    content = page.encode("utf-8")

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            parsed = urlparse(self.path)
            if parsed.path not in {"", "/", "/index.html"}:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)

        def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler API
            parsed = urlparse(self.path)
            if parsed.path not in {"", "/", "/index.html"}:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    server = http.server.ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, name="analyser-interactive-report", daemon=True)
    thread.start()
    return InteractiveReportServer(server, thread)


def _ensure_result(
    source: AnalysisResult | PortfolioAnalysisResult | InputSource,
    analysis_config: AnalysisConfig | None,
) -> AnalysisResult | PortfolioAnalysisResult:
    if isinstance(source, (AnalysisResult, PortfolioAnalysisResult)):
        if analysis_config is not None:
            raise ValueError("analysis_config is only accepted for raw report inputs")
        return source
    return analyze_file(source, analysis_config)


def _default_title(result: AnalysisResult | PortfolioAnalysisResult) -> str:
    if isinstance(result, PortfolioAnalysisResult):
        return "Portfolio analysis"
    return result.report.strategy_name.strip() or "MT5 strategy analysis"


def _default_description(result: AnalysisResult | PortfolioAnalysisResult) -> str:
    if isinstance(result, PortfolioAnalysisResult):
        return (
            f"{len(result.members)} strategies · {result.currency or 'unknown currency'} · "
            f"{result.timezone or 'report timezone'}"
        )
    return (
        f"{result.report.source_format or 'MT5 report'} · "
        f"{result.report.currency or 'unknown currency'} · "
        f"{result.report.timezone or 'report timezone'}"
    )


def _build_payload(
    result: AnalysisResult | PortfolioAnalysisResult,
    config: InteractiveReportConfig,
    monte_carlo: MonteCarloResult | None,
) -> dict[str, Any]:
    variants: dict[str, AnalysisResult | PortfolioAnalysisResult]
    if isinstance(result, PortfolioAnalysisResult):
        variants = {
            "all": result,
            "long": _filter_portfolio(result, LongOnly()),
            "short": _filter_portfolio(result, ShortOnly()),
        }
    else:
        variants = {
            "all": result,
            "long": result.apply_filters(LongOnly()),
            "short": result.apply_filters(ShortOnly()),
        }

    serialized_variants = {
        direction: _safe_result_payload(value, config)
        for direction, value in variants.items()
    }
    return {
        "schema_version": 1,
        "kind": "portfolio" if isinstance(result, PortfolioAnalysisResult) else "single",
        "default_direction": "all",
        "default_data": "portfolio" if isinstance(result, PortfolioAnalysisResult) else "single",
        "directions": ["all", "long", "short"],
        "variants": serialized_variants,
        "monte_carlo": _monte_carlo_payload(monte_carlo, result),
    }


def _filter_portfolio(
    result: PortfolioAnalysisResult,
    filter_spec: TradeFilter,
) -> PortfolioAnalysisResult:
    analyzed: list[AnalyzedPortfolioMember] = []
    for member in result.members:
        filtered = member.analysis.apply_filters(
            filter_spec,
            member.analysis.filter_config,
        )
        member_config = PortfolioMember(
            strategy_name=member.strategy_name,
            description=member.description,
            weight=member.weight,
            sample_periods=filtered.sample_period_config,
            what_if=filtered.what_if.config if filtered.what_if is not None else None,
        )
        analyzed.append(AnalyzedPortfolioMember(member.member_key, member_config, filtered))
    return combine_analyses(
        analyzed,
        result.config,
    )


def _safe_result_payload(
    result: AnalysisResult | PortfolioAnalysisResult,
    config: InteractiveReportConfig,
) -> dict[str, Any]:
    if isinstance(result, PortfolioAnalysisResult):
        return _safe_portfolio_payload(result, config)
    return _safe_analysis_payload(result, config)


def _safe_analysis_payload(
    result: AnalysisResult,
    config: InteractiveReportConfig,
    *,
    strategy_name: str | None = None,
) -> dict[str, Any]:
    report = result.report
    return {
        "kind": "single",
        "display_name": strategy_name or report.strategy_name or "Strategy",
        "currency": report.currency,
        "timezone": report.timezone,
        "metrics": _interactive_metrics_payload(result.metrics),
        "monthly": _json_safe([to_primitive(row) for row in result.monthly]),
        "monthly_drawdown": _json_safe([to_primitive(row) for row in result.monthly_drawdown]),
        "monthly_drawdown_table": _monthly_drawdown_table_payload(result.monthly_drawdown),
        "monthly_performance": _json_safe(result.monthly_performance.to_dict()),
        "equity": _curve_payload(result.equity),
        "balance": _curve_payload(result.balance),
        "source_equity": _curve_payload(result.source_equity),
        "source_balance": _curve_payload(result.source_balance),
        "trade_profit": _trade_profit_payload(result.trade_profit),
        "trades": _trades_payload(report.ordered_trades(), config),
        "periods": _period_payload(result.periods),
        "warnings": _diagnostics_payload(result.warnings),
        "validation": _json_safe(result.validation.to_dict()),
        "provenance": _safe_provenance(result.provenance),
        "selection": _selection_payload(result),
        "filter": _json_safe(result.filter_spec.to_dict() if result.filter_spec else None),
    }


def _monte_carlo_payload(
    simulation: MonteCarloResult | None,
    result: AnalysisResult | PortfolioAnalysisResult,
) -> dict[str, Any] | None:
    """Build the redacted presentation payload for an optional simulation.

    The simulation has already been performed by the public analyser API.  The
    interactive report receives only its typed summary and retained paths; it
    never randomises trades or recomputes Monte Carlo statistics in JavaScript.
    """

    if simulation is None:
        return None
    if not isinstance(simulation, MonteCarloResult):
        raise TypeError("monte_carlo must be a MonteCarloResult or None")

    if isinstance(result, PortfolioAnalysisResult):
        initial_equity = result.portfolio_initial_capital
        currency = result.currency
        scope = "portfolio"
    else:
        initial_equity = result.report.initial_deposit
        currency = result.report.currency
        scope = "strategy"

    return _json_safe({
        "scope": scope,
        "currency": currency,
        "initial_equity": initial_equity,
        "config": simulation.config.to_dict(),
        "summary": simulation.summary(),
        "path_indices": simulation.path_indices.tolist(),
        "equity_paths": simulation.equity_paths.tolist(),
        "winning_streak_paths": simulation.winning_streak_paths.tolist(),
        "losing_streak_paths": simulation.losing_streak_paths.tolist(),
    })


def _interactive_metrics_payload(metrics: Any) -> dict[str, Any]:
    """Return the compact metric surface used by the interactive UI.

    The canonical ``AnalysisResult.metrics`` object remains unchanged.  The
    interactive page intentionally omits the custom trade-event Sharpe, daily
    Sharpe, and SQN fields so that the browser presents one unambiguous metric
    set while callers can still retrieve those values from the Python API.
    """

    values = metrics.to_dict()
    return _json_safe({key: value for key, value in values.items() if key not in _HIDDEN_INTERACTIVE_METRICS})


def _monthly_drawdown_table_payload(rows: Any) -> dict[str, Any]:
    """Build a year-by-month maximum-drawdown table for the browser.

    This is a presentation reshape of the canonical monthly drawdown records.
    Each month uses maximum intramonth drawdown percentage, while the annual
    ``Worst`` value is the most negative monthly value for that year. There is
    deliberately no YTD column because drawdown is path-dependent.
    """

    by_year: dict[int, list[float | None]] = {}
    for row in rows:
        try:
            year_text, month_text = str(row.period).split("-", 1)
            year, month = int(year_text), int(month_text)
        except (AttributeError, ValueError):
            continue
        if month < 1 or month > 12:
            continue
        values = by_year.setdefault(year, [None] * 12)
        # The canonical analysis model stores drawdown as a positive
        # magnitude.  For the performance-style table, present drawdown as a
        # negative return so that the annual ``Worst`` cell can be computed as
        # the minimum defined monthly value and displayed consistently with
        # the chart.
        raw_value = row.maximum_intramonth_drawdown_percent
        values[month - 1] = None if raw_value is None else -abs(float(raw_value))

    table_rows = []
    for year in sorted(by_year):
        values = by_year[year]
        defined = [value for value in values if value is not None]
        table_rows.append({
            "year": year,
            "monthly_drawdown_pct": values,
            "annual_worst_drawdown_pct": min(defined) if defined else None,
        })
    return _json_safe({
        "month_labels": MONTH_LABELS,
        "worst_label": "Worst",
        "basis": "maximum_intramonth_drawdown_percent",
        "rows": table_rows,
    })


def _safe_portfolio_payload(
    result: PortfolioAnalysisResult,
    config: InteractiveReportConfig,
) -> dict[str, Any]:
    members: dict[str, Any] = {}
    labels: dict[str, str] = {}
    for member in result.members:
        labels[member.member_key] = member.strategy_name
        member_analysis = _safe_analysis_payload(
            member.analysis,
            config,
            strategy_name=member.strategy_name,
        )
        member_analysis["allocated_equity"] = _curve_payload(member.allocated_curve)
        member_analysis["raw_equity"] = _curve_payload(member.raw_curve)
        members[member.member_key] = {
            "member_key": member.member_key,
            "strategy_name": member.strategy_name,
            "description": member.description,
            "weight": member.weight,
            "normalized_weight": member.normalized_weight,
            "allocated_capital": member.allocated_capital,
            "allocation_scale": member.allocation_scale,
            "analysis": member_analysis,
        }

    trades = _trades_payload(
        result.portfolio_report.ordered_trades(),
        config,
        strategy_labels=labels,
    )
    return {
        "kind": "portfolio",
        "display_name": "Portfolio",
        "currency": result.currency,
        "timezone": result.timezone,
        "portfolio_initial_capital": result.portfolio_initial_capital,
        "metrics": _interactive_metrics_payload(result.metrics),
        "monthly": _json_safe([to_primitive(row) for row in result.monthly]),
        "monthly_drawdown": _json_safe([to_primitive(row) for row in result.monthly_drawdown]),
        "monthly_drawdown_table": _monthly_drawdown_table_payload(result.monthly_drawdown),
        "monthly_performance": _json_safe(result.monthly_performance.to_dict()),
        "equity": _curve_payload(result.equity),
        "balance": _curve_payload(result.balance),
        "source_equity": _curve_payload(result.source_equity),
        "source_balance": _curve_payload(result.source_balance),
        "trade_profit": _trade_profit_payload(result.trade_profit),
        "trades": trades,
        "members": members,
        "correlations": _correlations_payload(result),
        "periods": _period_payload(result.periods),
        "warnings": _diagnostics_payload(result.warnings),
        "validation": _json_safe(result.validation.to_dict()),
        "provenance": _safe_provenance(result.provenance),
    }


def _curve_payload(curve: Any) -> dict[str, Any] | None:
    if curve is None:
        return None
    return _json_safe({
        "timestamps": [value.isoformat() for value in curve.timestamps],
        "values": list(curve.values),
        "source": curve.source,
        "basis": curve.basis,
        "initial_value": curve.initial_value,
    })


def _trade_profit_payload(analysis: TradeProfitAnalysis) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for grouping in TradeProfitGrouping:
        grouped = analysis.get(grouping)
        result[grouping.value] = {
            "grouping": grouping.value,
            "currency": grouped.currency,
            "initial_capital": grouped.initial_capital,
            "timezone": grouped.timezone,
            "active_start": grouped.active_start.isoformat() if grouped.active_start else None,
            "active_end": grouped.active_end.isoformat() if grouped.active_end else None,
            "buckets": [
                {
                    "label": bucket.label,
                    "bucket_index": bucket.bucket_index,
                    "net_profit": bucket.net_profit,
                    "percentage_gain": bucket.percentage_gain,
                    "trade_count": bucket.trade_count,
                    "winning_trade_count": bucket.winning_trade_count,
                    "losing_trade_count": bucket.losing_trade_count,
                    "average_trade_profit": bucket.average_trade_profit,
                }
                for bucket in grouped.buckets
            ],
            "warnings": _diagnostics_payload(grouped.warnings),
        }
    result["warnings"] = _diagnostics_payload(analysis.warnings)
    return _json_safe(result)


def _trades_payload(
    trades: list[Trade],
    config: InteractiveReportConfig,
    *,
    strategy_labels: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    if not config.include_trade_table:
        return []
    rows: list[dict[str, Any]] = []
    for trade in trades:
        row: dict[str, Any] = {
            "strategy_name": strategy_labels.get(trade.strategy_id, "") if strategy_labels else "",
            "symbol": trade.symbol,
            "side": trade.side.value,
            "volume": trade.volume,
            "open_time": trade.open_time.isoformat() if trade.open_time else None,
            "close_time": trade.close_time.isoformat() if trade.close_time else None,
            "open_price": trade.open_price,
            "close_price": trade.close_price,
            "net_profit": trade.profit,
            "swap": trade.swap,
            "commission": trade.commission,
            "duration_seconds": trade.duration_seconds,
            "is_win": trade.is_win,
        }
        if config.include_trade_identifiers:
            row.update({"ticket": trade.ticket, "position_id": trade.position_id})
        if not config.redact_comments:
            row["comment"] = trade.comment
        rows.append(_json_safe(row))
    return rows


def _period_payload(periods: dict[str, Any]) -> dict[str, Any]:
    return {
        name: {
            "name": name,
            "start": item.window.start.isoformat(),
            "end": item.window.end.isoformat(),
        }
        for name, item in sorted(periods.items())
    }


def _correlations_payload(result: PortfolioAnalysisResult) -> dict[str, Any]:
    return _json_safe(result.correlations.to_dict())


def _selection_payload(result: AnalysisResult) -> dict[str, Any] | None:
    if result.selection is None:
        return None
    selection = result.selection
    return _json_safe({
        "source_trade_count": selection.source_trade_count,
        "selected_trade_count": selection.selected_trade_count,
        "excluded_trade_count": selection.excluded_trade_count,
        "excluded_by_filter": selection.excluded_by_filter,
        "filter_spec": selection.filter_spec,
        "filter_config": selection.filter_config,
    })


def _diagnostics_payload(diagnostics: Any) -> list[dict[str, Any]]:
    return _json_safe([
        item.to_dict() if isinstance(item, Diagnostic) else to_primitive(item)
        for item in diagnostics
    ])


def _safe_provenance(provenance: dict[str, Any]) -> dict[str, Any]:
    """Keep useful provenance while excluding source paths and raw metadata."""

    allowed = {
        "input_sha256",
        "input_size",
        "input_format",
        "parser_version",
        "package_version",
        "timezone",
        "analysis_config",
        "source_report_sha256",
        "source_trade_count",
    }
    return _json_safe({key: to_primitive(provenance[key]) for key in sorted(allowed) if key in provenance})


def _json_safe(value: Any) -> Any:
    value = to_primitive(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    return value


def _embed_json(value: Any) -> str:
    encoded = json.dumps(
        _json_safe(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    # Prevent a report field from terminating the data script element.
    return encoded.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


@lru_cache(maxsize=1)
def _logo_data_uri() -> str:
    """Return the packaged branding image as an inline data URI.

    Keeping the image inline preserves the single-file/offline contract of an
    interactive report.  The asset is packaged with the Python distribution,
    so rendering does not depend on the caller's current working directory.
    """

    logo_path = Path(__file__).with_name("assets") / "strategy-analyser-banner.png"
    encoded = base64.b64encode(logo_path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _render_html(
    *,
    title: str,
    description: str,
    payload_json: str,
    config_json: str,
    logo_data_uri: str,
) -> str:
    safe_title = html.escape(title, quote=True)
    safe_description = html.escape(description, quote=True)
    return (
        _HTML_TEMPLATE
        .replace("__REPORT_TITLE__", safe_title)
        .replace("__REPORT_DESCRIPTION__", safe_description)
        .replace("__REPORT_PAYLOAD__", payload_json)
        .replace("__REPORT_CONFIG__", config_json)
        .replace("__REPORT_LOGO__", html.escape(logo_data_uri, quote=True))
    )


_HTML_TEMPLATE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__REPORT_TITLE__</title>
<meta name="description" content="__REPORT_DESCRIPTION__">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'none'; base-uri 'none'; form-action 'none">
<style>
:root {
  color-scheme: dark;
  --bg: #07111f;
  --panel: #0d1c31;
  --panel-2: #102743;
  --panel-3: #132e4f;
  --text: #e6f0ff;
  --muted: #8da5c4;
  --border: #1d3a5d;
  --accent: #5db6ff;
  --accent-2: #7c6cff;
  --positive: #42d9a0;
  --negative: #ff6d83;
  --warning: #ffc96b;
  --shadow: 0 16px 50px rgba(0,0,0,.24);
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body { margin: 0; background: linear-gradient(145deg, #06101d 0%, #09182b 48%, #071321 100%); color: var(--text); line-height: 1.45; }
button, select, input { font: inherit; }
button, select { color: var(--text); background: var(--panel-2); border: 1px solid var(--border); border-radius: 8px; padding: .55rem .75rem; }
button { cursor: pointer; }
button:hover, select:hover { border-color: var(--accent); }
button:focus-visible, select:focus-visible, input:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
input[type=search] { color: var(--text); background: #09192e; border: 1px solid var(--border); border-radius: 8px; padding: .6rem .75rem; min-width: 200px; }
.shell { max-width: 1500px; margin: 0 auto; padding: 0 1.2rem 3.2rem; }
.hero { padding: 2.5rem 0 1.35rem; }
.brand-logo { display: block; width: min(100%, 1199px); height: auto; margin: 0 0 1.4rem; border: 1px solid rgba(93,182,255,.22); border-radius: 14px; box-shadow: var(--shadow); }
.eyebrow { color: var(--accent); font-size: .74rem; font-weight: 800; letter-spacing: .14em; text-transform: uppercase; }
h1 { font-size: clamp(1.8rem, 4vw, 3.2rem); line-height: 1.05; margin: .35rem 0 .65rem; }
.title-row { display: flex; flex-wrap: wrap; align-items: center; gap: .7rem; }
.title-row h1 { margin-bottom: .2rem; }
.title-edit-button { align-self: center; font-size: .78rem; padding: .42rem .65rem; }
.title-editor { display: flex; flex-wrap: wrap; align-items: end; gap: .55rem; margin: .35rem 0 .75rem; }
.title-editor[hidden] { display: none; }
.title-editor label { display: grid; gap: .2rem; color: var(--muted); font-size: .76rem; }
.title-editor input { width: min(100%, 520px); color: var(--text); background: #09192e; border: 1px solid var(--border); border-radius: 8px; padding: .55rem .7rem; }
.title-editor .status { align-self: center; }
h2 { font-size: 1.3rem; margin: 0; }
h3 { font-size: 1rem; margin: 0 0 .7rem; }
p { color: var(--muted); }
.hero p { max-width: 800px; margin: 0; }
.toolbar { position: sticky; top: 0; z-index: 10; margin: 0 -1.2rem 1.35rem; padding: .7rem 1.2rem; display: flex; flex-wrap: wrap; gap: .65rem; align-items: center; background: rgba(7,17,31,.92); border-top: 1px solid rgba(93,182,255,.12); border-bottom: 1px solid var(--border); backdrop-filter: blur(15px); }
.toolbar .meta { margin-left: auto; color: var(--muted); font-size: .84rem; }
.filter-chip { color: #031526; background: var(--accent); border-radius: 99px; padding: .28rem .6rem; font-size: .78rem; font-weight: 800; }
.nav { display: flex; flex-wrap: wrap; gap: .45rem; padding: .7rem 0 1.35rem; }
.nav a { color: var(--muted); text-decoration: none; background: rgba(16,39,67,.6); border: 1px solid var(--border); border-radius: 99px; padding: .42rem .72rem; font-size: .82rem; }
.nav a:hover { color: var(--text); border-color: var(--accent); }
.nav a[aria-selected="true"] { color: var(--text); border-color: var(--accent); background: rgba(93,182,255,.18); box-shadow: 0 0 0 1px rgba(93,182,255,.12); }
.nav a.monte-carlo-tab { color: #d8d1ff; border-color: rgba(124,108,255,.58); background: rgba(124,108,255,.14); }
.nav a.monte-carlo-tab:hover, .nav a.monte-carlo-tab[aria-selected="true"] { color: var(--text); border-color: var(--accent-2); background: rgba(124,108,255,.28); }
.section { scroll-margin-top: 84px; margin: 1.35rem 0; }
.tab-panel[hidden] { display: none; }
.section-heading { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: .75rem; margin-bottom: .78rem; }
.panel { background: linear-gradient(145deg, rgba(13,28,49,.96), rgba(9,24,43,.96)); border: 1px solid var(--border); border-radius: 15px; box-shadow: var(--shadow); padding: 1rem; }
.metric-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(145px, 1fr)); gap: .65rem; }
.metric { min-height: 84px; padding: .72rem; border-radius: 11px; background: rgba(16,39,67,.75); border: 1px solid rgba(93,182,255,.12); }
.metric .label-row { display: flex; align-items: center; gap: .35rem; }
.metric .label { color: var(--muted); font-size: .72rem; text-transform: uppercase; letter-spacing: .05em; }
.info-icon { position: relative; width: 1.08rem; height: 1.08rem; flex: 0 0 1.08rem; padding: 0; border-radius: 50%; color: var(--accent); background: transparent; border: 1px solid rgba(93,182,255,.55); font-size: .68rem; font-weight: 800; line-height: 1; }
.info-icon:hover, .info-icon:focus-visible { color: #031526; background: var(--accent); }
.info-icon::after { content: attr(data-tooltip); position: absolute; z-index: 30; left: 0; top: calc(100% + .45rem); width: 220px; padding: .55rem .65rem; border: 1px solid var(--accent); border-radius: 8px; background: #06111f; color: var(--text); font-size: .72rem; font-weight: 400; line-height: 1.35; text-align: left; letter-spacing: 0; text-transform: none; box-shadow: var(--shadow); opacity: 0; pointer-events: none; transform: translateY(-3px); transition: opacity .12s ease, transform .12s ease; }
.info-icon:hover::after, .info-icon:focus-visible::after { opacity: 1; transform: translateY(0); }
.metric .value { margin-top: .35rem; font-size: 1.13rem; font-weight: 750; font-variant-numeric: tabular-nums; }
.controls { display: flex; flex-wrap: wrap; align-items: center; gap: .55rem; }
.control-label { color: var(--muted); font-size: .82rem; }
.chart-wrap { overflow-x: auto; }
.chart-panel { background: #fff; border-color: #d5e0eb; }
.chart { width: 100%; min-width: 720px; height: 430px; display: block; }
.chart text { fill: #243b53; font-size: 11px; }
.chart .grid { stroke: rgba(36,59,83,.16); stroke-width: 1; }
.chart .axis { stroke: rgba(36,59,83,.38); stroke-width: 1; }
.chart .band-is { fill: rgba(66,217,160,.10); }
.chart .band-oos { fill: rgba(93,182,255,.10); }
.chart .band-excluded { fill: rgba(36,59,83,.08); }
.chart .zero { stroke: rgba(36,59,83,.42); stroke-dasharray: 4 4; }
.chart .initial-line { stroke: rgba(36,59,83,.72); stroke-dasharray: 6 4; stroke-width: 1.2; }
.chart .initial-label { fill: #243b53; font-size: 10px; font-weight: 700; }
.chart .equity-line { fill: none; stroke-width: 2.35; vector-effect: non-scaling-stroke; }
.chart .drawdown-line { fill: none; stroke-width: 1.8; vector-effect: non-scaling-stroke; }
.chart .member-equity-line, .chart .member-drawdown-line { stroke-dasharray: 7 5; }
.chart .drawdown-axis-label { fill: #dc2626; }
.chart .hover-line { stroke: rgba(36,59,83,.72); stroke-dasharray: 3 3; pointer-events: none; }
.chart-tooltip { position: fixed; display: none; z-index: 20; pointer-events: none; background: #06111f; border: 1px solid var(--accent); border-radius: 8px; padding: .5rem .65rem; box-shadow: var(--shadow); font-size: .78rem; white-space: nowrap; }
.legend { display: flex; flex-wrap: wrap; gap: .65rem 1rem; padding: .4rem 0 0; font-size: .78rem; color: var(--muted); }
.legend button { border: 0; padding: 0; background: transparent; color: var(--muted); }
.chart-panel .legend, .chart-panel .legend button { color: #243b53; }
.legend button.off { opacity: .38; text-decoration: line-through; }
.swatch { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: .3rem; vertical-align: 0; }
.trade-chart { width: 100%; min-width: 720px; height: 350px; display: block; }
.trade-chart text { fill: var(--muted); font-size: 11px; }
.trade-chart .grid { stroke: rgba(141,165,196,.16); stroke-width: 1; }
.trade-chart .axis { stroke: rgba(141,165,196,.46); stroke-width: 1; }
.trade-chart .zero { stroke: rgba(230,240,255,.44); stroke-dasharray: 4 4; }
.trade-chart .bar-value { fill: var(--muted); font-size: 10px; }
.trade-chart .bar-positive { fill: var(--positive); }
.trade-chart .bar-negative { fill: var(--negative); }
.mc-summary-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(165px, 1fr)); gap: .65rem; margin-bottom: 1rem; }
.mc-stat { min-height: 84px; padding: .72rem; border-radius: 11px; background: rgba(124,108,255,.12); border: 1px solid rgba(124,108,255,.28); }
.mc-stat .label { color: var(--muted); font-size: .72rem; text-transform: uppercase; letter-spacing: .05em; }
.mc-stat .value { margin-top: .35rem; font-size: 1.13rem; font-weight: 750; font-variant-numeric: tabular-nums; }
.mc-table { margin-top: .9rem; }
.mc-table th, .mc-table td { text-align: right; }
.mc-table th:first-child, .mc-table td:first-child { text-align: left; }
.mc-chart { width: 100%; min-width: 720px; height: 430px; display: block; }
.mc-chart text { fill: #243b53; font-size: 11px; }
.mc-chart .grid { stroke: rgba(36,59,83,.16); stroke-width: 1; }
.mc-chart .axis { stroke: rgba(36,59,83,.38); stroke-width: 1; }
.mc-chart .zero { stroke: rgba(36,59,83,.55); stroke-dasharray: 5 4; }
.mc-chart .initial-line { stroke: rgba(36,59,83,.72); stroke-dasharray: 6 4; stroke-width: 1.2; }
.mc-chart .band-wide { fill: rgba(93,182,255,.18); }
.mc-chart .band-central { fill: rgba(66,217,160,.22); }
.mc-chart .path { fill: none; stroke: rgba(36,59,83,.16); stroke-width: 1; vector-effect: non-scaling-stroke; }
.mc-chart .median { fill: none; stroke: #7c6cff; stroke-width: 2.6; vector-effect: non-scaling-stroke; }
.mc-legend { display: flex; flex-wrap: wrap; gap: .65rem 1rem; padding: .45rem 0 0; color: var(--muted); font-size: .78rem; }
.mc-legend span::before { content: ""; display: inline-block; width: 22px; height: 3px; margin-right: .35rem; vertical-align: middle; border-radius: 99px; background: var(--accent); }
.mc-legend .central::before { background: var(--positive); }
.mc-legend .median::before { background: var(--accent-2); }
.mc-legend .path-line::before { height: 1px; background: rgba(36,59,83,.42); }
.data-table { width: 100%; border-collapse: collapse; font-size: .82rem; font-variant-numeric: tabular-nums; }
.data-table th, .data-table td { padding: .48rem .55rem; border-bottom: 1px solid rgba(141,165,196,.13); text-align: right; white-space: nowrap; }
.data-table th:first-child, .data-table td:first-child { text-align: left; }
.data-table th { color: var(--muted); font-size: .7rem; text-transform: uppercase; letter-spacing: .04em; }
.data-table tbody tr:hover { background: rgba(93,182,255,.07); }
.monthly-table td, .monthly-table th { text-align: center; }
.monthly-table { width: 100%; table-layout: fixed; }
.monthly-table th, .monthly-table td { padding: .38rem .18rem; font-size: .74rem; white-space: normal; overflow-wrap: anywhere; }
.monthly-table th:first-child { width: 4.2rem; }
.monthly-table th:last-child { width: 4.2rem; }
.monthly-table-wrap { width: 100%; overflow: hidden; }
.monthly-drawdown-table { width: 100%; table-layout: fixed; }
.monthly-drawdown-table th, .monthly-drawdown-table td { white-space: normal; overflow-wrap: anywhere; }
.mobile-only { display: none; }
.monthly-table .positive { background: rgba(66,217,160,.12); color: #a1f2d3; }
.monthly-table .negative { background: rgba(255,109,131,.13); color: #ffabb9; }
.monthly-table .zero, .undefined { background: rgba(141,165,196,.08); color: var(--muted); }
.matrix-wrap { overflow-x: auto; }
.matrix td { min-width: 78px; text-align: center; border: 1px solid rgba(141,165,196,.12); }
.matrix th { text-align: center; }
.notice { padding: .72rem .85rem; border: 1px solid rgba(255,201,107,.35); background: rgba(255,201,107,.08); color: #ffdda0; border-radius: 10px; }
.notice.muted { border-color: var(--border); background: rgba(141,165,196,.07); color: var(--muted); }
.warning-list { display: grid; gap: .45rem; }
.warning { padding: .65rem .75rem; border-left: 3px solid var(--warning); background: rgba(255,201,107,.06); border-radius: 5px; }
.warning code { color: var(--warning); }
.small { color: var(--muted); font-size: .78rem; }
.pagination { display: flex; align-items: center; justify-content: space-between; gap: .6rem; margin-top: .75rem; }
.empty { color: var(--muted); padding: 1rem 0; }
.two-col { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 1rem; }
@media (max-width: 840px) { .two-col { grid-template-columns: 1fr; } .toolbar .meta { margin-left: 0; width: 100%; } .shell { padding-left: .75rem; padding-right: .75rem; } .toolbar { margin-left: -.75rem; margin-right: -.75rem; padding-left: .75rem; padding-right: .75rem; } }
@media (max-width: 640px) {
  html, body { overflow-x: hidden; }
  .shell { padding-left: .65rem; padding-right: .65rem; }
  .hero { padding-top: 1.25rem; }
  .brand-logo { border-radius: 10px; margin-bottom: 1rem; }
  .title-row { min-width: 0; flex-direction: column; align-items: flex-start; }
  .title-row h1 { width: 100%; min-width: 0; font-size: clamp(1.65rem, 8vw, 2.25rem); overflow-wrap: anywhere; }
  .title-edit-button { margin-top: .15rem; }
  .toolbar { min-width: 0; display: grid; grid-template-columns: max-content minmax(0, 1fr); margin-left: -.65rem; margin-right: -.65rem; padding-left: .65rem; padding-right: .65rem; gap: .45rem; }
  .toolbar > .control-label { min-width: 0; align-self: center; }
  .toolbar > select { width: 100%; min-width: 0; max-width: 100%; }
  .toolbar > button, .toolbar > .filter-chip { width: max-content; max-width: 100%; justify-self: start; }
  .toolbar > .meta { grid-column: 1 / -1; }
  .section { margin: 1rem 0; }
  .section-heading { min-width: 0; align-items: flex-start; }
  .section-heading > .small { flex-basis: 100%; }
  .panel { min-width: 0; padding: .72rem; border-radius: 12px; }
  .metric-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: .45rem; }
  .metric { min-width: 0; min-height: 78px; padding: .58rem; }
  .metric .label-row, .metric .label, .metric .value { min-width: 0; overflow-wrap: anywhere; }
  .metric .value { font-size: 1rem; }
  .mobile-only { display: inline; }
  .monthly-table-wrap, .matrix-wrap, .chart-wrap { overflow-x: auto; overflow-y: hidden; -webkit-overflow-scrolling: touch; scrollbar-width: thin; }
  .monthly-table-wrap { margin: 0 -.72rem; padding: 0 .72rem .35rem; }
  .monthly-table { width: max-content; min-width: 760px; table-layout: auto; }
  .monthly-table th, .monthly-table td { min-width: 3.8rem; white-space: nowrap; }
  .monthly-table th:first-child, .monthly-table td:first-child { position: sticky; left: 0; z-index: 2; min-width: 4.5rem; background: var(--panel-2); box-shadow: 3px 0 5px rgba(0,0,0,.18); }
  .monthly-table th:last-child, .monthly-table td:last-child { position: sticky; right: 0; z-index: 2; min-width: 4.5rem; background: var(--panel-2); box-shadow: -3px 0 5px rgba(0,0,0,.18); }
  .monthly-table thead th:first-child, .monthly-table thead th:last-child { z-index: 3; }
  .matrix-wrap .data-table { min-width: 760px; }
  .mc-table { width: 100%; min-width: 0; max-width: 100%; }
  .mc-table .data-table { width: max-content; min-width: 720px; }
  .nav { min-width: 0; }
  .nav a { max-width: 100%; }
  .pagination { align-items: flex-start; flex-direction: column; }
  .title-editor { align-items: stretch; }
  .title-editor label, .title-editor input { width: 100%; }
}
</style>
</head>
<body>
<div class="shell">
  <header class="hero">
    <img class="brand-logo" src="__REPORT_LOGO__" alt="Strategy Analyser logo">
    <div class="eyebrow">Deterministic MT5 analysis</div>
    <div class="title-row"><h1 id="reportTitle">__REPORT_TITLE__</h1><button id="editTitleButton" class="title-edit-button" type="button">Edit name</button></div>
    <div id="titleEditor" class="title-editor" hidden>
      <label for="reportTitleInput">Report name<input id="reportTitleInput" type="text" maxlength="140" autocomplete="off"></label>
      <button id="saveTitleButton" type="button">Save name</button><button id="cancelTitleButton" type="button">Cancel</button><span id="titleEditStatus" class="small status" aria-live="polite"></span>
    </div>
    <p id="reportDescription">__REPORT_DESCRIPTION__</p>
  </header>
  <div class="toolbar" aria-label="Report controls">
    <label class="control-label" for="dataSelect">Data</label>
    <select id="dataSelect" aria-label="Select portfolio or strategy"></select>
    <label class="control-label" for="directionSelect">Direction</label>
    <select id="directionSelect" aria-label="Select trade direction">
      <option value="all">Long + Short</option>
      <option value="long">Long only</option>
      <option value="short">Short only</option>
    </select>
    <span class="filter-chip" id="filterChip">All trades</span>
    <button id="resetButton" type="button">Reset</button>
    <span class="meta" id="toolbarMeta"></span>
  </div>
  <nav class="nav" role="tablist" aria-label="Report sections">
    <a id="tab-overview" href="#overview" role="tab" data-tab="overview" aria-controls="overview" aria-selected="true" tabindex="0">Overview</a><a id="tab-equity" href="#equity" role="tab" data-tab="equity" aria-controls="equity" aria-selected="false" tabindex="-1">Equity</a><a id="tab-trade-analysis" href="#trade-analysis" role="tab" data-tab="trade-analysis" aria-controls="trade-analysis" aria-selected="false" tabindex="-1">Trade analysis</a>
    <a id="tab-monthly" href="#monthly" role="tab" data-tab="monthly" aria-controls="monthly" aria-selected="false" tabindex="-1">Monthly performance</a><a id="tab-correlation" href="#correlation" role="tab" data-tab="correlation" aria-controls="correlation" aria-selected="false" tabindex="-1">Correlation</a><a id="tab-trades" href="#trades" role="tab" data-tab="trades" aria-controls="trades" aria-selected="false" tabindex="-1">Trades</a><a id="tab-monte-carlo" href="#monte-carlo" role="tab" data-tab="monte-carlo" aria-controls="monte-carlo" aria-selected="false" tabindex="-1" class="monte-carlo-tab">Monte Carlo</a><a id="tab-audit" href="#audit" role="tab" data-tab="audit" aria-controls="audit" aria-selected="false" tabindex="-1">Warnings & provenance</a>
  </nav>

  <main>
    <section class="section tab-panel" id="overview" role="tabpanel" data-tab-panel="overview" aria-labelledby="tab-overview">
      <div class="section-heading"><h2>Overview</h2><span class="small" id="overviewMeta"></span></div>
      <div class="panel"><div class="metric-grid" id="metrics"></div></div>
    </section>

    <section class="section tab-panel" id="equity" role="tabpanel" data-tab-panel="equity" aria-labelledby="tab-equity" hidden>
      <div class="section-heading"><h2>Equity & drawdown</h2><div class="controls">
        <button id="valueMode" type="button" aria-label="Toggle equity and drawdown values between percentage and currency">Values in %</button>
        <label class="control-label"><input id="memberToggle" type="checkbox"> show strategies</label>
        <label class="control-label"><input id="periodToggle" type="checkbox"> show sample periods</label>
        <span class="controls" aria-label="Chart navigation"><button id="panLeft" type="button" title="Pan left">←</button><button id="zoomOut" type="button" title="Zoom out">−</button><button id="zoomIn" type="button" title="Zoom in">+</button><button id="panRight" type="button" title="Pan right">→</button><button id="resetChart" type="button">Reset view</button></span>
      </div></div>
      <div class="panel chart-panel"><div class="chart-wrap" id="equityChart"></div><div class="legend" id="equityLegend"></div><div class="chart-tooltip" id="chartTooltip"></div></div>
    </section>

    <section class="section tab-panel" id="trade-analysis" role="tabpanel" data-tab-panel="trade-analysis" aria-labelledby="tab-trade-analysis" hidden>
      <div class="section-heading"><h2>Trade analysis</h2><div class="controls">
        <label class="control-label" for="groupingSelect">Group by</label><select id="groupingSelect">
          <option value="open_hour">Opening hour</option><option value="close_hour">Closing hour</option><option value="open_day_of_week">Opening day</option><option value="close_day_of_week">Closing day</option>
        </select>
        <label class="control-label" for="tradeMeasure">Measure</label><select id="tradeMeasure"><option value="net_profit">Net profit</option><option value="percentage_gain">Percentage gain</option></select>
      </div></div>
      <div class="panel"><div id="tradeBars"></div></div>
    </section>

    <section class="section tab-panel" id="monthly" role="tabpanel" data-tab-panel="monthly" aria-labelledby="tab-monthly" hidden>
      <div class="section-heading"><h2>Monthly performance</h2><span class="small">Percentages; two decimals; YTD is compounded. <span class="mobile-only">Swipe horizontally to view all columns.</span></span></div>
      <div class="panel monthly-panel"><div class="monthly-table-wrap" id="monthlyTable"></div></div>
      <div class="section-heading"><h2>Monthly drawdown</h2><span class="small">Maximum intramonth drawdown; Worst is the annual minimum. <span class="mobile-only">Swipe horizontally to view all columns.</span></span></div>
      <div class="panel monthly-panel"><div class="monthly-table-wrap" id="monthlyDrawdownTable"></div></div>
    </section>

    <section class="section tab-panel" id="correlation" role="tabpanel" data-tab-panel="correlation" aria-labelledby="tab-correlation" hidden>
      <div class="section-heading"><h2>Daily profit correlation</h2><div class="controls" id="correlationControls">
        <label class="control-label" for="correlationMode">Series</label><select id="correlationMode"><option value="raw">Raw</option><option value="allocated">Allocated</option></select>
      </div></div>
      <div class="panel" id="correlationPanel"></div>
    </section>

    <section class="section tab-panel" id="trades" role="tabpanel" data-tab-panel="trades" aria-labelledby="tab-trades" hidden>
      <div class="section-heading"><h2>Completed positions</h2><div class="controls">
        <input id="tradeSearch" type="search" placeholder="Search symbol or strategy" aria-label="Search trades">
        <label class="control-label" for="tradeSort">Sort</label><select id="tradeSort"><option value="close_desc">Close time ↓</option><option value="close_asc">Close time ↑</option><option value="profit_desc">Net profit ↓</option><option value="profit_asc">Net profit ↑</option></select>
      </div></div>
      <div class="panel"><div class="matrix-wrap" id="tradeTable"></div><div class="pagination" id="pagination"></div></div>
    </section>

    <section class="section tab-panel" id="monte-carlo" role="tabpanel" data-tab-panel="monte-carlo" aria-labelledby="tab-monte-carlo" hidden>
      <div class="section-heading"><h2>Monte Carlo robustness</h2><div class="controls"><button id="downloadMonteCarlo" type="button">Download Monte Carlo JSON</button></div></div>
      <div class="panel" id="monteCarloPanel"></div>
    </section>

    <section class="section tab-panel" id="audit" role="tabpanel" data-tab-panel="audit" aria-labelledby="tab-audit" hidden>
      <div class="section-heading"><h2>Warnings & provenance</h2><div class="controls"><button id="downloadCsv" type="button">Download CSV</button><button id="downloadJson" type="button">Download JSON</button><button id="downloadSvg" type="button">Download SVG</button><button id="downloadPng" type="button">Download PNG</button><button id="copyLink" type="button">Copy view link</button></div></div>
      <div class="two-col"><div class="panel"><h3>Diagnostics</h3><div class="warning-list" id="warnings"></div></div><div class="panel"><h3>Provenance</h3><div id="provenance"></div></div></div>
    </section>
  </main>
</div>
<script id="report-data" type="application/json">__REPORT_PAYLOAD__</script>
<script id="report-config" type="application/json">__REPORT_CONFIG__</script>
<script>
(() => {
  "use strict";
  const report = JSON.parse(document.getElementById("report-data").textContent);
  const config = JSON.parse(document.getElementById("report-config").textContent);
  const TAB_IDS = ["overview", "equity", "trade-analysis", "monthly", "correlation", "trades", "monte-carlo", "audit"];
  const state = {
    activeTab: "overview",
    direction: report.default_direction || "all",
    data: report.default_data || "single",
    curve: "primary",
    valueMode: "percent",
    drawdownMode: "percent",
    showMembers: report.kind === "portfolio",
    showPeriods: true,
    grouping: "open_hour",
    measure: "net_profit",
    correlationMode: "raw",
    search: "",
    sort: "close_desc",
    page: 1,
    hiddenCurves: {},
    windowStart: 0,
    windowEnd: 1,
    title: document.getElementById("reportTitle").textContent.trim() || "Report",
  };
  const COLORS = ["#5db6ff", "#42d9a0", "#a88bff", "#ffc96b", "#ff8ca2", "#79d8e8", "#d69cff"];
  const metricSpecs = [
    ["net_profit", "Net profit", "money", "Canonical closed-position net result."],
    ["total_profit_pct", "Total return", "pct", "Net profit divided by initial capital."],
    ["cagr_pct", "CAGR", "pct", "Annualized geometric return."],
    ["max_drawdown_money", "Max drawdown", "money", "Largest high-water-mark decline."],
    ["max_drawdown_pct", "Max drawdown %", "pct", "Peak-relative maximum drawdown."],
    ["annualized_daily_sharpe_ratio", "Annualized Sharpe", "ratio", "Calendar-day Sharpe annualized by the configured factor."],
    ["calmar_ratio", "Calmar", "ratio", "CAGR divided by maximum drawdown."],
    ["recovery_factor", "Recovery factor", "ratio", "Net profit divided by maximum drawdown."],
    ["profit_factor", "Profit factor", "ratio", "Gross winning net profit divided by absolute losses."],
    ["win_rate_pct", "Win rate", "pct", "Winning completed positions as a percentage of trades."],
    ["total_trades", "Trades", "integer", "Completed positions included in this view."],
    ["average_win", "Average win", "money", "Mean net profit of winning positions."],
    ["average_loss", "Average loss", "money", "Mean net profit of losing positions."],
    ["expectancy", "Expectancy", "money", "Mean net result per completed position."],
    ["payoff_ratio", "Payoff ratio", "ratio", "Average win divided by absolute average loss."],
    ["max_consecutive_wins", "Max win streak", "integer", "Maximum consecutive positive positions."],
    ["max_consecutive_losses", "Max loss streak", "integer", "Maximum consecutive negative positions."],
    ["max_stagnation_days", "Stagnation days", "number", "Longest time below a prior equity high."],
  ];
  const monteCarloMetricSpecs = [
    ["net_profit", "Net profit", "money", "Distribution of simulated net profit."],
    ["final_equity", "Final equity", "money", "Distribution of simulated ending equity."],
    ["max_drawdown_money", "Max drawdown", "money", "Distribution of simulated high-water-mark drawdown."],
    ["max_drawdown_pct", "Max drawdown %", "pct", "Distribution of simulated peak-relative drawdown."],
    ["max_consecutive_wins", "Max win streak", "number", "Distribution of the longest winning streak."],
    ["max_consecutive_losses", "Max loss streak", "number", "Distribution of the longest losing streak."],
  ];
  const groupingLabels = {open_hour: "Opening hour", close_hour: "Closing hour", open_day_of_week: "Opening day", close_day_of_week: "Closing day"};
  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value == null ? "" : value).replace(/[&<>\"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[char]));
  const fmt = (value, kind = "number") => {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "N/A";
    const number = Number(value);
    if (kind === "integer") return number.toLocaleString(undefined, {maximumFractionDigits: 0});
    if (kind === "pct") return `${number.toFixed(2)}%`;
    if (kind === "money") return `${number.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})} ${currentView().currency || ""}`.trim();
    if (kind === "number") return number.toFixed(2);
    return number.toFixed(2);
  };
  const currencyUnit = (currency) => ({USD: "$", EUR: "€", GBP: "£", JPY: "¥", AUD: "A$", NZD: "NZ$", CAD: "C$"}[currency] || currency || "$" );
  const axisMoneyLabel = (value, currency) => {
    const number = Number(value);
    const symbol = currencyUnit(currency);
    const sign = number < 0 ? "-" : "";
    const absolute = Math.abs(number);
    if (absolute >= 1000) return `${sign}${symbol}${Math.round(absolute / 1000)}k`;
    return `${sign}${symbol}${Math.round(absolute)}`;
  };
  const axisLabel = (value, mode, currency) => mode === "money" ? axisMoneyLabel(value, currency) : `${Number(value).toFixed(2)}%`;
  function axisTicks(min, max, mode) {
    if (mode !== "money") return [0, .25, .5, .75, 1].map((fraction) => min + fraction * (max - min));
    const lower = Math.floor(min / 1000) * 1000;
    const upper = Math.ceil(max / 1000) * 1000;
    const span = Math.max(1000, upper - lower);
    const step = Math.max(1000, Math.ceil(span / 4 / 1000) * 1000);
    const ticks = [];
    for (let value = lower; value <= upper; value += step) ticks.push(value);
    if (ticks[ticks.length - 1] !== upper) ticks.push(upper);
    return ticks;
  }
  const currentVariant = () => report.variants[state.direction] || report.variants.all;
  const currentMember = () => {
    const variant = currentVariant();
    return report.kind === "portfolio" && state.data !== "portfolio" ? variant.members[state.data] : null;
  };
  const currentView = () => {
    const variant = currentVariant();
    const member = currentMember();
    return member ? member.analysis : variant;
  };
  const displayName = () => currentMember()?.strategy_name || currentView().display_name || "Report";
  const isoTime = (value) => value ? new Date(value).toLocaleString() : "N/A";
  const formatDate = (value) => value ? new Date(value).toLocaleDateString() : "N/A";
  function normalizeTitle(value) { return String(value == null ? "" : value).replace(/[\u0000-\u001f\u007f]/g, "").trim().slice(0, 140); }
  function applyTitle(value) {
    const title = normalizeTitle(value) || "Report";
    state.title = title;
    $("reportTitle").textContent = title;
    document.title = title;
    $("reportTitleInput").value = title;
  }
  function updateHash() {
    const params = new URLSearchParams({tab: state.activeTab, direction: state.direction, data: state.data, curve: state.curve, equity: state.valueMode, drawdown: state.drawdownMode, members: state.showMembers ? "1" : "0", start: state.windowStart.toFixed(4), end: state.windowEnd.toFixed(4), title: state.title});
    history.replaceState(null, "", `#${params.toString()}`);
  }
  function readHash() {
    const rawHash = location.hash.slice(1);
    const params = new URLSearchParams(rawHash);
    if (TAB_IDS.includes(params.get("tab"))) state.activeTab = params.get("tab");
    else if (TAB_IDS.includes(rawHash)) state.activeTab = rawHash;
    if (["all", "long", "short"].includes(params.get("direction"))) state.direction = params.get("direction");
    if (report.kind === "portfolio" && (params.get("data") === "portfolio" || report.variants.all.members?.[params.get("data")])) state.data = params.get("data");
    if (["percent", "money"].includes(params.get("equity"))) state.valueMode = params.get("equity");
    state.drawdownMode = state.valueMode;
    state.showMembers = params.has("members") ? params.get("members") === "1" : report.kind === "portfolio";
    if (params.get("title")) applyTitle(params.get("title"));
    const hashStart = Number(params.get("start"));
    const hashEnd = Number(params.get("end"));
    if (Number.isFinite(hashStart) && Number.isFinite(hashEnd) && hashStart >= 0 && hashEnd <= 1 && hashStart < hashEnd) { state.windowStart = hashStart; state.windowEnd = hashEnd; }
  }
  function renderSelector() {
    const dataSelect = $("dataSelect");
    if (report.kind !== "portfolio") {
      dataSelect.innerHTML = "<option value='single'>Strategy report</option>";
      dataSelect.disabled = true;
      $("memberToggle").disabled = true;
      return;
    }
    const variant = currentVariant();
    const options = ["<option value='portfolio'>Portfolio</option>"];
    Object.entries(variant.members || {}).forEach(([key, member]) => options.push(`<option value='${esc(key)}'>${esc(member.strategy_name)}</option>`));
    dataSelect.innerHTML = options.join("");
    dataSelect.value = state.data;
    $("memberToggle").disabled = state.data !== "portfolio";
    $("memberToggle").checked = state.showMembers && state.data === "portfolio";
  }
  function renderToolbar() {
    renderSelector();
    $("directionSelect").value = state.direction;
    $("filterChip").textContent = state.direction === "all" ? "All trades" : state.direction === "long" ? "Long only" : "Short only";
    const view = currentView();
    const metrics = view.metrics || {};
    $("toolbarMeta").textContent = `${view.currency || ""} · ${view.timezone || "report timezone"} · ${fmt(metrics.total_trades, "integer")} trades`;
    $("overviewMeta").textContent = `${displayName()} · ${view.provenance?.input_format || "portfolio"} · ${view.timezone || "report timezone"}`;
  }
  function renderMetrics() {
    const metrics = currentView().metrics || {};
    $("metrics").innerHTML = metricSpecs.map(([key, label, kind, definition]) => `<div class='metric'><div class='label-row'><span class='label'>${esc(label)}</span><button class='info-icon' type='button' aria-label='Definition for ${esc(label)}' data-tooltip='${esc(definition)}' title='${esc(definition)}'>i</button></div><div class='value'>${esc(fmt(metrics[key], kind))}</div></div>`).join("");
  }
  function monteCarloQuantile(values, percentile) {
    if (!values.length) return null;
    const sorted = values.slice().sort((left, right) => left - right);
    const position = (sorted.length - 1) * percentile / 100;
    const lower = Math.floor(position);
    const upper = Math.ceil(position);
    if (lower === upper) return sorted[lower];
    return sorted[lower] + (sorted[upper] - sorted[lower]) * (position - lower);
  }
  function monteCarloPercentileSeries(paths, percentile) {
    if (!paths.length || !paths[0]?.length) return [];
    return Array.from({length: paths[0].length}, (_unused, index) => monteCarloQuantile(paths.map((path) => Number(path[index])).filter(Number.isFinite), percentile));
  }
  function monteCarloPoint(value, index, length, left, right, top, bottom, min, max) {
    const x = left + index / Math.max(1, length - 1) * (right - left);
    const y = bottom - (Number(value) - min) / (max - min || 1) * (bottom - top);
    return [x, y];
  }
  function monteCarloLinePath(values, left, right, top, bottom, min, max) {
    return values.map((value, index) => {
      const [x, y] = monteCarloPoint(value, index, values.length, left, right, top, bottom, min, max);
      return `${index ? "L" : "M"}${x.toFixed(2)},${y.toFixed(2)}`;
    }).join(" ");
  }
  function monteCarloBandPath(lower, upper, left, right, top, bottom, min, max) {
    const points = upper.map((value, index) => monteCarloPoint(value, index, upper.length, left, right, top, bottom, min, max));
    points.push(...lower.map((value, index) => monteCarloPoint(value, index, lower.length, left, right, top, bottom, min, max)).reverse());
    return `${points.map((point, index) => `${index ? "L" : "M"}${point[0].toFixed(2)},${point[1].toFixed(2)}`).join(" ")} Z`;
  }
  function renderMonteCarloPathChart(simulation) {
    const container = $("monteCarloChart");
    const paths = simulation.equity_paths || [];
    if (!paths.length || !paths[0]?.length) {
      container.innerHTML = "<div class='notice muted'>No retained simulated paths are available. Enable path retention to display the distribution chart.</div>";
      return;
    }
    const p5 = monteCarloPercentileSeries(paths, 5);
    const p25 = monteCarloPercentileSeries(paths, 25);
    const p50 = monteCarloPercentileSeries(paths, 50);
    const p75 = monteCarloPercentileSeries(paths, 75);
    const p95 = monteCarloPercentileSeries(paths, 95);
    let min = Number(simulation.initial_equity);
    let max = Number(simulation.initial_equity);
    paths.forEach((path) => path.forEach((value) => {
      const number = Number(value);
      if (!Number.isFinite(number)) return;
      min = Math.min(min, number);
      max = Math.max(max, number);
    }));
    if (!Number.isFinite(min)) min = 0;
    if (!Number.isFinite(max)) max = min + 1;
    const range = max - min || 1;
    min -= range * .08;
    max += range * .08;
    const width = 1140, height = 430, left = 118, right = 1110, top = 28, bottom = 378;
    const currency = simulation.currency || currentView().currency;
    const ticks = [0, .25, .5, .75, 1].map((fraction) => min + fraction * (max - min));
    const svg = [`<svg class='mc-chart' viewBox='0 0 ${width} ${height}' role='img' aria-label='Monte Carlo simulated equity paths'><rect x='0' y='0' width='${width}' height='${height}' fill='#ffffff'/>`];
    ticks.forEach((value) => {
      const y = bottom - (value - min) / (max - min || 1) * (bottom - top);
      svg.push(`<line class='grid' x1='${left}' x2='${right}' y1='${y.toFixed(2)}' y2='${y.toFixed(2)}'/><text x='${left-10}' y='${(y+4).toFixed(2)}' text-anchor='end'>${esc(axisMoneyLabel(value, currency))}</text>`);
    });
    const initial = Number(simulation.initial_equity);
    const initialY = bottom - (initial - min) / (max - min || 1) * (bottom - top);
    svg.push(`<line class='axis' x1='${left}' x2='${left}' y1='${top}' y2='${bottom}'/><line class='axis' x1='${left}' x2='${right}' y1='${bottom}' y2='${bottom}'/>`);
    svg.push(`<line class='initial-line' x1='${left}' x2='${right}' y1='${initialY.toFixed(2)}' y2='${initialY.toFixed(2)}'/><text class='initial-label' x='${left+6}' y='${Math.max(top+12, initialY-6)}'>Initial equity: ${esc(axisMoneyLabel(initial, currency))}</text>`);
    svg.push(`<path class='band-wide' d='${monteCarloBandPath(p5, p95, left, right, top, bottom, min, max)}'/><path class='band-central' d='${monteCarloBandPath(p25, p75, left, right, top, bottom, min, max)}'/>`);
    paths.forEach((path) => svg.push(`<path class='path' d='${monteCarloLinePath(path, left, right, top, bottom, min, max)}'/>`));
    svg.push(`<path class='median' d='${monteCarloLinePath(p50, left, right, top, bottom, min, max)}'/><text x='${left}' y='17'>Simulated equity</text><text x='${left + (right-left)/2}' y='${height-12}' text-anchor='middle'>Trade sequence · ${esc(String(paths.length))} retained paths</text></svg>`);
    container.innerHTML = `<div class='small'>Percentile bands are calculated at each simulated trade step from the retained paths. The browser only presents the deterministic simulation supplied by the analyser.</div><div class='chart-wrap'>${svg.join("")}</div><div class='mc-legend'><span class='path-line'>Retained paths</span><span>5–95% range</span><span class='central'>25–75% range</span><span class='median'>Median path</span></div>`;
  }
  function renderMonteCarlo() {
    const panel = $("monteCarloPanel");
    const simulation = report.monte_carlo;
    const downloadButton = $("downloadMonteCarlo");
    if (!simulation) {
      downloadButton.disabled = true;
      panel.innerHTML = "<div class='notice muted'>Monte Carlo was not run for this report. Enable it in the desktop GUI or pass a MonteCarloResult to the interactive report API.</div>";
      return;
    }
    downloadButton.disabled = false;
    const summary = simulation.summary || {};
    const statSpecs = [
      ["Probability of ruin", summary.probability_of_ruin_pct, "pct"],
      ["Median net profit", summary.net_profit?.p50, "money"],
      ["P95 max drawdown", summary.max_drawdown_money?.p95, "money"],
      ["Median final equity", summary.final_equity?.p50, "money"],
      ["P95 loss streak", summary.max_consecutive_losses?.p95, "number"],
    ];
    const cards = statSpecs.map(([label, value, kind]) => `<div class='mc-stat'><div class='label'>${esc(label)}</div><div class='value'>${esc(fmt(value, kind))}</div></div>`).join("");
    const rows = monteCarloMetricSpecs.map(([key, label, kind, definition]) => {
      const values = summary[key] || {};
      return `<tr><th><div class='label-row'><span class='label'>${esc(label)}</span><button class='info-icon' type='button' aria-label='Definition for ${esc(label)}' data-tooltip='${esc(definition)}' title='${esc(definition)}'>i</button></div></th><td>${esc(fmt(values.p5, kind))}</td><td>${esc(fmt(values.p50, kind))}</td><td>${esc(fmt(values.p95, kind))}</td><td>${esc(fmt(values.mean, kind))}</td><td>${esc(fmt(values.worst, kind))}</td></tr>`;
    }).join("");
    panel.innerHTML = `<div class='mc-summary-grid'>${cards}</div><div class='matrix-wrap mc-table'><table class='data-table'><thead><tr><th>Distribution</th><th>P5</th><th>Median</th><th>P95</th><th>Mean</th><th>Worst</th></tr></thead><tbody>${rows}</tbody></table></div><div class='panel chart-panel' style='margin-top:1rem'><div id='monteCarloChart'></div></div><p class='small'>Monte Carlo operates on completed-position net profits from the all-trades report view. Direction and member filters do not rerun this simulation. Permutation preserves the observed outcomes and changes their order; bootstrap samples with replacement. It does not simulate future tick paths or live execution.</p>`;
    renderMonteCarloPathChart(simulation);
  }
  function getCurve(view, key) {
    if (key === "reconstructed") return view.balance;
    if (key === "source_equity") return view.source_equity;
    if (key === "source_balance") return view.source_balance;
    return view.equity;
  }
  function valueAt(curve, index, mode) {
    const value = Number(curve.values[index]);
    if (mode === "money") return value;
    const initial = Number(curve.initial_value) || Number(curve.values[0]) || 1;
    return (value / initial - 1) * 100;
  }
  function drawdownValues(curve, mode) {
    let high = -Infinity;
    return curve.values.map((raw, index) => {
      const value = Number(raw);
      high = Math.max(high, value);
      if (mode === "money") return value - high;
      return high ? (value - high) / high * 100 : null;
    });
  }
  function xFor(timestamp, start, end, left, right) {
    if (end <= start) return left;
    return left + (new Date(timestamp).getTime() - start) / (end - start) * (right - left);
  }
  function pathFor(curve, values, start, end, left, right, top, bottom, min, max) {
    const points = [];
    curve.timestamps.forEach((timestamp, index) => {
      const time = new Date(timestamp).getTime();
      if (time < start || time > end) return;
      const value = values[index];
      if (value === null || value === undefined || !Number.isFinite(Number(value))) return;
      const x = xFor(timestamp, start, end, left, right);
      const y = bottom - (Number(value) - min) / (max - min || 1) * (bottom - top);
      points.push(`${points.length ? "L" : "M"}${x.toFixed(2)},${y.toFixed(2)}`);
    });
    return points.join(" ");
  }
  function addBands(periods, start, end, left, right, top, bottom) {
    if (!state.showPeriods || !periods || !Object.keys(periods).length) return "";
    const ordered = Object.values(periods).sort((leftPeriod, rightPeriod) => String(leftPeriod.start).localeCompare(String(rightPeriod.start)));
    const bands = [];
    let cursor = new Date(start).toISOString();
    const append = (periodStart, periodEnd, cls, label) => {
      const x1 = Math.max(left, xFor(periodStart, start, end, left, right));
      const x2 = Math.min(right, xFor(periodEnd, start, end, left, right));
      if (x2 > x1) bands.push(`<rect class='${cls}' x='${x1.toFixed(2)}' y='${top}' width='${(x2-x1).toFixed(2)}' height='${bottom-top}'><title>${esc(label)}</title></rect>`);
    };
    ordered.forEach((period) => {
      if (new Date(period.start).getTime() > new Date(cursor).getTime()) append(cursor, period.start, "band-excluded", "excluded");
      const cls = period.name === "in_sample" ? "band-is" : period.name === "out_of_sample" ? "band-oos" : "band-excluded";
      append(period.start, period.end, cls, period.name);
      if (new Date(period.end).getTime() > new Date(cursor).getTime()) cursor = period.end;
    });
    const endIso = new Date(end).toISOString();
    if (new Date(cursor).getTime() < end) append(cursor, endIso, "band-excluded", "excluded");
    return bands.join("");
  }
  function adjustZoom(factor) {
    const center = (state.windowStart + state.windowEnd) / 2;
    const width = Math.min(1, Math.max(.1, (state.windowEnd - state.windowStart) * factor));
    state.windowStart = Math.max(0, center - width / 2);
    state.windowEnd = Math.min(1, center + width / 2);
    if (state.windowStart === 0) state.windowEnd = width;
    if (state.windowEnd === 1) state.windowStart = 1 - width;
    renderEquity(); updateHash();
  }
  function panChart(amount) {
    const width = state.windowEnd - state.windowStart;
    const nextStart = Math.max(0, Math.min(1 - width, state.windowStart + amount * width));
    state.windowStart = nextStart; state.windowEnd = nextStart + width;
    renderEquity(); updateHash();
  }
  function renderEquity() {
    const view = currentView();
    state.drawdownMode = state.valueMode;
    state.curve = "primary";
    $("valueMode").textContent = `Values in ${state.valueMode === "percent" ? "%" : currencyUnit(view.currency)}`;
    $("valueMode").setAttribute("aria-pressed", state.valueMode === "money" ? "true" : "false");
    $("periodToggle").checked = state.showPeriods;
    const curve = getCurve(view, state.curve);
    if (!curve || !curve.timestamps.length) { $("equityChart").innerHTML = "<div class='empty'>No equity observations are available for this view.</div>"; $("equityLegend").innerHTML = ""; return; }
    const showMemberCurves = report.kind === "portfolio" && state.data === "portfolio" && state.showMembers;
    const memberOnlyMoney = showMemberCurves && state.valueMode === "money";
    const selected = memberOnlyMoney ? [] : [{key: "portfolio", label: displayName(), curve, color: COLORS[0]}];
    if (showMemberCurves) {
      Object.values(currentVariant().members || {}).forEach((member, index) => {
        const memberCurve = member.analysis?.allocated_equity;
        if (memberCurve) selected.push({key: `member-${member.member_key}`, label: member.strategy_name, curve: memberCurve, color: COLORS[(index + 1) % COLORS.length]});
      });
    }
    if (!selected.length) {
      if (memberOnlyMoney) {
        $("equityChart").innerHTML = "<div class='empty'>Individual strategy equity curves are unavailable for this portfolio view.</div>";
        $("equityLegend").innerHTML = "";
        return;
      }
      selected.push({key: "portfolio", label: displayName(), curve, color: COLORS[0]});
    }
    const allTimes = selected.flatMap((item) => item.curve.timestamps.map((timestamp) => new Date(timestamp).getTime()));
    const fullStart = Math.min(...allTimes), fullEnd = Math.max(...allTimes);
    const start = fullStart + (fullEnd - fullStart) * state.windowStart;
    const end = fullStart + (fullEnd - fullStart) * state.windowEnd;
    const left = 118, right = 1110, top = 28, equityBottom = 202, ddTop = 260, ddBottom = 408;
    const equityValues = selected.flatMap((item) => item.curve.values.map((_, index) => valueAt(item.curve, index, state.valueMode)));
    const initialCurve = memberOnlyMoney ? selected[0].curve : curve;
    const initialMoney = Number(initialCurve.initial_value) || Number(initialCurve.values[0]) || 0;
    const initialAxisValue = state.valueMode === "money" ? initialMoney : 0;
    let min = Math.min(initialAxisValue, ...equityValues), max = Math.max(initialAxisValue, ...equityValues);
    if (min === max) { max = initialAxisValue + 1; }
    if (state.valueMode === "money") {
      min = Math.floor(min / 1000) * 1000;
      max = Math.ceil(max / 1000) * 1000;
      if (min === max) max = min + 1000;
    } else {
      const range = max - min || 1;
      const pad = range * .08;
      if (min < initialAxisValue) min -= pad;
      max += pad;
    }
    const ddValues = selected.flatMap((item) => drawdownValues(item.curve, state.drawdownMode).filter((value) => value !== null));
    let ddMin = Math.min(0, ...ddValues), ddMax = Math.max(0, ...ddValues);
    if (ddMin === ddMax) ddMin = -1;
    const svg = [];
    svg.push(`<svg class='chart' id='equitySvg' viewBox='0 0 1140 435' role='img' aria-label='Equity and drawdown chart'><rect x='0' y='0' width='1140' height='435' fill='#ffffff'/>`);
    svg.push(addBands(view.periods, start, end, left, right, top, ddBottom));
    axisTicks(min, max, state.valueMode).forEach((value) => { const y = equityBottom - (value-min)/(max-min || 1) * (equityBottom-top); svg.push(`<line class='grid' x1='${left}' x2='${right}' y1='${y}' y2='${y}'/><text x='${left-10}' y='${y+4}' text-anchor='end'>${esc(axisLabel(value, state.valueMode, view.currency))}</text>`); });
    [0, .5, 1].forEach((fraction) => { const y = ddBottom - fraction * (ddBottom-ddTop); const value = ddMin + fraction*(ddMax-ddMin); svg.push(`<line class='grid' x1='${left}' x2='${right}' y1='${y}' y2='${y}'/><text x='${left-10}' y='${y+4}' text-anchor='end'>${esc(axisLabel(value, state.valueMode, view.currency))}</text>`); });
    const zeroY = ddBottom - (0-ddMin)/(ddMax-ddMin || 1)*(ddBottom-ddTop);
    const initialY = equityBottom - (initialAxisValue-min)/(max-min || 1)*(equityBottom-top);
    const initialLabel = `${memberOnlyMoney ? "Initial strategy allocation" : "Initial balance"}: ${axisMoneyLabel(initialMoney, view.currency)}${state.valueMode === "percent" ? " (0.00%)" : ""}`;
    svg.push(`<line class='zero' x1='${left}' x2='${right}' y1='${zeroY}' y2='${zeroY}'/>`);
    svg.push(`<line class='initial-line' x1='${left}' x2='${right}' y1='${initialY}' y2='${initialY}'/><text class='initial-label' x='${left+6}' y='${Math.max(top+12, initialY-6)}'>${esc(initialLabel)}</text>`);
    svg.push(`<line class='axis' x1='${left}' x2='${right}' y1='${equityBottom}' y2='${equityBottom}'/><line class='axis' x1='${left}' x2='${right}' y1='${ddBottom}' y2='${ddBottom}'/>`);
    svg.push(`<text x='${left}' y='17'>Equity</text><text class='drawdown-axis-label' x='${left}' y='250'>Drawdown</text>`);
    selected.forEach((item) => {
      const hidden = state.hiddenCurves[item.key];
      const isMember = item.key.startsWith("member-");
      const values = item.curve.values.map((_, index) => valueAt(item.curve, index, state.valueMode));
      const dds = drawdownValues(item.curve, state.drawdownMode);
      const visibility = hidden ? "display:none" : "";
      const equityClass = isMember ? "equity-line member-equity-line" : "equity-line";
      const drawdownClass = isMember ? "drawdown-line member-drawdown-line" : "drawdown-line";
      svg.push(`<path class='${equityClass}' data-series='${esc(item.key)}' d='${pathFor(item.curve, values, start, end, left, right, top, equityBottom, min, max)}' stroke='${item.color}' style='${visibility}'/>`);
      svg.push(`<path class='${drawdownClass}' data-series='${esc(item.key)}' d='${pathFor(item.curve, dds, start, end, left, right, ddTop, ddBottom, ddMin, ddMax)}' stroke='${item.color}' style='${visibility}' opacity='.8'/>`);
    });
    svg.push(`<line id='chartHoverLine' class='hover-line' x1='${left}' x2='${left}' y1='${top}' y2='${ddBottom}' style='display:none'/><rect id='chartHover' x='${left}' y='${top}' width='${right-left}' height='${ddBottom-top}' fill='transparent'/>`);
    svg.push(`</svg>`);
    $("equityChart").innerHTML = svg.join("");
    $("equityLegend").innerHTML = selected.map((item) => `<button type='button' data-legend='${esc(item.key)}' class='${state.hiddenCurves[item.key] ? "off" : ""}'><span class='swatch' style='background:${item.color}'></span>${esc(item.label)}</button>`).join("");
    document.querySelectorAll("[data-legend]").forEach((button) => button.addEventListener("click", () => { const key = button.dataset.legend; state.hiddenCurves[key] = !state.hiddenCurves[key]; renderEquity(); }));
    const hover = $("chartHover");
    const line = $("chartHoverLine");
    const hoverItem = selected[0];
    const hoverCurve = hoverItem.curve;
    hover.addEventListener("mousemove", (event) => {
      const rect = hover.getBoundingClientRect();
      const ratio = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
      const timestamp = start + ratio * (end - start);
      const visibleIndexes = hoverCurve.timestamps.map((value, index) => ({value, index})).filter((item) => { const time = new Date(item.value).getTime(); return time >= start && time <= end; });
      if (!visibleIndexes.length) return;
      const primaryIndex = visibleIndexes.reduce((best, item) => Math.abs(new Date(item.value).getTime()-timestamp) < Math.abs(new Date(best.value).getTime()-timestamp) ? item : best).index;
      const x = xFor(hoverCurve.timestamps[primaryIndex], start, end, left, right);
      line.setAttribute("x1", x); line.setAttribute("x2", x); line.style.display = "block";
      const tip = $("chartTooltip");
      tip.innerHTML = `<b>${esc(isoTime(hoverCurve.timestamps[primaryIndex]))}</b><br>${esc(hoverItem.label)}: ${esc(fmt(valueAt(hoverCurve, primaryIndex, state.valueMode), state.valueMode === "money" ? "money" : "number"))}`;
      tip.style.display = "block"; tip.style.left = `${event.clientX + 14}px`; tip.style.top = `${event.clientY + 14}px`;
    });
    hover.addEventListener("mouseleave", () => { line.style.display = "none"; $("chartTooltip").style.display = "none"; });
    hover.addEventListener("wheel", (event) => { event.preventDefault(); adjustZoom(event.deltaY < 0 ? .8 : 1.25); }, {passive: false});
    let dragX = null;
    hover.addEventListener("pointerdown", (event) => { dragX = event.clientX; hover.setPointerCapture(event.pointerId); });
    hover.addEventListener("pointerup", (event) => { if (dragX !== null) { const delta = (dragX - event.clientX) / Math.max(1, hover.getBoundingClientRect().width); panChart(delta); } dragX = null; });
  }
  function renderBars() {
    const view = currentView();
    const grouping = view.trade_profit?.[state.grouping];
    if (!grouping) { $("tradeBars").innerHTML = "<div class='empty'>Trade grouping is unavailable.</div>"; return; }
    const buckets = grouping.buckets || [];
    const values = buckets.map((bucket) => state.measure === "net_profit" ? bucket.net_profit : bucket.percentage_gain).map((value) => value == null ? 0 : Number(value));
    const kind = state.measure === "net_profit" ? "money" : "pct";
    const measureLabel = state.measure === "net_profit" ? `Net profit (${view.currency || "currency"})` : "Percentage gain (%)";
    const width = 1140, height = 360, left = 78, right = 24, top = 24, bottom = 68;
    const plotBottom = height - bottom, plotHeight = plotBottom - top, plotWidth = width - left - right;
    const min = Math.min(0, ...values), max = Math.max(0, ...values), span = max - min || 1;
    const yFor = (value) => plotBottom - (value - min) / span * plotHeight;
    const zeroY = yFor(0);
    const ticks = [0, .25, .5, .75, 1].map((fraction) => min + fraction * span);
    const barWidth = Math.max(4, Math.min(44, plotWidth / Math.max(1, buckets.length) * .62));
    const svg = [`<svg class='trade-chart' viewBox='0 0 ${width} ${height}' role='img' aria-label='${esc(measureLabel)} by ${esc(groupingLabels[state.grouping])}'>`];
    ticks.forEach((value) => {
      const y = yFor(value);
      svg.push(`<line class='grid' x1='${left}' x2='${width-right}' y1='${y.toFixed(2)}' y2='${y.toFixed(2)}'/><text x='${left-10}' y='${(y+4).toFixed(2)}' text-anchor='end'>${esc(fmt(value, kind))}</text>`);
    });
    svg.push(`<line class='axis' x1='${left}' x2='${left}' y1='${top}' y2='${plotBottom}'/><line class='axis' x1='${left}' x2='${width-right}' y1='${plotBottom}' y2='${plotBottom}'/><line class='zero' x1='${left}' x2='${width-right}' y1='${zeroY.toFixed(2)}' y2='${zeroY.toFixed(2)}'/>`);
    buckets.forEach((bucket, index) => {
      const value = values[index];
      const x = left + (index + .5) * plotWidth / Math.max(1, buckets.length);
      const valueY = yFor(value);
      const y = value >= 0 ? valueY : zeroY;
      const barHeight = Math.max(1, Math.abs(zeroY - valueY));
      const labelY = value >= 0 ? Math.max(top + 12, y - 6) : Math.min(plotBottom + 14, y + barHeight + 14);
      svg.push(`<rect class='${value >= 0 ? "bar-positive" : "bar-negative"}' x='${(x-barWidth/2).toFixed(2)}' y='${y.toFixed(2)}' width='${barWidth.toFixed(2)}' height='${barHeight.toFixed(2)}' rx='3'><title>${esc(bucket.label)} · ${esc(fmt(value, kind))}</title></rect>`);
      svg.push(`<text class='bar-value' x='${x.toFixed(2)}' y='${labelY.toFixed(2)}' text-anchor='middle'>${esc(fmt(value, kind))}</text><text x='${x.toFixed(2)}' y='${plotBottom+18}' text-anchor='middle'>${esc(bucket.label)}</text>`);
    });
    svg.push(`<text x='${left/2}' y='${top + plotHeight/2}' text-anchor='middle' transform='rotate(-90 ${left/2} ${top + plotHeight/2})'>${esc(measureLabel)}</text><text x='${left + plotWidth/2}' y='${height-12}' text-anchor='middle'>Timing · ${esc(groupingLabels[state.grouping])}</text></svg>`);
    $("tradeBars").innerHTML = `<div class='small'>${esc(groupingLabels[state.grouping])} · ${esc(measureLabel)} · ${esc(grouping.timezone || "report timezone")}</div><div class='chart-wrap'>${svg.join("")}</div>`;
  }
  function shadeClass(value) { if (value === null || value === undefined) return "undefined"; if (value > 0) return "positive"; if (value < 0) return "negative"; return "zero"; }
  function renderMonthly() {
    const table = currentView().monthly_performance;
    if (!table || !table.rows?.length) { $("monthlyTable").innerHTML = "<div class='empty'>Monthly performance is unavailable.</div>"; return; }
    const labels = table.month_labels || [];
    $("monthlyTable").innerHTML = `<table class='data-table monthly-table'><thead><tr><th>Year</th>${labels.map((label) => `<th>${esc(label)}</th>`).join("")}<th>YTD</th></tr></thead><tbody>${table.rows.map((row) => `<tr><th>${row.year}</th>${row.monthly_returns_pct.map((value) => `<td class='${shadeClass(value)}'>${esc(fmt(value, "pct"))}</td>`).join("")}<td class='${shadeClass(row.ytd_return_pct)}'>${esc(fmt(row.ytd_return_pct, "pct"))}</td></tr>`).join("")}</tbody></table>`;
    const drawdownTable = currentView().monthly_drawdown_table;
    if (!drawdownTable || !drawdownTable.rows?.length) { $("monthlyDrawdownTable").innerHTML = "<div class='empty'>Monthly drawdown is unavailable.</div>"; return; }
    const drawdownLabels = drawdownTable.month_labels || [];
    const maximumMagnitude = Math.max(0, ...drawdownTable.rows.flatMap((row) => row.monthly_drawdown_pct.concat(row.annual_worst_drawdown_pct)).filter((value) => value !== null && value !== undefined).map((value) => Math.abs(Number(value))));
    function drawdownHeatStyle(value) {
      if (value === null || value === undefined || Number.isNaN(Number(value))) return "background:rgba(255,109,131,.04);color:#8c1d32;";
      const magnitude = Math.min(1, Math.abs(Number(value)) / Math.max(maximumMagnitude, 1e-12));
      const alpha = (.12 + magnitude * .58).toFixed(3);
      return `background:rgba(220,38,38,${alpha});color:${magnitude > .45 ? "#fff" : "#7f1d1d"};`;
    }
    const drawdownCell = (value) => `<td class='${shadeClass(value)}' style='${drawdownHeatStyle(value)}'>${esc(fmt(value, "pct"))}</td>`;
    $("monthlyDrawdownTable").innerHTML = `<table class='data-table monthly-table monthly-drawdown-table'><thead><tr><th>Year</th>${drawdownLabels.map((label) => `<th>${esc(label)}</th>`).join("")}<th>${esc(drawdownTable.worst_label || "Worst")}</th></tr></thead><tbody>${drawdownTable.rows.map((row) => `<tr><th>${row.year}</th>${row.monthly_drawdown_pct.map(drawdownCell).join("")}${drawdownCell(row.annual_worst_drawdown_pct)}</tr>`).join("")}</tbody></table>`;
  }
  function matrixTable(matrix) {
    if (!matrix || !matrix.row_labels?.length) return "<div class='empty'>No correlation observations are available.</div>";
    const rows = matrix.row_labels.map((label, row) => `<tr><th>${esc(label)}</th>${matrix.values[row].map((value) => { const numeric = value == null ? null : Number(value); const alpha = numeric == null ? 0 : Math.min(.5, Math.abs(numeric)*.5); const color = numeric == null ? "transparent" : numeric >= 0 ? `rgba(66,217,160,${alpha})` : `rgba(255,109,131,${alpha})`; return `<td class='${numeric == null ? "undefined" : ""}' style='background:${color}'>${esc(fmt(numeric, "number"))}</td>`; }).join("")}</tr>`);
    return `<table class='data-table matrix'><thead><tr><th></th>${matrix.column_labels.map((label) => `<th>${esc(label)}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table>`;
  }
  function renderCorrelation() {
    const panel = $("correlationPanel");
    const controls = $("correlationControls");
    if (currentView().kind !== "portfolio" || !currentView().correlations) { controls.style.display = "none"; panel.innerHTML = "<div class='notice muted'>Correlation is not applicable to a single strategy. Select a portfolio to compare daily realized net-profit series.</div>"; return; }
    controls.style.display = "flex";
    const daily = currentView().correlations.daily_profit;
    const modeLabel = state.correlationMode === "allocated" ? "allocated" : "raw";
    panel.innerHTML = `<div class='small'>Daily realized net-profit correlation · ${esc(modeLabel)} series · ${esc(String(daily.observations))} overlapping observations · ${esc(daily.timezone || "report timezone")}</div><div class='matrix-wrap' style='margin-top:.75rem'>${matrixTable(daily.matrix)}</div><p class='small'>Positive scalar allocation changes profit magnitude but not Pearson correlation. Raw and allocated series are both embedded for export and audit.</p>`;
  }
  function renderWarnings() {
    const view = currentView();
    const warnings = view.warnings || [];
    $("warnings").innerHTML = warnings.length ? warnings.map((warning) => `<div class='warning'><code>${esc(warning.code)}</code><br>${esc(warning.message || "")}</div>`).join("") : "<div class='notice muted'>No warnings were emitted for this view.</div>";
    const provenance = view.provenance || {};
    const lines = Object.entries(provenance).map(([key, value]) => `<tr><th>${esc(key)}</th><td>${esc(typeof value === "object" ? JSON.stringify(value) : value)}</td></tr>`).join("");
    $("provenance").innerHTML = `<table class='data-table'><tbody>${lines}</tbody></table><p class='small'>Validation: ${esc(view.validation?.status || "not run")}. Original MT5 markup and redacted fields are not embedded.</p>`;
  }
  function filteredTrades() {
    const view = currentView();
    let rows = (view.trades || []).slice();
    const needle = state.search.trim().toLowerCase();
    if (needle) rows = rows.filter((trade) => `${trade.symbol || ""} ${trade.strategy_name || ""} ${trade.side || ""}`.toLowerCase().includes(needle));
    rows.sort((left, right) => {
      if (state.sort === "profit_desc" || state.sort === "profit_asc") { const diff = Number(left.net_profit || 0) - Number(right.net_profit || 0); return state.sort === "profit_desc" ? -diff : diff; }
      const diff = String(left.close_time || "").localeCompare(String(right.close_time || "")); return state.sort === "close_desc" ? -diff : diff;
    });
    return rows;
  }
  function renderTrades() {
    if (!config.include_trade_table) { $("tradeTable").innerHTML = "<div class='notice muted'>The trade table was disabled in InteractiveReportConfig.</div>"; $("pagination").innerHTML = ""; return; }
    const rows = filteredTrades();
    const pageSize = Number(config.table_page_size || 50);
    const pages = Math.max(1, Math.ceil(rows.length / pageSize)); state.page = Math.min(state.page, pages);
    const visible = rows.slice((state.page-1)*pageSize, state.page*pageSize);
    const showStrategy = currentView().kind === "portfolio";
    const headers = `${showStrategy ? "<th>Strategy</th>" : ""}<th>Side</th><th>Symbol</th><th>Open</th><th>Close</th><th>Volume</th><th>Net profit</th><th>Swap</th><th>Commission</th>`;
    $("tradeTable").innerHTML = visible.length ? `<table class='data-table'><thead><tr>${headers}</tr></thead><tbody>${visible.map((trade) => `<tr>${showStrategy ? `<td>${esc(trade.strategy_name)}</td>` : ""}<td>${esc(trade.side)}</td><td>${esc(trade.symbol)}</td><td>${esc(isoTime(trade.open_time))}</td><td>${esc(isoTime(trade.close_time))}</td><td>${esc(fmt(trade.volume, "number"))}</td><td class='${Number(trade.net_profit) >= 0 ? "positive" : "negative"}'>${esc(fmt(trade.net_profit, "money"))}</td><td>${esc(fmt(trade.swap, "money"))}</td><td>${esc(fmt(trade.commission, "money"))}</td></tr>`).join("")}</tbody></table>` : "<div class='empty'>No completed positions match the current view.</div>";
    $("pagination").innerHTML = `<span class='small'>${rows.length ? `${(state.page-1)*pageSize+1}–${Math.min(state.page*pageSize, rows.length)} of ${rows.length}` : "0 trades"}</span><span class='controls'><button type='button' id='prevPage' ${state.page <= 1 ? "disabled" : ""}>Previous</button><button type='button' id='nextPage' ${state.page >= pages ? "disabled" : ""}>Next</button></span>`;
    $("prevPage")?.addEventListener("click", () => { state.page--; renderTrades(); }); $("nextPage")?.addEventListener("click", () => { state.page++; renderTrades(); });
  }
  function renderTabs() {
    document.querySelectorAll("[data-tab]").forEach((tab) => {
      const active = tab.dataset.tab === state.activeTab;
      tab.setAttribute("aria-selected", active ? "true" : "false");
      tab.tabIndex = active ? 0 : -1;
    });
    document.querySelectorAll("[data-tab-panel]").forEach((panel) => {
      panel.hidden = panel.dataset.tabPanel !== state.activeTab;
    });
  }
  function selectTab(tabId) {
    state.activeTab = TAB_IDS.includes(tabId) ? tabId : "overview";
    renderTabs();
    updateHash();
    window.scrollTo(0, 0);
  }
  function moveTab(offset) {
    const currentIndex = Math.max(0, TAB_IDS.indexOf(state.activeTab));
    const nextIndex = (currentIndex + offset + TAB_IDS.length) % TAB_IDS.length;
    const nextTab = TAB_IDS[nextIndex];
    selectTab(nextTab);
    document.querySelector(`[data-tab="${nextTab}"]`)?.focus();
  }
  function rerender() { renderTabs(); renderToolbar(); renderMetrics(); renderMonteCarlo(); renderEquity(); renderBars(); renderMonthly(); renderCorrelation(); renderWarnings(); renderTrades(); updateHash(); }
  function download(name, content, type) { const blob = content instanceof Blob ? content : new Blob([content], {type}); const link = document.createElement("a"); link.href = URL.createObjectURL(blob); link.download = name; link.click(); setTimeout(() => URL.revokeObjectURL(link.href), 1000); }
  function downloadDataUrl(name, dataUrl) { const link = document.createElement("a"); link.href = dataUrl; link.download = name; link.click(); }
  function csvValue(value) { const text = value == null ? "" : String(value); return `"${text.replace(/"/g, '""')}"`; }
  function downloadCsv() { const rows = filteredTrades(); if (!rows.length) return; const keys = Object.keys(rows[0]); download("trades.csv", [keys.join(","), ...rows.map((row) => keys.map((key) => csvValue(row[key])).join(","))].join("\n"), "text/csv"); }
  function downloadJson() { download("analysis-view.json", JSON.stringify({title: state.title, tab: state.activeTab, direction: state.direction, data: state.data, view: currentView()}, null, 2), "application/json"); }
  function downloadMonteCarlo() { if (report.monte_carlo) download("monte-carlo.json", JSON.stringify(report.monte_carlo, null, 2), "application/json"); }
  function downloadSvg() { const svg = $("equitySvg"); if (svg) download("equity-drawdown.svg", new XMLSerializer().serializeToString(svg), "image/svg+xml"); }
  function downloadPng() { const svg = $("equitySvg"); if (!svg) return; const source = new XMLSerializer().serializeToString(svg); const image = new Image(); image.onload = () => { const canvas = document.createElement("canvas"); canvas.width = 1140; canvas.height = 435; const context = canvas.getContext("2d"); context.fillStyle = "#0d1c31"; context.fillRect(0, 0, canvas.width, canvas.height); context.drawImage(image, 0, 0); downloadDataUrl("equity-drawdown.png", canvas.toDataURL("image/png")); }; image.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(source)}`; }
  function bind() {
    $("editTitleButton").addEventListener("click", () => { $("titleEditor").hidden = false; $("titleEditStatus").textContent = ""; $("reportTitleInput").value = state.title; $("reportTitleInput").focus(); $("reportTitleInput").select(); });
    $("cancelTitleButton").addEventListener("click", () => { $("titleEditor").hidden = true; $("titleEditStatus").textContent = ""; $("reportTitleInput").value = state.title; });
    $("saveTitleButton").addEventListener("click", () => { const title = normalizeTitle($("reportTitleInput").value); if (!title) { $("titleEditStatus").textContent = "Enter a report name."; $("reportTitleInput").focus(); return; } applyTitle(title); $("titleEditor").hidden = true; $("titleEditStatus").textContent = ""; updateHash(); });
    $("reportTitleInput").addEventListener("keydown", (event) => { if (event.key === "Enter") $("saveTitleButton").click(); if (event.key === "Escape") $("cancelTitleButton").click(); });
    document.querySelectorAll("[data-tab]").forEach((tab) => {
      tab.addEventListener("click", (event) => { event.preventDefault(); selectTab(tab.dataset.tab); });
      tab.addEventListener("keydown", (event) => {
        if (event.key === "ArrowRight" || event.key === "ArrowDown") { event.preventDefault(); moveTab(1); }
        else if (event.key === "ArrowLeft" || event.key === "ArrowUp") { event.preventDefault(); moveTab(-1); }
        else if (event.key === "Home") { event.preventDefault(); selectTab(TAB_IDS[0]); document.querySelector(`[data-tab="${TAB_IDS[0]}"]`)?.focus(); }
        else if (event.key === "End") { event.preventDefault(); selectTab(TAB_IDS[TAB_IDS.length - 1]); document.querySelector(`[data-tab="${TAB_IDS[TAB_IDS.length - 1]}"]`)?.focus(); }
        else if (event.key === "Enter" || event.key === " ") { event.preventDefault(); selectTab(tab.dataset.tab); }
      });
    });
    window.addEventListener("hashchange", () => { readHash(); rerender(); window.scrollTo(0, 0); });
    $("dataSelect").addEventListener("change", (event) => { state.data = event.target.value; state.page = 1; state.hiddenCurves = {}; rerender(); });
    $("directionSelect").addEventListener("change", (event) => { state.direction = event.target.value; state.page = 1; state.hiddenCurves = {}; rerender(); });
    $("resetButton").addEventListener("click", () => { state.direction = "all"; state.data = report.default_data; state.curve = "primary"; state.valueMode = "percent"; state.drawdownMode = "percent"; state.showMembers = report.kind === "portfolio"; state.showPeriods = true; state.windowStart = 0; state.windowEnd = 1; state.page = 1; state.search = ""; $("tradeSearch").value = ""; rerender(); });
    $("valueMode").addEventListener("click", () => { state.valueMode = state.valueMode === "percent" ? "money" : "percent"; state.drawdownMode = state.valueMode; renderEquity(); updateHash(); });
    $("memberToggle").addEventListener("change", (event) => {
      state.showMembers = event.target.checked;
      if (state.showMembers && report.kind === "portfolio") {
        state.valueMode = "percent";
        state.drawdownMode = "percent";
      }
      renderEquity();
      updateHash();
    });
    $("periodToggle").addEventListener("change", (event) => { state.showPeriods = event.target.checked; renderEquity(); updateHash(); });
    $("panLeft").addEventListener("click", () => panChart(-.5)); $("panRight").addEventListener("click", () => panChart(.5));
    $("zoomIn").addEventListener("click", () => adjustZoom(.7)); $("zoomOut").addEventListener("click", () => adjustZoom(1.4));
    $("resetChart").addEventListener("click", () => { state.windowStart = 0; state.windowEnd = 1; renderEquity(); updateHash(); });
    $("groupingSelect").addEventListener("change", (event) => { state.grouping = event.target.value; renderBars(); });
    $("tradeMeasure").addEventListener("change", (event) => { state.measure = event.target.value; renderBars(); });
    $("correlationMode").addEventListener("change", (event) => { state.correlationMode = event.target.value; renderCorrelation(); });
    $("tradeSearch").addEventListener("input", (event) => { state.search = event.target.value; state.page = 1; renderTrades(); });
    $("tradeSort").addEventListener("change", (event) => { state.sort = event.target.value; renderTrades(); });
    $("downloadMonteCarlo").addEventListener("click", downloadMonteCarlo);
    $("downloadCsv").addEventListener("click", downloadCsv); $("downloadJson").addEventListener("click", downloadJson); $("downloadSvg").addEventListener("click", downloadSvg); $("downloadPng").addEventListener("click", downloadPng);
    $("copyLink").addEventListener("click", async () => { try { await navigator.clipboard.writeText(location.href); $("copyLink").textContent = "Copied"; setTimeout(() => $("copyLink").textContent = "Copy view link", 1200); } catch (_error) { $("copyLink").textContent = "Copy unavailable"; } });
  }
  readHash(); bind(); rerender();
})();
</script>
</body>
</html>
'''


__all__ = [
    "InteractiveReportConfig",
    "InteractiveReportServer",
    "render_interactive_report",
    "save_interactive_report",
    "serve_interactive_report",
]
