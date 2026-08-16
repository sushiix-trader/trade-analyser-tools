"""Deterministic in-sample/out-of-sample period metadata and suggestions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

from .errors import SamplePeriodConfigurationError
from .load import InputSource


def _coerce_boundary(value: date | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    raise SamplePeriodConfigurationError(
        "period boundaries must be datetime or date values"
    )


@dataclass(frozen=True)
class PeriodWindow:
    """One named half-open analysis period."""

    name: str
    start: date | datetime
    end: date | datetime
    source: str = "explicit"
    confidence: float | None = None
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        name = self.name.strip()
        if not name:
            raise SamplePeriodConfigurationError("period name is required")
        start = _coerce_boundary(self.start)
        end = _coerce_boundary(self.end)
        if start >= end:
            raise SamplePeriodConfigurationError("period start must be before period end")
        if self.source not in {"explicit", "inferred"}:
            raise SamplePeriodConfigurationError(
                "period source must be 'explicit' or 'inferred'"
            )
        if self.confidence is not None and not 0.0 <= float(self.confidence) <= 1.0:
            raise SamplePeriodConfigurationError("period confidence must be between 0 and 1")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        object.__setattr__(self, "evidence", tuple(self.evidence))

    def contains(self, timestamp: datetime | None) -> bool:
        if timestamp is None:
            return False
        try:
            return self.start <= timestamp < self.end
        except TypeError as exc:
            raise SamplePeriodConfigurationError(
                "sample-period boundaries and report timestamps must use the same aware/naive timestamp style"
            ) from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "source": self.source,
            "confidence": self.confidence,
            "evidence": self.evidence,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "PeriodWindow":
        return cls(
            name=str(payload["name"]),
            start=datetime.fromisoformat(str(payload["start"])),
            end=datetime.fromisoformat(str(payload["end"])),
            source=str(payload.get("source", "explicit")),
            confidence=payload.get("confidence"),
            evidence=tuple(payload.get("evidence", ())),
        )


@dataclass(frozen=True)
class SamplePeriodConfig:
    """Named period configuration.

    Any active configuration must contain both canonical v1 windows:
    ``in_sample`` and ``out_of_sample``. Additional named windows are retained
    for future extensibility but do not change the v1 calculations.
    """

    windows: Mapping[str, PeriodWindow]

    def __post_init__(self) -> None:
        normalized = dict(self.windows)
        if set(normalized) and not {"in_sample", "out_of_sample"}.issubset(normalized):
            raise SamplePeriodConfigurationError(
                "sample periods must define both 'in_sample' and 'out_of_sample'"
            )
        for name, window in normalized.items():
            if not isinstance(window, PeriodWindow):
                raise SamplePeriodConfigurationError(
                    f"sample period {name!r} must be a PeriodWindow"
                )
            if name != window.name:
                raise SamplePeriodConfigurationError(
                    f"sample period key {name!r} does not match window name {window.name!r}"
                )
        awareness = {window.start.tzinfo is not None for window in normalized.values()}
        if len(awareness) > 1:
            raise SamplePeriodConfigurationError(
                "all sample-period boundaries must use the same aware/naive timestamp style"
            )
        names = list(normalized)
        for index, left_name in enumerate(names):
            for right_name in names[index + 1 :]:
                left = normalized[left_name]
                right = normalized[right_name]
                if left.start < right.end and right.start < left.end:
                    raise SamplePeriodConfigurationError(
                        f"sample periods {left_name!r} and {right_name!r} overlap"
                    )
        object.__setattr__(self, "windows", normalized)

    @property
    def enabled(self) -> bool:
        return bool(self.windows)

    def __getitem__(self, name: str) -> PeriodWindow:
        return self.windows[name]

    def __iter__(self):
        return iter(self.windows)

    def to_dict(self) -> dict[str, Any]:
        return {"windows": {name: window.to_dict() for name, window in self.windows.items()}}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> "SamplePeriodConfig | None":
        if not payload:
            return None
        return cls(
            windows={
                name: PeriodWindow.from_dict(window)
                for name, window in payload.get("windows", {}).items()
            }
        )


@dataclass(frozen=True)
class PeriodSuggestion:
    window: PeriodWindow
    confidence: float
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class PeriodSuggestionResult:
    in_sample: PeriodSuggestion | None = None
    out_of_sample: PeriodSuggestion | None = None
    warnings: tuple[str, ...] = ()
    accepted: SamplePeriodConfig | None = None

    def accept(self) -> SamplePeriodConfig:
        if self.in_sample is None or self.out_of_sample is None:
            raise SamplePeriodConfigurationError(
                "both in-sample and out-of-sample suggestions are required"
            )
        accepted = SamplePeriodConfig(
            windows={
                "in_sample": self.in_sample.window,
                "out_of_sample": self.out_of_sample.window,
            }
        )
        object.__setattr__(self, "accepted", accepted)
        return accepted


_YEAR_RANGE = re.compile(r"(?<!\d)(20\d{2})\s*[-_]\s*(20\d{2})(?!\d)")
_IS_MARKER = re.compile(r"(?:^|[^a-z])(in[\s_-]*sample|is)(?:$|[^a-z])")
_OOS_MARKER = re.compile(r"(?:^|[^a-z])(out[\s_-]*of[\s_-]*sample|oos)(?:$|[^a-z])")


def _source_text(source: InputSource) -> str:
    if isinstance(source, (str, Path)):
        return str(Path(source))
    name = getattr(source, "name", "")
    return str(name) if name else ""


def _suggest_for_marker(text: str, marker: re.Pattern[str], name: str) -> PeriodSuggestion | None:
    parts = [part for part in re.split(r"[/\\]", text) if part]
    for part in parts:
        if marker.search(part.lower()):
            match = _YEAR_RANGE.search(part)
            if match:
                start_year, end_year = (int(item) for item in match.groups())
                window = PeriodWindow(
                    name,
                    datetime(start_year, 1, 1),
                    datetime(end_year + 1, 1, 1),
                    source="inferred",
                    confidence=0.95,
                    evidence=(part,),
                )
                return PeriodSuggestion(window, 0.95, (part,))
    return None


def suggest_sample_periods(source: InputSource) -> PeriodSuggestionResult:
    """Suggest conservative named periods from report path/folder metadata.

    Suggestions are never activated automatically.  A caller must explicitly
    call :meth:`PeriodSuggestionResult.accept` or construct a
    :class:`SamplePeriodConfig` manually.
    """

    text = _source_text(source)
    if not text:
        return PeriodSuggestionResult(warnings=("source has no filename metadata",))
    in_sample = _suggest_for_marker(text, _IS_MARKER, "in_sample")
    out_of_sample = _suggest_for_marker(text, _OOS_MARKER, "out_of_sample")
    warnings: list[str] = []
    if in_sample is None:
        warnings.append("no conservative in-sample folder/name suggestion was found")
    if out_of_sample is None:
        warnings.append("no conservative out-of-sample folder/name suggestion was found")
    return PeriodSuggestionResult(in_sample, out_of_sample, tuple(warnings))
