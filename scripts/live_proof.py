"""1-contract live proof. Confirms order placement + fills actually work on each
venue with the smallest possible real money (~$0.50 max exposure/leg). Round-trips
1 contract: buy 1 (IOC, marketable) then sell 1 (IOC) to flatten.

DRY RUN by default (prints the plan, sends nothing). Add --go to place real orders.

  ./.venv/bin/python scripts/live_proof.py            # show plan, no orders
  ./.venv/bin/python scripts/live_proof.py --go       # place real 1-contract orders
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import load_config
from src.venues.kalshi import KalshiClient
from src.venues.polyus import PolyUSClient

ap = argparse.ArgumentParser()
ap.add_argument("--go", action="store_true", help="ACTUALLY place real orders")
ap.add_argument("--venue", choices=["kalshi", "polyus", "both"], default="both")
args = ap.parse_args()
cfg = load_config()
kc, pc = KalshiClient(cfg), PolyUSClient(cfg)
MODE = "LIVE — REAL ORDERS" if args.go else "DRY RUN — no orders sent"
print("=== 1-contract live proof  [%s] ===\n" % MODE)


def pick_kalshi():
    ms = kc._get("/markets", {"series_ticker": "KXMLBGAME", "status": "open", "limit": 200}).get("markets", [])
    for m in ms:
        ya, yb = m.get("yes_ask_dollars"), m.get("yes_bid_dollars")
        if ya and yb and 0.35 <= float(ya) <= 0.65 and float(m.get("yes_ask_size_fp") or 0) >= 5 and float(m.get("yes_bid_size_fp") or 0) >= 5:
            return m
    return None


def pick_polyus():
    ml, off = [], 0
    while off < 8000 and len(ml) < 400:
        b = pc._get("/markets", {"active": "true", "closed": "false", "categories": "sports",
                                 "limit": 500, "offset": off}).get("markets", [])
        if not b:
            break
        ml += [m for m in b if m.get("sportsMarketTypeV2") == "SPORTS_MARKET_TYPE_MONEYLINE"]
        off += 500
    for m in ml:
        try:
            bbo = pc._get("/markets/%s/bbo" % m["slug"]).get("marketData", {})
            bb = float((bbo.get("bestBid") or {}).get("value") or 0)
            ba = float((bbo.get("bestAsk") or {}).get("value") or 0)
            if 0.35 <= ba <= 0.65 and bb > 0 and (ba - bb) <= 0.08:
                return m, bb, ba
        except Exception:
            continue
    return None, 0, 0


if args.venue in ("kalshi", "both"):
    print("--- KALSHI ---  balance $%.2f" % kc.get_balance())
    m = pick_kalshi()
    if not m:
        print("  no suitable liquid market found right now (try again later).")
    else:
        ya, yb = float(m["yes_ask_dollars"]), float(m["yes_bid_dollars"])
        print("  market: %s (%s)" % (m["ticker"], m.get("yes_sub_title")))
        print("  plan: BUY 1 YES @ %.2f (~$%.2f), then SELL 1 YES @ %.2f to flatten" % (ya, ya, yb))
        print("  est round-trip cost (spread+fees): ~$%.2f" % (ya - yb + 0.04))
        if args.go:
            print("  >> placing BUY ...");  b = kc.place_order(m["ticker"], "bid", 1, ya); print("     resp:", b)
            print("  >> placing SELL (flatten) ..."); s = kc.place_order(m["ticker"], "ask", 1, yb); print("     resp:", s)
            print("  balance after: $%.2f" % kc.get_balance())
    print()

if args.venue in ("polyus", "both"):
    print("--- POLYMARKET US ---  buying power $%.2f" % pc.get_balance())
    m, bb, ba = pick_polyus()
    if not m:
        print("  no suitable liquid moneyline found right now (try again later).")
    else:
        print("  market: %s (%s)" % (m["slug"], m.get("question")))
        print("  plan: BUY 1 YES @ %.2f (~$%.2f), then SELL 1 YES @ %.2f to flatten" % (ba, ba, bb))
        print("  est round-trip cost (spread+fees): ~$%.2f" % (ba - bb + 0.03))
        if args.go:
            print("  >> placing BUY ..."); b = pc.place_order(m["slug"], "OUTCOME_SIDE_YES", "ORDER_ACTION_BUY", 1, ba); print("     resp:", b)
            print("  >> placing SELL (flatten) ..."); s = pc.place_order(m["slug"], "OUTCOME_SIDE_YES", "ORDER_ACTION_SELL", 1, bb); print("     resp:", s)
            print("  buying power after: $%.2f" % pc.get_balance())
    print()

if not args.go:
    print("This was a DRY RUN. Re-run with --go to place the real 1-contract orders.")
