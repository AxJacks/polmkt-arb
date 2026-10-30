import time
from types import SimpleNamespace

from src.store import Store


def _book(ts):
    return SimpleNamespace(yes_bid=0.4, yes_ask=0.42, no_bid=0.58, no_ask=0.60,
                           yes_bid_size=10, yes_ask_size=10,
                           no_bid_size=10, no_ask_size=10, ts=ts)


def test_quotes_hist_pruned_past_retention(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    now = time.time()
    s.record_quote("p1", "kalshi", _book(now - Store.QUOTES_HIST_RETENTION_S - 60), stale=False)
    s._last_prune = 0.0   # force the periodic prune to run on the next insert
    s.record_quote("p1", "kalshi", _book(now), stale=False)
    rows = s.conn.execute("SELECT ts_recv FROM quotes_hist").fetchall()
    assert len(rows) == 1                      # the stale row was pruned
    assert abs(rows[0]["ts_recv"] - now) < 1


def test_quotes_hist_prune_is_periodic_not_per_insert(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    now = time.time()
    s.record_quote("p1", "kalshi", _book(now), stale=False)   # first insert primes _last_prune
    s.record_quote("p1", "kalshi", _book(now - Store.QUOTES_HIST_RETENTION_S - 60), stale=False)
    rows = s.conn.execute("SELECT count(*) c FROM quotes_hist").fetchone()
    assert rows["c"] == 2   # within the prune interval nothing is deleted
