"""Alert sinks: structured log line, recos.xlsx row, and optional webhook."""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("arb.alerts")

_XLSX_HEADERS = [
    "ts", "pair_id", "structure", "taker_edge", "maker_edge", "size",
    "slippage_buffer", "leg1_venue", "leg1_outcome", "leg1_price", "leg1_fee",
    "leg2_venue", "leg2_outcome", "leg2_price", "leg2_fee",
]


def _row(ts: float, pair_id: str, s, slippage_buffer: float) -> list:
    l1, l2 = s.taker_legs
    return [
        ts, pair_id, s.structure, round(s.taker_edge, 4), round(s.maker_edge, 4),
        s.taker_size, slippage_buffer,
        l1.venue, l1.outcome, l1.price, l1.fee,
        l2.venue, l2.outcome, l2.price, l2.fee,
    ]


def append_reco(path: str, ts: float, pair_id: str, structure, slippage_buffer: float) -> None:
    from openpyxl import Workbook, load_workbook

    p = Path(path)
    if p.exists():
        wb = load_workbook(p)
        ws = wb.active
    else:
        wb = Workbook()
        ws = wb.active
        ws.title = "recos"
        ws.append(_XLSX_HEADERS)
    ws.append(_row(ts, pair_id, structure, slippage_buffer))
    wb.save(p)


def send_webhook(url: str, payload: dict) -> None:
    if not url:
        return
    import httpx

    try:
        httpx.post(url, json=payload, timeout=10)
    except Exception as e:  # alerts must never crash the detector
        log.warning("webhook failed: %s", e)


def emit_alert(cfg, ts: float, pair_id: str, structure, slippage_buffer: float) -> None:
    l1, l2 = structure.taker_legs
    log.info(
        "ARB pair=%s %s taker_edge=%.4f maker_edge=%.4f size=%.0f | %s %s @%.3f (fee %.3f) + %s %s @%.3f (fee %.3f)",
        pair_id, structure.structure, structure.taker_edge, structure.maker_edge,
        structure.taker_size, l1.venue, l1.outcome, l1.price, l1.fee,
        l2.venue, l2.outcome, l2.price, l2.fee,
    )
    try:
        append_reco(cfg.recos_xlsx, ts, pair_id, structure, slippage_buffer)
    except Exception as e:
        log.warning("recos.xlsx write failed: %s", e)
    send_webhook(cfg.alert_webhook_url, {
        "ts": ts, "pair_id": pair_id, "structure": structure.structure,
        "taker_edge": structure.taker_edge, "maker_edge": structure.maker_edge,
        "size": structure.taker_size,
        "legs": [l1.__dict__, l2.__dict__],
    })
    try:  # Telegram (no-ops unless configured + TELEGRAM_ALERT_EDGES=true)
        from .notify import Notifier
        Notifier.from_config(cfg).edge(pair_id, structure)
    except Exception as e:
        log.warning("telegram edge alert failed: %s", e)
