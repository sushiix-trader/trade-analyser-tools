"""Money Management simulator.

Re-sizes each historical trade as if it had been traded with a compounding
money-management rule, then rebuilds the equity curve. Because the historical
report only gives absolute profit per trade, each trade is expressed as a
return relative to the entry-time equity and re-scaled by the chosen rule.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import Report


@dataclass
class MoneyManagementResult:
    equity: list[float]
    net_profit: float
    final_balance: float
    max_drawdown_money: float

    @property
    def initial_deposit(self) -> float:
        return self.equity[0]


def _max_drawdown(equity: list[float]) -> float:
    peak = equity[0]
    max_dd = 0.0
    for value in equity:
        peak = max(peak, value)
        max_dd = max(max_dd, peak - value)
    return max_dd


def fixed_fractional(
    report: Report, fraction: float
) -> MoneyManagementResult:
    """Re-trade every position scaled to ``fraction`` of current equity.

    ``fraction`` is the fraction of starting capital effectively deployed per
    trade (e.g. 1.0 = same size as history, 2.0 = double, 0.5 = half). The
    trade's profit is scaled by current_equity / initial_deposit so that
    position size compounds with the account.
    """
    deposit = report.initial_deposit
    equity = [deposit]
    current = deposit
    for profit in report.profits():
        scale = (current / deposit) * fraction if deposit > 0 else fraction
        current += profit * scale
        equity.append(current)
    return MoneyManagementResult(
        equity=equity,
        net_profit=current - deposit,
        final_balance=current,
        max_drawdown_money=_max_drawdown(equity),
    )


def fixed_ratio(report: Report, delta: float) -> MoneyManagementResult:
    """Ryan Jones' Fixed Ratio: +1 contract per ``delta`` of profit."""
    if delta <= 0:
        return fixed_fractional(report, 1.0)
    deposit = report.initial_deposit
    equity = [deposit]
    current = deposit
    for profit in report.profits():
        contracts = max(1.0, (current - deposit) / delta + 1.0)
        current += profit * contracts
        equity.append(current)
    return MoneyManagementResult(
        equity=equity,
        net_profit=current - deposit,
        final_balance=current,
        max_drawdown_money=_max_drawdown(equity),
    )
