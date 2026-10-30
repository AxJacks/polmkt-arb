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
  • Reject >max-edge (artifacts) + thin books. Up to --max-per-match trades per
    match with a --rearm-cooldown between them; a match is retired after
    --max-misses-per-match missed hedges (misses cost real money, edges don't).
  • POLY leg first (reversed 2026-07-12 after 19/19 Kalshi-first misses showed
    Poly always repriced before leg 2). Both legs are breakeven-derived IOC
    limits — any price still locking --min-residual-edge fills — and the hedge
    lands on Kalshi's deep book (observed unwinds fill at exactly -1c).
  • Fill status is read from order responses (positions polling is fallback
    only); a missed Poly leg triggers a VERIFIED unwind that escalates until
    flat, and per-stage latency is logged to --timing-file (JSONL).
  • Stops on: realized LOSS == --max-daily-loss; --max-capital deployed; HALT file
    (Telegram /halt creates it). 1 contract/trade. P&L from real balance deltas.

  python -u scripts/live_arb.py                # DRY RUN
  python -u scripts/live_arb.py --test-alert   # sample alerts
  python -u scripts/live_arb.py --go           # LIVE
"""
import argparse
import asyncio
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import load_config
from src.fees import KalshiFeeModel, PolyUSFeeModel
from src.hedging import kalshi_fill, max_poly_price, poly_fill
from src.notify import Notifier
from src.scanner import _names_match, _ntok, _parse_kalshi_game_dt, _parse_iso
from src.venues.kalshi import KalshiClient
from src.venues.polyus import PolyUSClient

ap = argparse.ArgumentParser()
ap.add_argument("--go", action="store_true")
ap.add_argument("--test-alert", action="store_true")
ap.add_argument("--leagues", default="atp,wta,mlb")
ap.add_argument("--min-edge", type=float, default=0.02)
ap.add_argument("--max-edge", type=float, default=0.10)
ap.add_argument("--max-spread", type=float, default=0.05,
                help="reject if Kalshi bid/ask spread > this (caps worst-case miss-unwind loss)")
ap.add_argument("--min-size", type=float, default=10)
ap.add_argument("--max-capital", type=float, default=20.0)
ap.add_argument("--max-daily-loss", type=float, default=20.0)
ap.add_argument("--poll", type=float, default=1.0)
ap.add_argument("--leg2-timeout", type=float, default=1.5,
                help="fallback positions-poll window if the Poly order response is inconclusive")
ap.add_argument("--min-price", type=float, default=0.10,
                help="skip edges with any leg priced below this (settlement-race guard)")
ap.add_argument("--max-price", type=float, default=0.90,
                help="skip edges with any leg priced above this (settlement-race guard)")
ap.add_argument("--min-residual-edge", type=float, default=0.005,
                help="leg-2 IOC limit keeps at least this much edge after fees")
ap.add_argument("--timing-file", default="./exec_timing.jsonl",
                help="JSONL per-stage latency/outcome record for every attempt")
ap.add_argument("--max-per-match", type=int, default=3,
                help="max completed hedges per match (re-arms after cooldown)")
ap.add_argument("--max-misses-per-match", type=int, default=2,
                help="retire a match after this many missed hedges")
ap.add_argument("--rearm-cooldown", type=float, default=60.0,
                help="seconds before the same match may trade again")
ap.add_argument("--rediscover", type=float, default=1800, help="refresh the slate every N seconds")
args = ap.parse_args()

cfg = load_config()
kc, pc = KalshiClient(cfg), PolyUSClient(cfg)
notify = Notifier.from_config(cfg)
HALT = Path(cfg.kill_switch_file)
SERIES = {"atp": "KXATPMATCH", "wta": "KXWTAMATCH", "mlb": "KXMLBGAME"}
PFEE = PolyUSFeeModel()
LEAGUES = [x.strip() for x in args.leagues.split(",") if x.strip()]

locked = realized = deployed = 0.0
state = {}   # per-slug trade/miss counters + re-arm timestamp


def slot(slug):
    return state.setdefault(slug, {"trades": 0, "misses": 0, "next_ok": 0.0})


def eligible(mt):
    s = slot(mt["slug"])
    return (time.time() >= s["next_ok"]
            and s["trades"] < args.max_per_match
            and s["misses"] < args.max_misses_per_match)

# Persisted balance floor: survives restarts so the $20-loss cap can't reset.
# Records starting total balance once; halts if real balance ever falls that far.
BASE = Path("secrets/.arb_baseline")
_last_floor_check = [0.0]


_bal_pool = ThreadPoolExecutor(max_workers=2)


def _total_balance():
    """Both venues' balances fetched CONCURRENTLY — one round trip, not two.
    This sits in front of leg 1, so its latency delays every order."""
    k, p = _bal_pool.submit(kc.get_balance), _bal_pool.submit(pc.get_balance)
    return k.result() + p.result()


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
        time.sleep(0.1)   # pace the burst — 17 back-to-back pages invites a 429
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
            a = (yes_s.get("team") or {}).get("name") or yes_s.get("description")  # Poly YES side
            b = (no_s.get("team") or {}).get("name") or no_s.get("description")    # other side
            if not (a and b):
                continue
            pstart = _parse_iso(pm.get("gameStartTime"))
            # NEAREST game within the window, not first match: MLB doubleheaders
            # put the same team-pair on the slate twice in one day.
            best = None
            for pref, mk in games.items():
                if len(mk) != 2:
                    continue
                dt, has_time = _parse_kalshi_game_dt(pref)
                if not (dt and pstart):
                    continue
                delta = abs((dt - pstart).total_seconds())
                if delta > (6 if has_time else 36) * 3600:
                    continue
                m1, m2 = mk
                s1, s2 = m1.get("yes_sub_title") or "", m2.get("yes_sub_title") or ""
                # Best BIJECTIVE assignment of {A,B} onto the two markets — plain
                # first-match confuses "Los Angeles D" with "Los Angeles A".
                if _tokscore(s1, a) + _tokscore(s2, b) >= _tokscore(s1, b) + _tokscore(s2, a):
                    sa, sb, k_b = s1, s2, m2
                else:
                    sa, sb, k_b = s2, s1, m1
                if not (_names_match(a, sa) and _names_match(b, sb)):
                    continue
                if best is None or delta < best[0]:
                    best = (delta, k_b)
            if best:
                matches.append({"slug": pm["slug"], "A": a, "B": b, "kB": best[1]["ticker"],
                                "start": pstart.timestamp()})
    # SAFETY: if two Poly markets map to the SAME Kalshi game (e.g. a rain-
    # postponed game re-listed next day), we cannot know which is the real
    # counterpart — drop ALL of them rather than hedge across different games.
    from collections import Counter
    kb_counts = Counter(m["kB"] for m in matches)
    dropped = [m for m in matches if kb_counts[m["kB"]] > 1]
    for m in dropped:
        print("  ambiguous mapping dropped: %s -> %s" % (m["slug"], m["kB"]))
    return [m for m in matches if kb_counts[m["kB"]] == 1]


def _tokscore(x, y):
    """Token-set Jaccard in [0,1] — a graded version of _names_match."""
    tx, ty = _ntok(x), _ntok(y)
    if not tx or not ty:
        return 0.0
    return len(tx & ty) / len(tx | ty)


def poly_yes(slug):
    md = pc._get("/markets/%s/bbo" % slug).get("marketData", {})
    ba = (md.get("bestAsk") or {}).get("value"); bb = (md.get("bestBid") or {}).get("value")
    return (float(ba) if ba else None, float(bb) if bb else None, float(md.get("askDepth") or 0))


def _pace_429(e):
    """Global cooldown on a Poly 429: firing the next request straight into an
    active rate-limit just extends it. Returns True if we cooled off."""
    if "429" in repr(e):
        print("   poly rate-limited — cooling off 5s")
        time.sleep(5)
        return True
    return False


class PolyBooks:
    """Poly US quotes via ONE WebSocket instead of per-match REST polling —
    the REST quote loop (25 req/s) drew 429 rate-limits within hours of going
    autonomous. stream_books reconnects on its own; we REST-prefill once per
    slate (gently paced) because quiet books only push on change."""

    def __init__(self, client):
        self.client = client
        self.books = {}
        self._loop = None
        self._thread = None

    def _on_book(self, slug, book):
        self.books[slug] = book

    def watch(self, slugs):
        self.stop()
        self.books = {}
        loop = asyncio.new_event_loop()
        self._loop = loop

        def run():
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self.client.stream_books(list(slugs), self._on_book))
            except Exception:
                pass

        self._thread = threading.Thread(target=run, daemon=True, name="poly-ws")
        self._thread.start()
        for s in slugs:   # prefill so books exist before the first WS push
            try:
                ask, bid, depth = poly_yes(s)
                if ask and s not in self.books:
                    self.books[s] = SimpleNamespace(
                        yes_ask=ask, yes_bid=(bid or 0.0), yes_ask_size=depth,
                        yes_bid_size=0.0, ts=time.time())
            except Exception as e:
                _pace_429(e)
            time.sleep(0.15)

    def stop(self):
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)


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


def poly_net(slug):
    """Signed net contracts held for slug (0 if none). This is the ONLY honest
    fill oracle: Poly reports executions ASYNC, so a filled IOC can return an
    empty executions array (which minted a naked CLE leg on 2026-07-12)."""
    pos = pc._get("/portfolio/positions").get("positions", {})
    for s, p in pos.items():
        if slug in str(s):
            try:
                return float(p.get("netPosition") or 0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def record_timing(rec):
    """Append one JSONL record per attempt (per-stage ms + outcome) for analysis."""
    rec["ts"] = time.time()
    try:
        with open(args.timing_file, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError as e:
        print("   (timing write failed: %s)" % e)


def unwind_kalshi(mt):
    """Sell back a naked Kalshi leg and VERIFY the fill. Re-fetches the bid each
    attempt, stepping 1c more aggressive; if still unfilled after 4 tries:
    alert + HALT — a naked leg must never linger silently."""
    for i in range(4):
        try:
            _, bid, _ = kalshi_q(mt["kB"])
            px = max(0.01, round((bid or 0.02) - 0.01 * i, 2))
            fq, _ = kalshi_fill(kc.place_order(mt["kB"], "ask", 1, px))
            if fq >= 1:
                return True
        except Exception as e:
            print("   unwind attempt %d error: %s" % (i + 1, repr(e)[:100]))
        time.sleep(0.3)
    msg = "🚨 UNWIND FAILED — NAKED Kalshi leg on %s. HALTED — close it manually." % mt["kB"]
    print("   " + msg); notify.send(msg); HALT.touch()
    return False


def emergency_flatten(mt, k_filled, slug_held_outcome):
    """Bug guard: we ended up holding the SAME player on both venues. Close both."""
    global realized
    msg = "🚨 SAFETY: same player on both venues (not a hedge) — EMERGENCY FLATTEN + HALT."
    print("   " + msg); notify.send(msg)
    try:
        if k_filled:
            unwind_kalshi(mt)                        # verified sell-back at the BID
        if slug_held_outcome:
            bbo = pc._get("/markets/%s/bbo" % mt["slug"]).get("marketData", {})
            bid = float((bbo.get("bestBid") or {}).get("value") or 0.01)
            pc.place_order(mt["slug"], "OUTCOME_SIDE_YES", "ORDER_ACTION_SELL", 1, bid)
    except Exception as e:
        notify.send("🚨 emergency flatten error: %s — CHECK POSITIONS MANUALLY" % repr(e)[:120])
    HALT.touch()


def unwind_poly(mt):
    """Sell back a naked Poly leg. Fill verification is via NET POSITION, never
    the order response alone (executions arrive async) — retrying a sell that
    only LOOKED unfilled would double-sell into a short position."""
    for i in range(4):
        try:
            pb = poly_books.books.get(mt["slug"])
            bid = (getattr(pb, "yes_bid", 0) or 0.02)
            px = max(0.01, round(bid - 0.01 * i, 2))
            fq, fpx = poly_fill(pc.place_order(
                mt["slug"], "OUTCOME_SIDE_YES", "ORDER_ACTION_SELL", 1, px))
            if fq and fq >= 1:
                return True, (fpx if fpx else px)
            t = time.time()   # ambiguous response: let the position tell us
            while time.time() - t < 1.2:
                if poly_net(mt["slug"]) <= 0:
                    return True, px
                time.sleep(0.3)
        except Exception as e:
            print("   poly unwind attempt %d error: %s" % (i + 1, repr(e)[:100]))
        time.sleep(0.3)
    msg = "🚨 UNWIND FAILED — NAKED Poly leg on %s. HALTED — close it manually." % mt["slug"]
    print("   " + msg); notify.send(msg); HALT.touch()
    return False, None


def execute(mt, a_ask, b_ask):
    """POLY FIRST (data-driven reversal, 2026-07-12): in 19/19 Kalshi-first
    attempts the Poly ask moved away before leg 2 — we kept buying Kalshi just
    as Poly repriced. So now we take the fleeting Poly price first and hedge
    into Kalshi, whose book is deep and tight (every unwind filled at -1c).
    Both legs are breakeven-limited IOCs; per-trade P&L comes from FILL PRICES
    (balance deltas lag settlement and overstated losses 10x)."""
    global deployed, realized, locked
    s = slot(mt["slug"])
    s["next_ok"] = time.time() + args.rearm_cooldown   # cooldown on ANY outcome
    tag = "%s(Poly) + %s(Kalshi)" % (mt["A"][:12], mt["B"][:12])
    tm = {"slug": mt["slug"], "a_ask": a_ask, "b_ask": b_ask}
    kf = kfee(mt["kB"])

    # ── leg 1: Poly buy A, IOC limited to keep the residual edge ──
    max_a = max_poly_price(1.0 - b_ask - kf.taker_fee(b_ask, 1) - args.min_residual_edge, PFEE)
    tm["max_a"] = max_a
    if max_a < 0.01:
        tm["outcome"] = "no_budget"; record_timing(tm); return
    t0 = time.time()
    print("  >> POLY buy 1 %s YES, IOC limit %.2f (ask %.2f, A=%s) ..."
          % (mt["slug"], max_a, a_ask, mt["A"][:16]))
    p_qty = p_px = None
    try:
        pr = pc.place_order(mt["slug"], "OUTCOME_SIDE_YES", "ORDER_ACTION_BUY", 1, max_a)
        p_qty, p_px = poly_fill(pr)
        if p_qty is None:
            print("     poly response shape unknown — raw: %s" % json.dumps(pr)[:400])
    except Exception as e:
        print("     poly order error (%s) — checking position" % repr(e)[:100])
        tm["p_error"] = repr(e)[:120]
    tm["p_rtt_ms"] = int((time.time() - t0) * 1000)
    if p_qty is None:   # ambiguous (empty executions / lost response): verify
        tm["p_via"] = "poll"   # via NET POSITION — the response can lie
        t1 = time.time()
        p_qty = 0.0
        while time.time() - t1 < args.leg2_timeout:
            if poly_net(mt["slug"]) >= 1:
                p_qty = 1.0
                break
            time.sleep(0.25)
    if p_qty < 1:
        tm["outcome"] = "p_nofill"; record_timing(tm)
        print("     poly IOC didn't fill — aborted (no position)."); return
    p_px = p_px if p_px is not None else max_a
    p_fee = PFEE.taker_fee(p_px, 1)
    tm["p_px"] = p_px
    print("     filled @ %.3f" % p_px)

    # ── leg 2: Kalshi buy B, IOC limited to keep the residual edge ──
    max_b = max_poly_price(1.0 - p_px - p_fee - args.min_residual_edge, kf)
    tm["max_b"] = max_b
    k_qty, k_px = 0.0, None
    if max_b >= 0.01:
        t2 = time.time()
        print("  >> KALSHI buy 1 %s YES, IOC limit %.2f (ask %.2f) ..." % (mt["kB"], max_b, b_ask))
        try:
            k_qty, k_px = kalshi_fill(kc.place_order(mt["kB"], "bid", 1, max_b))
        except Exception as e:
            # Kalshi state unknown while we hold a Poly leg: do NOT guess.
            msg = ("🚨 Kalshi hedge response lost on %s (%s) while holding Poly leg — "
                   "VERIFY manually. HALTED." % (mt["kB"], repr(e)[:100]))
            print("   " + msg); notify.send(msg); HALT.touch()
            tm["outcome"] = "k_unknown"; record_timing(tm); return
        tm["k_rtt_ms"] = int((time.time() - t2) * 1000)
    tm["total_ms"] = int((time.time() - t0) * 1000)

    if k_qty >= 1:
        k_px = k_px if k_px is not None else max_b
        k_fee_paid = kf.taker_fee(k_px, 1)
        # SAFETY (post-trade): confirm we hold OPPOSITE sides.
        held, t3 = None, time.time()
        while held is None and time.time() - t3 < 5:
            held = poly_held(mt["slug"])
            if held is None:
                time.sleep(0.5)
        if held and _names_match(held, mt["B"]):    # same competitor on both venues!
            tm["outcome"] = "same_player_flatten"; record_timing(tm)
            emergency_flatten(mt, True, held); return
        s["trades"] += 1
        spent = p_px + p_fee + k_px + k_fee_paid
        deployed += spent; lk = 1.0 - spent; locked += lk
        tm["outcome"] = "hedged"; tm["k_px"] = k_px
        tm["spent"] = round(spent, 4); tm["locked"] = round(lk, 4)
        record_timing(tm)
        msg = ("✅ HEDGE PLACED — %s\ncost $%.2f → LOCKED +$%.2f (settles at match end)\nrunning: %s"
               % (tag, spent, lk, running()))
        print("     ✅ LOCKED +$%.3f (cost $%.3f) [p %sms | k %sms | total %sms]"
              % (lk, spent, tm.get("p_rtt_ms"), tm.get("k_rtt_ms"), tm["total_ms"]))
        notify.send(msg)
    else:
        ok, sell_px = unwind_poly(mt)
        s["misses"] += 1
        if ok and sell_px:
            loss = (sell_px - p_px) - p_fee - PFEE.taker_fee(sell_px, 1)
        else:
            loss = -(p_px + p_fee)   # worst case until manually resolved
        realized += loss
        tm["outcome"] = "missed"; tm["realized"] = round(loss, 4)
        tm["kalshi_ask_at_entry"] = b_ask
        record_timing(tm)
        msg = ("⚠️ MISSED HEDGE — %s\nKalshi IOC didn't fill (limit %.2f, entry ask %.2f); "
               "unwound Poly @ %.2f\nrealized $%.2f\nrunning: %s"
               % (tag, max_b, b_ask, sell_px or 0, loss, running()))
        print("     ⚠ missed; unwound poly @ %s; realized $%.3f" % (sell_px, loss))
        notify.send(msg)


START_BAL = start_balance()
print("balance floor: start $%.2f, will stop if it drops to $%.2f" % (START_BAL, START_BAL - args.max_daily_loss))
print("discovering live %s matches ..." % ",".join(LEAGUES))
matches = discover()
last_disc = time.time()
poly_books = PolyBooks(pc)
poly_books.watch([mt["slug"] for mt in matches])
print("watching %d matches (%d poly books live via WS). (Telegram /halt or `touch %s` to stop)\n"
      % (len(matches), len(poly_books.books), HALT))
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
            poly_books.watch([mt["slug"] for mt in matches])
            print("re-discovered %d matches (%d books)" % (len(matches), len(poly_books.books)))
        for mt in matches:
            if not eligible(mt):
                continue
            if mt.get("start", 0) - time.time() > 8 * 3600:
                continue   # game too far out to have a real book — skip the Kalshi call
            try:
                pb = poly_books.books.get(mt["slug"])
                if pb is None or (time.time() - pb.ts) > 900:
                    continue   # no fresh Poly book (feed quiet or match suspended)
                a_ask, a_depth = pb.yes_ask, pb.yes_ask_size
                b_ask, b_bid, b_size = kalshi_q(mt["kB"])
                time.sleep(0.06)   # ~10 Kalshi reads/s max across the slate
                if not (a_ask and b_ask and b_bid and 0 < a_ask < 1 and 0 < b_ask < 1):
                    continue
                fee = kfee(mt["kB"]).taker_fee(b_ask, 1) + PFEE.taker_fee(a_ask, 1)
                edge = 1.0 - (a_ask + b_ask + fee)
                tag = "%s vs %s" % (mt["A"][:12], mt["B"][:12])
                if edge < args.min_edge:
                    continue
                if edge > args.max_edge:
                    print("skip %4.1f%% %-24s — too big (artifact)" % (edge * 100, tag)); continue
                # Settlement-race guard: near 0/1 the "edge" is usually just the
                # slower venue's stale quote on a nearly-decided outcome, books
                # get yanked mid-point, and a missed hedge GAPS (one -$0.85 miss
                # at 0.12/0.84 taught us this). Real tradable arbs live mid-range.
                if not (args.min_price <= a_ask <= args.max_price
                        and args.min_price <= b_ask <= args.max_price):
                    print("skip %4.1f%% %-24s — price outside [%.2f,%.2f] (settlement race)"
                          % (edge * 100, tag, args.min_price, args.max_price)); continue
                if b_size < args.min_size or a_depth < 2:
                    print("skip %4.1f%% %-24s — thin book" % (edge * 100, tag)); continue
                if (b_ask - b_bid) > args.max_spread:   # wide Kalshi spread => bad hedge book
                    print("skip %4.1f%% %-24s — wide K spread %.2f (unwind risk)"
                          % (edge * 100, tag, b_ask - b_bid)); continue
                a_bid = getattr(pb, "yes_bid", 0) or 0
                if (a_ask - a_bid) > args.max_spread:   # Poly is leg 1 => ITS spread caps miss cost
                    print("skip %4.1f%% %-24s — wide P spread %.2f (unwind risk)"
                          % (edge * 100, tag, a_ask - a_bid)); continue
                print("✅ REAL EDGE %4.1f%% | %-24s | A@poly %.2f + B@kalshi %.2f (sz %.0f)"
                      % (edge * 100, tag, a_ask, b_ask, b_size))
                if args.go:
                    execute(mt, a_ask, b_ask)
                else:
                    est = max_poly_price(1.0 - b_ask - kfee(mt["kB"]).taker_fee(b_ask, 1)
                                         - args.min_residual_edge, PFEE)
                    print("  (dry — Kalshi %s @%.2f, then Poly IOC limit %.2f vs quoted ask %.2f)"
                          % (mt["B"][:12], b_ask, est, a_ask))
            except Exception as e:
                if not _pace_429(e):
                    print("   loop error on %s: %s" % (mt.get("slug", "?"), repr(e)[:120]))
        time.sleep(args.poll)
except KeyboardInterrupt:
    print("\nstopped. %s" % running())
    if args.go:
        notify.send("⏹ Live arb test stopped.\nfinal: %s" % running())
