"""Telegram notifications + a small two-way control bot.

Outbound: trade fills, errors, kill-switch trips, edge alerts, daily summaries.
Inbound (optional `listen` command): /status /pnl /positions /halt /resume, so you
can check in or stop trading from your phone. Only the configured chat is obeyed.

All sends are best-effort: failures are logged, never raised, so notifications
can never crash detection or execution. No token in code — env only.

CLI:
  python -m src.notify test       # send a test message
  python -m src.notify chatid     # print chat ids that have DM'd your bot
  python -m src.notify daily      # send today's summary
  python -m src.notify listen     # run the command bot (long-poll)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

log = logging.getLogger("arb.notify")
_API = "https://api.telegram.org/bot{token}/{method}"


def _usd(x: float) -> str:
    return ("-$" if x < 0 else "$") + f"{abs(x):.2f}"


class Notifier:
    def __init__(self, token: str, chat_id: str, alert_edges: bool = False) -> None:
        self.token = token
        self.chat_id = str(chat_id) if chat_id else ""
        self.alert_edges = alert_edges
        self.enabled = bool(token and chat_id)

    @classmethod
    def from_config(cls, cfg) -> "Notifier":
        return cls(cfg.telegram_bot_token, cfg.telegram_chat_id, cfg.telegram_alert_edges)

    def send(self, text: str) -> bool:
        if not self.enabled:
            log.debug("telegram not configured; skipping: %s", text[:80])
            return False
        import httpx
        try:
            r = httpx.post(_API.format(token=self.token, method="sendMessage"),
                           json={"chat_id": self.chat_id, "text": text,
                                 "disable_web_page_preview": True}, timeout=10)
            if r.status_code != 200:
                log.warning("telegram send failed %s: %s", r.status_code, r.text[:200])
                return False
            return True
        except Exception as e:  # never let notifications break trading
            log.warning("telegram send error: %s", e)
            return False

    # ── typed messages ───────────────────────────────────────
    def startup(self, mode: str, n_pairs: int) -> None:
        self.send(f"▶️ polmkt-arb started in {mode.upper()} mode over {n_pairs} pairs.")

    def trade(self, pair_id: str, result) -> None:
        icon = {"completed": "✅", "partial_unwound": "⚠️",
                "leg2_failed_unwound": "⚠️", "leg1_insufficient": "⚠️",
                "leg1_no_fill": "ℹ️"}.get(result.status, "•")
        self.send(f"{icon} TRADE {pair_id}\n"
                  f"status: {result.status}\n"
                  f"hedged: {result.hedged_qty:.0f} contracts\n"
                  f"realized: {_usd(result.realized_pnl)} (net of fees)")

    def edge(self, pair_id: str, structure) -> None:
        if not self.alert_edges:
            return
        self.send(f"📈 EDGE {pair_id}\n{structure.structure}\n"
                  f"taker {structure.taker_edge*100:.1f}% / maker {structure.maker_edge*100:.1f}% "
                  f"at {structure.taker_size:.0f}")

    def error(self, context: str, msg: str) -> None:
        self.send(f"🛑 ERROR [{context}]\n{msg}")

    def kill_switch(self, reason: str) -> None:
        self.send(f"🚨 KILL SWITCH TRIPPED\n{reason}\nNo new orders until cleared.")

    def daily(self, stats: dict) -> None:
        ks = "  ⚠️ kill switch tripped" if stats["kill_switch_tripped"] else ""
        self.send(f"📊 Daily summary {stats['date']}\n"
                  f"realized PnL: {_usd(stats['realized'])} (net of fees)\n"
                  f"fees: {_usd(stats['fees'])}\n"
                  f"fills: {stats['fills']}\n"
                  f"open positions: {stats['open_positions']}{ks}")


# ── command bot (inbound) ────────────────────────────────────────
def _ro_store(db_path: str):
    import sqlite3
    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    c.row_factory = sqlite3.Row
    return c


def _cmd_status(cfg) -> str:
    from pathlib import Path
    halted = Path(cfg.kill_switch_file).exists()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        c = _ro_store(cfg.db_path)
        npairs = c.execute("SELECT COUNT(*) FROM pairs").fetchone()[0]
        openn = c.execute("SELECT COUNT(*) FROM edges WHERE closed_ts IS NULL").fetchone()[0]
        row = c.execute("SELECT realized FROM pnl_daily WHERE date=?", (today,)).fetchone()
        pnl = row["realized"] if row else 0.0
        c.close()
    except Exception as e:
        return f"status unavailable: {e}"
    return (f"📟 status\npairs: {npairs}\nopen opportunities: {openn}\n"
            f"today PnL: {_usd(pnl)}\nkill switch: {'ENGAGED 🚨' if halted else 'clear ✅'}")


def _cmd_pnl(cfg) -> str:
    try:
        c = _ro_store(cfg.db_path)
        rows = c.execute("SELECT date, realized, fees FROM pnl_daily ORDER BY date DESC LIMIT 7").fetchall()
        c.close()
    except Exception as e:
        return f"pnl unavailable: {e}"
    if not rows:
        return "no PnL recorded yet."
    return "💵 PnL (last 7d)\n" + "\n".join(
        f"{r['date']}: {_usd(r['realized'])}  (fees {_usd(r['fees'])})" for r in rows)


def _cmd_positions(cfg) -> str:
    try:
        c = _ro_store(cfg.db_path)
        rows = c.execute("SELECT venue, market_id, net_qty FROM positions WHERE net_qty != 0").fetchall()
        c.close()
    except Exception as e:
        return f"positions unavailable: {e}"
    if not rows:
        return "no open positions."
    return "📦 positions\n" + "\n".join(
        f"{r['venue']} {r['market_id'][:24]}: {r['net_qty']:.0f}" for r in rows)


def edge_report_text(cfg) -> str:
    """The empirical 'can the arb work?' readout from recorded edges."""
    import time

    from .store import Store
    st = Store(cfg.db_path)
    now = time.time()

    def block(s) -> str:
        if s["n"] == 0:
            return "  no actionable edges detected"
        top = ", ".join(f"{p}×{c}" for p, c in s["top"][:3])
        return (f"  {s['n']} edges ({s['open_now']} open now)\n"
                f"  catchable 1–30s: {s['catchable']}  |  persistent >30s (likely not real): {s['persistent']}\n"
                f"  median life: {s['median_ms']/1000:.1f}s  |  biggest: {s['max_edge']*100:.1f}%\n"
                f"  hypothetical if every one captured: ${s['hyp_profit']:.2f}\n"
                f"  most active: {top}")
    return ("📈 Arb edge report (paper/detection)\n— last 24h —\n"
            + block(st.edge_stats(now - 86400))
            + "\n— last 7d —\n" + block(st.edge_stats(now - 7 * 86400)))


def _cmd_report(cfg) -> str:
    return edge_report_text(cfg)


def _cmd_dashboard(cfg) -> str:
    url = (cfg.dashboard_public_url or "").rstrip("/")
    if not url:
        return "dashboard URL not set (DASHBOARD_PUBLIC_URL)."
    link = f"{url}/?token={cfg.dashboard_token}" if cfg.dashboard_token else f"{url}/"
    return f"📊 dashboard (token-gated):\n{link}"


def _cmd_version(cfg) -> str:
    from pathlib import Path
    p = Path("VERSION")
    if p.exists():
        return "🏷 deployed version:\n" + p.read_text().strip()
    return "no VERSION file (deploy via deploy/setup.sh to stamp it)."


def listen(cfg) -> None:
    """Long-poll for commands. Only the configured chat is obeyed."""
    import time
    from pathlib import Path

    import httpx
    n = Notifier.from_config(cfg)
    if not n.enabled:
        print("Telegram not configured (set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID).")
        return
    n.send("🤖 command bot online. /status /report /pnl /positions /dashboard /version /halt /resume")
    offset = None
    HELP = "commands: /status /report /pnl /positions /dashboard /version /halt /resume /help"
    while True:
        try:
            r = httpx.get(_API.format(token=cfg.telegram_bot_token, method="getUpdates"),
                          params={"timeout": 30, **({"offset": offset} if offset else {})}, timeout=40)
            for upd in r.json().get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                chat = str((msg.get("chat") or {}).get("id"))
                text = (msg.get("text") or "").strip().lower()
                if chat != n.chat_id:
                    continue  # ignore everyone but the owner
                if text.startswith("/status"):
                    n.send(_cmd_status(cfg))
                elif text.startswith("/report"):
                    n.send(_cmd_report(cfg))
                elif text.startswith("/pnl"):
                    n.send(_cmd_pnl(cfg))
                elif text.startswith("/positions"):
                    n.send(_cmd_positions(cfg))
                elif text.startswith("/dashboard"):
                    n.send(_cmd_dashboard(cfg))
                elif text.startswith("/version"):
                    n.send(_cmd_version(cfg))
                elif text.startswith("/halt"):
                    Path(cfg.kill_switch_file).touch()
                    n.send("🚨 HALT engaged — kill switch on. No new orders. /resume to clear.")
                elif text.startswith("/resume"):
                    p = Path(cfg.kill_switch_file)
                    if p.exists():
                        p.unlink()
                    n.send("✅ resumed — kill switch cleared.")
                else:
                    n.send(HELP)
        except Exception as e:
            log.warning("listen loop error: %s", e)
            time.sleep(3)


def main() -> None:
    import argparse

    import httpx

    from config import load_config
    from .store import Store

    ap = argparse.ArgumentParser(description="Telegram notifications + command bot")
    ap.add_argument("cmd", choices=["test", "chatid", "daily", "listen"])
    args = ap.parse_args()
    cfg = load_config()

    if args.cmd == "chatid":
        if not cfg.telegram_bot_token:
            print("Set TELEGRAM_BOT_TOKEN first.")
            return
        r = httpx.get(_API.format(token=cfg.telegram_bot_token, method="getUpdates"), timeout=15)
        chats = {}
        for upd in r.json().get("result", []):
            ch = (upd.get("message") or {}).get("chat") or {}
            if ch:
                chats[ch.get("id")] = ch.get("username") or ch.get("first_name")
        if chats:
            print("Chat IDs that have messaged your bot (DM the bot first if empty):")
            for cid, name in chats.items():
                print(f"  {cid}  ({name})")
        else:
            print("No chats found. Send your bot a DM, then re-run.")
    elif args.cmd == "test":
        ok = Notifier.from_config(cfg).send("✅ polmkt-arb test message — notifications are working.")
        print("sent" if ok else "not sent (check token/chat id)")
    elif args.cmd == "daily":
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        stats = Store(cfg.db_path).daily_stats(today)
        n = Notifier.from_config(cfg)
        n.daily(stats)
        n.send(edge_report_text(cfg))   # nightly: also the arb edge readout
        print("daily summary + edge report sent:", stats)
    elif args.cmd == "listen":
        listen(cfg)


if __name__ == "__main__":
    main()
