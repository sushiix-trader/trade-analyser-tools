"""Framework-agnostic GUI orchestration over the public analyser API.

This module is deliberately the only place where the desktop front end
coordinates report analysis, report rendering, serializers, and optional
Monte Carlo artifacts.  It does not parse MT5 files or calculate analytics.
Those responsibilities remain in :mod:`analyser`.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
from pathlib import Path
import re
from typing import Any

from analyser import (
    AnalysisConfig,
    AnalysisResult,
    DEFAULT_REPORT_MONTE_CARLO_CONFIG,
    InteractiveReportConfig,
    MonteCarloConfig,
    MonteCarloPathChartConfig,
    MonteCarloResult,
    analyze_file,
    run_monte_carlo,
    save_equity_drawdown_chart,
    save_interactive_report,
    save_monte_carlo_paths,
)


_SUPPORTED_SUFFIXES = frozenset({".htm", ".html", ".xml"})
_DEFAULT_PATH_COUNT = 500


def _default_report_monte_carlo_config() -> MonteCarloConfig:
    """Return the shared complete-report Monte Carlo configuration."""

    return DEFAULT_REPORT_MONTE_CARLO_CONFIG


@dataclass(frozen=True)
class GuiRunConfig:
    """Inputs for one desktop report-generation run.

    ``source`` is one single-run MT5 HTML/XML report.  A GUI run always
    produces the complete interactive HTML report and text serializers.  The
    default includes a deterministic permutation Monte Carlo run with bounded
    retained paths.  Pass ``monte_carlo=None`` explicitly to opt out.  The
    simulation operates on the already parsed canonical report, so the GUI does
    not parse the input a second time.
    """

    source: Path | str
    output_dir: Path | str
    analysis_config: AnalysisConfig = field(default_factory=AnalysisConfig)
    monte_carlo: MonteCarloConfig | None = field(
        default_factory=_default_report_monte_carlo_config
    )
    generate_equity_chart: bool = True
    generate_monte_carlo_chart: bool = True
    report_title: str | None = None

    def __post_init__(self) -> None:
        source = Path(self.source)
        output_dir = Path(self.output_dir)
        if not isinstance(self.analysis_config, AnalysisConfig):
            raise TypeError("analysis_config must be an AnalysisConfig")
        if self.monte_carlo is not None and not isinstance(self.monte_carlo, MonteCarloConfig):
            raise TypeError("monte_carlo must be a MonteCarloConfig or None")
        if not isinstance(self.generate_equity_chart, bool):
            raise TypeError("generate_equity_chart must be a boolean")
        if not isinstance(self.generate_monte_carlo_chart, bool):
            raise TypeError("generate_monte_carlo_chart must be a boolean")
        if self.report_title is not None and not isinstance(self.report_title, str):
            raise TypeError("report_title must be a string or None")
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "output_dir", output_dir)


@dataclass(frozen=True)
class GuiRunResult:
    """Artifacts returned by :func:`run_analysis`.

    The eager analyser result and Monte Carlo result (unless explicitly
    disabled) remain available to callers who want to inspect typed values.
    Paths point to the generated
    presentation and serialization artifacts that a desktop user can open.
    """

    analysis_result: AnalysisResult
    report_path: Path
    analysis_json_path: Path
    analysis_markdown_path: Path
    equity_chart_path: Path | None
    monte_carlo_result: MonteCarloResult | None = None
    monte_carlo_summary_path: Path | None = None
    monte_carlo_json_path: Path | None = None
    monte_carlo_chart_path: Path | None = None
    warnings: tuple[str, ...] = ()

    @property
    def output_paths(self) -> tuple[Path, ...]:
        """Return generated paths in stable display order."""

        paths: list[Path] = [
            self.report_path,
            self.analysis_json_path,
            self.analysis_markdown_path,
        ]
        if self.equity_chart_path is not None:
            paths.append(self.equity_chart_path)
        for path in (
            self.monte_carlo_summary_path,
            self.monte_carlo_json_path,
            self.monte_carlo_chart_path,
        ):
            if path is not None:
                paths.append(path)
        return tuple(paths)


def run_analysis(config: GuiRunConfig) -> GuiRunResult:
    """Run one GUI analysis through public ``analyser`` functions only.

    The function is intentionally independent of Tkinter so it can be called
    from another front end, tested headlessly, or used by a future packaged
    desktop application.  Standalone PNG chart generation is optional because
    matplotlib is an optional dependency; the interactive HTML report and
    eager serialized result do not depend on it.
    """

    if not isinstance(config, GuiRunConfig):
        raise TypeError("config must be a GuiRunConfig")
    source = config.source
    if not source.exists():
        raise FileNotFoundError(f"MT5 report does not exist: {source}")
    if not source.is_file():
        raise ValueError(f"MT5 report source is not a file: {source}")
    if source.suffix.lower() not in _SUPPORTED_SUFFIXES:
        raise ValueError("GUI report input must have an .htm, .html, or .xml suffix")

    output_dir = config.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_stem(source.stem)
    title = config.report_title or f"{source.stem} — Interactive Report"
    warnings: list[str] = []

    # The analyser owns parsing, eager calculations, diagnostics, and
    # provenance.  The GUI only passes the result to presentation adapters.
    result = analyze_file(source, config.analysis_config)
    warnings.extend(_diagnostic_messages(result.warnings))

    monte_carlo_result: MonteCarloResult | None = None
    monte_carlo_summary_path: Path | None = None
    monte_carlo_json_path: Path | None = None
    monte_carlo_chart_path: Path | None = None
    if config.monte_carlo is not None:
        monte_carlo_config = _path_config_if_needed(
            config.monte_carlo,
            generate_chart=config.generate_monte_carlo_chart,
        )
        # Use the canonical report already held by AnalysisResult.  This is
        # the public parsed-report seam and avoids a second file parse.
        monte_carlo_result = run_monte_carlo(result.report, monte_carlo_config)

    report_path = output_dir / f"{stem}-interactive-report.html"
    save_interactive_report(
        result,
        report_path,
        InteractiveReportConfig(title=title),
        monte_carlo=monte_carlo_result,
    )

    analysis_json_path = output_dir / f"{stem}-analysis.json"
    analysis_json_path.write_text(result.to_json() + "\n", encoding="utf-8")

    analysis_markdown_path = output_dir / f"{stem}-analysis.md"
    analysis_markdown_path.write_text(result.to_markdown(), encoding="utf-8")

    equity_chart_path: Path | None = None
    if config.generate_equity_chart:
        equity_chart_path = _try_save_equity_chart(
            result,
            output_dir / f"{stem}-equity-drawdown.png",
            title,
            warnings,
        )

    if monte_carlo_result is not None:
        monte_carlo_summary_path = output_dir / f"{stem}-monte-carlo-summary.json"
        monte_carlo_summary_path.write_text(
            _pretty_json(monte_carlo_result.summary()),
            encoding="utf-8",
        )
        monte_carlo_json_path = output_dir / f"{stem}-monte-carlo.json"
        monte_carlo_json_path.write_text(
            monte_carlo_result.to_json() + "\n",
            encoding="utf-8",
        )

        if config.generate_monte_carlo_chart:
            monte_carlo_chart_path = _try_save_monte_carlo_chart(
                monte_carlo_result,
                output_dir / f"{stem}-monte-carlo-paths.png",
                title,
                warnings,
            )

    return GuiRunResult(
        analysis_result=result,
        report_path=report_path,
        analysis_json_path=analysis_json_path,
        analysis_markdown_path=analysis_markdown_path,
        equity_chart_path=equity_chart_path,
        monte_carlo_result=monte_carlo_result,
        monte_carlo_summary_path=monte_carlo_summary_path,
        monte_carlo_json_path=monte_carlo_json_path,
        monte_carlo_chart_path=monte_carlo_chart_path,
        warnings=tuple(warnings),
    )


def _path_config_if_needed(
    config: MonteCarloConfig,
    *,
    generate_chart: bool,
) -> MonteCarloConfig:
    """Ensure bounded path retention when the caller requests a path chart."""

    if not generate_chart:
        return config
    path_count = config.path_count or min(config.iterations, _DEFAULT_PATH_COUNT)
    return replace(config, retain_paths=True, path_count=path_count)


def _try_save_equity_chart(
    result: AnalysisResult,
    destination: Path,
    title: str,
    warnings: list[str],
) -> Path | None:
    try:
        return save_equity_drawdown_chart(result, destination, title=title)
    except RuntimeError as exc:
        if "requires matplotlib" not in str(exc):
            raise
        warnings.append(str(exc))
        return None


def _try_save_monte_carlo_chart(
    result: MonteCarloResult,
    destination: Path,
    title: str,
    warnings: list[str],
) -> Path | None:
    try:
        return save_monte_carlo_paths(
            result,
            destination,
            title=f"{title} — Monte Carlo paths",
            chart_config=MonteCarloPathChartConfig(show_streaks=True),
        )
    except RuntimeError as exc:
        if "requires matplotlib" not in str(exc):
            raise
        warnings.append(str(exc))
        return None


def _diagnostic_messages(diagnostics: tuple[Any, ...]) -> list[str]:
    return [f"{item.code}: {item.message}" for item in diagnostics]


def _pretty_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"


def _safe_stem(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip(".-")
    return cleaned or "report"
