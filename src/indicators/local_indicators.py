"""Local technical indicator computation from OHLCV candle data.

Replaces external TAAPI dependency by computing indicators directly from
Hyperliquid candle snapshots. All functions accept lists of candle dicts
with keys: open, high, low, close, volume.

Fixes vs original:
- macd(): signal line is now correctly index-aligned with the MACD series
  by mapping signal EMA output back onto the original index positions.
- adx(): removed O(n^2) list.insert(0,...) loop; uses correct prepend logic.
"""

from __future__ import annotations
import math


def _closes(candles: list[dict]) -> list[float]:
    return [c["close"] for c in candles]


def _highs(candles: list[dict]) -> list[float]:
    return [c["high"] for c in candles]


def _lows(candles: list[dict]) -> list[float]:
    return [c["low"] for c in candles]


def _volumes(candles: list[dict]) -> list[float]:
    return [c["volume"] for c in candles]


# ---------------------------------------------------------------------------
# EMA / SMA
# ---------------------------------------------------------------------------

def sma(values: list[float], period: int) -> list[float | None]:
    """Simple moving average. Returns list same length as values."""
    result: list[float | None] = []
    for i in range(len(values)):
        if i < period - 1:
            result.append(None)
        else:
            result.append(sum(values[i - period + 1: i + 1]) / period)
    return result


def ema(values: list[float], period: int) -> list[float | None]:
    """Exponential moving average."""
    result: list[float | None] = []
    k = 2.0 / (period + 1)
    prev = None
    for i, v in enumerate(values):
        if i < period - 1:
            result.append(None)
        elif i == period - 1:
            prev = sum(values[:period]) / period
            result.append(prev)
        else:
            prev = v * k + prev * (1 - k)
            result.append(prev)
    return result


# ---------------------------------------------------------------------------
# RSI
# ---------------------------------------------------------------------------

def rsi(candles: list[dict], period: int = 14) -> list[float | None]:
    """Relative Strength Index using Wilder's smoothing."""
    closes = _closes(candles)
    if len(closes) < period + 1:
        return [None] * len(closes)

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    result: list[float | None] = [None] * period

    gains = [max(d, 0) for d in deltas[:period]]
    losses = [abs(min(d, 0)) for d in deltas[:period]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        result.append(100.0)
    else:
        rs = avg_gain / avg_loss
        result.append(round(100.0 - (100.0 / (1.0 + rs)), 4))

    for i in range(period, len(deltas)):
        gain = max(deltas[i], 0)
        loss = abs(min(deltas[i], 0))
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_loss == 0:
            result.append(100.0)
        else:
            rs = avg_gain / avg_loss
            result.append(round(100.0 - (100.0 / (1.0 + rs)), 4))

    return result


# ---------------------------------------------------------------------------
# MACD  (fixed signal-line alignment)
# ---------------------------------------------------------------------------

def macd(candles: list[dict], fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    """MACD line, signal line, and histogram.

    The signal line is the EMA of the MACD line.  The tricky part is that
    the first ``slow - 1`` elements of macd_line are None.  We compute the
    EMA only on the valid suffix, then map those values back onto their
    original positions so every series stays index-aligned with the input
    candles list.

    Returns:
        {"macd": [...], "signal": [...], "histogram": [...]}
    """
    closes = _closes(candles)
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)

    macd_line: list[float | None] = []
    for f, s in zip(ema_fast, ema_slow):
        if f is not None and s is not None:
            macd_line.append(round(f - s, 6))
        else:
            macd_line.append(None)

    # Identify the original indices where macd_line has valid values
    valid_indices = [i for i, v in enumerate(macd_line) if v is not None]

    # Pre-fill signal and histogram with None
    signal_line: list[float | None] = [None] * len(macd_line)
    histogram: list[float | None] = [None] * len(macd_line)

    if len(valid_indices) < signal:
        # Not enough data for signal EMA — return all-None signal/histogram
        return {"macd": macd_line, "signal": signal_line, "histogram": histogram}

    # Compute EMA of the valid MACD values
    valid_macd_values = [macd_line[i] for i in valid_indices]  # type: ignore[index]
    signal_ema_raw = ema(valid_macd_values, signal)

    # Map back onto original indices.
    # signal_ema_raw[j] corresponds to valid_indices[j].
    # Leading Nones from ema() are expected for j < signal-1.
    for j, orig_idx in enumerate(valid_indices):
        sig_val = signal_ema_raw[j]
        signal_line[orig_idx] = sig_val
        if sig_val is not None and macd_line[orig_idx] is not None:
            histogram[orig_idx] = round(macd_line[orig_idx] - sig_val, 6)  # type: ignore[operator]

    return {"macd": macd_line, "signal": signal_line, "histogram": histogram}


# ---------------------------------------------------------------------------
# ATR
# ---------------------------------------------------------------------------

def atr(candles: list[dict], period: int = 14) -> list[float | None]:
    """Average True Range."""
    if len(candles) < 2:
        return [None] * len(candles)

    true_ranges: list[float] = []
    for i in range(1, len(candles)):
        h = candles[i]["high"]
        l = candles[i]["low"]
        prev_c = candles[i - 1]["close"]
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        true_ranges.append(tr)

    if len(true_ranges) < period:
        return [None] * len(candles)

    result: list[float | None] = [None] * period  # first period values undefined

    avg = sum(true_ranges[:period]) / period
    result.append(round(avg, 6))

    for i in range(period, len(true_ranges)):
        avg = (avg * (period - 1) + true_ranges[i]) / period
        result.append(round(avg, 6))

    return result


# ---------------------------------------------------------------------------
# Bollinger Bands
# ---------------------------------------------------------------------------

def bbands(candles: list[dict], period: int = 20, std_dev: float = 2.0) -> dict:
    """Bollinger Bands: upper, middle (SMA), lower."""
    closes = _closes(candles)
    middle = sma(closes, period)
    upper: list[float | None] = []
    lower: list[float | None] = []

    for i in range(len(closes)):
        if middle[i] is None:
            upper.append(None)
            lower.append(None)
        else:
            window = closes[i - period + 1: i + 1]
            mean = middle[i]
            variance = sum((x - mean) ** 2 for x in window) / period
            sd = math.sqrt(variance)
            upper.append(round(mean + std_dev * sd, 6))
            lower.append(round(mean - std_dev * sd, 6))

    return {"upper": upper, "middle": middle, "lower": lower}


# ---------------------------------------------------------------------------
# Stochastic RSI
# ---------------------------------------------------------------------------

def stoch_rsi(
    candles: list[dict],
    rsi_period: int = 14,
    stoch_period: int = 14,
    k_smooth: int = 3,
    d_smooth: int = 3,
) -> dict:
    """Stochastic RSI returning %K and %D lines."""
    rsi_vals = rsi(candles, rsi_period)
    valid_rsi = [v for v in rsi_vals if v is not None]

    stoch_k_raw: list[float | None] = []
    for i in range(len(valid_rsi)):
        if i < stoch_period - 1:
            stoch_k_raw.append(None)
        else:
            window = valid_rsi[i - stoch_period + 1: i + 1]
            lo = min(window)
            hi = max(window)
            if hi == lo:
                stoch_k_raw.append(50.0)
            else:
                stoch_k_raw.append(round((valid_rsi[i] - lo) / (hi - lo) * 100, 4))

    valid_k = [v for v in stoch_k_raw if v is not None]
    k_line = sma(valid_k, k_smooth) if len(valid_k) >= k_smooth else [None] * len(valid_k)
    valid_k_smoothed = [v for v in k_line if v is not None]
    d_line = (
        sma(valid_k_smoothed, d_smooth)
        if len(valid_k_smoothed) >= d_smooth
        else [None] * len(valid_k_smoothed)
    )

    pad = len(rsi_vals) - len(valid_rsi)
    k_prefix_nones = pad + (len(valid_rsi) - len(valid_k)) + (len(valid_k) - len(k_line))
    full_k: list[float | None] = [None] * k_prefix_nones + list(k_line)
    full_d: list[float | None] = [None] * (len(rsi_vals) - len(d_line)) + list(d_line)

    return {"k": full_k, "d": full_d}


# ---------------------------------------------------------------------------
# ADX  (fixed: no O(n^2) insert loop, correct padding)
# ---------------------------------------------------------------------------

def adx(candles: list[dict], period: int = 14) -> list[float | None]:
    """Average Directional Index.

    Fixed vs original:
    - Padding is computed once as a simple prepend, not via an O(n^2) insert loop.
    - The leading-None count is derived from the actual output length, not a
      heuristic period*2 guess.
    """
    n = len(candles)
    if n < period + 1:
        return [None] * n

    plus_dm_list: list[float] = []
    minus_dm_list: list[float] = []
    tr_list: list[float] = []

    for i in range(1, n):
        h = candles[i]["high"]
        l = candles[i]["low"]
        prev_h = candles[i - 1]["high"]
        prev_l = candles[i - 1]["low"]
        prev_c = candles[i - 1]["close"]

        up_move = h - prev_h
        down_move = prev_l - l

        plus_dm = max(up_move, 0.0) if up_move > down_move else 0.0
        minus_dm = max(down_move, 0.0) if down_move > up_move else 0.0
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))

        plus_dm_list.append(plus_dm)
        minus_dm_list.append(minus_dm)
        tr_list.append(tr)

    if len(tr_list) < period:
        return [None] * n

    # Wilder smoothing seed values
    atr_val = sum(tr_list[:period])
    plus_dm_smooth = sum(plus_dm_list[:period])
    minus_dm_smooth = sum(minus_dm_list[:period])

    dx_list: list[float] = []

    def _dx(p_dm: float, m_dm: float, a: float) -> float:
        if a == 0:
            return 0.0
        p_di = (p_dm / a) * 100
        m_di = (m_dm / a) * 100
        di_sum = p_di + m_di
        return abs(p_di - m_di) / di_sum * 100 if di_sum else 0.0

    dx_list.append(_dx(plus_dm_smooth, minus_dm_smooth, atr_val))

    for i in range(period, len(tr_list)):
        atr_val = atr_val - (atr_val / period) + tr_list[i]
        plus_dm_smooth = plus_dm_smooth - (plus_dm_smooth / period) + plus_dm_list[i]
        minus_dm_smooth = minus_dm_smooth - (minus_dm_smooth / period) + minus_dm_list[i]
        dx_list.append(_dx(plus_dm_smooth, minus_dm_smooth, atr_val))

    # ADX is Wilder-smoothed DX, needs at least `period` DX values
    adx_values: list[float] = []
    if len(dx_list) >= period:
        adx_val = sum(dx_list[:period]) / period
        adx_values.append(round(adx_val, 4))
        for i in range(period, len(dx_list)):
            adx_val = (adx_val * (period - 1) + dx_list[i]) / period
            adx_values.append(round(adx_val, 4))

    # Pad front with Nones so len(result) == n
    pad_count = n - len(adx_values)
    return [None] * pad_count + adx_values


# ---------------------------------------------------------------------------
# OBV
# ---------------------------------------------------------------------------

def obv(candles: list[dict]) -> list[float]:
    """On-Balance Volume."""
    closes = _closes(candles)
    volumes = _volumes(candles)
    result = [0.0]
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            result.append(result[-1] + volumes[i])
        elif closes[i] < closes[i - 1]:
            result.append(result[-1] - volumes[i])
        else:
            result.append(result[-1])
    return result


# ---------------------------------------------------------------------------
# VWAP
# ---------------------------------------------------------------------------

def vwap(candles: list[dict]) -> list[float | None]:
    """Cumulative VWAP (resets not implemented — suitable for intraday)."""
    cum_vol = 0.0
    cum_tp_vol = 0.0
    result: list[float | None] = []
    for c in candles:
        tp = (c["high"] + c["low"] + c["close"]) / 3.0
        cum_vol += c["volume"]
        cum_tp_vol += tp * c["volume"]
        if cum_vol > 0:
            result.append(round(cum_tp_vol / cum_vol, 6))
        else:
            result.append(None)
    return result


# ---------------------------------------------------------------------------
# High-level helper: compute all standard indicators
# ---------------------------------------------------------------------------

_MIN_CANDLES_WARN = 52   # Need at least slow EMA period (26) + signal (9) + margin
_MIN_CANDLES_ABORT = 10  # Below this, indicators are meaningless


def compute_all(candles: list[dict]) -> dict:
    """Compute a standard suite of indicators from candle data.

    Returns an empty dict if there are fewer than _MIN_CANDLES_ABORT candles.
    Logs a warning if below _MIN_CANDLES_WARN.
    """
    if not candles:
        return {}

    n = len(candles)
    if n < _MIN_CANDLES_ABORT:
        import logging
        logging.warning(
            "compute_all: only %d candles — indicators unreliable; returning empty", n
        )
        return {}
    if n < _MIN_CANDLES_WARN:
        import logging
        logging.warning(
            "compute_all: %d candles is below recommended minimum of %d",
            n, _MIN_CANDLES_WARN,
        )

    closes = _closes(candles)
    ema20_series = ema(closes, 20)
    ema50_series = ema(closes, 50)
    rsi7_series = rsi(candles, 7)
    rsi14_series = rsi(candles, 14)
    macd_data = macd(candles)
    atr3_series = atr(candles, 3)
    atr14_series = atr(candles, 14)
    bbands_data = bbands(candles)
    adx_series = adx(candles)
    obv_series = obv(candles)
    vwap_series = vwap(candles)

    return {
        "ema20": ema20_series,
        "ema50": ema50_series,
        "rsi7": rsi7_series,
        "rsi14": rsi14_series,
        "macd": macd_data["macd"],
        "macd_signal": macd_data["signal"],
        "macd_histogram": macd_data["histogram"],
        "atr3": atr3_series,
        "atr14": atr14_series,
        "bbands_upper": bbands_data["upper"],
        "bbands_middle": bbands_data["middle"],
        "bbands_lower": bbands_data["lower"],
        "adx": adx_series,
        "obv": obv_series,
        "vwap": vwap_series,
    }


def last_n(series: list, n: int = 10) -> list:
    """Return the last ``n`` non-None values from a series."""
    valid = [v for v in series if v is not None]
    return valid[-n:]


def latest(series: list):
    """Return the last non-None value from a series, or None."""
    for v in reversed(series):
        if v is not None:
            return v
    return None