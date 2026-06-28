"""In-memory top-of-book state with staleness tracking.

A `VenueBook` is normalized to a binary market with explicit YES and NO sides.
Kalshi publishes both sides; for Polymarket US we assemble the NO side from the
NO outcome's book. Prices are in dollars [0,1]; sizes are in contracts.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class VenueBook:
    yes_bid: float
    yes_ask: float
    no_bid: float
    no_ask: float
    yes_bid_size: float
    yes_ask_size: float
    no_bid_size: float
    no_ask_size: float
    ts: float  # epoch seconds when this snapshot was received

    def age_ms(self, now: float | None = None) -> float:
        return ((now if now is not None else time.time()) - self.ts) * 1000.0


class OrderBookStore:
    """Thread-safe latest-snapshot store keyed by (pair_id, venue)."""

    def __init__(self, staleness_ms: int) -> None:
        self._staleness_ms = staleness_ms
        self._books: dict[tuple[str, str], VenueBook] = {}
        self._lock = threading.Lock()

    def update(self, pair_id: str, venue: str, book: VenueBook) -> None:
        with self._lock:
            self._books[(pair_id, venue)] = book

    def get(self, pair_id: str, venue: str) -> VenueBook | None:
        with self._lock:
            return self._books.get((pair_id, venue))

    def is_stale(self, pair_id: str, venue: str, now: float | None = None) -> bool:
        book = self.get(pair_id, venue)
        if book is None:
            return True
        return book.age_ms(now) > self._staleness_ms
