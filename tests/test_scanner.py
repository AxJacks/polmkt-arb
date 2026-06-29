from datetime import datetime, timezone

from src.scanner import (comparator_kalshi, comparator_poly, periods,
                         strike, _parse_kalshi_game_dt, _parse_iso)


def test_strike_anchored_poly():
    assert strike("cpic-uscpi-june-yoy-2026-07-14-gt3pt8pct", anchored=True) == 3.8
    assert strike("urc-us-seasonadj-gte-june-2026-07-02-atl4pt6", anchored=True) == 4.6
    assert strike("nfpc-uschange-gte-june-2026-07-02-atl250k", anchored=True) == 250000
    assert strike("gdpc-us-saa-q2-2026-07-30-atl2pt0", anchored=True) == 2.0


def test_strike_kalshi_subtitle():
    assert strike("Above 3.8%", anchored=False) == 3.8
    assert strike("Above 250,000", anchored=False) == 250000
    assert strike("Above 250K", anchored=False) == 250000
    # a bare year is not a strike
    assert strike("2026", anchored=False) is None


def test_periods_month_and_year():
    assert "jun" in periods("cpic-uscpi-june-yoy-2026")
    assert "2026" in periods("June 2026")
    assert "q2" in periods("gdpc-us-saa-q2-2026")


def test_comparator_mismatch_detection():
    # the unemployment trap: Kalshi 'above' (>) vs Poly 'at least' (>=)
    assert comparator_poly("urc-...-atl4pt3") == "≥"
    assert comparator_kalshi("U-3 unemployment rate above 4.3%") == ">"
    # CPI matches: both strict greater
    assert comparator_poly("cpic-...-gt3pt8pct") == ">"
    assert comparator_kalshi("CPI increases by more than 3.8%") == ">"


def test_kalshi_game_dt_parsing():
    # 26JUN29 19:07 ET -> +4h -> 23:07 UTC
    dt = _parse_kalshi_game_dt("KXMLBGAME-26JUN291907NYMTOR")
    assert dt == datetime(2026, 6, 29, 23, 7, tzinfo=timezone.utc)
    assert _parse_kalshi_game_dt("KXMLBGAME-badformat") is None


def test_iso_parse():
    assert _parse_iso("2026-06-30T01:40:00Z") == datetime(2026, 6, 30, 1, 40, tzinfo=timezone.utc)
    assert _parse_iso(None) is None
