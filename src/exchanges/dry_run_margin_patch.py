"""
src/exchanges/dry_run_margin_patch.py

PATCH for NEW-001: DryRunBroker Never Deducts Margin From Cash

This file shows the EXACT changes to make in src/exchanges/dry_run.py.
Apply these by editing dry_run.py directly.

PROBLEM: _open_market() records a position but never deducts margin from cash.
As a result, paper trading always shows full initial balance regardless of
how many positions are open. Paper testing results are MISLEADING — a strategy
that opens 5 simultaneous positions will appear viable in dry-run but fail
in live trading when the 2nd or 3rd position fails the balance reserve check.

IMPACT: Traders who paper-test and then go live will encounter unexpected
"insufficient balance" failures. Support burden. Misleading backtesting.
"""

# ─────────────────────────────────────────────────────────────────────────────
# BEFORE (broken) — _open_market in DryRunBroker
# ─────────────────────────────────────────────────────────────────────────────

BEFORE_OPEN_MARKET = '''
def _open_market(self, asset: str, is_buy: bool, size: float, px: float) -> dict:
    """Record a virtual market order fill."""
    self.state["positions"][asset] = {
        "szi":     size if is_buy else -size,
        "entryPx": px,
        "leverage": {"type": "cross", "value": self.state["leverage"].get(asset, 1)},
        "unrealizedPnl": "0",
        "returnOnEquity": "0",
    }
    self.state["fill_history"].append({
        "asset": asset, "px": px, "sz": size,
        "side": "buy" if is_buy else "sell",
        "time": int(time.time() * 1000),
    })
    return {"status": "ok", "filled": True, "px": px, "sz": size}
'''

# ─────────────────────────────────────────────────────────────────────────────
# AFTER (fixed) — _open_market in DryRunBroker
# ─────────────────────────────────────────────────────────────────────────────

AFTER_OPEN_MARKET = '''
def _open_market(self, asset: str, is_buy: bool, size: float, px: float) -> dict:
    """Record a virtual market order fill with realistic margin deduction."""
    leverage = float(self.state["leverage"].get(asset, 1) or 1)

    # ── MARGIN-001 FIX: Deduct required margin from virtual cash ─────────────
    # margin_required = position_notional / leverage
    # This mirrors how a real exchange works: only margin is debited, not notional.
    margin_required = (size * px) / max(leverage, 1.0)
    current_cash    = float(self.state["cash"])

    if current_cash < margin_required:
        # Simulate exchange rejection — insufficient margin
        logging.warning(
            "DryRun: Insufficient margin for %s. "
            "Required: %.4f, Available: %.4f",
            asset, margin_required, current_cash,
        )
        return {
            "status":  "error",
            "filled":  False,
            "error":   f"Insufficient margin: need {margin_required:.4f}, have {current_cash:.4f}",
        }

    self.state["cash"] = current_cash - margin_required

    self.state["positions"][asset] = {
        "szi":              size if is_buy else -size,
        "entryPx":          px,
        "leverage":         {"type": "cross", "value": leverage},
        "unrealizedPnl":    "0",
        "returnOnEquity":   "0",
        # ── Store margin for accurate release on close ──────────────────────
        "_margin_posted":   margin_required,
    }
    self.state["fill_history"].append({
        "asset": asset, "px": px, "sz": size,
        "side":  "buy" if is_buy else "sell",
        "time":  int(time.time() * 1000),
    })
    return {"status": "ok", "filled": True, "px": px, "sz": size}
'''

# ─────────────────────────────────────────────────────────────────────────────
# BEFORE (broken) — _close_position in DryRunBroker
# ─────────────────────────────────────────────────────────────────────────────

BEFORE_CLOSE_POSITION = '''
def _close_position(self, asset: str, close_px: float) -> dict:
    """Record a virtual position close."""
    pos  = self.state["positions"].get(asset)
    if not pos:
        return {"status": "error", "error": "no position"}

    szi        = float(pos["szi"])
    entry_px   = float(pos["entryPx"])
    is_long    = szi > 0
    size       = abs(szi)
    pnl        = (close_px - entry_px) * size * (1 if is_long else -1)

    self.state["cash"] = float(self.state["cash"]) + pnl
    del self.state["positions"][asset]
    return {"status": "ok", "filled": True, "pnl": pnl, "px": close_px}
'''

# ─────────────────────────────────────────────────────────────────────────────
# AFTER (fixed) — _close_position in DryRunBroker
# ─────────────────────────────────────────────────────────────────────────────

AFTER_CLOSE_POSITION = '''
def _close_position(self, asset: str, close_px: float) -> dict:
    """Record a virtual position close with correct margin release."""
    pos = self.state["positions"].get(asset)
    if not pos:
        return {"status": "error", "error": "no position"}

    szi      = float(pos["szi"])
    entry_px = float(pos["entryPx"])
    is_long  = szi > 0
    size     = abs(szi)

    # ── Realized PnL calculation ─────────────────────────────────────────────
    realized_pnl = (close_px - entry_px) * size * (1 if is_long else -1)

    # ── MARGIN-001 FIX: Return posted margin + realized PnL to cash ──────────
    # Previously: only PnL was added back, margin was never returned.
    # Correctly: margin_posted + pnl is returned (pnl can be negative).
    margin_posted = float(pos.get("_margin_posted", 0.0))

    # If margin_posted is 0 (positions opened before this fix was applied),
    # fall back to recomputing it from leverage.
    if margin_posted == 0.0:
        leverage      = float(pos.get("leverage", {}).get("value", 1) or 1)
        margin_posted = (size * entry_px) / max(leverage, 1.0)

    self.state["cash"] = float(self.state["cash"]) + margin_posted + realized_pnl

    del self.state["positions"][asset]

    self.state["fill_history"].append({
        "asset":        asset,
        "px":           close_px,
        "sz":           size,
        "side":         "sell" if is_long else "buy",
        "time":         int(time.time() * 1000),
        "realized_pnl": realized_pnl,
        "type":         "close",
    })

    return {
        "status":  "ok",
        "filled":  True,
        "pnl":     realized_pnl,
        "px":      close_px,
    }
'''

# ─────────────────────────────────────────────────────────────────────────────
# ALSO FIX: get_user_state — expose correct account_value
# ─────────────────────────────────────────────────────────────────────────────

AFTER_GET_USER_STATE = '''
def get_user_state(self, open_orders: list | None = None) -> dict:
    """
    Return simulated account state with correct margin accounting.

    account_value = free_cash + sum(margin_posted for open positions) + unrealized_pnl
    balance       = free_cash  (available for new margin)
    """
    positions      = []
    total_upnl     = 0.0
    total_margin   = 0.0
    current_prices = {}  # populated by _trigger_simulation_if_needed

    for asset, pos in self.state["positions"].items():
        mark_px = float(self.state.get("mark_prices", {}).get(asset, pos["entryPx"]))
        szi     = float(pos["szi"])
        is_long = szi > 0
        size    = abs(szi)
        upnl    = (mark_px - float(pos["entryPx"])) * size * (1 if is_long else -1)
        margin  = float(pos.get("_margin_posted", 0.0))

        total_upnl   += upnl
        total_margin += margin

        positions.append({
            "coin":          asset,
            "szi":           str(szi),
            "entryPx":       pos["entryPx"],
            "unrealizedPnl": str(upnl),
            "leverage":      pos.get("leverage", {"type": "cross", "value": 1}),
            "liquidationPx": self._compute_liquidation_px(pos, mark_px),
        })

    free_cash     = float(self.state["cash"])
    account_value = free_cash + total_margin + total_upnl

    return {
        "balance":             free_cash,     # available for new positions
        "account_value":       account_value,  # total equity
        "unrealized_pnl":      total_upnl,
        "positions":           positions,
        "pending_entry_orders": [],
        "open_orders":         self.state.get("open_orders", []),
    }

def _compute_liquidation_px(self, pos: dict, mark_px: float) -> str:
    """Estimate liquidation price for display purposes."""
    try:
        szi      = float(pos["szi"])
        entry_px = float(pos["entryPx"])
        leverage = float(pos.get("leverage", {}).get("value", 1) or 1)
        is_long  = szi > 0
        # Simplified: liquidation when margin is exhausted
        margin_ratio = 1.0 / max(leverage, 1.0)
        if is_long:
            liq_px = entry_px * (1 - margin_ratio + 0.005)  # 0.5% maintenance
        else:
            liq_px = entry_px * (1 + margin_ratio - 0.005)
        return f"{liq_px:.4f}"
    except Exception:
        return "0"
'''

# ─────────────────────────────────────────────────────────────────────────────
# Test to verify the fix works
# ─────────────────────────────────────────────────────────────────────────────

TEST_MARGIN_ACCOUNTING = '''
# tests/test_dry_run_margin.py

import pytest

def test_margin_deducted_on_open(dry_run_broker):
    """Cash must decrease by margin when a position is opened."""
    initial_cash = 10_000.0
    dry_run_broker.state["cash"] = initial_cash
    dry_run_broker.state["leverage"]["BTC"] = 5.0

    price = 50_000.0
    size  = 0.01   # notional = $500, margin @ 5x = $100

    result = dry_run_broker._open_market("BTC", is_buy=True, size=size, px=price)
    assert result["status"] == "ok"

    expected_margin = (size * price) / 5.0  # = 100
    assert abs(dry_run_broker.state["cash"] - (initial_cash - expected_margin)) < 0.01


def test_margin_returned_on_close_with_profit(dry_run_broker):
    """Cash must return margin + PnL on close."""
    dry_run_broker.state["cash"] = 9_900.0
    dry_run_broker.state["leverage"]["BTC"] = 5.0
    dry_run_broker.state["positions"]["BTC"] = {
        "szi": 0.01, "entryPx": 50_000.0,
        "leverage": {"type": "cross", "value": 5},
        "_margin_posted": 100.0,
    }

    close_px = 51_000.0
    result = dry_run_broker._close_position("BTC", close_px)
    assert result["status"] == "ok"

    expected_pnl  = (51_000 - 50_000) * 0.01  # = 10
    expected_cash = 9_900.0 + 100.0 + 10.0   # = 10_010
    assert abs(dry_run_broker.state["cash"] - expected_cash) < 0.01


def test_insufficient_margin_rejected(dry_run_broker):
    """Opening position requiring more margin than available must be rejected."""
    dry_run_broker.state["cash"] = 50.0
    dry_run_broker.state["leverage"]["BTC"] = 2.0

    result = dry_run_broker._open_market(
        "BTC", is_buy=True, size=0.01, px=50_000.0
    )
    # margin_required = (0.01 * 50_000) / 2 = 250 > 50 available
    assert result["status"] == "error"
    assert "Insufficient margin" in result["error"]
    assert dry_run_broker.state["cash"] == 50.0  # unchanged


def test_five_simultaneous_positions_deplete_cash(dry_run_broker):
    """
    Regression test for NEW-001: with old code, 5 positions would all show
    cash = 10,000. With fix, cash should be reduced by each margin.
    """
    dry_run_broker.state["cash"] = 10_000.0
    assets = ["BTC", "ETH", "SOL", "BNB", "ARB"]
    for asset in assets:
        dry_run_broker.state["leverage"][asset] = 3.0
        dry_run_broker._open_market(asset, True, size=0.01, px=10_000.0)
        # Each margin = (0.01 * 10_000) / 3 ≈ 33.33

    remaining_cash = dry_run_broker.state["cash"]
    total_margin   = sum(
        float(p["_margin_posted"])
        for p in dry_run_broker.state["positions"].values()
    )
    assert abs(remaining_cash + total_margin - 10_000.0) < 0.01
    # Cash should NOT still be 10_000 (that was the bug)
    assert remaining_cash < 10_000.0
'''

if __name__ == "__main__":
    print("Apply the BEFORE/AFTER patches to src/exchanges/dry_run.py")
    print("See AFTER_OPEN_MARKET, AFTER_CLOSE_POSITION, AFTER_GET_USER_STATE above.")
