"""Offline proof of Phase 1: drives the real Detector with a synthetic feed (no
network, no keys). Simulates an edge appearing, persisting across ticks, then
closing — exercising the exact code path the live detector uses, including fee
math, alerting (recos.xlsx), and opportunity persistence timing.

Run:  ./.venv/bin/python scripts/demo_synthetic.py
"""
import dataclasses
import logging
import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import load_config
from src.books import VenueBook
from src.detector import Detector
from src.fees import KalshiFeeModel, PolyUSFeeModel
from src.mapping import MappedPair, VenueLeg

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("demo")

# Fresh artifacts in scratch so we don't clobber real ones.
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_demo_out")
os.makedirs(OUT, exist_ok=True)
for f in ("arb.db", "recos.xlsx"):
    p = os.path.join(OUT, f)
    if os.path.exists(p):
        os.remove(p)

cfg = dataclasses.replace(
    load_config(),
    db_path=os.path.join(OUT, "arb.db"),
    recos_xlsx=os.path.join(OUT, "recos.xlsx"),
    alert_webhook_url="",
    min_edge=0.01, min_size=10, slippage_buffer=0.005,
    settlement_window_s=3600, staleness_ms=10_000,
)

far = datetime(2026, 12, 1, tzinfo=timezone.utc)
pair = MappedPair(
    "cpi-jun-2026-over-0p3",
    VenueLeg("KXCPIYOY-26JUN-T0.3", "June CPI MoM > 0.3%", "BLS", far, "NO if exactly 0.3%", False),
    VenueLeg("cpi-june-2026-above-0-3", "June CPI MoM > 0.3%", "BLS", far, "NO if exactly 0.3%", False),
    "ajswag", "2026-06-28", True,
)

det = Detector(cfg,
               SimpleNamespace(get_fee_model=lambda mid: KalshiFeeModel(taker_multiplier=0.07, maker_multiplier=0.0)),
               SimpleNamespace(get_fee_model=lambda mid: PolyUSFeeModel()))
det.pairs = [pair]
det.store.upsert_pair(pair)

t0 = datetime(2026, 6, 28, tzinfo=timezone.utc).timestamp()


def tick(label, k, p, now):
    det.books.update(pair.id, "kalshi", k)
    det.books.update(pair.id, "polyus", p)
    log.info("── tick: %s (t=%.0f) ──", label, now - t0)
    det.evaluate_all(now)


# t=0: no edge (prices sum ~1.00 after fees)
tick("no edge", VenueBook(0.49, 0.51, 0.49, 0.51, 100, 100, 100, 100, t0),
     VenueBook(0.49, 0.51, 0.49, 0.51, 100, 100, 100, 100, t0), t0)

# t=2: Poly YES bid jumps to 0.45 -> NO ask 0.55, Kalshi YES ask 0.40 -> ARB opens
k_div = VenueBook(0.38, 0.40, 0.60, 0.62, 90, 100, 100, 90, t0 + 2)
p_div = VenueBook(0.45, 0.47, 0.53, 0.55, 80, 70, 70, 80, t0 + 2)
tick("edge appears", k_div, p_div, t0 + 2)

# t=3.5: still diverged (edge persists) — size shrinks a bit
tick("edge persists", k_div, VenueBook(0.45, 0.47, 0.53, 0.55, 40, 30, 30, 40, t0 + 3.5), t0 + 3.5)

# t=5: prices converge -> edge closes
tick("edge closes", VenueBook(0.49, 0.51, 0.49, 0.51, 100, 100, 100, 100, t0 + 5),
     VenueBook(0.49, 0.51, 0.49, 0.51, 100, 100, 100, 100, t0 + 5), t0 + 5)

print("\n=== edges table (persistence timing) ===")
for r in det.store.conn.execute(
        "SELECT pair_id, structure, taker_edge, maker_edge, size_available, "
        "round(opened_ts-?,1) AS opened_rel, round(closed_ts-?,1) AS closed_rel, "
        "round(duration_ms,0) AS dur_ms, peak_taker_edge "
        "FROM edges", (t0, t0)):
    print(dict(r))

print(f"\nrecos.xlsx written: {os.path.exists(cfg.recos_xlsx)}  ->  {cfg.recos_xlsx}")
print(f"arb.db written:     {cfg.db_path}")
