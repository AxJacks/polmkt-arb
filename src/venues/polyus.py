"""Polymarket US venue client (read-only for Phase 1). The CFTC-regulated DCM —
NOT Polymarket Global (Polygon). We never place orders on Global.

Auth: Ed25519. Signed message = timestamp_ms + METHOD + path (body NOT signed).
Secret key is base64; first 32 bytes are the Ed25519 seed. Timestamp must be
within 30s of server time. Docs: https://docs.polymarket.us  (verified 2026-06).

Book normalization: we stream the market's YES book (bids/offers). The NOT-A
(NO) exposure is obtained by trading the YES token (sell YES = take the YES bid),
so we derive  no_ask = 1 - yes_bid,  no_bid = 1 - yes_ask  with mirrored sizes.
If Poly US later exposes an independent NO book we subscribe to it directly.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Awaitable, Callable
from urllib.parse import urlsplit

import httpx
import websockets

log = logging.getLogger("arb.polyus")
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ..books import VenueBook
from ..fees import PolyUSFeeModel
from .base import VenueClient


class PolyUSAuthError(RuntimeError):
    pass


def load_signing_key(secret_key_b64: str) -> Ed25519PrivateKey:
    raw = base64.b64decode(secret_key_b64)
    return Ed25519PrivateKey.from_private_bytes(raw[:32])


def sign(key: Ed25519PrivateKey, timestamp_ms: int, method: str, path: str) -> str:
    msg = f"{timestamp_ms}{method}{path}".encode()
    return base64.b64encode(key.sign(msg)).decode()


def _px(value) -> float:
    """Extract a float price from a px field that may be a dict {value,currency},
    a string, or a number."""
    if isinstance(value, dict):
        value = value.get("value")
    return float(value)


class PolyUSClient(VenueClient):
    name = "polyus"

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._key = None

    @property
    def key(self) -> Ed25519PrivateKey:
        if self._key is None:
            if not self.cfg.polyus_secret_key:
                raise PolyUSAuthError("POLYUS_SECRET_KEY not set")
            self._key = load_signing_key(self.cfg.polyus_secret_key)
        return self._key

    def _headers(self, method: str, url: str) -> dict:
        ts = int(time.time() * 1000)
        path = urlsplit(url).path
        return {
            "X-PM-Access-Key": self.cfg.polyus_key_id,
            "X-PM-Timestamp": str(ts),
            "X-PM-Signature": sign(self.key, ts, method, path),
            "Content-Type": "application/json",
        }

    def _get(self, endpoint: str, params: dict | None = None) -> dict:
        url = self.cfg.polyus_rest_base + endpoint
        r = httpx.get(url, params=params, headers=self._headers("GET", url), timeout=15)
        r.raise_for_status()
        return r.json()

    # ── reads ────────────────────────────────────────────────
    def list_markets(self, **filters) -> list[dict]:
        data = self._get("/markets", {"limit": filters.get("limit", 1000)})
        return data.get("markets", data if isinstance(data, list) else [])

    def market_is_open(self, market_id: str) -> bool:
        m = self._get(f"/markets/{market_id}")
        m = m.get("market", m)
        status = str(m.get("status", "")).lower()
        return "active" in status or "open" in status

    def get_fee_model(self, market_id: str) -> PolyUSFeeModel:
        # Published 2026 schedule: taker theta 0.05, maker theta -0.0125 (rebate).
        # Single schedule across categories (verified at docs.polymarket.us/fees).
        return PolyUSFeeModel()

    # ── account (read-only; safe in Phase 1/2 paper) ─────────
    def get_balance(self) -> float:
        """USD buying power in dollars."""
        data = self._get("/account/balances")
        balances = data.get("balances", [])
        if not balances:
            return 0.0
        b = balances[0]
        return float(b.get("buyingPower", b.get("currentBalance", 0)))

    # ── feed ─────────────────────────────────────────────────
    async def stream_books(
        self,
        market_ids: list[str],
        on_book: Callable[[str, VenueBook], Awaitable[None] | None],
    ) -> None:
        path = urlsplit(self.cfg.polyus_ws_url).path
        backoff = 1.0
        while True:
            try:
                ts = int(time.time() * 1000)
                headers = {
                    "X-PM-Access-Key": self.cfg.polyus_key_id,
                    "X-PM-Timestamp": str(ts),
                    "X-PM-Signature": sign(self.key, ts, "GET", path),
                }
                async with websockets.connect(
                    self.cfg.polyus_ws_url, additional_headers=headers,
                    ping_interval=20, ping_timeout=20,
                ) as ws:
                    await ws.send(json.dumps({
                        "subscribe": {
                            "requestId": "md-sub-1",
                            "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
                            "marketSlugs": market_ids,
                        }
                    }))
                    backoff = 1.0
                    async for raw in ws:
                        m = json.loads(raw)
                        book = self._parse_book(m)
                        if book is None:
                            continue
                        slug, vbook = book
                        if slug not in market_ids:
                            continue
                        res = on_book(slug, vbook)
                        if res is not None:
                            await res
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Poly US WS dropped (%s); reconnecting in %.0fs", repr(e)[:120], backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    @staticmethod
    def _parse_book(m: dict) -> tuple[str, VenueBook] | None:
        """Parse a MARKET_DATA message into (slug, VenueBook). Returns None for
        non-book messages (acks, heartbeats)."""
        data = m.get("marketData") or m.get("data") or m
        slug = data.get("marketSlug") or data.get("slug")
        bids = data.get("bids")
        offers = data.get("offers") or data.get("asks")
        if slug is None or bids is None or offers is None:
            return None
        best_bid = _px(bids[0]["px"]) if bids else 0.0
        best_bid_sz = float(bids[0]["qty"]) if bids else 0.0
        best_ask = _px(offers[0]["px"]) if offers else 1.0
        best_ask_sz = float(offers[0]["qty"]) if offers else 0.0
        return slug, VenueBook(
            yes_bid=best_bid, yes_ask=best_ask,
            no_bid=1.0 - best_ask, no_ask=1.0 - best_bid,
            yes_bid_size=best_bid_sz, yes_ask_size=best_ask_sz,
            no_bid_size=best_ask_sz, no_ask_size=best_bid_sz,
            ts=time.time(),
        )
