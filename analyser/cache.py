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
from typing import Any

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

    def contains(self, key: str) -> bool:
        return self.path_for(key).is_file()

    def save(self, key: str, result: AnalysisResult) -> Path:
        destination = self.path_for(key)
        temporary = destination.with_suffix(".json.tmp")
        temporary.write_text(result.to_json(), encoding="utf-8")
        os.replace(temporary, destination)
        return destination

    def load(self, key: str) -> AnalysisResult:
        payload = json.loads(self.path_for(key).read_text(encoding="utf-8"))
        return AnalysisResult.from_dict(payload)

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

    def delete(self, key: str) -> bool:
        path = self.path_for(key)
        if not path.exists():
            return False
        path.unlink()
        return True
