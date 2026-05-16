"""Persistence helpers for bot state that must survive restarts."""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import date, datetime, timezone
from typing import Any

from src.utils.paths import data_path

ACTIVE_TRADES_FILE = "active_trades.json"
RISK_STATE_FILE = "risk_state.json"
_SAVE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Active trades
# ---------------------------------------------------------------------------

def load_active_trades() -> list[dict]:
    """Load active trades from disk. Returns empty list on missing/corrupt file."""
    active_path = data_path(ACTIVE_TRADES_FILE)
    if not active_path.exists():
        return []
    try:
        with open(active_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            logging.warning("active_trades.json has unexpected structure; resetting")
            return []
        return data
    except (json.JSONDecodeError, OSError) as exc:
        logging.error("Failed to load active_trades.json: %s — resetting", exc)
        return []


def save_active_trades(trades: list[dict]) -> None:
    """Atomically write active trades to disk.

    The existing file remains valid if the process dies before ``os.replace``;
    stale ``.tmp`` files are ignored by ``load_active_trades`` on restart.
    """
    active_path = data_path(ACTIVE_TRADES_FILE)
    tmp = active_path.with_name(
        f"{active_path.name}.tmp.{os.getpid()}.{threading.get_ident()}"
    )
    with _SAVE_LOCK:
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(trades, f, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, active_path)
        except OSError as exc:
            logging.error("Failed to save active_trades: %s", exc)
            try:
                os.remove(tmp)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Risk manager state (circuit breaker + daily high watermark)
# ---------------------------------------------------------------------------

def load_risk_state() -> dict:
    """Load persisted risk state for today. Returns empty dict if stale or missing."""
    risk_path = data_path(RISK_STATE_FILE)
    if not risk_path.exists():
        return {}
    try:
        with open(risk_path, "r", encoding="utf-8") as f:
            state = json.load(f)
        saved_date_str = state.get("date", "")
        if not saved_date_str:
            return {}
        saved_date = date.fromisoformat(saved_date_str)
        today = datetime.now(timezone.utc).date()
        if saved_date != today:
            # State is from a previous day — discard
            return {}
        return state
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        logging.error("Failed to load risk_state.json: %s — resetting", exc)
        return {}


def save_risk_state(state: dict[str, Any]) -> None:
    """Atomically write risk state to disk."""
    risk_path = data_path(RISK_STATE_FILE)
    tmp = risk_path.with_name(f"{risk_path.name}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, risk_path)
    except OSError as exc:
        logging.error("Failed to save risk_state: %s", exc)
