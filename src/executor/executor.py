"""Phase 2 executor. Modes: paper (default, simulated) and live (gated, off).

Decision path for every candidate arb (consider()):
  1. kill switch?            -> halt, no order
  2. resolution guard        -> fully mapped + not near settlement + books fresh
  3. hard limits (limits.py) -> reject if any cap breached
  4. both-legs state machine -> execute via Router (paper or live)
  5. persist fills, PnL, positions; trip kill switch if daily loss breached

Live mode refuses to start unless ALL gates pass: --live + ARMED + funded
balances on both venues + non-empty verified mappings.yaml.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from ..edge import compute_edges, is_actionable, normalize
from ..mapping import MappedPair
from ..notify import Notifier
from . import limits as L
from .killswitch import KillSwitch
from .router import LegPlan, PaperRouter, Router
from .state_machine import ArbPlan, execute_arb, pick_thinner_first

log = logging.getLogger("arb.executor")


class LiveGuardError(RuntimeError):
    """Live mode requested but a required gate is not satisfied."""


def ensure_live_allowed(cfg, live_flag: bool, mappings: list, balances: dict | None) -> None:
    problems = []
    if not live_flag:
        problems.append("--live flag not set")
    if not cfg.armed:
        problems.append("ARMED is not true")
    if not mappings:
        problems.append("mappings.yaml is empty (no verified pairs)")
    if not balances or not all(v and v > 0 for v in balances.values()):
        problems.append("funded balances not confirmed on both venues")
    if problems:
        raise LiveGuardError("LIVE refused: " + "; ".join(problems))


def _legs_for_structure(structure: str, kbook, kfee, k_inv, pbook, pfee, p_inv):
    """Return (legA_holder, legNOTA_holder) as LegPlans in mapped-outcome space."""
    k = normalize(kbook, k_inv)
    p = normalize(pbook, p_inv)

    def leg(venue, sides, outcome, fee):
        if outcome == "A":
            return LegPlan(venue, "", "A", sides.a_ask, sides.a_ask_size,
                           sides.a_bid, sides.a_bid_size, fee)
        return LegPlan(venue, "", "NOT-A", sides.nota_ask, sides.nota_ask_size,
                       sides.nota_bid, sides.nota_bid_size, fee)

    if structure == "A@kalshi/NOTA@polyus":
        return leg("kalshi", k, "A", kfee), leg("polyus", p, "NOT-A", pfee)
    return leg("polyus", p, "A", pfee), leg("kalshi", k, "NOT-A", kfee)


class Executor:
    def __init__(self, cfg, store, router: Router | None = None, mode: str = "paper") -> None:
        self.cfg = cfg
        self.store = store
        self.mode = mode
        self.router = router or PaperRouter()
        self.limits = L.Limits.from_config(cfg)
        self.kill = KillSwitch(cfg.kill_switch_file, cfg.max_daily_loss, store)
        self.notify = Notifier.from_config(cfg)
        self._inflight = 0

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _seconds_to_settlement(self, pair: MappedPair, now_dt) -> float:
        return min((pair.kalshi.settlement_time - now_dt).total_seconds(),
                   (pair.polyus.settlement_time - now_dt).total_seconds())

    def _account_state(self) -> L.AccountState:
        pos = self.store.positions()
        deployed = sum(abs(q) for q in pos.values())  # contracts proxy
        return L.AccountState(open_arbs=self._inflight, deployed_capital=deployed,
                              position_contracts=pos)

    def consider(self, pair: MappedPair, kbook, kfee, k_inv, pbook, pfee, p_inv,
                 now: float):
        """Evaluate + (paper/live) execute one candidate arb. Returns ArbResult or None."""
        tripped, reason = self.kill.status()
        if tripped:
            log.warning("kill switch active (%s) — no orders", reason)
            return None

        now_dt = datetime.fromtimestamp(now, tz=timezone.utc)
        if self._seconds_to_settlement(pair, now_dt) <= self.cfg.settlement_window_s:
            log.info("%s within settlement window — skip", pair.id)
            return None

        res = compute_edges(pair.id, kbook, kfee, k_inv, pbook, pfee, p_inv)
        if not is_actionable(res.best, self.cfg.min_edge, self.cfg.min_size,
                             self.cfg.slippage_buffer, getattr(self.cfg, "max_plausible_edge", 1.0)):
            return None

        size = int(min(res.best.taker_size, self.limits.max_order_size))
        leg_a, leg_nota = _legs_for_structure(res.best.structure, kbook, kfee, k_inv,
                                              pbook, pfee, p_inv)
        leg_a.market_id = pair.kalshi.market_id if leg_a.venue == "kalshi" else pair.polyus.market_id
        leg_nota.market_id = pair.kalshi.market_id if leg_nota.venue == "kalshi" else pair.polyus.market_id

        try:
            L.check(self.limits, edge=res.best.taker_edge - self.cfg.slippage_buffer,
                    size=size, legs=[leg_a, leg_nota], state=self._account_state())
        except L.LimitError as e:
            log.info("limit rejected %s: %s", pair.id, e)
            return None

        leg1, leg2 = pick_thinner_first(res.best.structure, leg_a, leg_nota)
        plan = ArbPlan(pair.id, res.best.structure, leg1, leg2, size,
                       min_fill=min(self.cfg.min_size, size))

        self._inflight += 1
        try:
            result = execute_arb(plan, self.router)
        finally:
            self._inflight -= 1

        self._persist(pair.id, result, now)
        return result

    def _persist(self, pair_id, result, now) -> None:
        fees = 0.0
        for f in result.fills:
            role = "taker"
            self.store.record_paper_fill(pair_id, f.leg.venue, f.leg.outcome, role,
                                         f.price, f.filled_qty, f.fee, f.ts)
            fees += f.fee
            dq = f.filled_qty if f.side == "buy" else -f.filled_qty
            self.store.upsert_position(f.leg.venue, f.leg.market_id, dq, f.price, now)
        self.store.add_pnl(self._today(), result.realized_pnl, fees)
        log.info("ARB %s %s hedged=%.0f pnl=%.4f", pair_id, result.status,
                 result.hedged_qty, result.realized_pnl)
        if result.status != "leg1_no_fill":
            self.notify.trade(pair_id, result)
        # daily-loss kill switch
        if self.store.get_today_realized(self._today()) <= -abs(self.cfg.max_daily_loss):
            self.kill.trip_for_loss()
            self.notify.kill_switch(f"daily loss reached MAX_DAILY_LOSS (${self.cfg.max_daily_loss})")


async def _run(cfg, kalshi, polyus, mode: str, router) -> None:
    import asyncio

    from ..books import OrderBookStore
    from ..mapping import load_mappings
    from ..store import Store

    pairs = load_mappings(cfg.mappings_path)
    store = Store(cfg.db_path)
    ex = Executor(cfg, store, router=router, mode=mode)
    books = OrderBookStore(cfg.staleness_ms)
    fees: dict = {}

    def handler(venue, idx):
        def h(market_id, book):
            pid = idx.get(market_id)
            if pid:
                books.update(pid, venue, book)
        return h

    k_idx = {p.kalshi.market_id: p.id for p in pairs}
    p_idx = {p.polyus.market_id: p.id for p in pairs}

    async def poll():
        import time
        while True:
            now = time.time()
            for pair in pairs:
                kb, pb = books.get(pair.id, "kalshi"), books.get(pair.id, "polyus")
                if kb is None or pb is None:
                    continue
                if books.is_stale(pair.id, "kalshi", now) or books.is_stale(pair.id, "polyus", now):
                    continue
                if pair.id not in fees:
                    try:
                        fees[pair.id] = (kalshi.get_fee_model(pair.kalshi.market_id),
                                         polyus.get_fee_model(pair.polyus.market_id))
                    except Exception as e:
                        log.error("fee fetch %s: %s", pair.id, e)
                        continue
                kfee, pfee = fees[pair.id]
                try:
                    ex.consider(pair, kb, kfee, pair.kalshi.invert, pb, pfee,
                                pair.polyus.invert, now)
                except Exception as e:
                    log.error("consider %s: %s", pair.id, e)
            await asyncio.sleep(cfg.edge_poll_ms / 1000.0)

    log.info("executor running in %s mode over %d pairs", mode, len(pairs))
    ex.notify.startup(mode, len(pairs))
    await asyncio.gather(
        kalshi.stream_books([p.kalshi.market_id for p in pairs], handler("kalshi", k_idx)),
        polyus.stream_books([p.polyus.market_id for p in pairs], handler("polyus", p_idx)),
        poll(),
    )


def main() -> None:
    import argparse
    import asyncio

    from config import load_config
    from ..mapping import load_mappings
    from ..venues.kalshi import KalshiClient
    from ..venues.polyus import PolyUSClient
    from .router import LiveRouter, PaperRouter

    ap = argparse.ArgumentParser(description="Phase 2 executor (paper default; live is gated)")
    ap.add_argument("--live", action="store_true", help="request live mode (also needs ARMED + funding + mappings)")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    cfg = load_config()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.FileHandler(cfg.log_path), logging.StreamHandler()],
    )
    kalshi, polyus = KalshiClient(cfg), PolyUSClient(cfg)
    want_live = args.live or cfg.mode == "live"

    if want_live:
        try:
            balances = {"kalshi": kalshi.get_balance(), "polyus": polyus.get_balance()}
        except Exception as e:
            balances = None
            log.error("could not confirm balances: %s", e)
        try:
            ensure_live_allowed(cfg, args.live, load_mappings(cfg.mappings_path), balances)
        except LiveGuardError as e:
            import sys
            print(f"\n{e}\n\nTo arm live trading ALL of these must hold:\n"
                  "  • run with --live\n  • ARMED=true in .env\n"
                  "  • funded balances on both venues\n  • non-empty mappings.yaml\n"
                  "Live order placement itself is also not built yet — paper mode only.")
            sys.exit(2)
        log.warning("LIVE MODE ARMED — real orders enabled")
        router = LiveRouter({"kalshi": kalshi, "polyus": polyus}, cfg.leg2_timeout_ms)
        mode = "live"
    else:
        router = PaperRouter()
        mode = "paper"

    try:
        asyncio.run(_run(cfg, kalshi, polyus, mode, router))
    except KeyboardInterrupt:
        log.info("executor stopped")
    except Exception as e:
        log.exception("executor crashed")
        Notifier.from_config(cfg).error("executor", f"{type(e).__name__}: {e}")
        raise


if __name__ == "__main__":
    main()
