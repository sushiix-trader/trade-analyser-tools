"""Optional local persistence for eager analysis results.

The store is deliberately explicit: callers choose where report-derived
artifacts are written.  A cache key is derived from the input bytes, analysis
configuration, parser version, and package version, so changing any of those
causes a new artifact rather than stale reuse.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .analysis import AnalysisResult, analyze
from .config import AnalysisConfig
from .load import InputSource, load_report, read_input
from .serialization import deterministic_json

PACKAGE_VERSION = "0.1.0"
PARSER_VERSION = "1"


@dataclass(frozen=True)
class AnalysisArtifact:
    key: str
    path: Path
    result: AnalysisResult
    cache_hit: bool


@dataclass(frozen=True)
class PortfolioAnalysisArtifact:
    key: str
    path: Path
    result: "PortfolioAnalysisResult"
    cache_hit: bool


class AnalysisStore:
    """A deterministic filesystem store for completed analysis results."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key_for_bytes(data: bytes, config: AnalysisConfig | None = None) -> str:
        config = config or AnalysisConfig()
        descriptor = {
            "input_sha256": hashlib.sha256(data).hexdigest(),
            "analysis_config": config.to_dict(),
            "parser_version": PARSER_VERSION,
            "package_version": PACKAGE_VERSION,
        }
        return hashlib.sha256(deterministic_json(descriptor).encode("utf-8")).hexdigest()

    def path_for(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def portfolio_path_for(self, key: str) -> Path:
        return self.root / "portfolio" / f"{key}.json"

    def contains(self, key: str) -> bool:
        return self.path_for(key).is_file()

    def save(self, key: str, result: AnalysisResult) -> Path:
        destination = self.path_for(key)
        temporary = destination.with_suffix(".json.tmp")
        temporary.write_text(result.to_json(), encoding="utf-8")
        os.replace(temporary, destination)
        return destination

    def save_portfolio(self, key: str, result: "PortfolioAnalysisResult") -> Path:
        destination = self.portfolio_path_for(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".json.tmp")
        temporary.write_text(result.to_json(), encoding="utf-8")
        os.replace(temporary, destination)
        return destination

    def load(self, key: str) -> AnalysisResult:
        payload = json.loads(self.path_for(key).read_text(encoding="utf-8"))
        return AnalysisResult.from_dict(payload)

    def load_portfolio(self, key: str) -> "PortfolioAnalysisResult":
        from .portfolio import PortfolioAnalysisResult

        payload = json.loads(self.portfolio_path_for(key).read_text(encoding="utf-8"))
        return PortfolioAnalysisResult.from_dict(payload)

    @staticmethod
    def key_for_portfolio(
        descriptors: Sequence[dict[str, Any]],
        config: "PortfolioConfig",
    ) -> str:
        descriptor = {
            "members": list(descriptors),
            "portfolio_config": config.to_dict(),
            "parser_version": PARSER_VERSION,
            "package_version": PACKAGE_VERSION,
        }
        return hashlib.sha256(deterministic_json(descriptor).encode("utf-8")).hexdigest()

    def analyze_or_load(
        self,
        source: InputSource,
        config: AnalysisConfig | None = None,
    ) -> AnalysisArtifact:
        config = config or AnalysisConfig()
        data, filename = read_input(source)
        key = self.key_for_bytes(data, config)
        path = self.path_for(key)
        if path.is_file():
            return AnalysisArtifact(key, path, self.load(key), True)

        report = load_report(data)
        report.source_file = filename
        result = analyze(report, config)
        self.save(key, result)
        return AnalysisArtifact(key, path, result, False)

    def analyze_portfolio_or_load(
        self,
        members: Sequence["PortfolioMember"],
        config: "PortfolioConfig | None" = None,
    ) -> PortfolioAnalysisArtifact:
        """Cache member analyses and the aggregate portfolio result."""

        from .portfolio import (
            AnalyzedPortfolioMember,
            PortfolioConfig,
            _analyze_member_bytes,
            combine_analyses,
        )

        config = config or PortfolioConfig()
        prepared: list[tuple["PortfolioMember", bytes, str]] = []
        descriptors: list[dict[str, Any]] = []
        for member in members:
            if member.source is None:
                raise ValueError("PortfolioMember.source is required for cached analysis")
            data, filename = read_input(member.source)
            input_hash = hashlib.sha256(data).hexdigest()
            prepared.append((member, data, filename))
            descriptors.append({
                "input_sha256": input_hash,
                "strategy_name": member.strategy_name,
                "description": member.description,
                "weight": member.weight,
            })
        key = self.key_for_portfolio(descriptors, config)
        path = self.portfolio_path_for(key)
        if path.is_file():
            return PortfolioAnalysisArtifact(key, path, self.load_portfolio(key), True)

        analyzed: list[AnalyzedPortfolioMember] = []
        for member, data, filename in prepared:
            member_key = hashlib.sha256(data).hexdigest()
            individual_key = self.key_for_bytes(data, config.analysis_config)
            individual_path = self.path_for(individual_key)
            if individual_path.is_file():
                result = self.load(individual_key)
                result.report.source_file = filename
                result.provenance["source_filename"] = Path(filename).name
            else:
                analyzed_member = _analyze_member_bytes(
                    member, data, filename, config.analysis_config
                )
                result = analyzed_member.analysis
                self.save(individual_key, result)
            analyzed.append(AnalyzedPortfolioMember(member_key, member, result))
        result = combine_analyses(analyzed, config)
        self.save_portfolio(key, result)
        return PortfolioAnalysisArtifact(key, path, result, False)

    def delete(self, key: str) -> bool:
        path = self.path_for(key)
        if not path.exists():
            return False
        path.unlink()
        return True
