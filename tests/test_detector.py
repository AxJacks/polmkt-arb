import dataclasses
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from config import load_config
from src.books import OrderBookStore, VenueBook
from src.detector import Detector, Skip, evaluate_pair
from src.fees import KalshiFeeModel, PolyUSFeeModel
from src.mapping import MappedPair, VenueLeg

NOW = datetime(2026, 6, 28, tzinfo=timezone.utc).timestamp()


def make_pair():
    far = datetime(2026, 12, 1, tzinfo=timezone.utc)
    leg_k = VenueLeg("KX-T1", "X happens", "src", far, "n/a", False)
    leg_p = VenueLeg("x-happens", "X happens", "src", far, "n/a", False)
    return MappedPair("p1", leg_k, leg_p, "tester", "2026-06-28", True)


def cfg_ns(**over):
    base = dict(settlement_window_s=3600, min_edge=0.01, min_size=10, slippage_buffer=0.005)
    base.update(over)
    return SimpleNamespace(**base)


def diverged_books(ts):
    kalshi = VenueBook(0.38, 0.40, 0.60, 0.62, 90, 100, 100, 90, ts)
    poly = VenueBook(0.45, 0.47, 0.53, 0.55, 80, 70, 70, 80, ts)
    return kalshi, poly


def no_edge_books(ts):
    b = VenueBook(0.49, 0.51, 0.49, 0.51, 100, 100, 100, 100, ts)
    return b, VenueBook(0.49, 0.51, 0.49, 0.51, 100, 100, 100, 100, ts)


# ── staleness gate ───────────────────────────────────────────────
def test_stale_feed_marks_untradeable():
    store = OrderBookStore(staleness_ms=2000)
    pair = make_pair()
    k, p = diverged_books(NOW)
    store.update("p1", "kalshi", k)
    store.update("p1", "polyus", VenueBook(0.45, 0.47, 0.53, 0.55, 80, 70, 70, 80, NOW - 10))
    out = evaluate_pair(pair, store, KalshiFeeModel(0.07, 0.0), PolyUSFeeModel(), cfg_ns(), now=NOW)
    assert isinstance(out, Skip) and "polyus" in out.reason


def test_settlement_window_gate():
    store = OrderBookStore(staleness_ms=10_000)
    near = datetime(2026, 6, 28, 0, 10, tzinfo=timezone.utc)  # 10 min after NOW
    pair = MappedPair("p1",
                      VenueLeg("k", "x", "s", near, "n/a", False),
                      VenueLeg("p", "x", "s", near, "n/a", False),
                      "t", "2026-06-28", True)
    k, p = diverged_books(NOW)
    store.update("p1", "kalshi", k)
    store.update("p1", "polyus", p)
    out = evaluate_pair(pair, store, KalshiFeeModel(0.07, 0.0), PolyUSFeeModel(), cfg_ns(), now=NOW)
    assert isinstance(out, Skip) and "settlement" in out.reason


# ── synthetic divergence fires an alert with correct math ────────
def test_synthetic_divergence_records_edge_and_alert(tmp_path):
    cfg = dataclasses.replace(
        load_config(),
        db_path=str(tmp_path / "arb.db"),
        recos_xlsx=str(tmp_path / "recos.xlsx"),
        alert_webhook_url="",
        min_edge=0.01, min_size=10, slippage_buffer=0.005,
        settlement_window_s=3600, staleness_ms=10_000,
    )
    kfee = KalshiFeeModel(0.07, 0.0)
    pfee = PolyUSFeeModel()
    fake_kalshi = SimpleNamespace(get_fee_model=lambda mid: kfee)
    fake_polyus = SimpleNamespace(get_fee_model=lambda mid: pfee)

    det = Detector(cfg, fake_kalshi, fake_polyus)
    pair = make_pair()
    det.pairs = [pair]

    k, p = diverged_books(NOW)
    det.books.update("p1", "kalshi", k)
    det.books.update("p1", "polyus", p)

    det.evaluate_all(NOW)

    # edge recorded with correct net-of-fee math (~0.02)
    row = det.store.conn.execute(
        "SELECT taker_edge, size_available, structure, closed_ts FROM edges").fetchone()
    assert row is not None
    assert row["taker_edge"] == pytest.approx(0.02, abs=1e-9)
    assert row["size_available"] == 80
    assert row["structure"] == "A@kalshi/NOTA@polyus"
    assert row["closed_ts"] is None  # still open

    # alert written to recos.xlsx
    assert (tmp_path / "recos.xlsx").exists()


def test_opportunity_persistence_open_then_close(tmp_path):
    cfg = dataclasses.replace(
        load_config(),
        db_path=str(tmp_path / "arb.db"),
        recos_xlsx=str(tmp_path / "recos.xlsx"),
        alert_webhook_url="",
        min_edge=0.01, min_size=10, slippage_buffer=0.005,
        settlement_window_s=3600, staleness_ms=10_000,
    )
    det = Detector(cfg,
                   SimpleNamespace(get_fee_model=lambda mid: KalshiFeeModel(0.07, 0.0)),
                   SimpleNamespace(get_fee_model=lambda mid: PolyUSFeeModel()))
    det.pairs = [make_pair()]

    k, p = diverged_books(NOW)
    det.books.update("p1", "kalshi", k)
    det.books.update("p1", "polyus", p)
    det.evaluate_all(NOW)  # opens

    # edge disappears -> opportunity closes with a duration
    k2, p2 = no_edge_books(NOW + 5)
    det.books.update("p1", "kalshi", k2)
    det.books.update("p1", "polyus", p2)
    det.evaluate_all(NOW + 5)  # closes

    row = det.store.conn.execute(
        "SELECT opened_ts, closed_ts, duration_ms FROM edges").fetchone()
    assert row["closed_ts"] is not None
    assert row["duration_ms"] == pytest.approx(5000, abs=1)
