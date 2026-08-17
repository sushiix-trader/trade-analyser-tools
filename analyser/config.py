"""Explicit analysis configuration.

The defaults are intentionally conservative and are recorded in every
:class:`AnalysisResult` so repeated analyses can be reproduced exactly.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .periods import SamplePeriodConfig
from .what_if import WhatIfConfig


@dataclass(frozen=True)
class SharpeConfig:
    method: str = "custom_trade_event"
    return_type: str = "simple"
    risk_free_rate: float = 0.0
    annualization_factor: float | None = None
    ddof: int = 0
    daily_risk_free_rate: float = 0.0
    daily_annualization_factor: float | None = 365.2425


@dataclass(frozen=True)
class AnalysisConfig:
    timezone: str = "report"
    primary_curve: str = "source_then_reconstructed"
    include_breakdowns: bool = True
    include_monthly: bool = True
    include_drawdown_series: bool = True
    strict: bool = False
    warn_via_python_warnings: bool = False
    sharpe: SharpeConfig = field(default_factory=SharpeConfig)
    sample_periods: SamplePeriodConfig | None = None
    what_if: WhatIfConfig | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
