"""Order routing abstraction shared by paper and live execution.

A LegPlan carries everything needed to (paper-)execute one side: the taker buy
price + available size, the bid to unwind into, and the venue fee model. The
state machine talks only to Router.buy()/sell(), so paper and live share logic.

PaperRouter simulates fills against the live book snapshot (taker, real fees).
LiveRouter builds real orders and calls the venue client — whose place_order is
still a Phase-2 stub (NotImplementedError), so live cannot actually fire until
that is built and explicitly armed. Structure is here; trading is not.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field


@dataclass
class LegPlan:
    venue: str
    market_id: str
    outcome: str          # "A" or "NOT-A" (mapped outcome space)
    buy_price: float      # taker ask we'd pay
    buy_size: float       # size available at/through buy_price
    sell_price: float     # best bid we'd unwind into
    sell_size: float
    fee_model: object     # FeeModel for this venue/market


@dataclass
class Fill:
    leg: LegPlan
    side: str             # "buy" | "sell"
    filled_qty: float
    price: float
    fee: float
    client_order_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    ts: float = field(default_factory=time.time)

    @property
    def cash(self) -> float:
        """Signed cash flow: negative when buying (cash out), positive when selling."""
        gross = self.price * self.filled_qty
        return (-gross - self.fee) if self.side == "buy" else (gross - self.fee)


class Router:
    def buy(self, leg: LegPlan, qty: float) -> Fill:
        raise NotImplementedError

    def sell(self, leg: LegPlan, qty: float) -> Fill:
        raise NotImplementedError


def _fee(fee_model, price: float, qty: float) -> float:
    if qty <= 0 or not (0 < price < 1):
        return 0.0
    return fee_model.taker_fee(price, qty)


class PaperRouter(Router):
    """Simulates taker IOC fills against the live top-of-book snapshot."""

    def buy(self, leg: LegPlan, qty: float) -> Fill:
        fq = max(0.0, min(qty, leg.buy_size))
        return Fill(leg, "buy", fq, leg.buy_price, _fee(leg.fee_model, leg.buy_price, fq))

    def sell(self, leg: LegPlan, qty: float) -> Fill:
        fq = max(0.0, min(qty, leg.sell_size))
        return Fill(leg, "sell", fq, leg.sell_price, _fee(leg.fee_model, leg.sell_price, fq))


class LiveRouter(Router):
    """Real order placement. Calls the venue client's place_order, which is a
    Phase-2 stub until built — so this raises rather than silently no-op."""

    def __init__(self, clients: dict, leg2_timeout_ms: int) -> None:
        self.clients = clients
        self.leg2_timeout_ms = leg2_timeout_ms

    def buy(self, leg: LegPlan, qty: float) -> Fill:
        client = self.clients[leg.venue]
        client.place_order(market_id=leg.market_id, outcome=leg.outcome,
                           side="buy", qty=qty, price=leg.buy_price, tif="IOC")
        raise NotImplementedError("LiveRouter.buy: live order placement not built/armed")

    def sell(self, leg: LegPlan, qty: float) -> Fill:
        client = self.clients[leg.venue]
        client.place_order(market_id=leg.market_id, outcome=leg.outcome,
                           side="sell", qty=qty, price=leg.sell_price, tif="IOC")
        raise NotImplementedError("LiveRouter.sell: live order placement not built/armed")
