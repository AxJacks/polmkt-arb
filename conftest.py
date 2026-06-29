import os
import sys

import pytest

# Make repo root importable so tests can `import config` and `from src... import`.
sys.path.insert(0, os.path.dirname(__file__))


@pytest.fixture(autouse=True)
def _no_real_telegram(monkeypatch):
    """Safety net: blank the Telegram creds for EVERY test so a real Notifier
    (e.g. built from load_config()) can never send a message to a real chat."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
