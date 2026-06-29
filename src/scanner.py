"""Cross-venue divergence scanner (DETECTION ONLY — never trades).

Kalshi's open universe is enormous (500k+ markets) and cross-venue title matching
is noisy, so we don't brute-force. Instead we route each Polymarket US family
(identified by slug prefix) to its specific Kalshi series, then align individual
contracts by normalized threshold + period (month/quarter/year). This is fast
(a handful of REST calls) and precise (no cross-family false pairs).

CRITICAL: an auto-matched edge is NOT a confirmed arbitrage — the two contracts
may resolve differently (source / exact threshold / tie-void / timing). Output
goes to candidates.yaml as UNVERIFIED. Nothing is tradable until a human verifies
it and promotes it into mappings.yaml by hand.

Adding coverage = adding a route below. Threshold ladders (CPI/jobs/GDP/unemp)
align cleanly; categorical families (elections) and sports (team+date matching)
are future routes.
"""
from __future__ import annotations

import logging
import re
import time

from .books import VenueBook
from .edge import compute_edges
from .fees import KalshiFeeModel, PolyUSFeeModel

log = logging.getLogger("arb.scanner")

# Poly slug-prefix -> Kalshi series ticker(s). Threshold-ladder families.
ROUTES: dict[str, list[str]] = {
    "cpic": ["KXCPIYOY"],     # CPI YoY
    "urc": ["KXU3"],          # unemployment rate
    "nfpc": ["KXUSNFP"],      # nonfarm payrolls / jobs added
    "gdpc": ["KXGDP"],        # GDP growth
}

_MONTHS = {m: m[:3] for m in (
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december")}
_MON_ABBR = {v for v in _MONTHS.values()}


def periods(text: str) -> set[str]:
    """Canonical period tokens: 3-letter month, qN, and 4-digit year."""
    t = text.lower()
    out: set[str] = set()
    for full, ab in _MONTHS.items():
        if full in t or ab in t:
            out.add(ab)
    out |= set(re.findall(r"q[1-4]", t))
    out |= set(re.findall(r"20\d\d", t))
    return out


def strike(text: str, anchored: bool) -> float | None:
    """Normalized threshold. 'k' => thousands, 'pt'/'.' => decimal point.
    anchored=True (poly slug): the value follows gt/gte/lt/lte/atl (skips the
    year). anchored=False (kalshi yes_sub_title): first number in the subtitle."""
    t = text.lower().replace(",", "")
    if anchored:
        m = re.search(r"(?:gt|gte|lt|lte|atl|atleast)(\d+)(?:pt(\d+))?(k)?", t)
    else:
        m = re.search(r"(\d+)(?:\.(\d+))?\s*(k)?", t)
    if not m:
        return None
    whole, frac, kilo = m.group(1), m.group(2), m.group(3)
    val = float(whole + ("." + frac if frac else ""))
    if kilo:
        val *= 1000
    if not anchored and 2000 <= val <= 2100 and not frac:  # a year, not a strike
        return None
    return val


def comparator_poly(slug: str) -> str:
    s = slug.lower()
    if "gte" in s or "atl" in s or "atleast" in s:
        return "≥"  # >=
    if "lte" in s or "atm" in s or "atmost" in s:
        return "≤"  # <=
    if "gt" in s:
        return ">"
    if "lt" in s:
        return "<"
    return "?"


def comparator_kalshi(text: str) -> str:
    t = text.lower()
    if "at least" in t or "or more" in t or "or higher" in t or "or above" in t:
        return "≥"
    if "at most" in t or "or less" in t or "or lower" in t or "or below" in t:
        return "≤"
    if "above" in t or "more than" in t or "greater than" in t or "exceed" in t:
        return ">"
    if "below" in t or "less than" in t or "under" in t or "fewer than" in t:
        return "<"
    return "?"


def _kalshi_book(m: dict) -> VenueBook | None:
    yb, ya = m.get("yes_bid_dollars"), m.get("yes_ask_dollars")
    if yb is None or ya is None:
        return None
    yb, ya = float(yb), float(ya)
    ybs = float(m.get("yes_bid_size_fp") or 0)
    yas = float(m.get("yes_ask_size_fp") or 0)
    return VenueBook(yb, ya, 1 - ya, 1 - yb, ybs, yas, yas, ybs, time.time())


def _poly_book(bbo: dict) -> VenueBook | None:
    md = bbo.get("marketData", bbo)
    bb, ba = md.get("bestBid"), md.get("bestAsk")
    if not bb or not ba:
        return None
    yb = float(bb["value"]) if isinstance(bb, dict) else float(bb)
    ya = float(ba["value"]) if isinstance(ba, dict) else float(ba)
    bd, ad = float(md.get("bidDepth") or 0), float(md.get("askDepth") or 0)
    return VenueBook(yb, ya, 1 - ya, 1 - yb, bd, ad, ad, bd, time.time())


def _norm_strike_for_match(v: float | None) -> float | None:
    # NFP poly uses 'k' (×1000); Kalshi reports absolute. _norm both to thousands.
    return v


def scan(cfg, kalshi_client, polyus_client, top=40):
    pc, kc = polyus_client, kalshi_client
    # Poly active markets, grouped by family prefix.
    poly, off = [], 0
    while off < 12000:
        b = pc._get("/markets", {"active": "true", "closed": "false",
                                 "limit": 500, "offset": off}).get("markets", [])
        if not b:
            break
        poly += b
        off += 500
    by_prefix: dict[str, list[dict]] = {}
    for m in poly:
        pre = (m.get("slug") or "").split("-")[0]
        by_prefix.setdefault(pre, []).append(m)
    log.info("poly active=%d; routed families present: %s", len(poly),
             {p: len(by_prefix.get(p, [])) for p in ROUTES if p in by_prefix})

    pfee = PolyUSFeeModel()
    fee_cache: dict[str, KalshiFeeModel] = {}
    results = []
    for prefix, series_list in ROUTES.items():
        pmkts = by_prefix.get(prefix, [])
        if not pmkts:
            continue
        # gather Kalshi markets across the routed series
        kmkts = []
        for st in series_list:
            kmkts += kc._get("/markets", {"series_ticker": st, "status": "open",
                                          "limit": 1000}).get("markets", [])
        for pm in pmkts:
            ps = strike(pm.get("slug", ""), anchored=True)
            pp = periods(pm.get("slug", "") + " " + (pm.get("question") or ""))
            if ps is None:
                continue
            for km in kmkts:
                ks = strike(km.get("yes_sub_title") or "", anchored=False)
                kp = periods((km.get("ticker") or "") + " " + (km.get("title") or ""))
                if ks is None or abs(ks - ps) > 1e-6:
                    continue
                # require a shared month/quarter (not just year) to fix the period
                if not ((pp & kp) & (_MON_ABBR | {"q1", "q2", "q3", "q4"})):
                    continue
                kb = _kalshi_book(km)
                if kb is None:
                    continue
                try:
                    pb = _poly_book(pc._get(f"/markets/{pm['slug']}/bbo"))
                except Exception:
                    pb = None
                if pb is None:
                    continue
                stk = km.get("series_ticker") or km["ticker"].split("-", 1)[0]
                if stk not in fee_cache:
                    try:
                        fee_cache[stk] = kc.get_fee_model(km["ticker"])
                    except Exception:
                        fee_cache[stk] = KalshiFeeModel(0.07, 0.0175)
                res = compute_edges(km["ticker"], kb, fee_cache[stk], False, pb, pfee, False)
                cp = comparator_poly(pm.get("slug", ""))
                ck = comparator_kalshi((km.get("yes_sub_title") or "") + " " + (km.get("title") or "")
                                       + " " + (km.get("rules_primary") or ""))
                mismatch = cp != ck and "?" not in (cp, ck)
                results.append((prefix, km, pm, res, cp, ck, mismatch))
                break  # one Kalshi match per Poly market

    results.sort(key=lambda x: -x[3].best.taker_edge)
    return results


# ── Sports routing (no-draw match-winner markets) ────────────────
# Poly moneyline = one market, outcomes [competitor1, competitor2]. Kalshi
# KX*GAME/MATCH/FIGHT = two markets/event (one YES per competitor, full name in
# yes_sub_title). We pair an event by competitor identity (team CODES for team
# sports, or competitor NAMES for player sports) + start time. Only NO-DRAW
# events make the binary hedge exhaustive; soccer/draws are excluded.
import json as _json
import unicodedata as _ud

_MON3 = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7,
         "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
_MLB_ALIAS = {"WHITESOX": "CWS"}

# Poly slug-league -> Kalshi match-winner series. All no-draw match winners.
SPORTS_ROUTES = {
    "mlb": ("aec-mlb-", "KXMLBGAME"),
    "npb": ("aec-npb-", "KXNPBGAME"),
    "kbo": ("aec-kbo-", "KXKBOGAME"),
    "wnba": ("aec-wnba-", "KXWNBAGAME"),
    "atp": ("aec-atp-", "KXATPMATCH"),
    "wta": ("aec-wta-", "KXWTAMATCH"),
    "itfme": ("aec-itfme-", "KXITFMATCH"),
    "itfwo": ("aec-itfwo-", "KXITFWMATCH"),
    "ufc": ("aec-ufc-", "KXUFCFIGHT"),
}
ALL_SPORTS = list(SPORTS_ROUTES)


def _parse_kalshi_game_dt(prefix: str):
    """Return (utc_datetime, has_time). Ticker date is YYMONDD with optional HHMM
    (US Eastern). +4h ≈ EDT -> UTC; the match window tolerates DST/tz slack."""
    import re as _re
    from datetime import datetime, timedelta, timezone
    m = _re.search(r"-(\d{2})([a-z]{3})(\d{2})(\d{2})(\d{2})", prefix.lower())
    has_time = bool(m)
    if not m:
        m = _re.search(r"-(\d{2})([a-z]{3})(\d{2})", prefix.lower())
    if not m:
        return None, False
    g = m.groups()
    yy, mon, dd = g[0], g[1], g[2]
    hh, mm = (g[3], g[4]) if has_time else ("12", "00")
    if mon not in _MON3:
        return None, False
    naive = datetime(2000 + int(yy), _MON3[mon], int(dd), int(hh), int(mm))
    return naive.replace(tzinfo=timezone.utc) + timedelta(hours=4), has_time


def _parse_iso(s: str):
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat((s or "").replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None


def _ntok(s: str) -> set:
    s = "".join(c for c in _ud.normalize("NFKD", s or "") if not _ud.combining(c))
    import re as _re
    return {t for t in _re.findall(r"[a-z0-9]+", s.lower()) if len(t) > 1}


def _names_match(a: str, b: str) -> bool:
    ta, tb = _ntok(a), _ntok(b)
    if not ta or not tb:
        return False
    inter = ta & tb
    if not inter:
        return False
    return len(inter) / len(ta | tb) >= 0.5 or ta <= tb or tb <= ta


def _outcomes(pm: dict) -> list:
    o = pm.get("outcomes")
    if isinstance(o, str):
        try:
            o = _json.loads(o)
        except _json.JSONDecodeError:
            o = []
    return o if isinstance(o, list) else []


def match_sports_games(cfg, kalshi_client, polyus_client, league="mlb", window_h=12):
    """Pair Poly moneyline events to Kalshi per-competitor markets. A = Poly
    outcome[0]. Returns {km1 (Kalshi market for A), pm, c1 (A label), c2, start}.
    Matches by team code-set first, then by competitor name; both gated on a
    date window (tight when the ticker carries a time, looser for date-only)."""
    kc, pc = kalshi_client, polyus_client
    poly_prefix, k_series = SPORTS_ROUTES[league]

    poly, off = [], 0
    while off < 8000:
        b = pc._get("/markets", {"active": "true", "closed": "false",
                                 "categories": "sports", "limit": 500, "offset": off}).get("markets", [])
        if not b:
            break
        poly += [m for m in b if m["slug"].startswith(poly_prefix)
                 and m.get("sportsMarketTypeV2") == "SPORTS_MARKET_TYPE_MONEYLINE"]
        off += 500

    km = kc._get("/markets", {"series_ticker": k_series, "status": "open", "limit": 1000}).get("markets", [])
    games = {}
    for m in km:
        pref = m["ticker"].rsplit("-", 1)[0]
        code = m["ticker"].rsplit("-", 1)[1].upper()
        if pref not in games:
            dt, has_time = _parse_kalshi_game_dt(pref)
            games[pref] = {"dt": dt, "has_time": has_time, "by_code": {}}
        games[pref]["by_code"][code] = m
    by_set = {}
    for g in games.values():
        by_set.setdefault(frozenset(g["by_code"]), []).append(g)

    def date_ok(g, pstart):
        if g["dt"] is None or pstart is None:
            return False
        win = window_h if g["has_time"] else 36
        return abs((g["dt"] - pstart).total_seconds()) <= win * 3600

    def sub(m):
        return m.get("yes_sub_title") or ""

    def km_for_A(game, outs):
        """Within a 2-competitor game, pick the Kalshi market for A = outs[0].
        A is ALWAYS Poly outcome[0] (matches the Poly BBO side). Choose by name;
        if A's name is too short to match (e.g. 'A's'), take the complement of
        the market that matches outs[1]."""
        mkts = list(game["by_code"].values())
        if len(mkts) != 2:
            return None
        mA = next((m for m in mkts if _names_match(outs[0], sub(m))), None)
        mB = next((m for m in mkts if _names_match(outs[1], sub(m))), None)
        if mA is None and mB is not None:
            mA = next(m for m in mkts if m is not mB)
        return mA

    matches = []
    for pm in poly:
        parts = pm["slug"][len(poly_prefix):].split("-")
        outs = _outcomes(pm)
        if len(parts) < 3 or len(outs) < 2:
            continue
        pstart = _parse_iso(pm.get("gameStartTime"))

        # Find the matched Kalshi game: team code-set first (disambiguates
        # multi-game series by date), else competitor names.
        game = None
        c1c, c2c = parts[0].upper(), parts[1].upper()
        c1c, c2c = _MLB_ALIAS.get(c1c, c1c), _MLB_ALIAS.get(c2c, c2c)
        for g in by_set.get(frozenset({c1c, c2c}), []):
            if date_ok(g, pstart):
                game = g
                break
        if game is None:
            for g in games.values():
                if not date_ok(g, pstart):
                    continue
                mkts = list(g["by_code"].values())
                if (any(_names_match(outs[0], sub(m)) for m in mkts)
                        and any(_names_match(outs[1], sub(m)) for m in mkts)):
                    game = g
                    break
        if game is None:
            continue

        km1 = km_for_A(game, outs)   # A = outs[0], consistent across both venues
        if km1 is not None:
            matches.append({"km1": km1, "pm": pm, "c1": outs[0], "c2": outs[1], "start": pstart})
    return matches


def scan_sports(cfg, kalshi_client, polyus_client, league="mlb", window_h=12):
    kc, pc = kalshi_client, polyus_client
    pfee = PolyUSFeeModel()
    fee_cache = {}
    results = []
    for mt in match_sports_games(cfg, kc, pc, league, window_h):
        km1, pm, c1, c2 = mt["km1"], mt["pm"], mt["c1"], mt["c2"]
        kb = _kalshi_book(km1)               # Kalshi YES = A wins; NO = other (no draw)
        if kb is None:
            continue
        try:
            pb = _poly_book(pc._get(f"/markets/{pm['slug']}/bbo"))  # YES = outcome[0] = A
        except Exception:
            pb = None
        if pb is None:
            continue
        st = km1.get("series_ticker") or SPORTS_ROUTES[league][1]
        if st not in fee_cache:
            try:
                fee_cache[st] = kc.get_fee_model(km1["ticker"])
            except Exception:
                fee_cache[st] = KalshiFeeModel(0.07, 0.0175)
        res = compute_edges(km1["ticker"], kb, fee_cache[st], False, pb, pfee, False)
        results.append((league, km1, pm, res, c1, c2))
    results.sort(key=lambda x: -x[3].best.taker_edge)
    return results


def _shortlabel(s: str) -> str:
    s = str(s)
    return s if len(s) <= 12 else (s.split()[-1] if s.split() else s[:12])


def build_sports_pairs(cfg, kalshi_client, polyus_client, leagues="mlb"):
    """Auto-built, UNVERIFIED MappedPairs for live sports watching across one or
    more leagues (detection only — never enter mappings.yaml, never tradable).
    settlement_time = start + 6h (≈ end) so events aren't gated out mid-play.
    A = Poly outcome[0]; both venues invert=False."""
    from datetime import timedelta

    from .mapping import MappedPair, VenueLeg
    if isinstance(leagues, str):
        leagues = [leagues]
    pairs = []
    for league in leagues:
        if league not in SPORTS_ROUTES:
            continue
        for mt in match_sports_games(cfg, kalshi_client, polyus_client, league):
            km1, pm, c1, c2 = mt["km1"], mt["pm"], mt["c1"], mt["c2"]
            end, _ = (mt["start"], True) if mt["start"] else _parse_kalshi_game_dt(km1["ticker"])
            if end is None:
                continue
            end = end + timedelta(hours=6)
            a, b = _shortlabel(c1), _shortlabel(c2)
            ymeans = f"{c1} beats {c2}"
            pairs.append(MappedPair(
                id=f"{league.upper()} {a}v{b} {(mt['start'] or end).strftime('%m-%d')}",
                kalshi=VenueLeg(km1["ticker"], ymeans, "venue settlement", end,
                                "no-draw; UNVERIFIED auto-match", False),
                polyus=VenueLeg(pm["slug"], ymeans, "venue settlement", end,
                                "no-draw; UNVERIFIED auto-match", False),
                verified_by="AUTO-SCANNER (unverified)", verified_at="auto", enabled=True))
    return pairs


def main() -> None:
    import argparse

    from config import load_config
    from .suggester import write_candidates
    from .venues.kalshi import KalshiClient
    from .venues.polyus import PolyUSClient

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(description="Family-routed cross-venue scanner (detection only)")
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--sports", action="store_true", help="scan sports moneyline (no-draw) instead of macro")
    ap.add_argument("--league", default="mlb")
    args = ap.parse_args()
    cfg = load_config()
    kc, pc = KalshiClient(cfg), PolyUSClient(cfg)

    if args.sports:
        sres = scan_sports(cfg, kc, pc, league=args.league)
        print(f"\n=== {args.league.upper()} MONEYLINE apparent edges (UNVERIFIED — no-draw hedge) ===")
        print(f"{'taker':>7} {'maker':>7}  {'A=team1':9} {'Kalshi(team1)':28} {'Poly game':30} {'sz':>4}")
        ssh = []
        for league, km1, pm, res, c1, c2 in sres[:args.top]:
            e = res.best
            flag = "  <-- EDGE (verify)" if e.taker_edge > 0 else ""
            print(f"{e.taker_edge*100:6.1f}% {e.maker_edge*100:6.1f}%  {c1+' v '+c2:9} "
                  f"{km1['ticker'][:28]:28} {pm['slug'][:30]:30} {e.taker_size:4.0f}{flag}")
            if e.taker_edge > 0:
                ssh.append({
                    "kalshi_ticker": km1["ticker"], "polyus_slug": pm["slug"], "confidence": 0.5,
                    "title_kalshi": km1.get("title"), "title_polyus": pm.get("question"),
                    "differing_fields": [f"taker_edge={e.taker_edge*100:.1f}% ({c1} wins; no-draw moneyline)",
                                         "VERIFY: same game/date, postponement/suspension & extra-innings void rules",
                                         "assumes Poly BBO is outcome[0]=team1 — confirm"],
                    "model": "scanner-sports-mlb"})
        print(f"\n{len(sres)} matched games; {len(ssh)} with positive apparent edge.")
        if ssh:
            write_candidates(cfg.candidates_path, ssh)
            print(f"Wrote {len(ssh)} UNVERIFIED sports candidates to {cfg.candidates_path}")
        return

    results = scan(cfg, kc, pc, top=args.top)

    print("\n=== APPARENT EDGES across routed families (UNVERIFIED — verify before trading) ===")
    print(f"{'taker':>7} {'maker':>7}  {'fam':5} {'cmp':>7} {'Kalshi':22} {'Poly US':38} {'K_sz':>5}")
    shown = []
    for prefix, km, pm, res, cp, ck, mismatch in results[:args.top]:
        e = res.best
        cmpstr = f"K{ck}/P{cp}"
        if mismatch:
            flag = "  <-- COMPARATOR MISMATCH (fake edge: double-loss risk)"
        elif e.taker_edge > 0:
            flag = "  <-- EDGE (verify)"
        else:
            flag = ""
        print(f"{e.taker_edge*100:6.1f}% {e.maker_edge*100:6.1f}%  {prefix:5} {cmpstr:>7} "
              f"{km['ticker'][:22]:22} {pm.get('slug','')[:38]:38} {e.taker_size:5.0f}{flag}")
        if e.taker_edge > 0:
            diff = [f"taker_edge={e.taker_edge*100:.1f}% (auto-matched, UNVERIFIED)",
                    "VERIFY: resolution source, exact threshold, tie/void, timing"]
            if mismatch:
                diff.insert(0, f"⚠ COMPARATOR MISMATCH Kalshi '{ck}' vs Poly '{cp}' — likely FAKE "
                               "edge, double-loss at the boundary value. DO NOT TRADE without resolving.")
            shown.append({
                "kalshi_ticker": km["ticker"], "polyus_slug": pm.get("slug"),
                "confidence": 0.2 if mismatch else 0.6,
                "title_kalshi": (km.get("title") or "") + " / " + (km.get("yes_sub_title") or ""),
                "title_polyus": pm.get("question"),
                "differing_fields": diff,
                "model": "scanner-family-routed",
            })
    n_mis = sum(1 for r in results if r[6])
    print(f"\n{len(results)} aligned pairs; {len(shown)} with positive apparent edge; "
          f"{n_mis} have a comparator mismatch (fake edges).")
    if shown:
        write_candidates(cfg.candidates_path, shown)
        print(f"Wrote {len(shown)} UNVERIFIED candidates to {cfg.candidates_path}")


if __name__ == "__main__":
    main()
