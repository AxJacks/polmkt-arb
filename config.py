"""Central configuration. Values come from environment / .env only.

No secrets are ever hardcoded here. Missing *secrets* are tolerated until the
component that needs them is used (then it raises) so Phase 1 read-only work can
run with partial config. Numeric limits always have safe defaults.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Load .env if present (tiny parser; avoids a hard python-dotenv dependency).
def _load_dotenv(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        # Strip inline comments only when clearly separated.
        if "  #" in val:
            val = val.split("  #", 1)[0].strip()
        os.environ.setdefault(key, val)


_load_dotenv()


def _f(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _i(name: str, default: int) -> int:
    return int(float(os.environ.get(name, default)))


def _b(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _s(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Config:
    # ── Kalshi ──────────────────────────────────────────────
    kalshi_key_id: str = field(default_factory=lambda: _s("KALSHI_API_KEY_ID"))
    kalshi_private_key_path: str = field(default_factory=lambda: _s("KALSHI_PRIVATE_KEY_PATH", "./secrets/kalshi_rsa_private.pem"))
    kalshi_rest_base: str = field(default_factory=lambda: _s("KALSHI_REST_BASE", "https://external-api.kalshi.com/trade-api/v2"))
    kalshi_ws_url: str = field(default_factory=lambda: _s("KALSHI_WS_URL", "wss://external-api-ws.kalshi.com/trade-api/ws/v2"))

    # ── Polymarket US ───────────────────────────────────────
    polyus_key_id: str = field(default_factory=lambda: _s("POLYUS_KEY_ID"))
    polyus_secret_key: str = field(default_factory=lambda: _s("POLYUS_SECRET_KEY"))
    polyus_rest_base: str = field(default_factory=lambda: _s("POLYUS_REST_BASE", "https://api.polymarket.us/v1"))
    polyus_ws_url: str = field(default_factory=lambda: _s("POLYUS_WS_URL", "wss://api.polymarket.us/v1/ws/markets"))

    # ── Anthropic suggester ─────────────────────────────────
    anthropic_api_key: str = field(default_factory=lambda: _s("ANTHROPIC_API_KEY"))
    suggester_model: str = field(default_factory=lambda: _s("SUGGESTER_MODEL", "claude-haiku-4-5"))

    # ── Detector / edge ─────────────────────────────────────
    min_edge: float = field(default_factory=lambda: _f("MIN_EDGE", 0.02))
    # Edges above this are treated as data errors (stale/inverted odds, bad
    # auto-match), not opportunities — not actionable, not alerted.
    max_plausible_edge: float = field(default_factory=lambda: _f("MAX_PLAUSIBLE_EDGE", 0.10))
    min_size: int = field(default_factory=lambda: _i("MIN_SIZE", 10))
    slippage_buffer: float = field(default_factory=lambda: _f("SLIPPAGE_BUFFER", 0.005))
    # Re-alert the same opportunity at most once per this many seconds.
    alert_cooldown_s: int = field(default_factory=lambda: _i("ALERT_COOLDOWN_S", 900))
    # NOTE: staleness is measured from the last WS book message. Illiquid books
    # only push on change, so a quiet (not dead) feed can look stale — hence 30s.
    # Liquid markets (where arbs occur) update sub-second. A proper fix is
    # connection-heartbeat liveness; tracked as a refinement.
    staleness_ms: int = field(default_factory=lambda: _i("STALENESS_MS", 30000))
    settlement_window_s: int = field(default_factory=lambda: _i("SETTLEMENT_WINDOW_S", 3600))
    edge_poll_ms: int = field(default_factory=lambda: _i("EDGE_POLL_MS", 500))

    # ── Alerts ──────────────────────────────────────────────
    recos_xlsx: str = field(default_factory=lambda: _s("RECOS_XLSX", "./recos.xlsx"))
    alert_webhook_url: str = field(default_factory=lambda: _s("ALERT_WEBHOOK_URL"))

    # ── Telegram notifications ──────────────────────────────
    telegram_bot_token: str = field(default_factory=lambda: _s("TELEGRAM_BOT_TOKEN"))
    telegram_chat_id: str = field(default_factory=lambda: _s("TELEGRAM_CHAT_ID"))
    telegram_alert_edges: bool = field(default_factory=lambda: _b("TELEGRAM_ALERT_EDGES", False))

    # ── Dashboard (public access via token) ─────────────────
    dashboard_token: str = field(default_factory=lambda: _s("DASHBOARD_TOKEN"))
    dashboard_public_url: str = field(default_factory=lambda: _s("DASHBOARD_PUBLIC_URL"))

    # ── Files ───────────────────────────────────────────────
    db_path: str = field(default_factory=lambda: _s("DB_PATH", "./arb.db"))
    log_path: str = field(default_factory=lambda: _s("LOG_PATH", "./arb.log"))
    mappings_path: str = field(default_factory=lambda: _s("MAPPINGS_PATH", "./mappings.yaml"))
    candidates_path: str = field(default_factory=lambda: _s("CANDIDATES_PATH", "./candidates.yaml"))

    # ── Phase 2 executor (gated; unused in Phase 1) ─────────
    armed: bool = field(default_factory=lambda: _b("ARMED", False))
    mode: str = field(default_factory=lambda: _s("MODE", "paper"))
    max_contracts_per_market: int = field(default_factory=lambda: _i("MAX_CONTRACTS_PER_MARKET", 20))
    max_total_capital: float = field(default_factory=lambda: _f("MAX_TOTAL_CAPITAL", 100))
    max_concurrent_arbs: int = field(default_factory=lambda: _i("MAX_CONCURRENT_ARBS", 2))
    max_order_size: int = field(default_factory=lambda: _i("MAX_ORDER_SIZE", 10))
    max_daily_loss: float = field(default_factory=lambda: _f("MAX_DAILY_LOSS", 15))
    leg2_timeout_ms: int = field(default_factory=lambda: _i("LEG2_TIMEOUT_MS", 1500))
    kill_switch_file: str = field(default_factory=lambda: _s("KILL_SWITCH_FILE", "./HALT"))


def load_config() -> Config:
    return Config()
