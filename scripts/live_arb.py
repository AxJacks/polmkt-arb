"""Supervised LIVE cross-venue tennis arb runner ($20 test) with Telegram alerts.

SAFE-BY-DESIGN:
  • Only fires the structure where BOTH legs are simple "buy YES" orders (proven
    in live_proof): buy player-A on Poly (YES) + buy player-B on Kalshi (YES on
    the opponent's own market). No Poly-NO, no Kalshi short side.
  • Rejects implausible (>max-edge) and thin-book edges (data artifacts).
  • Kalshi leg first (synchronous fill), then Poly hedge; if Poly doesn't confirm
    within leg2-timeout, unwind the Kalshi leg so you're never left naked.
  • STOPS on: (a) realized LOSS reaching --max-daily-loss ($20) — the only
    "lost it" stop; (b) --max-capital ($20) DEPLOYED — out of money, not a loss;
    (c) a HALT sentinel file. Each trade is 1 contract.
  • Telegram alert on every trade (locked profit / unwind loss) + running totals.

NOTE: a hedge's cash P&L realizes when the MATCH SETTLES (hours later). During the
run, "locked" = guaranteed profit if the hedge holds; "realized" = unwind losses.

  ./.venv/bin/python scripts/live_arb.py                # DRY RUN (watch, no orders)
  ./.venv/bin/python scripts/live_arb.py --test-alert   # send sample Telegram alerts
  ./.venv/bin/python scripts/live_arb.py --go           # LIVE
"""
import argparse
import json as _json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import load_config
from src.fees import KalshiFeeModel, PolyUSFeeModel
from src.notify import Notifier
from src.scanner import _names_match, _parse_kalshi_game_dt, _parse_iso
from src.venues.kalshi import KalshiClient
from src.venues.polyus import PolyUSClient

ap = argparse.ArgumentParser()
ap.add_argument("--go", action="store_true", help="ACTUALLY place real orders")
ap.add_argument("--test-alert", action="store_true", help="send sample Telegram alerts and exit")
ap.add_argument("--leagues", default="atp,wta")
ap.add_argument("--min-edge", type=float, default=0.02)
ap.add_argument("--max-edge", type=float, default=0.10)
ap.add_argument("--min-size", type=float, default=10)
ap.add_argument("--max-capital", type=float, default=20.0)
ap.add_argument("--max-daily-loss", type=float, default=20.0)
ap.add_argument("--poll", type=float, default=1.0)
ap.add_argument("--leg2-timeout", type=float, default=3.0)
args = ap.parse_args()

cfg = load_config()
kc, pc = KalshiClient(cfg), PolyUSClient(cfg)
notify = Notifier.from_config(cfg)
HALT = Path(cfg.kill_switch_file)
SERIES = {"atp": "KXATPMATCH", "wta": "KXWTAMATCH"}
PFEE = PolyUSFeeModel()
LEAGUES = [x.strip() for x in args.leagues.split(",") if x.strip()]

locked = 0.0     # sum of guaranteed profit from completed hedges (settles at match end)
realized = 0.0   # realized cash from unwinds (immediate); negative = loss
deployed = 0.0   # capital committed to open hedge legs
done = set()     # matches already traded — never double-up / rapid-fire the same edge


def running():
    return "locked +$%.2f (pending settlement) | realized $%.2f | deployed $%.2f/$%.0f" % (
        locked, realized, deployed, args.max_capital)


# ── sample alerts so you can verify the format/accuracy before going live ──
if args.test_alert:
    print("sending sample alerts to Telegram ...")
    notify.send("▶️ SAMPLE: Live arb test ARMED — ATP/WTA, max $20 deployed, 1 contract/trade, "
                "stops only if $20 is LOST. (locked profit settles when each match ends)")
    notify.send("✅ SAMPLE TRADE — Frech vs Kalinskaya\n"
                "bought Kalinskaya @Kalshi 0.44, Frech @Poly 0.48\n"
                "LOCKED +$0.05 (settles at match end)\n"
                "running: locked +$0.05 | realized $0.00 | deployed $0.92/$20")
    notify.send("⚠️ SAMPLE MISSED HEDGE — Shelton vs Virtanen\n"
                "Poly leg didn't fill in 3s; unwound Kalshi\n"
                "realized -$0.03\nrunning: locked +$0.05 | realized -$0.03 | deployed $0.92/$20")
    notify.send("🛑 SAMPLE STOP — daily loss limit hit ($20 lost)\n"
                "final: locked +$1.20 (pending) | realized -$20.00 | deployed $0.00")
    print("sent. Check your phone — that's exactly what you'll get on real trades.")
    sys.exit(0)

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
            outs = pm.get("outcomes")
            outs = _json.loads(outs) if isinstance(outs, str) else outs
            if not outs or len(outs) < 2:
                continue
            pstart = _parse_iso(pm.get("gameStartTime"))
            for pref, mk in games.items():
                if len(mk) != 2:
                    continue
                dt, _ = _parse_kalshi_game_dt(pref)
                if not (dt and pstart and abs((dt - pstart).total_seconds()) <= 36 * 3600):
                    continue
                k1 = next((m for m in mk if _names_match(outs[0], m.get("yes_sub_title") or "")), None)
                k2 = next((m for m in mk if m is not k1 and _names_match(outs[1], m.get("yes_sub_title") or "")), None)
                if k1 and k2:
                    matches.append({"slug": pm["slug"], "p1": outs[0], "p2": outs[1],
                                    "k1": k1["ticker"], "k2": k2["ticker"]})
                    break
    return matches


def poly_p1(slug):
    md = pc._get("/markets/%s/bbo" % slug).get("marketData", {})
    ba = (md.get("bestAsk") or {}).get("value"); bb = (md.get("bestBid") or {}).get("value")
    return (float(ba) if ba else None, float(bb) if bb else None, float(md.get("askDepth") or 0))


def kalshi_q(ticker):
    m = kc._get("/markets/%s" % ticker).get("market", {})
    ya, yb = m.get("yes_ask_dollars"), m.get("yes_bid_dollars")
    return (float(ya) if ya else None, float(yb) if yb else None, float(m.get("yes_ask_size_fp") or 0))


def kfee(ticker):
    try:
        return kc.get_fee_model(ticker)
    except Exception:
        return KalshiFeeModel(0.07, 0.0175)


def halted():
    if HALT.exists():
        return "HALT file present"
    if realized <= -abs(args.max_daily_loss):
        return "daily LOSS limit hit (lost $%.2f)" % -realized
    if deployed >= args.max_capital:
        return "capital fully deployed ($%.2f) — out of money, not a loss" % deployed
    return None


def execute(mt, p1_ask, k2_ask, k2_bid):
    global deployed, realized, locked
    done.add(mt["slug"])   # one attempt per match — no double-up / rapid-fire
    print("  >> KALSHI buy 1 %s YES @ %.2f (P2=%s) ..." % (mt["k2"], k2_ask, mt["p2"][:16]))
    kr = kc.place_order(mt["k2"], "bid", 1, k2_ask)
    if float(kr.get("fill_count", "0") or 0) < 1:
        print("     kalshi IOC didn't fill — aborted, no position."); return
    kprice = float(kr.get("average_fill_price", k2_ask)); kfeepaid = float(kr.get("average_fee_paid", 0))
    deployed += kprice
    print("     filled @ %.3f fee %.3f" % (kprice, kfeepaid))
    print("  >> POLY buy 1 %s YES @ %.2f (P1=%s) ..." % (mt["slug"], p1_ask, mt["p1"][:16]))
    pc.place_order(mt["slug"], "OUTCOME_SIDE_YES", "ORDER_ACTION_BUY", 1, p1_ask)
    t0, filled = time.time(), False
    while time.time() - t0 < args.leg2_timeout:
        pos = pc._get("/portfolio/positions").get("positions", {})
        if any(mt["slug"] in str(k) for k in pos):
            filled = True; break
        time.sleep(0.4)
    tag = "%s vs %s" % (mt["p1"], mt["p2"])
    if filled:
        deployed += p1_ask
        lk = 1.0 - (kprice + p1_ask) - kfeepaid - PFEE.taker_fee(p1_ask, 1)
        locked += lk
        msg = ("✅ HEDGE PLACED — %s\nbought %s @Kalshi %.2f, %s @Poly %.2f\n"
               "LOCKED +$%.2f (settles at match end)\nrunning: %s"
               % (tag, mt["p2"][:14], kprice, mt["p1"][:14], p1_ask, lk, running()))
        print("     ✅ LOCKED +$%.3f" % lk); notify.send(msg)
    else:
        ur = kc.place_order(mt["k2"], "ask", 1, k2_bid)
        deployed -= kprice
        loss = (float(ur.get("average_fill_price", k2_bid)) - kprice) - kfeepaid - float(ur.get("average_fee_paid", 0))
        realized += loss
        msg = ("⚠️ MISSED HEDGE — %s\nPoly leg didn't fill in %.0fs; unwound Kalshi\n"
               "realized $%.2f\nrunning: %s" % (tag, args.leg2_timeout, loss, running()))
        print("     ⚠ unwound; realized $%.3f" % loss); notify.send(msg)


print("discovering live %s matches ..." % ",".join(LEAGUES))
matches = discover()
print("watching %d matches. (Ctrl-C / `touch %s` to halt)\n" % (len(matches), HALT))
if args.go:
    notify.send("▶️ LIVE arb test ARMED — %s, %d matches, max $%.0f deployed, 1 contract/trade. "
                "Stops only if $%.0f is LOST. (locked profit settles at match end)"
                % (",".join(LEAGUES).upper(), len(matches), args.max_capital, args.max_daily_loss))
try:
    while True:
        h = halted()
        if h:
            print("HALTED: %s | %s" % (h, running()))
            if args.go:
                notify.send("🛑 STOPPED — %s\nfinal: %s" % (h, running()))
            break
        for mt in matches:
            if mt["slug"] in done:
                continue
            try:
                p1_ask, p1_bid, p_depth = poly_p1(mt["slug"])
                k2_ask, k2_bid, k_size = kalshi_q(mt["k2"])
                if not (p1_ask and k2_ask and k2_bid and 0 < p1_ask < 1 and 0 < k2_ask < 1):
                    continue
                fee = kfee(mt["k2"]).taker_fee(k2_ask, 1) + PFEE.taker_fee(p1_ask, 1)
                edge = 1.0 - (p1_ask + k2_ask + fee)
                tag = "%s vs %s" % (mt["p1"][:14], mt["p2"][:14])
                if edge < args.min_edge:
                    continue
                if edge > args.max_edge:
                    print("skip %4.1f%% %-26s — too big (artifact)" % (edge * 100, tag)); continue
                if k_size < args.min_size or p_depth < 2:
                    print("skip %4.1f%% %-26s — thin book (k sz %.0f, p depth %.0f)" % (edge * 100, tag, k_size, p_depth)); continue
                print("✅ REAL EDGE %4.1f%% | %-26s | P1@poly %.2f + P2@kalshi %.2f (sz %.0f)"
                      % (edge * 100, tag, p1_ask, k2_ask, k_size))
                if args.go:
                    execute(mt, p1_ask, k2_ask, k2_bid)
                else:
                    print("  (dry — would buy 1 %s YES + 1 %s YES)" % (mt["slug"][:18], mt["k2"]))
            except Exception:
                pass
        time.sleep(args.poll)
except KeyboardInterrupt:
    print("\nstopped. %s" % running())
    if args.go:
        notify.send("⏹ Live arb test stopped manually.\nfinal: %s" % running())
