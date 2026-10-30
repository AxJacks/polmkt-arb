"""Fast-hedge helpers for the live arb runner (pure, unit-tested).

The missed-hedge fix has three parts (used by scripts/live_arb.py):

  1. Leg 2 is priced from BREAKEVEN, not the quoted ask: after leg 1 fills we
     compute the highest Poly price that still locks a residual edge and send
     the IOC limit there. A 1-2c move against us degrades the profit instead of
     converting it into a miss + unwind loss (`max_poly_price`).
  2. Fill status is read from the order-create RESPONSE when the venue provides
     it (Kalshi: `fill_count`; Poly US: best-effort field sniffing), replacing
     the 0.4s-interval, 3s-timeout portfolio polling loop. `poly_fill` returns
     qty=None when the shape is unrecognized — the caller must then fall back
     to positions polling, never assume.
  3. The unwind of a naked leg re-fetches the bid and VERIFIES its own fill,
     escalating price until flat (scripts/live_arb.py:unwind_kalshi).
"""
from __future__ import annotations

import math


def floor_cent(x: float) -> float:
    """Round DOWN to the cent tick (never price above what the budget allows)."""
    return math.floor(x * 100 + 1e-9) / 100.0


def max_poly_price(budget: float, fee_model) -> float:
    """Highest cent-tick price p with p + taker_fee(p, 1) <= budget.

    Returns a value < 0.01 when no viable price exists (caller must unwind).
    """
    p = floor_cent(min(0.99, budget))
    while p >= 0.01 and p + fee_model.taker_fee(p, 1) > budget + 1e-9:
        p = round(p - 0.01, 2)
    return p


def _num(v) -> float:
    """Price/qty fields arrive as str, number, or {"value": ...}."""
    if isinstance(v, dict):
        v = v.get("value")
    return float(v)


def kalshi_fill(resp: dict) -> tuple[float, float | None]:
    """(filled_qty, avg_price_dollars | None) from a Kalshi order-create response.

    Handles both top-level and {"order": {...}} shapes; a price > 1 is assumed
    to be in cents and converted.
    """
    o = resp if "fill_count" in resp else resp.get("order", {})
    qty = float(o.get("fill_count") or 0)
    px = o.get("average_fill_price")
    if px in (None, ""):
        return qty, None
    px = _num(px)
    return qty, (px / 100.0 if px > 1 else px)


_POLY_QTY_KEYS = ("filledQuantity", "filled_quantity", "fillQuantity",
                  "fill_quantity", "matchedQuantity", "sizeMatched")
_POLY_PX_KEYS = ("avgFillPrice", "averageFillPrice", "avg_fill_price")
_TERMINAL_UNFILLED = ("CANCELED", "CANCELLED", "EXPIRED", "REJECTED", "UNFILLED")


def poly_fill(resp: dict) -> tuple[float | None, float | None]:
    """(filled_qty, avg_price) from a Poly US order-create response.

    VERIFIED LIVE 2026-07-12: the response is {"id": ..., "executions": [...]},
    and executions arrive ASYNCHRONOUSLY — a filled IOC can respond with an
    EMPTY list (this produced a naked leg before we learned it). Empty list =
    AMBIGUOUS, so qty=None: the caller MUST verify via the positions API.
    Only a non-empty executions list is a trustworthy fill report.
    """
    o = resp.get("order", resp) if isinstance(resp, dict) else {}
    ex = o.get("executions")
    if isinstance(ex, list):
        if not ex:
            return None, None
        qty = cash = 0.0
        for e in ex:
            q = p = None
            for k in ("quantity", "qty", "count", "size"):
                if e.get(k) is not None:
                    try:
                        q = _num(e[k]); break
                    except (TypeError, ValueError):
                        pass
            for k in ("price", "px", "avgPrice", "fillPrice"):
                if e.get(k) is not None:
                    try:
                        p = _num(e[k]); break
                    except (TypeError, ValueError):
                        pass
            if q:
                qty += q
                cash += q * (p or 0.0)
        return qty, (cash / qty if qty and cash else None)
    qty = None
    for k in _POLY_QTY_KEYS:
        if o.get(k) is not None:
            try:
                qty = _num(o[k])
                break
            except (TypeError, ValueError):
                pass
    if qty is None:
        st = str(o.get("status", "")).upper()
        if any(t in st for t in _TERMINAL_UNFILLED):
            qty = 0.0
        elif st.endswith("FILLED") and "PARTIALLY" not in st:
            try:
                qty = _num(o.get("quantity"))
            except (TypeError, ValueError):
                qty = None
    px = None
    for k in _POLY_PX_KEYS:
        if o.get(k) is not None:
            try:
                px = _num(o[k])
                break
            except (TypeError, ValueError):
                pass
    return qty, px
