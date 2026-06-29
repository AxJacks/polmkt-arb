"""Read-only web dashboard over arb.db. Shows live opportunities, mapped pairs
with both venues' top-of-book + staleness, and opportunity history (persistence
timing). Reads only — never places orders, never writes the DB.

Run:  ./.venv/bin/python -m src.dashboard --db ./arb.db --port 8787
then open http://127.0.0.1:8787
"""
from __future__ import annotations

import sqlite3
import time

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from .books import VenueBook
from .edge import compute_edges
from .fees import KalshiFeeModel, PolyUSFeeModel

# Zero-fee models => the dashboard shows GROSS (pre-fee) edge from live quotes as
# a watch indicator. Net-of-fee edges live in the `edges` table (recorded by the
# detector only when actionable).
_GROSS_K = KalshiFeeModel(taker_multiplier=0.0, maker_multiplier=0.0)
_GROSS_P = PolyUSFeeModel(taker_theta=0.0, maker_theta=0.0)


def _book_from_row(r) -> VenueBook:
    return VenueBook(
        yes_bid=r["yes_bid"] or 0.0, yes_ask=r["yes_ask"] or 1.0,
        no_bid=r["no_bid"] or 0.0, no_ask=r["no_ask"] or 1.0,
        yes_bid_size=r["yes_bid_size"] or 0.0, yes_ask_size=r["yes_ask_size"] or 0.0,
        no_bid_size=r["no_bid_size"] or 0.0, no_ask_size=r["no_ask_size"] or 0.0,
        ts=r["ts_recv"] or 0.0,
    )


def create_app(db_path: str, staleness_ms: int = 2000) -> FastAPI:
    app = FastAPI(title="polmkt-arb dashboard")

    def conn() -> sqlite3.Connection:
        c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        c.row_factory = sqlite3.Row
        return c

    @app.get("/api/state")
    def state() -> JSONResponse:
        now = time.time()
        try:
            c = conn()
        except sqlite3.OperationalError:
            return JSONResponse({"error": f"cannot open db: {db_path}", "pairs": [],
                                 "open_edges": [], "recent_edges": []})
        try:
            quotes = {(r["pair_id"], r["venue"]): r
                      for r in c.execute("SELECT * FROM quotes")}
            pairs = []
            for p in c.execute("SELECT * FROM pairs ORDER BY id"):
                pid = p["id"]
                kq = quotes.get((pid, "kalshi"))
                pq = quotes.get((pid, "polyus"))
                row = {
                    "id": pid, "enabled": bool(p["enabled"]),
                    "yes_means": p["yes_means_kalshi"],
                    "kalshi_ticker": p["kalshi_ticker"], "polyus_slug": p["polyus_slug"],
                    "kalshi": _quote_view(kq, now, staleness_ms),
                    "polyus": _quote_view(pq, now, staleness_ms),
                    "gross_edge": None, "gross_size": None, "structure": None,
                }
                if kq and pq:
                    res = compute_edges(pid, _book_from_row(kq), _GROSS_K, False,
                                        _book_from_row(pq), _GROSS_P, False)
                    row["gross_edge"] = round(res.best.taker_edge, 4)
                    row["gross_size"] = res.best.taker_size
                    row["structure"] = res.best.structure
                pairs.append(row)

            open_edges = [dict(r) for r in c.execute(
                "SELECT * FROM edges WHERE closed_ts IS NULL ORDER BY opened_ts DESC")]
            for e in open_edges:
                e["age_s"] = round(now - e["opened_ts"], 1)
            recent = [dict(r) for r in c.execute(
                "SELECT * FROM edges WHERE closed_ts IS NOT NULL "
                "ORDER BY closed_ts DESC LIMIT 50")]
            pnl = [dict(r) for r in c.execute(
                "SELECT date, realized, fees, kill_switch_tripped FROM pnl_daily "
                "ORDER BY date DESC LIMIT 14")]
            positions = [dict(r) for r in c.execute(
                "SELECT venue, market_id, net_qty FROM positions WHERE net_qty != 0 "
                "ORDER BY venue, market_id")]
            fills = [dict(r) for r in c.execute(
                "SELECT pair_id, venue, outcome, price, qty, fee, ts FROM paper_fills "
                "ORDER BY id DESC LIMIT 20")]
            return JSONResponse({
                "now": now, "db": db_path, "staleness_ms": staleness_ms,
                "pairs": pairs, "open_edges": open_edges, "recent_edges": recent,
                "pnl": pnl, "positions": positions, "paper_fills": fills,
            })
        finally:
            c.close()

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _HTML

    return app


def _quote_view(q, now: float, staleness_ms: int) -> dict | None:
    if q is None:
        return None
    age_ms = (now - (q["ts_recv"] or 0.0)) * 1000.0
    return {
        "yes_bid": q["yes_bid"], "yes_ask": q["yes_ask"],
        "no_bid": q["no_bid"], "no_ask": q["no_ask"],
        "yes_bid_size": q["yes_bid_size"], "yes_ask_size": q["yes_ask_size"],
        "age_ms": round(age_ms), "stale": age_ms > staleness_ms,
    }


_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>polmkt-arb</title>
<style>
:root{--bg:#0e1116;--card:#171b22;--line:#262c36;--fg:#e6edf3;--mut:#8b949e;
--grn:#3fb950;--red:#f85149;--amb:#d29922;--acc:#58a6ff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.45 ui-sans-serif,-apple-system,Segoe UI,Roboto,sans-serif}
header{display:flex;align-items:center;gap:16px;padding:14px 22px;
border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);z-index:5}
header h1{font-size:15px;margin:0;font-weight:600;letter-spacing:.3px}
header .sub{color:var(--mut);font-size:12px}
.chips{margin-left:auto;display:flex;gap:10px}
.chip{background:var(--card);border:1px solid var(--line);border-radius:999px;
padding:5px 12px;font-size:12px;color:var(--mut)}
.chip b{color:var(--fg)}
main{padding:18px 22px;display:grid;gap:18px;max-width:1280px;margin:0 auto}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
section>h2{margin:0;padding:11px 16px;font-size:12px;text-transform:uppercase;
letter-spacing:.6px;color:var(--mut);border-bottom:1px solid var(--line)}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th,td{text-align:right;padding:8px 12px;border-bottom:1px solid var(--line);white-space:nowrap}
th{font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.4px;font-weight:500}
td:first-child,th:first-child{text-align:left}
tr:last-child td{border-bottom:none}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.pos{color:var(--grn)} .neg{color:var(--red)} .mut{color:var(--mut)}
.badge{display:inline-block;padding:2px 7px;border-radius:5px;font-size:11px}
.b-ok{background:rgba(63,185,80,.15);color:var(--grn)}
.b-stale{background:rgba(248,81,73,.15);color:var(--red)}
.b-off{background:rgba(139,148,158,.15);color:var(--mut)}
.empty{padding:20px 16px;color:var(--mut);font-size:13px}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:6px}
.flash{animation:fl 1s ease}@keyframes fl{from{background:rgba(88,166,255,.18)}to{}}
small{color:var(--mut)}
</style></head>
<body>
<header>
  <h1>polmkt-arb <span class="sub">Kalshi &harr; Polymarket US &middot; detector (read-only)</span></h1>
  <div class="chips" id="chips"></div>
</header>
<main>
  <section><h2>Live opportunities &mdash; net of fees (open)</h2><div id="open"></div></section>
  <section><h2>Mapped pairs &mdash; live quotes &amp; gross (pre-fee) edge</h2><div id="pairs"></div></section>
  <section><h2>Recent opportunities &mdash; persistence timing</h2><div id="recent"></div></section>
  <section><h2>Paper PnL &mdash; net of real fees (Phase 2)</h2><div id="pnl"></div></section>
  <section><h2>Positions &amp; recent paper fills</h2>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:0">
      <div id="positions" style="border-right:1px solid var(--line)"></div>
      <div id="fills"></div>
    </div>
  </section>
</main>
<script>
const $=id=>document.getElementById(id);
const pct=x=>x==null?'&mdash;':(x*100).toFixed(2)+'%';
const cls=x=>x==null?'mut':(x>0?'pos':(x<0?'neg':'mut'));
const px=x=>x==null?'&mdash;':Number(x).toFixed(3);
function qcell(q){
  if(!q) return '<span class="badge b-off">no feed</span>';
  const b=q.stale?'<span class="badge b-stale">stale '+q.age_ms+'ms</span>'
                  :'<span class="badge b-ok">'+q.age_ms+'ms</span>';
  return '<span class="mono">'+px(q.yes_bid)+' / '+px(q.yes_ask)+'</span> &nbsp;'+b;
}
function table(head,rows,empty){
  if(!rows.length) return '<div class="empty">'+empty+'</div>';
  return '<table><thead><tr>'+head.map(h=>'<th>'+h+'</th>').join('')+
    '</tr></thead><tbody>'+rows.join('')+'</tbody></table>';
}
async function tick(){
  let s; try{ s=await (await fetch('/api/state')).json(); }catch(e){ return; }
  const openN=s.open_edges.length, pairsN=s.pairs.length;
  const fresh=s.pairs.filter(p=>p.kalshi&&!p.kalshi.stale&&p.polyus&&!p.polyus.stale).length;
  const usd0=x=>(x<0?'-$':'$')+Math.abs(Number(x)).toFixed(2);
  const todayPnl=(s.pnl&&s.pnl.length)?s.pnl[0].realized:null;
  $('chips').innerHTML=
    '<span class="chip"><span class="dot" style="background:'+(openN?'var(--grn)':'var(--mut)')+'"></span>'
    +'<b>'+openN+'</b> live</span>'
    +'<span class="chip"><b>'+pairsN+'</b> pairs</span>'
    +'<span class="chip"><b>'+fresh+'</b> both-feeds fresh</span>'
    +(todayPnl!=null?'<span class="chip"><b class="'+cls(todayPnl)+'">'+usd0(todayPnl)+'</b> paper PnL</span>':'')
    +'<span class="chip mono">'+new Date(s.now*1000).toLocaleTimeString()+'</span>';

  $('open').innerHTML=table(
    ['Pair','Structure','Taker edge','Maker edge','Size','Age'],
    s.open_edges.map(e=>'<tr class="flash"><td>'+e.pair_id+'</td><td class="mono mut">'+e.structure
      +'</td><td class="'+cls(e.taker_edge)+'">'+pct(e.taker_edge)+'</td><td class="'+cls(e.maker_edge)
      +'">'+pct(e.maker_edge)+'</td><td class="mono">'+e.size_available+'</td><td class="mono mut">'
      +e.age_s+'s</td></tr>'),
    'No open opportunities above MIN_EDGE right now.');

  $('pairs').innerHTML=table(
    ['Pair','Kalshi yes b/a','Poly US yes b/a','Gross edge','Best size','Structure'],
    s.pairs.map(p=>'<tr><td>'+p.id+(p.enabled?'':' <span class="badge b-off">off</span>')
      +'<br><small>'+(p.yes_means||'')+'</small></td><td>'+qcell(p.kalshi)+'</td><td>'+qcell(p.polyus)
      +'</td><td class="'+cls(p.gross_edge)+'">'+pct(p.gross_edge)+'</td><td class="mono">'
      +(p.gross_size==null?'&mdash;':p.gross_size)+'</td><td class="mono mut">'
      +(p.structure||'&mdash;')+'</td></tr>'),
    'No mapped pairs / quotes yet. Run the detector (or the synthetic demo) to populate.');

  $('recent').innerHTML=table(
    ['Pair','Structure','Peak taker','Max size','Duration'],
    s.recent_edges.map(e=>'<tr><td>'+e.pair_id+'</td><td class="mono mut">'+e.structure
      +'</td><td class="'+cls(e.peak_taker_edge)+'">'+pct(e.peak_taker_edge)+'</td><td class="mono">'
      +e.size_available+'</td><td class="mono mut">'+(e.duration_ms/1000).toFixed(1)+'s</td></tr>'),
    'No closed opportunities recorded yet.');

  const usd=x=>(x<0?'-$':'$')+Math.abs(Number(x)).toFixed(2);
  $('pnl').innerHTML=table(
    ['Date','Realized','Fees','Kill switch'],
    (s.pnl||[]).map(p=>'<tr><td class="mono">'+p.date+'</td><td class="mono '+cls(p.realized)
      +'">'+usd(p.realized)+'</td><td class="mono mut">'+usd(p.fees)+'</td><td>'
      +(p.kill_switch_tripped?'<span class="badge b-stale">TRIPPED</span>':'<span class="mut">—</span>')
      +'</td></tr>'),
    'No paper PnL yet — run: python -m src.executor  (or scripts/demo_paper.py)');
  $('positions').innerHTML='<h2 style="border:none">Open positions</h2>'+table(
    ['Venue','Market','Net qty'],
    (s.positions||[]).map(p=>'<tr><td>'+p.venue+'</td><td class="mono mut">'+(p.market_id||'').slice(0,26)
      +'</td><td class="mono">'+p.net_qty+'</td></tr>'),
    'No open positions.');
  $('fills').innerHTML='<h2 style="border:none">Recent paper fills</h2>'+table(
    ['Venue','Out','Qty','Px','Fee'],
    (s.paper_fills||[]).map(f=>'<tr><td>'+f.venue+'</td><td class="mut">'+f.outcome+'</td><td class="mono">'
      +Number(f.qty).toFixed(0)+'</td><td class="mono">'+px(f.price)+'</td><td class="mono mut">'
      +usd(f.fee)+'</td></tr>'),
    'No fills yet.');
}
tick(); setInterval(tick,1500);
</script>
</body></html>"""


def main() -> None:
    import argparse

    import uvicorn

    from config import load_config

    cfg = load_config()
    ap = argparse.ArgumentParser(description="polmkt-arb read-only dashboard")
    ap.add_argument("--db", default=cfg.db_path)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    args = ap.parse_args()

    app = create_app(args.db, cfg.staleness_ms)
    print(f"dashboard: http://{args.host}:{args.port}  (db={args.db})")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
