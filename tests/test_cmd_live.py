import json

from config import load_config
from src.notify import _cmd_live


def _write(tmp_path, rows):
    (tmp_path / "exec_timing.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows))


def test_cmd_live_no_attempts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert "no live attempts" in _cmd_live(load_config())


def test_cmd_live_counts_and_gates(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rows = ([{"outcome": "hedged", "locked": 0.05, "total_ms": 300}] * 18
            + [{"outcome": "missed", "realized": -0.03, "total_ms": 900}] * 2
            + [{"outcome": "k_nofill"}])
    _write(tmp_path, rows)
    out = _cmd_live(load_config())
    assert "hedged 18, missed 2" in out
    assert "20/20" in out                    # trades gate met
    assert "10%" in out                      # miss rate 2/20
    assert "ALL GATES PASSED" in out         # net = 0.90 - 0.06 > 0


def test_cmd_live_gates_open_until_target(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, [{"outcome": "hedged", "locked": 0.04, "total_ms": 250}] * 3)
    out = _cmd_live(load_config())
    assert "3/20" in out
    assert "keep running" in out


def test_cmd_live_flags_position_verified(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, [{"outcome": "hedged", "locked": 0.05, "total_ms": 2500, "p_via": "poll"}])
    assert "position-verified" in _cmd_live(load_config())
