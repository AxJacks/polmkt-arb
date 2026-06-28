import pytest

from src.books import VenueBook
from src.edge import compute_edges, is_actionable
from src.fees import KalshiFeeModel, PolyUSFeeModel


def make_books():
    # Kalshi: YES ask 0.40 (size 100). NOT-A path on Poly is its NO side.
    kalshi = VenueBook(
        yes_bid=0.38, yes_ask=0.40, no_bid=0.60, no_ask=0.62,
        yes_bid_size=90, yes_ask_size=100, no_bid_size=100, no_ask_size=90, ts=0.0,
    )
    # Poly: YES bid 0.45 (size 80) => NO ask = 0.55 with size 80 (sell-YES liquidity)
    poly = VenueBook(
        yes_bid=0.45, yes_ask=0.47, no_bid=0.53, no_ask=0.55,
        yes_bid_size=80, yes_ask_size=70, no_bid_size=70, no_ask_size=80, ts=0.0,
    )
    return kalshi, poly


def test_known_divergence_net_of_fee_edge_and_size():
    kalshi, poly = make_books()
    kfee = KalshiFeeModel(taker_multiplier=0.07, maker_multiplier=0.0)
    pfee = PolyUSFeeModel()

    res = compute_edges("p1", kalshi, kfee, False, poly, pfee, False)
    best = res.best

    # Best direction is buy A on Kalshi (0.40) + buy NOT-A on Poly (0.55).
    assert best.structure == "A@kalshi/NOTA@polyus"
    # net = 0.40 + 0.55 + 0.02 (kalshi taker) + 0.01 (poly taker) = 0.98
    assert best.taker_edge == pytest.approx(0.02, abs=1e-9)
    # size = min(kalshi yes_ask 100, poly no_ask 80) = 80
    assert best.taker_size == 80

    # leg fees recorded correctly
    l1, l2 = best.taker_legs
    assert l1.venue == "kalshi" and l1.price == pytest.approx(0.40) and l1.fee == pytest.approx(0.02)
    assert l2.venue == "polyus" and l2.price == pytest.approx(0.55) and l2.fee == pytest.approx(0.01)


def test_actionable_gate():
    kalshi, poly = make_books()
    res = compute_edges("p1", kalshi, KalshiFeeModel(0.07, 0.0), False, poly, PolyUSFeeModel(), False)
    # edge 0.02; with slippage 0.005, net 0.015 >= min_edge 0.01 and size 80 >= 10
    assert is_actionable(res.best, min_edge=0.01, min_size=10, slippage_buffer=0.005)
    # raise min_edge above net -> not actionable
    assert not is_actionable(res.best, min_edge=0.02, min_size=10, slippage_buffer=0.005)
    # size gate
    assert not is_actionable(res.best, min_edge=0.01, min_size=100, slippage_buffer=0.005)


def test_boundary_prices_do_not_raise():
    # Live books can show asks/bids at exactly 0.00 or 1.00 (illiquid strikes).
    # Fees are undefined there, but edge calc must degrade gracefully, not raise.
    kalshi = VenueBook(0.00, 0.04, 0.96, 1.00, 0, 343, 343, 0, 0.0)
    poly = VenueBook(0.01, 0.29, 0.71, 0.99, 1111, 1111, 1111, 1111, 0.0)
    res = compute_edges("p1", kalshi, KalshiFeeModel(0.07, 0.0175), False,
                        poly, PolyUSFeeModel(), False)
    assert res.best.taker_edge < 0  # no free money at the boundary
    assert not is_actionable(res.best, 0.01, 10, 0.005)


def test_no_edge_when_prices_sum_high():
    # Prices leave no room after fees -> negative edge, not actionable.
    kalshi = VenueBook(0.49, 0.51, 0.49, 0.51, 100, 100, 100, 100, 0.0)
    poly = VenueBook(0.49, 0.51, 0.49, 0.51, 100, 100, 100, 100, 0.0)
    res = compute_edges("p1", kalshi, KalshiFeeModel(0.07, 0.0), False, poly, PolyUSFeeModel(), False)
    assert res.best.taker_edge < 0
    assert not is_actionable(res.best, 0.01, 10, 0.005)
