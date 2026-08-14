"""Structured warnings and validation diagnostics."""

from __future__ import annotations

import warnings as _warnings
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    severity: str = "warning"
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def add_diagnostic(
    collection: list[Diagnostic],
    code: str,
    message: str,
    *,
    severity: str = "warning",
    context: dict[str, Any] | None = None,
    emit: bool = False,
) -> None:
    diagnostic = Diagnostic(code, message, severity, context or {})
    collection.append(diagnostic)
    if emit:
        _warnings.warn(message, RuntimeWarning, stacklevel=3)


@dataclass(frozen=True)
class ValidationResult:
    status: str = "not_run"
    checks: dict[str, Any] = field(default_factory=dict)
    discrepancies: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
