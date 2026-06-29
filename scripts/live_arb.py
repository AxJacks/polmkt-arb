"""Supervised LIVE cross-venue tennis arb runner ($20) with Telegram alerts.

CORRECT side mapping (the bug fix): Poly's OUTCOME_SIDE_YES buys the marketSide
with long==True (NOT outcomes[0]). So:
  • Poly YES = the long-side player (call them A). We BUY A on Poly (YES).
  • We BUY the OTHER player (B) on Kalshi (B's own market, YES) — the hedge.
Both legs are simple "buy YES" orders (proven safe). Opposite players => real hedge.

Safety:
  • Runtime hedge check: after both legs fill, confirm Poly holds A and Kalshi
    holds B (DIFFERENT players). If it ever holds the SAME player on both
    (any residual mapping error), EMERGENCY FLATTEN both + HALT.
  • Reject >max-edge (artifacts) + thin books; one trade per match.
  • Kalshi leg first (sync), then Poly; unwind Kalshi if Poly misses.
  • Stops on: realized LOSS == --max-daily-loss; --max-capital deployed; HALT file
    (Telegram /halt creates it). 1 contract/trade. P&L from real balance deltas.

  python -u scripts/live_arb.py                # DRY RUN
  python -u scripts/live_arb.py --test-alert   # sample alerts
  python -u scripts/live_arb.py --go           # LIVE
"""
import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import load_config
from src.fees import KalshiFeeModel, PolyUSFeeModel
from src.notify import Notifier
from src.scanner import _names_match, _parse_kalshi_game_dt, _parse_iso
from src.venues.kalshi import KalshiClient
from src.venues.polyus import PolyUSClient

ap = argparse.ArgumentParser()
ap.add_argument("--go", action="store_true")
ap.add_argument("--test-alert", action="store_true")
ap.add_argument("--leagues", default="atp,wta")
ap.add_argument("--min-edge", type=float, default=0.02)
ap.add_argument("--max-edge", type=float, default=0.10)
ap.add_argument("--max-spread", type=float, default=0.05,
                help="reject if Kalshi bid/ask spread > this (caps worst-case miss-unwind loss)")
ap.add_argument("--min-size", type=float, default=10)
ap.add_argument("--max-capital", type=float, default=20.0)
ap.add_argument("--max-daily-loss", type=float, default=20.0)
ap.add_argument("--poll", type=float, default=1.0)
ap.add_argument("--leg2-timeout", type=float, default=3.0)
ap.add_argument("--rediscover", type=float, default=1800, help="refresh the slate every N seconds")
args = ap.parse_args()

cfg = load_config()
kc, pc = KalshiClient(cfg), PolyUSClient(cfg)
notify = Notifier.from_config(cfg)
HALT = Path(cfg.kill_switch_file)
SERIES = {"atp": "KXATPMATCH", "wta": "KXWTAMATCH"}
PFEE = PolyUSFeeModel()
LEAGUES = [x.strip() for x in args.leagues.split(",") if x.strip()]

locked = realized = deployed = 0.0
done = set()

# Persisted balance floor: survives restarts so the $20-loss cap can't reset.
# Records starting total balance once; halts if real balance ever falls that far.
BASE = Path("secrets/.arb_baseline")
_last_floor_check = [0.0]


def _total_balance():
    return kc.get_balance() + pc.get_balance()


def start_balance():
    if BASE.exists():
        try:
            return float(BASE.read_text().strip())
        except Exception:
            pass
    sb = _total_balance()
    BASE.write_text("%.4f" % sb)
    return sb


def running():
    return ("locked +$%.2f (pending settlement) | realized $%.2f | deployed $%.2f/$%.0f"
            % (locked, realized, deployed, args.max_capital))


if args.test_alert:
    notify.send("▶️ SAMPLE: Live arb test ARMED — ATP/WTA, $20, 1 contract, stops only if $20 LOST.")
    notify.send("✅ SAMPLE HEDGE — Nakashima(Poly) + Jones(Kalshi)\ncost $0.95 → LOCKED +$0.05\nrunning: ...")
    notify.send("⚠️ SAMPLE MISSED — Poly leg didn't fill; unwound Kalshi. realized -$0.04")
    notify.send("🛑 SAMPLE STOP — daily loss limit hit ($20 lost)")
    print("sample alerts sent."); sys.exit(0)

MODE = "LIVE — REAL ORDERS" if args.go else "DRY RUN"
print("=== live tennis arb runner [%s]  caps: 1 contract, $%.0f deployed, $%.0f loss ===\n"
      % (MODE, args.max_capital, args.max_daily_loss))


def discover():
    matches, poly, off = [], [], 0
    while off < 8000:
        b = pc._get("/markets", {"active": "true", "closed": "false", "categories": "sports",
                                 "limit": 500, "offset": off}).get("markets", [])
        if not b:
            break
        poly += b
        off += 500
    for lg in LEAGUES:
        pm_list = [m for m in poly if (m.get("slug") or "").startswith("aec-%s-" % lg)
                   and m.get("sportsMarketTypeV2") == "SPORTS_MARKET_TYPE_MONEYLINE"]
        km = kc._get("/markets", {"series_ticker": SERIES[lg], "status": "open", "limit": 1000}).get("markets", [])
        games = {}
        for m in km:
            games.setdefault(m["ticker"].rsplit("-", 1)[0], []).append(m)
        for pm in pm_list:
            sides = pm.get("marketSides") or []
            yes_s = next((s for s in sides if s.get("long") is True), None)
            no_s = next((s for s in sides if s.get("long") is False), None)
            if not (yes_s and no_s):
                continue
            a = (yes_s.get("team") or {}).get("name") or yes_s.get("description")  # Poly YES player
            b = (no_s.get("team") or {}).get("name") or no_s.get("description")    # other player
            if not (a and b):
                continue
            pstart = _parse_iso(pm.get("gameStartTime"))
            for pref, mk in games.items():
                if len(mk) != 2:
                    continue
                dt, _ = _parse_kalshi_game_dt(pref)
                if not (dt and pstart and abs((dt - pstart).total_seconds()) <= 36 * 3600):
                    continue
                k_b = next((m for m in mk if _names_match(b, m.get("yes_sub_title") or "")), None)
                k_a = next((m for m in mk if m is not k_b and _names_match(a, m.get("yes_sub_title") or "")), None)
                if k_a and k_b:   # both players found on Kalshi
                    matches.append({"slug": pm["slug"], "A": a, "B": b, "kB": k_b["ticker"]})
                    break
    return matches


def poly_yes(slug):
    md = pc._get("/markets/%s/bbo" % slug).get("marketData", {})
    ba = (md.get("bestAsk") or {}).get("value"); bb = (md.get("bestBid") or {}).get("value")
    return (float(ba) if ba else None, float(bb) if bb else None, float(md.get("askDepth") or 0))


def kalshi_q(ticker):
    m = kc._get("/markets/%s" % ticker).get("market", {})
    ya, yb = m.get("yes_ask_dollars"), m.get("yes_bid_dollars")
    return (float(ya) if ya else None, float(yb) if yb else None, float(m.get("yes_ask_size_fp") or 0))


def kfee(t):
    try:
        return kc.get_fee_model(t)
    except Exception:
        return KalshiFeeModel(0.07, 0.0175)


def halted():
    if HALT.exists():
        return "HALT file present (Telegram /halt or manual)"
    if realized <= -abs(args.max_daily_loss):
        return "daily LOSS limit hit (lost $%.2f)" % -realized
    # Restart-proof real-money floor: actual balance fell >= cap below start.
    now = time.time()
    if now - _last_floor_check[0] > 25:
        _last_floor_check[0] = now
        try:
            drop = START_BAL - _total_balance()
            if drop >= abs(args.max_daily_loss):
                return "balance floor hit — down $%.2f from start (real loss)" % drop
        except Exception:
            pass
    if deployed >= args.max_capital:
        return "capital fully deployed ($%.2f) — out of money, not a loss" % deployed
    return None


def poly_held(slug):
    pos = pc._get("/portfolio/positions").get("positions", {})
    for s, p in pos.items():
        if slug in str(s):
            return p.get("marketMetadata", {}).get("outcome")
    return None


def emergency_flatten(mt, k_filled, slug_held_outcome):
    """Bug guard: we ended up holding the SAME player on both venues. Close both."""
    global realized
    msg = "🚨 SAFETY: same player on both venues (not a hedge) — EMERGENCY FLATTEN + HALT."
    print("   " + msg); notify.send(msg)
    try:
        kb, _, _ = kalshi_q(mt["kB"])
        if k_filled:
            kc.place_order(mt["kB"], "ask", 1, kb)   # sell back the Kalshi leg
        if slug_held_outcome:
            bbo = pc._get("/markets/%s/bbo" % mt["slug"]).get("marketData", {})
            bid = float((bbo.get("bestBid") or {}).get("value") or 0.01)
            pc.place_order(mt["slug"], "OUTCOME_SIDE_YES", "ORDER_ACTION_SELL", 1, bid)
    except Exception as e:
        notify.send("🚨 emergency flatten error: %s — CHECK POSITIONS MANUALLY" % repr(e)[:120])
    HALT.touch()


def execute(mt, a_ask, b_ask, b_bid):
    """Buy A on Poly (YES) + B on Kalshi (kB). Opposite players => hedge."""
    global deployed, realized, locked
    done.add(mt["slug"])
    tot0 = kc.get_balance() + pc.get_balance()
    print("  >> KALSHI buy 1 %s YES @ %.2f (B=%s) ..." % (mt["kB"], b_ask, mt["B"][:16]))
    kr = kc.place_order(mt["kB"], "bid", 1, b_ask)
    if float(kr.get("fill_count", "0") or 0) < 1:
        print("     kalshi IOC didn't fill — aborted."); return
    print("     filled @ %.3f" % float(kr.get("average_fill_price", b_ask)))
    print("  >> POLY buy 1 %s YES @ %.2f (A=%s) ..." % (mt["slug"], a_ask, mt["A"][:16]))
    pc.place_order(mt["slug"], "OUTCOME_SIDE_YES", "ORDER_ACTION_BUY", 1, a_ask)
    t0, held = time.time(), None
    while time.time() - t0 < args.leg2_timeout:
        held = poly_held(mt["slug"])
        if held:
            break
        time.sleep(0.4)
    tag = "%s(Poly) + %s(Kalshi)" % (mt["A"][:12], mt["B"][:12])
    if held:
        # SAFETY: Poly must hold A and Kalshi holds B — different players.
        if _names_match(held, mt["B"]):     # same player on both venues!
            emergency_flatten(mt, True, held); return
        spent = tot0 - (kc.get_balance() + pc.get_balance())
        deployed += spent; lk = 1.0 - spent; locked += lk
        msg = ("✅ HEDGE PLACED — %s\ncost $%.2f → LOCKED +$%.2f (settles at match end)\nrunning: %s"
               % (tag, spent, lk, running()))
        print("     ✅ LOCKED +$%.3f (cost $%.3f)" % (lk, spent)); notify.send(msg)
    else:
        kc.place_order(mt["kB"], "ask", 1, b_bid)   # unwind naked Kalshi leg
        loss = (kc.get_balance() + pc.get_balance()) - tot0
        realized += loss
        msg = ("⚠️ MISSED HEDGE — %s\nPoly leg didn't fill in %.0fs; unwound Kalshi\nrealized $%.2f\nrunning: %s"
               % (tag, args.leg2_timeout, loss, running()))
        print("     ⚠ unwound; realized $%.3f" % loss); notify.send(msg)


START_BAL = start_balance()
print("balance floor: start $%.2f, will stop if it drops to $%.2f" % (START_BAL, START_BAL - args.max_daily_loss))
print("discovering live %s matches ..." % ",".join(LEAGUES))
matches = discover()
last_disc = time.time()
print("watching %d matches. (Telegram /halt or `touch %s` to stop)\n" % (len(matches), HALT))
if args.go:
    notify.send("▶️ LIVE arb test ARMED — %s, %d matches, max $%.0f, 1 contract. Stops only if $%.0f LOST. "
                "Kill: send /halt. (locked profit settles at match end)"
                % (",".join(LEAGUES).upper(), len(matches), args.max_capital, args.max_daily_loss))
try:
    while True:
        h = halted()
        if h:
            print("HALTED: %s | %s" % (h, running()))
            if args.go:
                notify.send("🛑 STOPPED — %s\nfinal: %s" % (h, running()))
            break
        if time.time() - last_disc > args.rediscover:
            matches = discover(); last_disc = time.time()
            print("re-discovered %d matches" % len(matches))
        for mt in matches:
            if mt["slug"] in done:
                continue
            try:
                a_ask, a_bid, a_depth = poly_yes(mt["slug"])
                b_ask, b_bid, b_size = kalshi_q(mt["kB"])
                if not (a_ask and b_ask and b_bid and 0 < a_ask < 1 and 0 < b_ask < 1):
                    continue
                fee = kfee(mt["kB"]).taker_fee(b_ask, 1) + PFEE.taker_fee(a_ask, 1)
                edge = 1.0 - (a_ask + b_ask + fee)
                tag = "%s vs %s" % (mt["A"][:12], mt["B"][:12])
                if edge < args.min_edge:
                    continue
                if edge > args.max_edge:
                    print("skip %4.1f%% %-24s — too big (artifact)" % (edge * 100, tag)); continue
                if b_size < args.min_size or a_depth < 2:
                    print("skip %4.1f%% %-24s — thin book" % (edge * 100, tag)); continue
                if (b_ask - b_bid) > args.max_spread:   # wide Kalshi spread => big unwind loss on a miss
                    print("skip %4.1f%% %-24s — wide spread %.2f (unwind risk)"
                          % (edge * 100, tag, b_ask - b_bid)); continue
                print("✅ REAL EDGE %4.1f%% | %-24s | A@poly %.2f + B@kalshi %.2f (sz %.0f)"
                      % (edge * 100, tag, a_ask, b_ask, b_size))
                if args.go:
                    execute(mt, a_ask, b_ask, b_bid)
                else:
                    print("  (dry — would buy %s on Poly YES + %s on Kalshi)" % (mt["A"][:12], mt["B"][:12]))
            except Exception:
                pass
        time.sleep(args.poll)
except KeyboardInterrupt:
    print("\nstopped. %s" % running())
    if args.go:
        notify.send("⏹ Live arb test stopped.\nfinal: %s" % running())
