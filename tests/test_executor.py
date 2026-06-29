import dataclasses
from datetime import datetime, timezone

import pytest

from config import load_config
from src.books import VenueBook
from src.executor import limits as L
from src.executor.executor import Executor, LiveGuardError, ensure_live_allowed
from src.executor.killswitch import KillSwitch
from src.executor.router import Fill, LegPlan, PaperRouter, Router
from src.executor.state_machine import ArbPlan, execute_arb, pick_thinner_first
from src.fees import KalshiFeeModel, PolyUSFeeModel
from src.mapping import MappedPair, VenueLeg
from src.store import Store

NOW = datetime(2026, 6, 28, tzinfo=timezone.utc).timestamp()


def leg(venue="kalshi", buy=0.40, bsz=100, sell=0.38, ssz=100):
    return LegPlan(venue, "M", "A", buy, bsz, sell, ssz, KalshiFeeModel(0.07, 0.0175))


class ScriptRouter(Router):
    """Returns scripted fills regardless of qty, for state-machine tests."""

    def __init__(self, buys, sells=()):
        self.buys = list(buys)
        self.sells = list(sells)
        self.sell_calls = 0

    def buy(self, leg, qty):
        q, p, f = self.buys.pop(0)
        return Fill(leg, "buy", q, p, f)

    def sell(self, leg, qty):
        self.sell_calls += 1
        q, p, f = self.sells.pop(0)
        return Fill(leg, "sell", q, p, f)


# ── state machine ────────────────────────────────────────────────
def test_completed_both_legs():
    plan = ArbPlan("p", "s", leg(), leg("polyus", 0.55, 80, 0.53, 80), 10, min_fill=10)
    r = ScriptRouter(buys=[(10, 0.40, 0.17), (10, 0.55, 0.12)])
    res = execute_arb(plan, r)
    assert res.status == "completed"
    assert res.hedged_qty == 10
    # -4.0-0.17 -5.5-0.12 +10 = 0.21
    assert res.realized_pnl == pytest.approx(0.21, abs=1e-9)
    assert r.sell_calls == 0


def test_leg2_timeout_triggers_leg1_unwind():
    plan = ArbPlan("p", "s", leg(), leg("polyus", 0.55, 80), 10, min_fill=10)
    # leg2 returns 0 fill (== live LEG2_TIMEOUT); leg1 must be unwound via sell
    r = ScriptRouter(buys=[(10, 0.40, 0.17), (0, 0.55, 0.0)], sells=[(10, 0.38, 0.17)])
    res = execute_arb(plan, r)
    assert res.status == "leg2_failed_unwound"
    assert res.hedged_qty == 0
    assert r.sell_calls == 1                 # leg 1 was unwound, not left naked
    assert res.realized_pnl < 0              # realized the failed-attempt cost


def test_partial_leg2_unwinds_excess():
    plan = ArbPlan("p", "s", leg(), leg("polyus", 0.55, 80), 10, min_fill=10)
    r = ScriptRouter(buys=[(10, 0.40, 0.17), (6, 0.55, 0.07)], sells=[(4, 0.38, 0.07)])
    res = execute_arb(plan, r)
    assert res.status == "partial_unwound"
    assert res.hedged_qty == 6
    assert r.sell_calls == 1                 # 4 unhedged contracts unwound


def test_leg1_no_fill_opens_no_position():
    plan = ArbPlan("p", "s", leg(), leg("polyus", 0.55, 80), 10, min_fill=10)
    r = ScriptRouter(buys=[(0, 0.40, 0.0)])
    res = execute_arb(plan, r)
    assert res.status == "leg1_no_fill"
    assert res.hedged_qty == 0 and r.sell_calls == 0


def test_pick_thinner_first():
    a = leg("kalshi", bsz=100)
    b = leg("polyus", bsz=40)
    first, second = pick_thinner_first("s", a, b)
    assert first.venue == "polyus" and second.venue == "kalshi"


# ── hard limits ──────────────────────────────────────────────────
def _limits():
    return L.Limits(max_contracts_per_market=20, max_total_capital=100,
                    max_concurrent_arbs=2, max_order_size=10, min_edge=0.01,
                    max_daily_loss=15)


def _empty_state(open_arbs=0, deployed=0.0, pos=None):
    return L.AccountState(open_arbs, deployed, pos or {})


def test_limit_order_size_rejected():
    with pytest.raises(L.LimitError):
        L.check(_limits(), edge=0.05, size=11, legs=[leg()], state=_empty_state())


def test_limit_min_edge_rejected():
    with pytest.raises(L.LimitError):
        L.check(_limits(), edge=0.005, size=5, legs=[leg()], state=_empty_state())


def test_limit_per_market_rejected():
    st = _empty_state(pos={("kalshi", "M"): 18})
    with pytest.raises(L.LimitError):
        L.check(_limits(), edge=0.05, size=5, legs=[leg()], state=st)


def test_limit_capital_rejected():
    legs = [leg(buy=0.9), leg("polyus", buy=0.9)]
    st = _empty_state(deployed=90.0)
    with pytest.raises(L.LimitError):
        L.check(_limits(), edge=0.05, size=10, legs=legs, state=st)


def test_limit_concurrent_rejected():
    with pytest.raises(L.LimitError):
        L.check(_limits(), edge=0.05, size=5, legs=[leg()], state=_empty_state(open_arbs=2))


def test_limit_valid_passes():
    L.check(_limits(), edge=0.05, size=5, legs=[leg(buy=0.4), leg("polyus", buy=0.5)],
            state=_empty_state())


# ── kill switch ──────────────────────────────────────────────────
def test_kill_switch_daily_loss(tmp_path):
    store = Store(str(tmp_path / "k.db"))
    ks = KillSwitch(str(tmp_path / "HALT"), max_daily_loss=15, store=store)
    today = ks._today()
    assert not ks.tripped()
    store.add_pnl(today, -16.0, 0.0)
    tripped, reason = ks.status()
    assert tripped and "daily loss" in reason


def test_kill_switch_sentinel(tmp_path):
    store = Store(str(tmp_path / "k.db"))
    sentinel = tmp_path / "HALT"
    ks = KillSwitch(str(sentinel), max_daily_loss=15, store=store)
    assert not ks.tripped()
    ks.engage_manual()
    assert ks.tripped()
    ks.release_manual()
    assert not ks.tripped()


# ── live-mode guard ──────────────────────────────────────────────
def test_live_guard_refuses_without_all_gates():
    cfg = dataclasses.replace(load_config(), armed=False)
    pairs = [object()]
    bal = {"kalshi": 26, "polyus": 79}
    with pytest.raises(LiveGuardError):       # ARMED false
        ensure_live_allowed(cfg, True, pairs, bal)
    cfg2 = dataclasses.replace(load_config(), armed=True)
    with pytest.raises(LiveGuardError):       # --live flag false
        ensure_live_allowed(cfg2, False, pairs, bal)
    with pytest.raises(LiveGuardError):       # empty mappings
        ensure_live_allowed(cfg2, True, [], bal)
    with pytest.raises(LiveGuardError):       # unfunded
        ensure_live_allowed(cfg2, True, pairs, {"kalshi": 0, "polyus": 0})
    # all gates satisfied -> no raise
    ensure_live_allowed(cfg2, True, pairs, bal)


# ── paper integration ────────────────────────────────────────────
def _pair():
    far = datetime(2026, 12, 1, tzinfo=timezone.utc)
    return MappedPair("p1", VenueLeg("KX", "x", "s", far, "n/a", False),
                      VenueLeg("px", "x", "s", far, "n/a", False), "t", "2026-06-28", True)


def _cfg(tmp_path, **over):
    base = dict(db_path=str(tmp_path / "e.db"), kill_switch_file=str(tmp_path / "HALT"),
                min_edge=0.01, min_size=10, slippage_buffer=0.005,
                settlement_window_s=3600, max_order_size=10, max_total_capital=100,
                max_contracts_per_market=20, max_concurrent_arbs=2)
    base.update(over)
    return dataclasses.replace(load_config(), **base)


def test_executor_paper_completes_and_books_pnl(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    ex = Executor(cfg, store, router=PaperRouter(), mode="paper")
    kb = VenueBook(0.38, 0.40, 0.60, 0.62, 90, 100, 100, 90, NOW)
    pb = VenueBook(0.45, 0.47, 0.53, 0.55, 80, 70, 70, 80, NOW)
    res = ex.consider(_pair(), kb, KalshiFeeModel(0.07, 0.0175), False,
                      pb, PolyUSFeeModel(), False, NOW)
    assert res is not None and res.status == "completed"
    assert res.realized_pnl > 0
    assert store.get_today_realized(ex._today()) == pytest.approx(res.realized_pnl, abs=1e-6)
    assert store.conn.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0] >= 2


def test_executor_daily_loss_trips_kill_switch(tmp_path):
    cfg = _cfg(tmp_path, max_daily_loss=0.5)
    store = Store(cfg.db_path)
    # router forces a failed leg2 (big loss) to trip the daily-loss kill switch
    router = ScriptRouter(buys=[(10, 0.40, 0.17), (0, 0.55, 0.0)], sells=[(10, 0.30, 0.17)])
    ex = Executor(cfg, store, router=router, mode="paper")
    kb = VenueBook(0.38, 0.40, 0.60, 0.62, 90, 100, 100, 90, NOW)
    pb = VenueBook(0.45, 0.47, 0.53, 0.55, 80, 70, 70, 80, NOW)
    pair = _pair()
    res = ex.consider(pair, kb, KalshiFeeModel(0.07, 0.0175), False,
                      pb, PolyUSFeeModel(), False, NOW)
    assert res.realized_pnl < 0
    assert ex.kill.tripped()                       # daily-loss kill switch fired
    # a subsequent attempt is blocked
    assert ex.consider(pair, kb, KalshiFeeModel(0.07, 0.0175), False,
                       pb, PolyUSFeeModel(), False, NOW) is None
