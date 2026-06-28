"""SQLite persistence (arb.db). Holds mapped pairs, quote snapshots/history,
detected edges with persistence timing, and Phase-2 tables (paper fills, live
orders, positions, daily PnL) created up-front so the executor slots in cleanly.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS pairs (
    id TEXT PRIMARY KEY,
    kalshi_ticker TEXT NOT NULL,
    polyus_slug TEXT NOT NULL,
    yes_means_kalshi TEXT NOT NULL,
    yes_means_polyus TEXT NOT NULL,
    resolution_kalshi TEXT NOT NULL,
    resolution_polyus TEXT NOT NULL,
    settlement_kalshi TEXT NOT NULL,
    settlement_polyus TEXT NOT NULL,
    tie_void_kalshi TEXT NOT NULL,
    tie_void_polyus TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    verified_by TEXT,
    verified_at TEXT,
    updated_ts REAL NOT NULL
);

-- latest snapshot per (pair, venue)
CREATE TABLE IF NOT EXISTS quotes (
    pair_id TEXT NOT NULL,
    venue TEXT NOT NULL,
    yes_bid REAL, yes_ask REAL, no_bid REAL, no_ask REAL,
    yes_bid_size REAL, yes_ask_size REAL, no_bid_size REAL, no_ask_size REAL,
    ts_recv REAL NOT NULL,
    stale INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (pair_id, venue)
);

CREATE TABLE IF NOT EXISTS quotes_hist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pair_id TEXT NOT NULL, venue TEXT NOT NULL,
    yes_bid REAL, yes_ask REAL, no_bid REAL, no_ask REAL,
    ts_recv REAL NOT NULL
);

-- one row per detected actionable opportunity, with open/close persistence
CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pair_id TEXT NOT NULL,
    structure TEXT NOT NULL,
    taker_edge REAL NOT NULL,
    maker_edge REAL NOT NULL,
    size_available REAL NOT NULL,
    slippage_buffer REAL NOT NULL,
    legs_json TEXT NOT NULL,
    opened_ts REAL NOT NULL,
    closed_ts REAL,
    duration_ms REAL,
    peak_taker_edge REAL NOT NULL,
    actionable INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kalshi_ticker TEXT, polyus_slug TEXT,
    confidence REAL, title_kalshi TEXT, title_polyus TEXT,
    differing_fields TEXT, model TEXT, status TEXT, suggested_at REAL
);

-- ── Phase 2 (created now, written only by executor) ──────────────
CREATE TABLE IF NOT EXISTS paper_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pair_id TEXT, venue TEXT, outcome TEXT, role TEXT,
    price REAL, qty REAL, fee REAL, ts REAL
);

CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    pair_id TEXT, venue TEXT, leg INTEGER, outcome TEXT,
    side TEXT, type TEXT, tif TEXT,
    price REAL, qty REAL, filled_qty REAL DEFAULT 0,
    state TEXT NOT NULL, venue_order_id TEXT,
    created_ts REAL, updated_ts REAL
);

CREATE TABLE IF NOT EXISTS positions (
    venue TEXT NOT NULL, market_id TEXT NOT NULL,
    net_qty REAL NOT NULL, avg_price REAL,
    updated_ts REAL NOT NULL,
    PRIMARY KEY (venue, market_id)
);

CREATE TABLE IF NOT EXISTS pnl_daily (
    date TEXT PRIMARY KEY,
    realized REAL NOT NULL DEFAULT 0,
    fees REAL NOT NULL DEFAULT 0,
    kill_switch_tripped INTEGER NOT NULL DEFAULT 0
);
"""


class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    @contextmanager
    def _tx(self):
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def close(self) -> None:
        self.conn.close()

    # ── pairs ────────────────────────────────────────────────
    def upsert_pair(self, pair) -> None:
        with self._tx() as c:
            c.execute(
                """INSERT INTO pairs (id, kalshi_ticker, polyus_slug,
                    yes_means_kalshi, yes_means_polyus, resolution_kalshi,
                    resolution_polyus, settlement_kalshi, settlement_polyus,
                    tie_void_kalshi, tie_void_polyus, enabled, verified_by,
                    verified_at, updated_ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                    kalshi_ticker=excluded.kalshi_ticker,
                    polyus_slug=excluded.polyus_slug,
                    yes_means_kalshi=excluded.yes_means_kalshi,
                    yes_means_polyus=excluded.yes_means_polyus,
                    resolution_kalshi=excluded.resolution_kalshi,
                    resolution_polyus=excluded.resolution_polyus,
                    settlement_kalshi=excluded.settlement_kalshi,
                    settlement_polyus=excluded.settlement_polyus,
                    tie_void_kalshi=excluded.tie_void_kalshi,
                    tie_void_polyus=excluded.tie_void_polyus,
                    enabled=excluded.enabled, verified_by=excluded.verified_by,
                    verified_at=excluded.verified_at, updated_ts=excluded.updated_ts""",
                (
                    pair.id, pair.kalshi.market_id, pair.polyus.market_id,
                    pair.kalshi.yes_means, pair.polyus.yes_means,
                    pair.kalshi.resolution_source, pair.polyus.resolution_source,
                    pair.kalshi.settlement_time.isoformat(),
                    pair.polyus.settlement_time.isoformat(),
                    pair.kalshi.tie_void, pair.polyus.tie_void,
                    int(pair.enabled), pair.verified_by, pair.verified_at, time.time(),
                ),
            )

    # ── quotes ───────────────────────────────────────────────
    def record_quote(self, pair_id: str, venue: str, book, stale: bool) -> None:
        with self._tx() as c:
            c.execute(
                """INSERT INTO quotes (pair_id, venue, yes_bid, yes_ask, no_bid,
                    no_ask, yes_bid_size, yes_ask_size, no_bid_size, no_ask_size,
                    ts_recv, stale) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(pair_id, venue) DO UPDATE SET
                    yes_bid=excluded.yes_bid, yes_ask=excluded.yes_ask,
                    no_bid=excluded.no_bid, no_ask=excluded.no_ask,
                    yes_bid_size=excluded.yes_bid_size, yes_ask_size=excluded.yes_ask_size,
                    no_bid_size=excluded.no_bid_size, no_ask_size=excluded.no_ask_size,
                    ts_recv=excluded.ts_recv, stale=excluded.stale""",
                (pair_id, venue, book.yes_bid, book.yes_ask, book.no_bid, book.no_ask,
                 book.yes_bid_size, book.yes_ask_size, book.no_bid_size, book.no_ask_size,
                 book.ts, int(stale)),
            )
            c.execute(
                """INSERT INTO quotes_hist (pair_id, venue, yes_bid, yes_ask,
                    no_bid, no_ask, ts_recv) VALUES (?,?,?,?,?,?,?)""",
                (pair_id, venue, book.yes_bid, book.yes_ask, book.no_bid, book.no_ask, book.ts),
            )

    # ── edges / opportunity persistence ──────────────────────
    def open_edge(self, pair_id: str, structure_result, slippage_buffer: float, now: float) -> int:
        s = structure_result
        legs = [leg.__dict__ for leg in s.taker_legs]
        with self._tx() as c:
            cur = c.execute(
                """INSERT INTO edges (pair_id, structure, taker_edge, maker_edge,
                    size_available, slippage_buffer, legs_json, opened_ts,
                    peak_taker_edge, actionable)
                   VALUES (?,?,?,?,?,?,?,?,?,1)""",
                (pair_id, s.structure, s.taker_edge, s.maker_edge, s.taker_size,
                 slippage_buffer, json.dumps(legs), now, s.taker_edge),
            )
            return cur.lastrowid

    def bump_edge(self, edge_id: int, taker_edge: float, size: float) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE edges SET peak_taker_edge=MAX(peak_taker_edge, ?), size_available=? WHERE id=?",
                (taker_edge, size, edge_id),
            )

    def close_edge(self, edge_id: int, now: float) -> None:
        with self._tx() as c:
            row = c.execute("SELECT opened_ts FROM edges WHERE id=?", (edge_id,)).fetchone()
            if row is None:
                return
            dur = (now - row["opened_ts"]) * 1000.0
            c.execute(
                "UPDATE edges SET closed_ts=?, duration_ms=?, actionable=0 WHERE id=?",
                (now, dur, edge_id),
            )

    # ── candidates ───────────────────────────────────────────
    def record_candidate(self, c_dict: dict) -> None:
        with self._tx() as c:
            c.execute(
                """INSERT INTO candidates (kalshi_ticker, polyus_slug, confidence,
                    title_kalshi, title_polyus, differing_fields, model, status,
                    suggested_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                (c_dict.get("kalshi_ticker"), c_dict.get("polyus_slug"),
                 c_dict.get("confidence"), c_dict.get("title_kalshi"),
                 c_dict.get("title_polyus"),
                 json.dumps(c_dict.get("differing_fields", [])),
                 c_dict.get("model"), c_dict.get("status", "needs_review"), time.time()),
            )
