"""Phase 1 detector. NO order placement anywhere in this module.

Flow: load mappings -> connect both feeds -> on each poll, for every enabled,
fully-specified pair that is not stale and not near settlement, compute the
fee-adjusted edge, persist quotes/edges, and alert on actionable taker edges.
Opportunity persistence (open/close/duration) is tracked so we can see whether
edges survive long enough to capture at real latency.

The per-pair evaluation is a pure function (`evaluate_pair`) so it can be unit
tested with injected books and no network.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from .alerts import emit_alert
from .books import OrderBookStore
from .edge import EdgeResult, compute_edges, is_actionable
from .mapping import MappedPair, load_mappings
from .store import Store

log = logging.getLogger("arb.detector")


@dataclass
class Skip:
    pair_id: str
    reason: str


def _seconds_to_settlement(pair: MappedPair, now_dt: datetime) -> float:
    k = (pair.kalshi.settlement_time - now_dt).total_seconds()
    p = (pair.polyus.settlement_time - now_dt).total_seconds()
    return min(k, p)


def evaluate_pair(
    pair: MappedPair,
    store_books: OrderBookStore,
    kalshi_fee,
    polyus_fee,
    cfg,
    now: float | None = None,
) -> tuple[EdgeResult, bool] | Skip:
    """Return (EdgeResult, actionable) or a Skip with the reason.

    Pure: reads books/fees passed in, no network. Enforces the tradability gates
    (enabled, settlement window, staleness, both books present)."""
    now = now if now is not None else time.time()
    now_dt = datetime.fromtimestamp(now, tz=timezone.utc)

    if not pair.enabled:
        return Skip(pair.id, "disabled")

    secs = _seconds_to_settlement(pair, now_dt)
    if secs <= cfg.settlement_window_s:
        return Skip(pair.id, f"within settlement window ({secs:.0f}s <= {cfg.settlement_window_s}s)")

    if store_books.is_stale(pair.id, "kalshi", now):
        return Skip(pair.id, "kalshi feed stale/missing")
    if store_books.is_stale(pair.id, "polyus", now):
        return Skip(pair.id, "polyus feed stale/missing")

    kb = store_books.get(pair.id, "kalshi")
    pb = store_books.get(pair.id, "polyus")
    if kb is None or pb is None:
        return Skip(pair.id, "missing book")

    res = compute_edges(
        pair.id,
        kb, kalshi_fee, pair.kalshi.invert,
        pb, polyus_fee, pair.polyus.invert,
    )
    actionable = is_actionable(res.best, cfg.min_edge, cfg.min_size, cfg.slippage_buffer,
                               getattr(cfg, "max_plausible_edge", 1.0))
    return res, actionable


class OpportunityTracker:
    """Tracks open opportunities per (pair_id, structure) for persistence timing."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self._open: dict[tuple[str, str], int] = {}

    def on_eval(self, pair_id: str, res: EdgeResult, actionable: bool, cfg, now: float) -> bool:
        """Returns True only when a NEW opportunity opens (so callers alert once,
        not every poll)."""
        key = (pair_id, res.best.structure)
        edge_id = self._open.get(key)
        if actionable and edge_id is None:
            self._open[key] = self.store.open_edge(pair_id, res.best, cfg.slippage_buffer, now)
            return True
        elif actionable and edge_id is not None:
            self.store.bump_edge(edge_id, res.best.taker_edge, res.best.taker_size)
        elif not actionable and edge_id is not None:
            self.store.close_edge(edge_id, now)
            del self._open[key]
        return False


class Detector:
    def __init__(self, cfg, kalshi_client, polyus_client, sports_league: str | None = None) -> None:
        self.cfg = cfg
        self.kalshi = kalshi_client
        self.polyus = polyus_client
        self.sports_league = sports_league
        self.books = OrderBookStore(cfg.staleness_ms)
        self.store = Store(cfg.db_path)
        self.tracker = OpportunityTracker(self.store)
        self.pairs: list[MappedPair] = []
        self._fees: dict[str, tuple] = {}  # pair_id -> (kalshi_fee, polyus_fee)
        self._last_alert: dict[tuple, float] = {}  # (pair_id, structure) -> last alert ts

    def load(self) -> None:
        if self.sports_league:
            # DETECTION ONLY: auto-matched, UNVERIFIED pairs. Never tradable.
            from .scanner import ALL_SPORTS, build_macro_pairs, build_sports_pairs
            toks = [x.strip() for x in self.sports_league.strip().lower().split(",") if x.strip()]
            sport, macro = set(), False
            for t in toks:
                if t == "all":
                    sport |= set(ALL_SPORTS)
                elif t == "macro":
                    macro = True
                elif t in ALL_SPORTS:
                    sport.add(t)
            self.pairs = []
            if sport:
                self.pairs += build_sports_pairs(self.cfg, self.kalshi, self.polyus, sorted(sport))
            if macro:
                self.pairs += build_macro_pairs(self.cfg, self.kalshi, self.polyus)
            removed = self.store.prune_pairs([p.id for p in self.pairs])
            log.warning("WATCH mode (%s): %d auto-matched pairs (UNVERIFIED — detection only); pruned %d finished",
                        ",".join(toks), len(self.pairs), removed)
        else:
            self.pairs = [p for p in load_mappings(self.cfg.mappings_path)]
            log.info("loaded %d verified mapped pairs", len(self.pairs))
        for p in self.pairs:
            self.store.upsert_pair(p)

    def _fees_for(self, pair: MappedPair) -> tuple:
        if pair.id not in self._fees:
            self._fees[pair.id] = (
                self.kalshi.get_fee_model(pair.kalshi.market_id),
                self.polyus.get_fee_model(pair.polyus.market_id),
            )
        return self._fees[pair.id]

    def evaluate_all(self, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        for pair in self.pairs:
            try:
                kfee, pfee = self._fees_for(pair)
            except Exception as e:
                log.error("pair %s: fee fetch failed: %s", pair.id, e)
                continue
            try:
                out = evaluate_pair(pair, self.books, kfee, pfee, self.cfg, now)
            except Exception as e:
                log.error("pair %s: evaluation failed: %s", pair.id, e)
                continue
            if isinstance(out, Skip):
                log.debug("skip %s: %s", out.pair_id, out.reason)
                continue
            res, actionable = out
            opened = self.tracker.on_eval(pair.id, res, actionable, self.cfg, now)
            log.debug("pair %s best=%s taker_edge=%.4f size=%.0f actionable=%s",
                      pair.id, res.best.structure, res.best.taker_edge,
                      res.best.taker_size, actionable)
            # alert once per opportunity (on open), throttled per pair
            if actionable and opened:
                key = (pair.id, res.best.structure)
                cooldown = getattr(self.cfg, "alert_cooldown_s", 900)
                if now - self._last_alert.get(key, 0) >= cooldown:
                    self._last_alert[key] = now
                    emit_alert(self.cfg, now, pair.id, res.best, self.cfg.slippage_buffer)

    def _on_book(self, venue: str):
        # map venue market_id -> pair_id
        if venue == "kalshi":
            idx = {p.kalshi.market_id: p.id for p in self.pairs}
        else:
            idx = {p.polyus.market_id: p.id for p in self.pairs}

        def handler(market_id: str, book) -> None:
            pair_id = idx.get(market_id)
            if pair_id is None:
                return
            self.books.update(pair_id, venue, book)
            stale = False
            self.store.record_quote(pair_id, venue, book, stale)

        return handler

    async def run(self) -> None:
        self.load()
        if not self.pairs:
            log.warning("no mapped pairs — nothing to detect")
            return
        kalshi_ids = [p.kalshi.market_id for p in self.pairs]
        polyus_ids = [p.polyus.market_id for p in self.pairs]

        async def poll_loop():
            while True:
                try:
                    self.evaluate_all()
                except Exception as e:
                    log.error("poll loop error: %s", e)
                await asyncio.sleep(self.cfg.edge_poll_ms / 1000.0)

        await asyncio.gather(
            self.kalshi.stream_books(kalshi_ids, self._on_book("kalshi")),
            self.polyus.stream_books(polyus_ids, self._on_book("polyus")),
            poll_loop(),
        )


def main() -> None:
    import argparse

    from config import load_config
    from .venues.kalshi import KalshiClient
    from .venues.polyus import PolyUSClient

    ap = argparse.ArgumentParser(description="Phase 1 cross-venue arb detector (no trading)")
    ap.add_argument("--log-level", default="INFO")
    ap.add_argument("--sports", metavar="LEAGUES", default=None,
                    help="watch live no-draw sports instead of mappings.yaml (detection only). "
                         "Comma list (e.g. mlb,atp,wta,ufc) or 'all'.")
    args = ap.parse_args()

    cfg = load_config()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.FileHandler(cfg.log_path), logging.StreamHandler()],
    )
    det = Detector(cfg, KalshiClient(cfg), PolyUSClient(cfg), sports_league=args.sports)
    from .notify import Notifier
    notifier = Notifier.from_config(cfg)
    try:
        asyncio.run(det.run())
    except KeyboardInterrupt:
        log.info("detector stopped")
    except Exception as e:
        log.exception("detector crashed")
        notifier.error("detector", f"{type(e).__name__}: {e}")
        raise


if __name__ == "__main__":
    main()
