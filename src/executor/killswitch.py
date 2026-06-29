"""Kill switch. Two independent triggers, checked before EVERY order:
  1. A sentinel file on disk (manual / external halt).
  2. Daily realized loss reaching MAX_DAILY_LOSS (automatic).
Once tripped, no new orders are allowed for the rest of the day. Optionally the
operator can also flatten open positions (handled by the executor)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("arb.killswitch")


class KillSwitch:
    def __init__(self, sentinel_path: str, max_daily_loss: float, store) -> None:
        self.sentinel_path = Path(sentinel_path)
        self.max_daily_loss = max_daily_loss
        self.store = store

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def status(self) -> tuple[bool, str]:
        """Return (tripped, reason)."""
        if self.sentinel_path.exists():
            return True, f"sentinel file present: {self.sentinel_path}"
        realized = self.store.get_today_realized(self._today())
        if realized <= -abs(self.max_daily_loss):
            return True, f"daily loss {realized:.2f} <= -MAX_DAILY_LOSS {self.max_daily_loss}"
        return False, ""

    def tripped(self) -> bool:
        return self.status()[0]

    def trip_for_loss(self) -> None:
        """Persist that the daily-loss kill switch fired today."""
        self.store.mark_kill_switch(self._today())
        log.error("KILL SWITCH tripped on daily loss")

    def engage_manual(self) -> None:
        self.sentinel_path.touch()
        log.error("KILL SWITCH engaged (sentinel %s created)", self.sentinel_path)

    def release_manual(self) -> None:
        if self.sentinel_path.exists():
            self.sentinel_path.unlink()
