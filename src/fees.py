"""Per-venue fee models. Real formulas, verified against official docs.

Sign convention: a fee is a COST and returns a non-negative number, EXCEPT a
maker rebate which returns a NEGATIVE number (a credit). Edge math adds fees to
net cost, so a negative maker fee correctly *reduces* cost.

Both venues use the same shape  fee = theta * contracts * p * (1 - p)  but differ
in theta and in rounding:

  Kalshi  — theta is per-series ("fee_multiplier"), general ~0.07, higher for
            sports/special. Fee is rounded UP to the next cent on the TOTAL.
            (NOTE: this differs slightly from the "round per-contract to 2dp"
            shorthand; Kalshi's published formula rounds the aggregate up.)
            Maker fee is a separate, smaller multiplier (often ~1/4 of taker;
            0 after rounding on small size). Pulled live, never assumed.

  Polymarket US — taker theta = 0.05 (~1.25% at p=0.50), maker theta = -0.0125
            (a REBATE), banker's rounding (half-to-even) to the cent. Verified
            at https://docs.polymarket.us/fees  (2026). No settlement fee.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal


class FeeError(RuntimeError):
    """Raised when fee inputs are missing — never trade on assumed fees."""


def _ceil_cents(amount: float) -> float:
    """Round UP to the next cent (Kalshi rule)."""
    return math.ceil(round(amount, 10) * 100) / 100.0


def _bankers_cents(amount: float) -> float:
    """Round half-to-even to the cent (Polymarket US rule)."""
    return float(Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN))


def _check_price(price: float) -> None:
    if not (0.0 < price < 1.0):
        raise FeeError(f"price out of (0,1): {price!r}")


@dataclass(frozen=True)
class KalshiFeeModel:
    """Kalshi fees. `taker_multiplier` / `maker_multiplier` are pulled live from
    the series fee endpoint; constructing this with None means we refuse to quote
    a fee (fail loud)."""

    taker_multiplier: float | None
    maker_multiplier: float | None = 0.0
    venue: str = "kalshi"

    def taker_fee(self, price: float, contracts: float) -> float:
        _check_price(price)
        if self.taker_multiplier is None:
            raise FeeError("Kalshi taker fee_multiplier missing for this market")
        return _ceil_cents(self.taker_multiplier * contracts * price * (1.0 - price))

    def maker_fee(self, price: float, contracts: float) -> float:
        _check_price(price)
        if self.maker_multiplier is None:
            raise FeeError("Kalshi maker fee_multiplier missing for this market")
        return _ceil_cents(self.maker_multiplier * contracts * price * (1.0 - price))


@dataclass(frozen=True)
class PolyUSFeeModel:
    """Polymarket US fees. Defaults are the published 2026 schedule; override only
    if the live /fees endpoint reports different theta (then re-verify)."""

    taker_theta: float = 0.05
    maker_theta: float = -0.0125  # rebate (negative cost)
    venue: str = "polyus"

    def taker_fee(self, price: float, contracts: float) -> float:
        _check_price(price)
        return _bankers_cents(self.taker_theta * contracts * price * (1.0 - price))

    def maker_fee(self, price: float, contracts: float) -> float:
        _check_price(price)
        return _bankers_cents(self.maker_theta * contracts * price * (1.0 - price))
