"""Hard limits, enforced in code (not advisory). Every order plan must pass
`check()` or it is rejected with LimitError before anything is placed."""
from __future__ import annotations

from dataclasses import dataclass


class LimitError(RuntimeError):
    """A proposed action would breach a hard risk limit. Reject it."""


@dataclass(frozen=True)
class Limits:
    max_contracts_per_market: int
    max_total_capital: float
    max_concurrent_arbs: int
    max_order_size: int
    min_edge: float
    max_daily_loss: float

    @classmethod
    def from_config(cls, cfg) -> "Limits":
        return cls(
            max_contracts_per_market=cfg.max_contracts_per_market,
            max_total_capital=cfg.max_total_capital,
            max_concurrent_arbs=cfg.max_concurrent_arbs,
            max_order_size=cfg.max_order_size,
            min_edge=cfg.min_edge,
            max_daily_loss=cfg.max_daily_loss,
        )


@dataclass(frozen=True)
class AccountState:
    """Snapshot used to evaluate limits at decision time."""

    open_arbs: int
    deployed_capital: float
    position_contracts: dict[tuple[str, str], float]  # (venue, market_id) -> net qty


def check(
    limits: Limits,
    *,
    edge: float,
    size: int,
    legs: list,            # iterable of LegPlan (.venue, .market_id, .buy_price)
    state: AccountState,
) -> None:
    """Raise LimitError if the proposed arb breaches any hard limit."""
    if edge < limits.min_edge:
        raise LimitError(f"edge {edge:.4f} < MIN_EDGE {limits.min_edge}")
    if size <= 0:
        raise LimitError(f"size {size} <= 0")
    if size > limits.max_order_size:
        raise LimitError(f"size {size} > MAX_ORDER_SIZE {limits.max_order_size}")
    if state.open_arbs >= limits.max_concurrent_arbs:
        raise LimitError(f"open arbs {state.open_arbs} >= MAX_CONCURRENT_ARBS {limits.max_concurrent_arbs}")

    incremental_cost = 0.0
    for leg in legs:
        key = (leg.venue, leg.market_id)
        existing = state.position_contracts.get(key, 0.0)
        if existing + size > limits.max_contracts_per_market:
            raise LimitError(
                f"{leg.venue}:{leg.market_id} position {existing}+{size} "
                f"> MAX_CONTRACTS_PER_MARKET {limits.max_contracts_per_market}")
        incremental_cost += leg.buy_price * size

    if state.deployed_capital + incremental_cost > limits.max_total_capital:
        raise LimitError(
            f"capital {state.deployed_capital:.2f}+{incremental_cost:.2f} "
            f"> MAX_TOTAL_CAPITAL {limits.max_total_capital}")
