"""Cross-venue arbitrage edge math.

A mapped pair defines a single real-world outcome "A" (the mapped YES). Because
mappings.yaml is curated so that YES means the SAME outcome on both venues, a
fully-hedged position = buy A on one venue + buy NOT-A on the other. Holding both
to settlement pays exactly $1 per matched contract regardless of outcome.

    net_cost = price(buy A on X) + price(buy NOT-A on Y) + fee_X + fee_Y
    edge     = 1.00 - net_cost            (per matched contract)

Positive edge with size = arbitrage. We evaluate both directions (A-on-Kalshi vs
A-on-Poly) and report the better. We compute:

  • taker edge  — cross both spreads now (asks), taker fees. Guaranteed-fill,
                  conservative; this is what gates alerts.
  • maker edge  — rest both legs at the bid, maker fees (Poly maker is a rebate).
                  Optimistic upper bound; fills are NOT guaranteed.

Per-venue `invert` flips which native side represents outcome A, for the rare
market whose native YES is the opposite real-world outcome. Default False.
"""
from __future__ import annotations

from dataclasses import dataclass

from .books import VenueBook


@dataclass(frozen=True)
class SidePrices:
    """Prices/sizes to BUY outcome A and to BUY NOT-A on a single venue."""

    a_ask: float
    a_ask_size: float
    a_bid: float
    a_bid_size: float
    nota_ask: float
    nota_ask_size: float
    nota_bid: float
    nota_bid_size: float


def normalize(book: VenueBook, invert: bool) -> SidePrices:
    if not invert:
        return SidePrices(
            a_ask=book.yes_ask, a_ask_size=book.yes_ask_size,
            a_bid=book.yes_bid, a_bid_size=book.yes_bid_size,
            nota_ask=book.no_ask, nota_ask_size=book.no_ask_size,
            nota_bid=book.no_bid, nota_bid_size=book.no_bid_size,
        )
    # invert: outcome A is the venue's native NO.
    return SidePrices(
        a_ask=book.no_ask, a_ask_size=book.no_ask_size,
        a_bid=book.no_bid, a_bid_size=book.no_bid_size,
        nota_ask=book.yes_ask, nota_ask_size=book.yes_ask_size,
        nota_bid=book.yes_bid, nota_bid_size=book.yes_bid_size,
    )


@dataclass(frozen=True)
class LegQuote:
    venue: str
    outcome: str          # "A" or "NOT-A"
    role: str             # "taker" or "maker"
    price: float
    size: float
    fee: float


@dataclass(frozen=True)
class StructureResult:
    """One direction of the hedge (which venue holds outcome A)."""

    structure: str        # e.g. "A@kalshi/NOTA@polyus"
    taker_edge: float
    taker_size: float
    taker_legs: tuple[LegQuote, LegQuote]
    maker_edge: float
    maker_size: float
    maker_legs: tuple[LegQuote, LegQuote]


@dataclass(frozen=True)
class EdgeResult:
    pair_id: str
    best: StructureResult         # higher taker_edge of the two directions
    both: tuple[StructureResult, StructureResult]


def _structure(
    name: str,
    a_venue: str, a: SidePrices, a_fee,
    nota_venue: str, n: SidePrices, n_fee,
) -> StructureResult:
    """Build a structure where `a_venue` holds outcome A and `nota_venue` holds NOT-A.

    `a_fee` / `n_fee` are FeeModel instances for the respective venues.
    """
    # Taker: cross both asks.
    a_t_price, n_t_price = a.a_ask, n.nota_ask
    a_t_fee = a_fee.taker_fee(a_t_price, 1) if a_t_price > 0 else 0.0
    n_t_fee = n_fee.taker_fee(n_t_price, 1) if n_t_price > 0 else 0.0
    taker_net = a_t_price + n_t_price + a_t_fee + n_t_fee
    taker_edge = 1.0 - taker_net
    taker_size = min(a.a_ask_size, n.nota_ask_size)
    taker_legs = (
        LegQuote(a_venue, "A", "taker", a_t_price, a.a_ask_size, a_t_fee),
        LegQuote(nota_venue, "NOT-A", "taker", n_t_price, n.nota_ask_size, n_t_fee),
    )

    # Maker: rest both at the bid.
    a_m_price, n_m_price = a.a_bid, n.nota_bid
    a_m_fee = a_fee.maker_fee(a_m_price, 1) if a_m_price > 0 else 0.0
    n_m_fee = n_fee.maker_fee(n_m_price, 1) if n_m_price > 0 else 0.0
    maker_net = a_m_price + n_m_price + a_m_fee + n_m_fee
    maker_edge = 1.0 - maker_net
    maker_size = min(a.a_bid_size, n.nota_bid_size)
    maker_legs = (
        LegQuote(a_venue, "A", "maker", a_m_price, a.a_bid_size, a_m_fee),
        LegQuote(nota_venue, "NOT-A", "maker", n_m_price, n.nota_bid_size, n_m_fee),
    )

    return StructureResult(
        structure=name,
        taker_edge=taker_edge, taker_size=taker_size, taker_legs=taker_legs,
        maker_edge=maker_edge, maker_size=maker_size, maker_legs=maker_legs,
    )


def compute_edges(
    pair_id: str,
    kalshi_book: VenueBook, kalshi_fee, kalshi_invert: bool,
    polyus_book: VenueBook, polyus_fee, polyus_invert: bool,
) -> EdgeResult:
    k = normalize(kalshi_book, kalshi_invert)
    p = normalize(polyus_book, polyus_invert)

    s1 = _structure("A@kalshi/NOTA@polyus", "kalshi", k, kalshi_fee, "polyus", p, polyus_fee)
    s2 = _structure("A@polyus/NOTA@kalshi", "polyus", p, polyus_fee, "kalshi", k, kalshi_fee)
    best = s1 if s1.taker_edge >= s2.taker_edge else s2
    return EdgeResult(pair_id=pair_id, best=best, both=(s1, s2))


def is_actionable(structure: StructureResult, min_edge: float, min_size: int,
                  slippage_buffer: float) -> bool:
    """Taker-now gate: edge net of the slippage buffer clears MIN_EDGE at size."""
    return (structure.taker_edge - slippage_buffer) >= min_edge and structure.taker_size >= min_size
