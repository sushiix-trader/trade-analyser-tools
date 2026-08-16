"""Deterministic trade re-sizing transformations for historical analysis."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .diagnostics import Diagnostic
from .errors import WhatIfConfigurationError, WhatIfError
from .models import Report, Trade, TradeSide
from .serialization import to_primitive


@dataclass(frozen=True)
class InstrumentSpec:
    """Static monetary tick metadata used by risk-based what-if sizing.

    ``tick_value`` is the account/report-currency value of one ``tick_size``
    movement for one lot.  The spec is intentionally static and must be
    supplied per report when the source report does not provide equivalent
    metadata.
    """

    symbol: str
    tick_size: float
    tick_value: float
    account_currency: str | None = None
    contract_size: float | None = None
    quote_currency: str | None = None

    def validate(self, report_currency: str | None = None) -> None:
        if not self.symbol.strip():
            raise WhatIfConfigurationError("instrument symbol is required")
        if not math.isfinite(float(self.tick_size)) or self.tick_size <= 0:
            raise WhatIfConfigurationError("instrument tick_size must be positive and finite")
        if not math.isfinite(float(self.tick_value)) or self.tick_value <= 0:
            raise WhatIfConfigurationError("instrument tick_value must be positive and finite")
        if self.contract_size is not None and (
            not math.isfinite(float(self.contract_size)) or self.contract_size <= 0
        ):
            raise WhatIfConfigurationError("instrument contract_size must be positive and finite")
        if report_currency and self.account_currency:
            if self.account_currency.strip().upper() != report_currency.strip().upper():
                raise WhatIfConfigurationError(
                    "instrument account_currency must match the report currency"
                )

    def to_dict(self) -> dict[str, Any]:
        return to_primitive(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "InstrumentSpec | None":
        if payload is None:
            return None
        return cls(**payload)


@dataclass(frozen=True)
class WhatIfConfig:
    """One mutually exclusive deterministic re-sizing mode."""

    mode: str
    value: float
    initial_capital: float | None = None
    instrument_spec: InstrumentSpec | None = None
    lot_precision: float = 0.01

    FLAT_LOT = "flat_lot"
    PERCENT_RISK = "percent_risk"
    DOLLAR_RISK = "dollar_risk"
    MODES = frozenset((FLAT_LOT, PERCENT_RISK, DOLLAR_RISK))

    def __post_init__(self) -> None:
        if self.mode == self.PERCENT_RISK and self.value > 100.0:
            object.__setattr__(self, "value", 100.0)

    @classmethod
    def flat_lot(cls, lots: float, *, lot_precision: float = 0.01) -> "WhatIfConfig":
        config = cls(cls.FLAT_LOT, lots, lot_precision=lot_precision)
        config.validate()
        return config

    @classmethod
    def percent_risk(
        cls,
        percent: float,
        *,
        initial_capital: float | None = None,
        instrument_spec: InstrumentSpec | None = None,
        lot_precision: float = 0.01,
    ) -> "WhatIfConfig":
        config = cls(
            cls.PERCENT_RISK,
            percent,
            initial_capital=initial_capital,
            instrument_spec=instrument_spec,
            lot_precision=lot_precision,
        )
        config.validate()
        return config

    @classmethod
    def dollar_risk(
        cls,
        amount: float,
        *,
        initial_capital: float | None = None,
        instrument_spec: InstrumentSpec | None = None,
        lot_precision: float = 0.01,
    ) -> "WhatIfConfig":
        config = cls(
            cls.DOLLAR_RISK,
            amount,
            initial_capital=initial_capital,
            instrument_spec=instrument_spec,
            lot_precision=lot_precision,
        )
        config.validate()
        return config

    def validate(self, report: Report | None = None) -> None:
        if self.mode not in self.MODES:
            raise WhatIfConfigurationError(
                f"mode must be one of {sorted(self.MODES)}"
            )
        if not math.isfinite(float(self.value)) or self.value <= 0:
            raise WhatIfConfigurationError("what-if value must be positive and finite")
        if not math.isfinite(float(self.lot_precision)) or self.lot_precision <= 0:
            raise WhatIfConfigurationError("lot_precision must be positive and finite")
        if self.initial_capital is not None and (
            not math.isfinite(float(self.initial_capital)) or self.initial_capital <= 0
        ):
            raise WhatIfConfigurationError(
                "initial_capital must be positive and finite when supplied"
            )
        if self.mode == self.FLAT_LOT:
            units = self.value / self.lot_precision
            if not math.isclose(units, round(units), rel_tol=0.0, abs_tol=1e-9):
                raise WhatIfConfigurationError(
                    f"flat lot size must be a multiple of {self.lot_precision}"
                )
        if self.mode in {self.PERCENT_RISK, self.DOLLAR_RISK}:
            if self.instrument_spec is None:
                raise WhatIfConfigurationError(
                    "risk-based what-if sizing requires an InstrumentSpec"
                )
            self.instrument_spec.validate(report.currency if report else None)
            if report and self.instrument_spec.symbol.casefold() not in {
                symbol.casefold() for symbol in report.symbols()
            }:
                raise WhatIfConfigurationError(
                    "instrument specification symbol does not match the report symbol"
                )

    def to_dict(self) -> dict[str, Any]:
        return to_primitive(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "WhatIfConfig | None":
        if payload is None:
            return None
        data = dict(payload)
        data["instrument_spec"] = InstrumentSpec.from_dict(data.get("instrument_spec"))
        return cls(**data)


@dataclass(frozen=True)
class SizingAudit:
    """Per-position explanation of a what-if volume decision."""

    ticket: str
    position_id: str | None
    symbol: str
    original_volume: float
    effective_volume: float
    sizing_mode: str
    requested_risk_amount: float | None
    calculated_risk_amount: float | None
    risk_distance: float | None
    risk_source: str
    rounding: str
    status: str
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return to_primitive(self)


@dataclass(frozen=True)
class WhatIfResult:
    """Audit and diagnostics for a transformed report."""

    config: WhatIfConfig
    capital_base: float
    original_trade_count: int
    transformed_trade_count: int
    excluded_trade_count: int
    audits: tuple[SizingAudit, ...]
    warnings: tuple[Diagnostic, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "config": self.config.to_dict(),
            "capital_base": self.capital_base,
            "original_trade_count": self.original_trade_count,
            "transformed_trade_count": self.transformed_trade_count,
            "excluded_trade_count": self.excluded_trade_count,
            "audits": [audit.to_dict() for audit in self.audits],
            "warnings": [warning.to_dict() for warning in self.warnings],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "WhatIfResult":
        return cls(
            config=WhatIfConfig.from_dict(payload["config"]),
            capital_base=float(payload["capital_base"]),
            original_trade_count=int(payload["original_trade_count"]),
            transformed_trade_count=int(payload["transformed_trade_count"]),
            excluded_trade_count=int(payload["excluded_trade_count"]),
            audits=tuple(SizingAudit(**item) for item in payload.get("audits", [])),
            warnings=tuple(Diagnostic(**item) for item in payload.get("warnings", [])),
        )


def _floor_lots(value: float, precision: float) -> float:
    # The small epsilon only stabilises binary representations such as 0.3 / 0.01.
    units = math.floor((value / precision) + 1e-10)
    return float(round(units * precision, 10))


def _scaled_trade(trade: Trade, effective_volume: float) -> Trade:
    if trade.volume <= 0 or not math.isfinite(float(trade.volume)):
        raise WhatIfError(f"trade {trade.ticket} has invalid original volume")
    scale = effective_volume / trade.volume
    return Trade(
        **{
            **trade.__dict__,
            "volume": effective_volume,
            "profit": trade.profit * scale,
            "swap": trade.swap * scale,
            "commission": trade.commission * scale,
        }
    )


def _excluded_audit(
    trade: Trade,
    config: WhatIfConfig,
    *,
    code: str,
    reason: str,
    risk_source: str,
) -> tuple[SizingAudit, Diagnostic]:
    diagnostic = Diagnostic(
        code,
        reason,
        context={
            "ticket": trade.ticket,
            "position_id": trade.position_id,
            "symbol": trade.symbol,
        },
    )
    audit = SizingAudit(
        ticket=trade.ticket,
        position_id=trade.position_id,
        symbol=trade.symbol,
        original_volume=trade.volume,
        effective_volume=0.0,
        sizing_mode=config.mode,
        requested_risk_amount=None,
        calculated_risk_amount=None,
        risk_distance=None,
        risk_source=risk_source,
        rounding=f"floor_to_{config.lot_precision:g}",
        status="excluded",
        reason=reason,
    )
    return audit, diagnostic


def transform_report(report: Report, config: WhatIfConfig) -> tuple[Report, WhatIfResult]:
    """Transform completed positions and return a fresh report plus audit data."""

    config.validate(report)
    capital_base = (
        float(config.initial_capital)
        if config.initial_capital is not None
        else float(report.initial_deposit)
    )
    if config.mode in {WhatIfConfig.PERCENT_RISK, WhatIfConfig.DOLLAR_RISK} and (
        not math.isfinite(capital_base) or capital_base <= 0
    ):
        raise WhatIfConfigurationError(
            "risk-based what-if sizing requires a positive initial capital base"
        )

    requested_risk = (
        capital_base * config.value / 100.0
        if config.mode == WhatIfConfig.PERCENT_RISK
        else config.value if config.mode == WhatIfConfig.DOLLAR_RISK else None
    )
    transformed: list[Trade] = []
    audits: list[SizingAudit] = []
    diagnostics: list[Diagnostic] = []
    for trade in report.ordered_trades():
        if trade.volume <= 0 or not math.isfinite(float(trade.volume)):
            raise WhatIfError(f"trade {trade.ticket} has invalid original volume")
        if config.mode == WhatIfConfig.FLAT_LOT:
            effective_volume = float(round(config.value, 10))
            transformed.append(_scaled_trade(trade, effective_volume))
            audits.append(SizingAudit(
                ticket=trade.ticket,
                position_id=trade.position_id,
                symbol=trade.symbol,
                original_volume=trade.volume,
                effective_volume=effective_volume,
                sizing_mode=config.mode,
                requested_risk_amount=None,
                calculated_risk_amount=None,
                risk_distance=None,
                risk_source="not_applicable",
                rounding=f"exact_{config.lot_precision:g}",
                status="sized",
            ))
            continue

        if trade.sl is None or trade.open_price is None:
            audit, diagnostic = _excluded_audit(
                trade,
                config,
                code="what_if_missing_stop_excluded",
                reason="Trade was excluded because risk-based sizing requires an explicit stop loss and entry price",
                risk_source="missing_stop",
            )
            audits.append(audit)
            diagnostics.append(diagnostic)
            continue
        valid_side = (
            trade.side == TradeSide.LONG and trade.sl < trade.open_price
        ) or (
            trade.side == TradeSide.SHORT and trade.sl > trade.open_price
        )
        if not valid_side or trade.sl == trade.open_price:
            audit, diagnostic = _excluded_audit(
                trade,
                config,
                code="what_if_invalid_stop_excluded",
                reason="Trade was excluded because its stop loss is on the invalid side of entry",
                risk_source="invalid_stop",
            )
            audits.append(audit)
            diagnostics.append(diagnostic)
            continue

        spec = config.instrument_spec
        assert spec is not None
        risk_distance = abs(trade.open_price - trade.sl)
        risk_per_lot = (risk_distance / spec.tick_size) * spec.tick_value
        if risk_per_lot <= 0 or not math.isfinite(risk_per_lot):
            raise WhatIfError(f"trade {trade.ticket} has an invalid monetary stop risk")
        theoretical_volume = float(requested_risk / risk_per_lot)
        effective_volume = _floor_lots(theoretical_volume, config.lot_precision)
        if effective_volume <= 0:
            audit, diagnostic = _excluded_audit(
                trade,
                config,
                code="what_if_size_below_precision_excluded",
                reason="Trade was excluded because calculated volume was below the configured lot precision",
                risk_source="explicit_stop",
            )
            audits.append(
                SizingAudit(
                    **{
                        **audit.__dict__,
                        "requested_risk_amount": requested_risk,
                        "risk_distance": risk_distance,
                    }
                )
            )
            diagnostics.append(diagnostic)
            continue
        calculated_risk = effective_volume * risk_per_lot
        transformed.append(_scaled_trade(trade, effective_volume))
        audits.append(SizingAudit(
            ticket=trade.ticket,
            position_id=trade.position_id,
            symbol=trade.symbol,
            original_volume=trade.volume,
            effective_volume=effective_volume,
            sizing_mode=config.mode,
            requested_risk_amount=requested_risk,
            calculated_risk_amount=calculated_risk,
            risk_distance=risk_distance,
            risk_source="explicit_stop",
            rounding=f"floor_to_{config.lot_precision:g}",
            status="sized",
        ))

    if not transformed:
        raise WhatIfError("what-if sizing excluded every completed trade")
    result = WhatIfResult(
        config=config,
        capital_base=capital_base,
        original_trade_count=len(report.trades),
        transformed_trade_count=len(transformed),
        excluded_trade_count=len(report.trades) - len(transformed),
        audits=tuple(audits),
        warnings=tuple(diagnostics),
    )
    transformed_report = Report(
        **{
            **report.__dict__,
            "trades": transformed,
            "source_balance_points": [],
            "source_equity_points": [],
            "reported_metrics": {},
            "metadata": {
                **report.metadata,
                "what_if": result.to_dict(),
            },
        }
    )
    return transformed_report, result
