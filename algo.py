"""Custom strategy file loaded when ENABLE_ALGO_TRADING=true.

This template includes two standardized hooks that all trader variants should
keep in algo.py:
1) calculate_confidence(...)  -> float in [0, 1]
2) confidence_to_leverage(...) -> leverage in [1, MAX_LEVERAGE]

Your strategy can change the signal logic freely, but these hooks keep the
confidence and leverage contract stable across different trader algos.
"""

from __future__ import annotations


MAX_LEVERAGE = 10.0
MIN_TRADE_CONFIDENCE = 0.58


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def calculate_confidence(asset_section: dict, direction: str) -> float:
    """Return directional confidence score in [0, 1].

    direction must be "buy" or "sell".
    """
    if not asset_section:
        return 0.0

    current_price = float(asset_section.get("current_price") or 0)
    intraday = asset_section.get("intraday") or {}
    long_term = asset_section.get("long_term") or {}

    ema20_5m = float(intraday.get("ema20") or 0)
    macd_5m = float(intraday.get("macd") or 0)
    rsi14_5m = float(intraday.get("rsi14") or 0)
    ema20_4h = float(long_term.get("ema20") or 0)
    ema50_4h = float(long_term.get("ema50") or 0)
    atr14_4h = float(long_term.get("atr14") or 0)

    if min(current_price, ema20_5m, ema20_4h, ema50_4h) <= 0:
        return 0.0

    # Structure and momentum are the core confidence drivers.
    trend_gap = (ema20_4h - ema50_4h) / current_price
    intraday_gap = (current_price - ema20_5m) / current_price
    atr_ratio = atr14_4h / current_price if atr14_4h > 0 else 0.0

    if direction == "buy":
        trend_score = _clamp(0.5 + trend_gap * 220.0, 0.0, 1.0)
        momentum_score = _clamp(0.5 + macd_5m * 12.0, 0.0, 1.0)
        bias_score = _clamp(0.5 + intraday_gap * 260.0, 0.0, 1.0)
        rsi_score = _clamp(1.0 - abs(rsi14_5m - 60.0) / 40.0, 0.0, 1.0)
    else:
        trend_score = _clamp(0.5 - trend_gap * 220.0, 0.0, 1.0)
        momentum_score = _clamp(0.5 - macd_5m * 12.0, 0.0, 1.0)
        bias_score = _clamp(0.5 - intraday_gap * 260.0, 0.0, 1.0)
        rsi_score = _clamp(1.0 - abs(rsi14_5m - 40.0) / 40.0, 0.0, 1.0)

    volatility_penalty = _clamp((atr_ratio - 0.018) * 16.0, 0.0, 0.35)
    confidence = (
        0.35 * trend_score
        + 0.30 * momentum_score
        + 0.20 * bias_score
        + 0.15 * rsi_score
        - volatility_penalty
    )
    return round(_clamp(confidence, 0.0, 1.0), 4)


def confidence_to_leverage(confidence: float, max_leverage: float = MAX_LEVERAGE) -> float:
    """Map confidence [0,1] to leverage [1,max_leverage] with conservative curvature."""
    bounded = _clamp(float(confidence), 0.0, 1.0)
    curved = bounded ** 1.6
    leverage = 1.0 + curved * (max_leverage - 1.0)
    return round(_clamp(leverage, 1.0, max_leverage), 2)


def _hold_decision(asset: str, reason: str, confidence: float = 0.0) -> dict:
    return {
        "asset": asset,
        "action": "hold",
        "allocation_usd": 0.0,
        "order_type": "market",
        "limit_price": None,
        "tp_price": None,
        "sl_price": None,
        "exit_plan": "",
        "rationale": reason,
        "confidence": round(_clamp(confidence, 0.0, 1.0), 4),
        "leverage": 1.0,
    }


def generate_trade_decisions(context):
    """Return per-asset decisions with confidence and leverage.

    context keys:
    - assets: list[str]
    - market_data: list[dict]
    - capital_budget_usd: float
    - account: dict
    - invocation: dict

    Output decision schema (superset):
    - asset, action, allocation_usd, order_type, limit_price,
      tp_price, sl_price, exit_plan, rationale, confidence, leverage
    """
    assets = context.get("assets", [])
    capital_budget = float(context.get("capital_budget_usd") or 0.0)
    sections = {
        str(s.get("asset")): s
        for s in (context.get("market_data") or [])
        if isinstance(s, dict) and s.get("asset")
    }

    draft = []
    for asset in assets:
        section = sections.get(asset)
        if not section:
            draft.append(_hold_decision(asset, "No market section available for this asset."))
            continue

        buy_conf = calculate_confidence(section, "buy")
        sell_conf = calculate_confidence(section, "sell")

        if buy_conf >= sell_conf:
            direction = "buy"
            confidence = buy_conf
        else:
            direction = "sell"
            confidence = sell_conf

        if confidence < MIN_TRADE_CONFIDENCE:
            draft.append(
                _hold_decision(
                    asset,
                    f"Confidence {confidence:.2f} is below threshold {MIN_TRADE_CONFIDENCE:.2f}",
                    confidence,
                )
            )
            continue

        current_price = float(section.get("current_price") or 0)
        atr14_4h = float((section.get("long_term") or {}).get("atr14") or 0)
        atr_proxy = atr14_4h if atr14_4h > 0 else max(current_price * 0.01, 0.01)

        if direction == "buy":
            tp_price = round(current_price + 1.5 * atr_proxy, 2)
            sl_price = round(current_price - 1.0 * atr_proxy, 2)
        else:
            tp_price = round(current_price - 1.5 * atr_proxy, 2)
            sl_price = round(current_price + 1.0 * atr_proxy, 2)

        draft.append({
            "asset": asset,
            "action": direction,
            "allocation_usd": 0.0,
            "order_type": "market",
            "limit_price": None,
            "tp_price": tp_price,
            "sl_price": sl_price,
            "exit_plan": "Exit on 5m momentum reversal or hard SL/TP.",
            "rationale": (
                f"Directional confidence={confidence:.2f}; signal passed threshold "
                f"{MIN_TRADE_CONFIDENCE:.2f}."
            ),
            "confidence": confidence,
            "leverage": confidence_to_leverage(confidence, MAX_LEVERAGE),
        })

    actionable = [d for d in draft if d.get("action") in {"buy", "sell"}]
    if actionable and capital_budget > 0:
        # Allocate more notional to higher-confidence setups.
        weights = [max(float(d.get("confidence") or 0.0) - MIN_TRADE_CONFIDENCE, 0.02) for d in actionable]
        weight_total = sum(weights)
        for idx, decision in enumerate(actionable):
            allocation = capital_budget * (weights[idx] / weight_total) if weight_total > 0 else 0.0
            decision["allocation_usd"] = round(max(allocation, 0.0), 2)

    if capital_budget <= 0:
        draft = [
            _hold_decision(
                d["asset"],
                "Capital budget is 0 for algo mode.",
                float(d.get("confidence") or 0.0),
            )
            for d in draft
        ]

    return {
        "reasoning": (
            "Confidence-filtered strategy executed. "
            f"Trades require confidence >= {MIN_TRADE_CONFIDENCE:.2f} and leverage is mapped up to {MAX_LEVERAGE:.0f}x."
        ),
        "trade_decisions": draft,
    }
