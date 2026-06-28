"""Venue client interface. Phase 1 uses only the read methods. The order/account
methods are declared here as stubs that raise NotImplementedError so the Phase 2
executor can be added without touching the detector — this is the seam where live
trading plugs in."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Awaitable, Callable

from ..books import VenueBook


class VenueClient(ABC):
    name: str

    # ── Phase 1: read-only ───────────────────────────────────
    @abstractmethod
    def list_markets(self, **filters) -> list[dict]:
        """Return raw market dicts (for the suggester / mapping verification)."""

    @abstractmethod
    def get_fee_model(self, market_id: str):
        """Return a FeeModel for this market, raising if fee data is missing."""

    @abstractmethod
    def market_is_open(self, market_id: str) -> bool:
        """True only if the market is actively trading with a usable book."""

    @abstractmethod
    async def stream_books(
        self,
        market_ids: list[str],
        on_book: Callable[[str, VenueBook], Awaitable[None] | None],
    ) -> None:
        """Connect, subscribe, and call on_book(market_id, VenueBook) on updates."""

    # ── Phase 2: order placement & account (stubs) ───────────
    def place_order(self, *args, **kwargs):
        raise NotImplementedError(f"{self.name}: order placement is Phase 2 (not built)")

    def cancel_order(self, *args, **kwargs):
        raise NotImplementedError(f"{self.name}: cancel is Phase 2 (not built)")

    def get_open_orders(self, *args, **kwargs):
        raise NotImplementedError(f"{self.name}: open orders is Phase 2 (not built)")

    def get_balance(self, *args, **kwargs):
        raise NotImplementedError(f"{self.name}: balances is Phase 2 (not built)")

    def get_positions(self, *args, **kwargs):
        raise NotImplementedError(f"{self.name}: positions is Phase 2 (not built)")
