"""Shared risk math utilities used by strategies and execution guards."""

from __future__ import annotations


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def confidence_to_leverage(confidence: float, max_leverage: float) -> float:
    """Map confidence [0, 1] to leverage [1, max_leverage].

    The 1.6 power curve is intentionally conservative at medium confidence, so
    fallback rules never exceed the leverage profile used by the custom algo.
    """
    bounded = clamp(float(confidence), 0.0, 1.0)
    curved = bounded ** 1.6
    leverage = 1.0 + curved * (max_leverage - 1.0)
    return round(clamp(leverage, 1.0, max_leverage), 2)
