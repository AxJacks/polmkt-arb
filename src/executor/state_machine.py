"""Both-legs-or-cancel execution. Leg risk is the main failure mode, so:

  1. Place the thinner/less-liquid leg first (limit IOC at target).
  2. If it fills (to >= min_fill), immediately fire the second leg as taker IOC.
  3. If leg 2 fills short, unwind the unhedged remainder of leg 1 at market.
  4. If leg 2 doesn't fill at all (incl. live LEG2_TIMEOUT), unwind leg 1 fully.

Never leave a naked leg open silently. PnL is realized at fill (a true hedge
pays $1 per matched contract regardless of outcome).

Pure: talks only to a Router, so it runs identically in paper and live and is
unit-tested with a scripted fake router.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .router import Fill, LegPlan, Router


@dataclass
class ArbPlan:
    pair_id: str
    structure: str
    leg1: LegPlan        # thinner venue — placed first
    leg2: LegPlan
    size: float
    min_fill: float = 1.0


@dataclass
class ArbResult:
    pair_id: str
    status: str          # completed | partial_unwound | leg2_failed_unwound | leg1_no_fill | leg1_insufficient
    hedged_qty: float
    realized_pnl: float
    fills: list = field(default_factory=list)


def execute_arb(plan: ArbPlan, router: Router) -> ArbResult:
    f1 = router.buy(plan.leg1, plan.size)
    fills = [f1]

    if f1.filled_qty < plan.min_fill:
        if f1.filled_qty <= 0:
            return ArbResult(plan.pair_id, "leg1_no_fill", 0.0, 0.0, fills)
        # filled but below min: don't proceed, unwind the dust
        u = router.sell(plan.leg1, f1.filled_qty)
        fills.append(u)
        return ArbResult(plan.pair_id, "leg1_insufficient", 0.0, f1.cash + u.cash, fills)

    q = f1.filled_qty
    f2 = router.buy(plan.leg2, q)
    fills.append(f2)
    matched = min(q, f2.filled_qty)
    excess = q - f2.filled_qty

    if excess > 1e-9:
        u = router.sell(plan.leg1, excess)   # unwind unhedged remainder of leg 1
        fills.append(u)

    realized = sum(f.cash for f in fills) + matched  # matched hedged pairs pay $1 each

    if matched >= q - 1e-9:
        status = "completed"
    elif matched > 0:
        status = "partial_unwound"
    else:
        status = "leg2_failed_unwound"
    return ArbResult(plan.pair_id, status, matched, realized, fills)


def pick_thinner_first(structure, leg_a: LegPlan, leg_b: LegPlan) -> tuple[LegPlan, LegPlan]:
    """Leg 1 = the less-liquid side (smaller available buy size), placed first so
    the harder fill is attempted before committing the easy one."""
    return (leg_a, leg_b) if leg_a.buy_size <= leg_b.buy_size else (leg_b, leg_a)
