from src.fees import PolyUSFeeModel
from src.hedging import kalshi_fill, max_poly_price, poly_fill

PFEE = PolyUSFeeModel()


# ── max_poly_price ──────────────────────────────────────────────

def test_max_poly_price_accounts_for_fee():
    p = max_poly_price(0.50, PFEE)
    assert p == 0.49
    assert p + PFEE.taker_fee(p, 1) <= 0.50


def test_max_poly_price_never_exceeds_budget_across_range():
    for cents in range(2, 100):
        budget = cents / 100.0
        p = max_poly_price(budget, PFEE)
        if p >= 0.01:
            assert p + PFEE.taker_fee(p, 1) <= budget + 1e-9


def test_max_poly_price_no_viable_price_on_tiny_budget():
    assert max_poly_price(0.005, PFEE) < 0.01


def test_max_poly_price_caps_at_99c():
    assert max_poly_price(5.0, PFEE) == 0.99


# ── kalshi_fill ─────────────────────────────────────────────────

def test_kalshi_fill_top_level_dollar_string():
    assert kalshi_fill({"fill_count": "1", "average_fill_price": "0.55"}) == (1.0, 0.55)


def test_kalshi_fill_nested_order_cents():
    assert kalshi_fill({"order": {"fill_count": 1, "average_fill_price": 55}}) == (1.0, 0.55)


def test_kalshi_fill_no_fill_no_price():
    assert kalshi_fill({"fill_count": 0}) == (0.0, None)


# ── poly_fill ───────────────────────────────────────────────────

def test_poly_fill_quantity_and_price_dict():
    q, px = poly_fill({"order": {"filledQuantity": "1", "avgFillPrice": {"value": "0.44"}}})
    assert (q, px) == (1.0, 0.44)


def test_poly_fill_terminal_status_is_zero():
    q, _ = poly_fill({"order": {"status": "ORDER_STATUS_CANCELED", "quantity": 1}})
    assert q == 0.0


def test_poly_fill_filled_status_uses_quantity():
    q, _ = poly_fill({"order": {"status": "ORDER_STATUS_FILLED", "quantity": "1"}})
    assert q == 1.0


def test_poly_fill_partial_fill_status_is_inconclusive():
    q, _ = poly_fill({"order": {"status": "ORDER_STATUS_PARTIALLY_FILLED", "quantity": "1"}})
    assert q is None


def test_poly_fill_unknown_shape_returns_none():
    assert poly_fill({"orderId": "abc"}) == (None, None)


# verified live 2026-07-12: order-create returns {"id": ..., "executions": [...]}
# and executions arrive ASYNC — an empty list can accompany a real fill.

def test_poly_fill_empty_executions_is_ambiguous_not_nofill():
    assert poly_fill({"id": "B69KYARP471G", "executions": []}) == (None, None)


def test_poly_fill_executions_with_fill():
    q, px = poly_fill({"id": "x", "executions": [
        {"quantity": "1", "price": {"value": "0.44"}}]})
    assert (q, px) == (1.0, 0.44)


def test_poly_fill_executions_multi_weighted_price():
    q, px = poly_fill({"id": "x", "executions": [
        {"quantity": "1", "price": {"value": "0.40"}},
        {"quantity": "1", "price": {"value": "0.50"}}]})
    assert q == 2.0 and abs(px - 0.45) < 1e-9
