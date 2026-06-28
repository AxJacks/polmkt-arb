import textwrap

import pytest

from src.mapping import MappingError, load_mappings, parse_pair

COMPLETE = {
    "id": "p1",
    "verified_by": "tester",
    "verified_at": "2026-06-28",
    "enabled": True,
    "kalshi": {
        "market_id": "KX-T1",
        "yes_means": "X happens",
        "resolution_source": "Official source",
        "settlement_time": "2026-12-01T00:00:00Z",
        "tie_void": "n/a",
    },
    "polyus": {
        "market_id": "x-happens",
        "yes_means": "X happens",
        "resolution_source": "Official source",
        "settlement_time": "2026-12-01T00:00:00Z",
        "tie_void": "n/a",
    },
}


def test_complete_pair_parses_and_is_tradable():
    pair = parse_pair(COMPLETE)
    assert pair.id == "p1"
    assert pair.enabled is True
    assert pair.kalshi.resolution_source == "Official source"
    assert pair.polyus.settlement_time.year == 2026


def test_missing_resolution_metadata_is_never_tradable():
    bad = {**COMPLETE, "polyus": {**COMPLETE["polyus"]}}
    del bad["polyus"]["resolution_source"]
    with pytest.raises(MappingError):
        parse_pair(bad)


def test_missing_tie_void_raises():
    bad = {**COMPLETE, "kalshi": {**COMPLETE["kalshi"]}}
    del bad["kalshi"]["tie_void"]
    with pytest.raises(MappingError):
        parse_pair(bad)


def test_missing_settlement_time_raises():
    bad = {**COMPLETE, "kalshi": {**COMPLETE["kalshi"]}}
    del bad["kalshi"]["settlement_time"]
    with pytest.raises(MappingError):
        parse_pair(bad)


def test_load_rejects_duplicate_ids(tmp_path):
    f = tmp_path / "mappings.yaml"
    f.write_text(textwrap.dedent("""
    - id: dup
      verified_by: t
      verified_at: 2026-06-28
      kalshi: {market_id: a, yes_means: x, resolution_source: s, settlement_time: 2026-12-01T00:00:00Z, tie_void: n/a}
      polyus: {market_id: b, yes_means: x, resolution_source: s, settlement_time: 2026-12-01T00:00:00Z, tie_void: n/a}
    - id: dup
      verified_by: t
      verified_at: 2026-06-28
      kalshi: {market_id: c, yes_means: x, resolution_source: s, settlement_time: 2026-12-01T00:00:00Z, tie_void: n/a}
      polyus: {market_id: d, yes_means: x, resolution_source: s, settlement_time: 2026-12-01T00:00:00Z, tie_void: n/a}
    """))
    with pytest.raises(MappingError):
        load_mappings(str(f))
