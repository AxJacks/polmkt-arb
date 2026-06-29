"""Phase 2 paper-mode proof (no keys, no real orders). Drives the real Executor
through the both-legs state machine against a synthetic divergence and reports
net-of-fee PnL, fills, and positions from SQLite.

Run:  ./.venv/bin/python scripts/demo_paper.py
"""
import dataclasses
import logging
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import load_config
from src.books import VenueBook
from src.executor.executor import Executor
from src.executor.router import PaperRouter
from src.fees import KalshiFeeModel, PolyUSFeeModel
from src.mapping import MappedPair, VenueLeg
from src.store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_demo_out")
os.makedirs(OUT, exist_ok=True)
for f in ("paper.db",):
    p = os.path.join(OUT, f)
    if os.path.exists(p):
        os.remove(p)

cfg = dataclasses.replace(
    load_config(), db_path=os.path.join(OUT, "paper.db"),
    kill_switch_file=os.path.join(OUT, "HALT"),
    min_edge=0.01, min_size=10, slippage_buffer=0.005, settlement_window_s=3600,
    max_order_size=10, max_total_capital=100, max_contracts_per_market=20, max_concurrent_arbs=2,
)
store = Store(cfg.db_path)
ex = Executor(cfg, store, router=PaperRouter(), mode="paper")

far = datetime(2026, 12, 1, tzinfo=timezone.utc)
pair = MappedPair(
    "cpi-demo", VenueLeg("KXCPIYOY-26JUN-T3.8", "CPI>3.8%", "BLS", far, "n/a", False),
    VenueLeg("cpic-...-gt3pt8pct", "CPI>3.8%", "BLS", far, "n/a", False), "ajswag", "2026-06-28", True)

now = datetime(2026, 6, 28, tzinfo=timezone.utc).timestamp()
# Synthetic divergence: Kalshi YES cheap (0.40), Poly NO cheap -> ~2% edge.
kb = VenueBook(0.38, 0.40, 0.60, 0.62, 90, 100, 100, 90, now)
pb = VenueBook(0.45, 0.47, 0.53, 0.55, 80, 70, 70, 80, now)

print("\n=== running paper executor on a synthetic arb ===")
res = ex.consider(pair, kb, KalshiFeeModel(0.07, 0.0175), False, pb, PolyUSFeeModel(), False, now)
print(f"result: status={res.status} hedged={res.hedged_qty:.0f} realized_pnl=${res.realized_pnl:.4f}")

print("\nfills:")
for f in res.fills:
    print(f"  {f.side:4} {f.leg.venue:7} {f.leg.outcome:6} qty {f.filled_qty:.0f} @ {f.price:.3f} fee {f.fee:.3f} cash {f.cash:+.3f}")

print("\n=== SQLite state ===")
print("paper_fills:", store.conn.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0])
for r in store.conn.execute("SELECT date, round(realized,4) realized, round(fees,4) fees, kill_switch_tripped FROM pnl_daily"):
    print("  pnl_daily:", dict(r))
print("  positions:")
for r in store.conn.execute("SELECT venue, market_id, net_qty FROM positions"):
    print("   ", dict(r))
print(f"\nNet realized PnL after real fees: ${store.get_today_realized(ex._today()):.4f}")
