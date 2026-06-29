from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from src import notify
from src.notify import Notifier, _cmd_pnl, _cmd_positions, _cmd_status
from src.store import Store


def test_disabled_is_noop():
    n = Notifier("", "")
    assert n.enabled is False
    assert n.send("hi") is False


def test_send_posts_to_telegram(monkeypatch):
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return SimpleNamespace(status_code=200, text="ok")

    monkeypatch.setattr(httpx, "post", fake_post)
    n = Notifier("TOKEN", "12345")
    assert n.send("hello") is True
    assert "botTOKEN/sendMessage" in captured["url"]
    assert captured["json"]["chat_id"] == "12345"
    assert captured["json"]["text"] == "hello"


def test_send_handles_failure(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: SimpleNamespace(status_code=429, text="rate"))
    assert Notifier("T", "C").send("x") is False


def test_trade_message_formats(monkeypatch):
    sent = []
    n = Notifier("T", "C")
    monkeypatch.setattr(n, "send", lambda text: sent.append(text) or True)
    res = SimpleNamespace(status="completed", hedged_qty=10, realized_pnl=0.21)
    n.trade("cpi-3.8", res)
    assert "TRADE cpi-3.8" in sent[0]
    assert "completed" in sent[0] and "$0.21" in sent[0]


def test_edge_alert_respects_gate(monkeypatch):
    sent = []
    struct = SimpleNamespace(structure="A@kalshi/NOTA@polyus", taker_edge=0.03,
                             maker_edge=0.05, taker_size=20)
    off = Notifier("T", "C", alert_edges=False)
    monkeypatch.setattr(off, "send", lambda text: sent.append(text))
    off.edge("p", struct)
    assert sent == []                      # gated off
    on = Notifier("T", "C", alert_edges=True)
    monkeypatch.setattr(on, "send", lambda text: sent.append(text))
    on.edge("p", struct)
    assert sent and "EDGE p" in sent[0]


def test_daily_stats_and_summary(tmp_path):
    store = Store(str(tmp_path / "n.db"))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now = datetime.now(timezone.utc).timestamp()
    store.add_pnl(today, 0.21, 0.29)
    store.record_paper_fill("p", "kalshi", "A", "taker", 0.40, 10, 0.17, now)
    store.record_paper_fill("p", "polyus", "NOT-A", "taker", 0.55, 10, 0.12, now)
    store.upsert_position("kalshi", "M", 10, 0.40, now)
    s = store.daily_stats(today)
    assert s["realized"] == 0.21 and s["fills"] == 2 and s["open_positions"] == 1
    assert s["kill_switch_tripped"] is False


def test_edge_stats_and_report(tmp_path):
    import time
    store = Store(str(tmp_path / "e.db"))
    now = time.time()
    rows = [  # (opened_ago, closed_ago_or_None, duration_ms, peak, size)
        (10, None, None, 0.03, 20),       # open now
        (100, 95, 5000, 0.04, 10),        # catchable (1-30s)
        (200, 140, 60000, 0.06, 30),      # persistent (>30s, likely fake)
    ]
    for ago, cago, dur, peak, size in rows:
        store.conn.execute(
            "INSERT INTO edges (pair_id, structure, taker_edge, maker_edge, size_available, "
            "slippage_buffer, legs_json, opened_ts, closed_ts, duration_ms, peak_taker_edge, actionable) "
            "VALUES ('p','s',?,0,?,0.005,'[]',?,?,?,?,1)",
            (peak, size, now - ago, (now - cago) if cago else None, dur, peak))
    store.conn.commit()
    s = store.edge_stats(now - 3600)
    assert s["n"] == 3 and s["open_now"] == 1 and s["catchable"] == 1 and s["persistent"] == 1
    assert s["max_edge"] == 0.06
    assert s["hyp_profit"] == pytest.approx(0.03 * 20 + 0.04 * 10 + 0.06 * 30)

    from src import notify
    cfg = SimpleNamespace(db_path=str(tmp_path / "e.db"))
    txt = notify.edge_report_text(cfg)
    assert "edge report" in txt and "24h" in txt


def test_command_handlers(tmp_path):
    store = Store(str(tmp_path / "c.db"))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    store.add_pnl(today, 1.50, 0.30)
    store.upsert_position("polyus", "x-mkt", 5, 0.5, datetime.now(timezone.utc).timestamp())
    cfg = SimpleNamespace(db_path=str(tmp_path / "c.db"),
                          kill_switch_file=str(tmp_path / "HALT"))
    assert "status" in _cmd_status(cfg).lower()
    assert "$1.50" in _cmd_pnl(cfg)
    assert "x-mkt" in _cmd_positions(cfg)
