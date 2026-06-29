"""Kalshi venue client (read-only for Phase 1).

Auth: RSA-PSS / SHA256. Signed message = timestamp_ms + METHOD + path.
Docs: https://docs.kalshi.com  (verified 2026-06).

Order-book model: Kalshi publishes two resting-bid books, `yes` and `no`, as
[price_cents, size] levels. A NO bid at q is equivalent to a YES ask at (100-q),
so we derive a clean top-of-book:
    yes_bid = best yes level,        no_ask  = 1 - yes_bid
    no_bid  = best no level,         yes_ask = 1 - no_bid
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Awaitable, Callable

import httpx
import websockets

log = logging.getLogger("arb.kalshi")
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from ..books import VenueBook
from ..fees import KalshiFeeModel
from .base import VenueClient


class KalshiAuthError(RuntimeError):
    pass


def load_private_key(path: str):
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def sign(private_key, timestamp_ms: int, method: str, path: str) -> str:
    msg = f"{timestamp_ms}{method}{path}".encode()
    sig = private_key.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode()


class _BookState:
    """Maintains yes/no price->size level maps from snapshot + deltas.

    Live Kalshi WS format (verified 2026-06):
      snapshot msg: yes_dollars_fp / no_dollars_fp = [[price_dollars, size], ...]
      delta msg:    price_dollars, delta_fp, side ("yes"/"no")
    Prices are dollar strings on a 1c tick; we key levels by integer cents and
    store fractional-contract sizes as floats.
    """

    def __init__(self) -> None:
        self.yes: dict[int, float] = {}
        self.no: dict[int, float] = {}

    @staticmethod
    def _cents(price_str) -> int:
        return int(round(float(price_str) * 100))

    def snapshot(self, msg: dict) -> None:
        self.yes = {self._cents(p): float(s) for p, s in msg.get("yes_dollars_fp", [])}
        self.no = {self._cents(p): float(s) for p, s in msg.get("no_dollars_fp", [])}

    def delta(self, msg: dict) -> None:
        side = msg["side"]
        price = self._cents(msg["price_dollars"])
        book = self.yes if side == "yes" else self.no
        book[price] = book.get(price, 0.0) + float(msg["delta_fp"])
        if book[price] <= 1e-9:
            book.pop(price, None)

    def top(self) -> VenueBook:
        yes_bid_c = max(self.yes) if self.yes else 0
        no_bid_c = max(self.no) if self.no else 0
        yes_bid = yes_bid_c / 100.0
        no_bid = no_bid_c / 100.0
        # ask side derived from the opposite book's best bid.
        yes_ask = (100 - no_bid_c) / 100.0 if self.no else 1.0
        no_ask = (100 - yes_bid_c) / 100.0 if self.yes else 1.0
        return VenueBook(
            yes_bid=yes_bid, yes_ask=yes_ask, no_bid=no_bid, no_ask=no_ask,
            yes_bid_size=float(self.yes.get(yes_bid_c, 0)),
            yes_ask_size=float(self.no.get(no_bid_c, 0)),   # contracts available to sell yes = no bid size
            no_bid_size=float(self.no.get(no_bid_c, 0)),
            no_ask_size=float(self.yes.get(yes_bid_c, 0)),
            ts=time.time(),
        )


class KalshiClient(VenueClient):
    name = "kalshi"

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._pk = None
        self._fee_cache: dict[str, KalshiFeeModel] = {}

    @property
    def pk(self):
        if self._pk is None:
            if not self.cfg.kalshi_key_id:
                raise KalshiAuthError("KALSHI_API_KEY_ID not set")
            self._pk = load_private_key(self.cfg.kalshi_private_key_path)
        return self._pk

    def _headers(self, method: str, path: str) -> dict:
        ts = int(time.time() * 1000)
        return {
            "KALSHI-ACCESS-KEY": self.cfg.kalshi_key_id,
            "KALSHI-ACCESS-TIMESTAMP": str(ts),
            "KALSHI-ACCESS-SIGNATURE": sign(self.pk, ts, method, path),
            "Content-Type": "application/json",
        }

    def _get(self, path: str, params: dict | None = None) -> dict:
        # `path` is the signed path (without query); base already ends /trade-api/v2
        full_path = "/trade-api/v2" + path
        url = self.cfg.kalshi_rest_base + path
        r = httpx.get(url, params=params, headers=self._headers("GET", full_path), timeout=15)
        r.raise_for_status()
        return r.json()

    # ── reads ────────────────────────────────────────────────
    def list_markets(self, **filters) -> list[dict]:
        out: list[dict] = []
        cursor = None
        while True:
            params = {"limit": 1000, "status": filters.get("status", "open")}
            if cursor:
                params["cursor"] = cursor
            data = self._get("/markets", params)
            out.extend(data.get("markets", []))
            cursor = data.get("cursor")
            if not cursor or len(out) >= filters.get("max", 5000):
                break
        return out

    def _get_market(self, ticker: str) -> dict:
        return self._get(f"/markets/{ticker}").get("market", {})

    def market_is_open(self, market_id: str) -> bool:
        return self._get_market(market_id).get("status") == "active"

    # Documented base coefficients for Kalshi's fee formulas. The per-series
    # `fee_multiplier` (from the API) SCALES these; sports/special series carry a
    # multiplier > 1. Base 0.07 is Kalshi's published quadratic taker rate; maker
    # is ~1/4 of taker on the maker-fee variant. Unknown fee_type => raise.
    _BASE_TAKER = {"quadratic": 0.07, "quadratic_with_maker_fees": 0.07}
    _BASE_MAKER = {"quadratic": 0.0, "quadratic_with_maker_fees": 0.0175}

    def get_fee_model(self, market_id: str) -> KalshiFeeModel:
        if market_id in self._fee_cache:
            return self._fee_cache[market_id]
        market = self._get_market(market_id)
        series_ticker = market.get("series_ticker") or market_id.split("-", 1)[0]
        series = self._get(f"/series/{series_ticker}").get("series", {})
        mult = series.get("fee_multiplier")
        ftype = series.get("fee_type")
        if mult is None:
            raise KalshiAuthError(
                f"Kalshi series {series_ticker} has no fee_multiplier — refusing to assume a fee")
        if ftype not in self._BASE_TAKER:
            raise KalshiAuthError(
                f"Kalshi series {series_ticker} unknown fee_type {ftype!r} — refusing to assume a fee")
        model = KalshiFeeModel(
            taker_multiplier=self._BASE_TAKER[ftype] * float(mult),
            maker_multiplier=self._BASE_MAKER[ftype] * float(mult),
        )
        self._fee_cache[market_id] = model
        return model

    # ── feed ─────────────────────────────────────────────────
    async def stream_books(
        self,
        market_ids: list[str],
        on_book: Callable[[str, VenueBook], Awaitable[None] | None],
    ) -> None:
        path = "/trade-api/ws/v2"
        backoff = 1.0
        while True:
            try:
                ts = int(time.time() * 1000)
                headers = {
                    "KALSHI-ACCESS-KEY": self.cfg.kalshi_key_id,
                    "KALSHI-ACCESS-TIMESTAMP": str(ts),
                    "KALSHI-ACCESS-SIGNATURE": sign(self.pk, ts, "GET", path),
                }
                states: dict[str, _BookState] = {m: _BookState() for m in market_ids}
                async with websockets.connect(
                    self.cfg.kalshi_ws_url, additional_headers=headers,
                    ping_interval=20, ping_timeout=20,
                ) as ws:
                    await ws.send(json.dumps({
                        "id": 1, "cmd": "subscribe",
                        "params": {"channels": ["orderbook_delta"], "market_tickers": market_ids},
                    }))
                    backoff = 1.0  # connected cleanly; reset backoff
                    async for raw in ws:
                        m = json.loads(raw)
                        mtype = m.get("type")
                        msg = m.get("msg", {})
                        ticker = msg.get("market_ticker")
                        if ticker not in states:
                            continue
                        if mtype == "orderbook_snapshot":
                            states[ticker].snapshot(msg)
                        elif mtype == "orderbook_delta":
                            states[ticker].delta(msg)
                        else:
                            continue
                        book = states[ticker].top()
                        res = on_book(ticker, book)
                        if res is not None:
                            await res
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Kalshi WS dropped (%s); reconnecting in %.0fs", repr(e)[:120], backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
