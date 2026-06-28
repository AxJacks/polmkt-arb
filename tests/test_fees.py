import math

import pytest

from src.fees import FeeError, KalshiFeeModel, PolyUSFeeModel


# ── Kalshi: fee = ceil_to_cent(theta * C * p * (1-p)), rounded UP ──
def test_kalshi_taker_standard_multiplier():
    m = KalshiFeeModel(taker_multiplier=0.07)
    # 5c: 0.07*0.05*0.95 = 0.003325 -> ceil cent = 0.01
    assert m.taker_fee(0.05, 1) == pytest.approx(0.01)
    # 50c: 0.07*0.25 = 0.0175 -> ceil cent = 0.02
    assert m.taker_fee(0.50, 1) == pytest.approx(0.02)
    # 95c symmetric with 5c = 0.01
    assert m.taker_fee(0.95, 1) == pytest.approx(0.01)
    # 100 contracts at 50c: 0.07*100*0.25 = 1.75 exactly
    assert m.taker_fee(0.50, 100) == pytest.approx(1.75)


def test_kalshi_sports_multiplier_higher():
    sports = KalshiFeeModel(taker_multiplier=0.35)
    # 0.35*0.25 = 0.0875 -> ceil cent = 0.09
    assert sports.taker_fee(0.50, 1) == pytest.approx(0.09)
    # higher than standard at the same price
    assert sports.taker_fee(0.50, 1) > KalshiFeeModel(taker_multiplier=0.07).taker_fee(0.50, 1)


def test_kalshi_maker_is_fraction_of_taker():
    m = KalshiFeeModel(taker_multiplier=0.07, maker_multiplier=0.0175)
    # 0.0175*100*0.25 = 0.4375 -> ceil cent = 0.44
    assert m.maker_fee(0.50, 100) == pytest.approx(0.44)
    assert m.maker_fee(0.50, 100) < m.taker_fee(0.50, 100)


def test_kalshi_missing_multiplier_raises():
    with pytest.raises(FeeError):
        KalshiFeeModel(taker_multiplier=None).taker_fee(0.50, 1)


# ── Polymarket US: taker theta 0.05, maker theta -0.0125 (rebate) ──
def test_polyus_taker():
    m = PolyUSFeeModel()
    # 0.05*100*0.25 = 1.25
    assert m.taker_fee(0.50, 100) == pytest.approx(1.25)
    # ~1.25% at midprice on a $1 contract block
    assert m.taker_fee(0.50, 1) == pytest.approx(0.01)


def test_polyus_maker_is_rebate():
    m = PolyUSFeeModel()
    # -0.0125*100*0.25 = -0.3125 -> -0.31 (credit)
    assert m.maker_fee(0.50, 100) == pytest.approx(-0.31)
    assert m.maker_fee(0.50, 100) < 0


def test_price_bounds_raise():
    m = PolyUSFeeModel()
    for bad in (0.0, 1.0, -0.1, 1.5):
        with pytest.raises(FeeError):
            m.taker_fee(bad, 1)
