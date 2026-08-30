"""Optional QuantAnalyzer-style simulations.

Simulations are intentionally separate from the eager report analytics API.
"""

from .money_management import MoneyManagementResult, fixed_fractional, fixed_ratio
from .monte_carlo import (
    DEFAULT_REPORT_MONTE_CARLO_CONFIG,
    MonteCarloConfig,
    MonteCarloResult,
    run_monte_carlo,
    run_monte_carlo_file,
)
from .what_if import (
    add_commission,
    by_hours,
    by_symbol,
    by_weekdays,
    in_date_range,
    remove_worst_percent,
)

__all__ = [
    "run_monte_carlo",
    "run_monte_carlo_file",
    "MonteCarloConfig",
    "DEFAULT_REPORT_MONTE_CARLO_CONFIG",
    "MonteCarloResult",
    "fixed_fractional",
    "fixed_ratio",
    "MoneyManagementResult",
    "by_hours",
    "by_weekdays",
    "by_symbol",
    "remove_worst_percent",
    "add_commission",
    "in_date_range",
]
