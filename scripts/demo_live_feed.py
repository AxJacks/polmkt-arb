"""Continuous synthetic feed for eyeballing the dashboard without API keys. Drives
the real Detector with fresh-timestamped books whose prices oscillate, so edges
appear and close on a cycle. Writes to the same arb.db the dashboard reads.

Run (terminal 1):  ./.venv/bin/python scripts/demo_live_feed.py
Run (terminal 2):  ./.venv/bin/python -m src.dashboard
Then open:         http://127.0.0.1:8787
"""
import argparse
import dataclasses
import logging
import os
import sys
import time
from datetime import datetime, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import load_config
from src.books import VenueBook
from src.detector import Detector
from src.fees import KalshiFeeModel, PolyUSFeeModel
from src.mapping import MappedPair, VenueLeg

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

ap = argparse.ArgumentParser()
ap.add_argument("--db", default=load_config().db_path)
ap.add_argument("--seconds", type=float, default=0, help="run time; 0 = forever")
args = ap.parse_args()

cfg = dataclasses.replace(
    load_config(), db_path=args.db, alert_webhook_url="",
    min_edge=0.01, min_size=10, slippage_buffer=0.005,
    settlement_window_s=3600, staleness_ms=3000,
)
far = datetime(2026, 12, 1, tzinfo=timezone.utc)
pairs = [
    MappedPair("cpi-jun-2026-over-0p3",
               VenueLeg("KXCPIYOY-26JUN-T0.3", "June CPI MoM > 0.3%", "BLS", far, "NO if exactly 0.3%", False),
               VenueLeg("cpi-june-2026-above-0-3", "June CPI MoM > 0.3%", "BLS", far, "NO if exactly 0.3%", False),
               "ajswag", "2026-06-28", True),
    MappedPair("fed-jul-2026-cut",
               VenueLeg("KXFED-26JUL-C25", "Fed cuts 25bps in July", "FOMC", far, "n/a", False),
               VenueLeg("fed-july-2026-cut-25", "Fed cuts 25bps in July", "FOMC", far, "n/a", False),
               "ajswag", "2026-06-28", True),
]
det = Detector(cfg, SimpleNamespace(get_fee_model=lambda mid: KalshiFeeModel(0.07, 0.0)),
               SimpleNamespace(get_fee_model=lambda mid: PolyUSFeeModel()))
det.pairs = pairs
for p in pairs:
    det.store.upsert_pair(p)

print(f"feeding synthetic books into {args.db}  (Ctrl-C to stop)")
start = time.time()
i = 0
try:
    while True:
        now = time.time()
        # Pair 1 oscillates in/out of an arb every ~8s.
        if int(now) % 8 < 4:
            k1 = VenueBook(0.38, 0.40, 0.60, 0.62, 90, 100, 100, 90, now)
            p1 = VenueBook(0.45, 0.47, 0.53, 0.55, 80, 70, 70, 80, now)
        else:
            k1 = VenueBook(0.49, 0.51, 0.49, 0.51, 120, 120, 120, 120, now)
            p1 = VenueBook(0.49, 0.51, 0.49, 0.51, 120, 120, 120, 120, now)
        det.books.update("cpi-jun-2026-over-0p3", "kalshi", k1)
        det.books.update("cpi-jun-2026-over-0p3", "polyus", p1)
        det.store.record_quote("cpi-jun-2026-over-0p3", "kalshi", k1, False)
        det.store.record_quote("cpi-jun-2026-over-0p3", "polyus", p1, False)

        # Pair 2 stays tight (no arb) — shows a healthy, fresh, no-edge pair.
        k2 = VenueBook(0.71, 0.73, 0.27, 0.29, 200, 200, 200, 200, now)
        p2 = VenueBook(0.71, 0.73, 0.27, 0.29, 150, 150, 150, 150, now)
        det.books.update("fed-jul-2026-cut", "kalshi", k2)
        det.books.update("fed-jul-2026-cut", "polyus", p2)
        det.store.record_quote("fed-jul-2026-cut", "kalshi", k2, False)
        det.store.record_quote("fed-jul-2026-cut", "polyus", p2, False)

        det.evaluate_all(now)
        i += 1
        if args.seconds and (now - start) >= args.seconds:
            break
        time.sleep(0.8)
except KeyboardInterrupt:
    pass
print(f"stopped after {i} ticks")
