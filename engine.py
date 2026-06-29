"""
engine.py
Pure computation:
  - Align CE+PE candle series
  - Compute pair price (premium sum) and pair volume
  - Compute EMA9 and VWAP
  - Build all 169 pairs
  - Rank pairs at any given candle index
"""

import math
import logging
from typing import Dict, List, Optional, Tuple

import config
import fetcher

logger = logging.getLogger(__name__)

# ── types ─────────────────────────────────────────────────────────────────────
# PairSeries: full time-series for one CE/PE pair
# {
#   "label":   "24000C/23800P",
#   "ce_sym":  "NSE:NIFTY...",
#   "pe_sym":  "NSE:NIFTY...",
#   "times":   [epoch, ...],
#   "price":   [float, ...],     # CE_close + PE_close
#   "ema9":    [float, ...],
#   "vwap":    [float, ...],
# }

_pair_cache: Dict[str, dict] = {}   # label → PairSeries


# ── public API ────────────────────────────────────────────────────────────────

def _latest_close(sym: str) -> float:
    """Return the most recent close price for a symbol, or 0 if no data."""
    candles = fetcher.candle_store.get(sym, [])
    if not candles:
        return 0.0
    return candles[-1]["c"]


def build_all_pairs() -> Dict[str, dict]:
    """
    Compute series for all valid CE×PE combinations.
    Skips any CE or PE whose latest close price is below MIN_OPTION_PRICE.
    Returns _pair_cache.
    """
    global _pair_cache
    _pair_cache = {}

    ce_syms = fetcher.ce_symbols
    pe_syms = fetcher.pe_symbols
    min_px  = config.MIN_OPTION_PRICE

    # Filter out illiquid strikes (price < min_px) before pairing
    valid_ce = [s for s in ce_syms if _latest_close(s) >= min_px]
    valid_pe = [s for s in pe_syms if _latest_close(s) >= min_px]

    skipped_ce = len(ce_syms) - len(valid_ce)
    skipped_pe = len(pe_syms) - len(valid_pe)
    logger.info(
        "Price filter (< %.0f): skipped %d CE, %d PE | valid: %d CE × %d PE = %d max pairs",
        min_px, skipped_ce, skipped_pe, len(valid_ce), len(valid_pe),
        len(valid_ce) * len(valid_pe),
    )

    for ce_sym in valid_ce:
        for pe_sym in valid_pe:
            label  = _make_label(ce_sym, pe_sym)
            series = _compute_pair(ce_sym, pe_sym, label)
            if series:
                _pair_cache[label] = series

    logger.info("Built %d pair series", len(_pair_cache))
    return _pair_cache


def ranked_pairs_at(candle_idx: int) -> Tuple[List[dict], List[dict]]:
    """
    At candle_idx return two ranked lists:
      case1 = EMA9 > VWAP  (sorted descending by rank_score)
      case2 = EMA9 < VWAP  (sorted descending by rank_score)

    Each item: { label, pct_diff, widening_score, rank_score, ema_gt_vwap }
    """
    if not _pair_cache:
        build_all_pairs()

    case1, case2 = [], []
    w = config.WIDENING_WINDOW

    for label, series in _pair_cache.items():
        n = len(series["times"])
        if candle_idx >= n or candle_idx < 1:
            continue

        idx     = min(candle_idx, n - 1)
        ema9    = series["ema9"][idx]
        vwap    = series["vwap"][idx]

        if vwap == 0:
            continue

        diff     = ema9 - vwap
        pct_diff = diff / vwap * 100
        ema_gt   = diff > 0

        # Widening: slope of abs(diff) over last `w` candles
        start = max(0, idx - w + 1)
        abs_diffs = [
            abs(series["ema9"][i] - series["vwap"][i])
            for i in range(start, idx + 1)
        ]
        widening_score = _linear_slope_normalized(abs_diffs)

        rank_score = abs(pct_diff) * (1 + config.WIDENING_BOOST * widening_score)

        # Crossover: did EMA9 flip side vs VWAP this candle?
        crossed = False
        cross_dir = None
        if idx > 0:
            prev_ema9 = series["ema9"][idx - 1]
            prev_vwap = series["vwap"][idx - 1]
            prev_gt   = prev_ema9 > prev_vwap
            if prev_gt != ema_gt:
                crossed   = True
                cross_dir = "above" if ema_gt else "below"   # crossed above = bullish

        row = {
            "label":          label,
            "ce_sym":         series["ce_sym"],
            "pe_sym":         series["pe_sym"],
            "pct_diff":       round(pct_diff, 4),
            "widening_score": round(widening_score, 4),
            "rank_score":     round(rank_score, 4),
            "ema_gt_vwap":    ema_gt,
            "crossed":        crossed,
            "cross_dir":      cross_dir,
            "price":          round(series["price"][idx], 2),
            "ema9":           round(ema9, 2),
            "vwap":           round(vwap, 2),
        }

        if ema_gt:
            case1.append(row)
        else:
            case2.append(row)

    case1.sort(key=lambda x: x["rank_score"], reverse=True)
    case2.sort(key=lambda x: x["rank_score"], reverse=True)
    return case1, case2


def get_pair_series(label: str, up_to_idx: int) -> Optional[dict]:
    """Return price/ema9/vwap series for one pair up to candle index."""
    if not _pair_cache:
        build_all_pairs()
    series = _pair_cache.get(label)
    if not series:
        return None

    n = min(up_to_idx + 1, len(series["times"]))
    return {
        "label":  label,
        "times":  series["times"][:n],
        "price":  [round(v, 2) for v in series["price"][:n]],
        "ema9":   [round(v, 2) for v in series["ema9"][:n]],
        "vwap":   [round(v, 2) for v in series["vwap"][:n]],
    }


def candle_count() -> int:
    """Return number of candles in the first available symbol (75 max for full day)."""
    for series in _pair_cache.values():
        return len(series["times"])
    return 0


# ── internal ──────────────────────────────────────────────────────────────────

def _compute_pair(ce_sym: str, pe_sym: str, label: str) -> Optional[dict]:
    ce_candles = fetcher.candle_store.get(ce_sym, [])
    pe_candles = fetcher.candle_store.get(pe_sym, [])
    if not ce_candles or not pe_candles:
        return None

    # Align by timestamp
    ce_map = {c["t"]: c for c in ce_candles}
    pe_map = {c["t"]: c for c in pe_candles}
    common_times = sorted(set(ce_map) & set(pe_map))
    if not common_times:
        return None

    prices  = []
    volumes = []
    for t in common_times:
        ce, pe = ce_map[t], pe_map[t]
        prices.append(ce["c"] + pe["c"])              # premium sum
        volumes.append(ce["v"] + pe["v"])

    ema9_series = _ema(prices, config.EMA_PERIOD)
    vwap_series = _vwap(prices, volumes)

    return {
        "label":  label,
        "ce_sym": ce_sym,
        "pe_sym": pe_sym,
        "times":  common_times,
        "price":  prices,
        "ema9":   ema9_series,
        "vwap":   vwap_series,
    }


def _ema(prices: List[float], period: int) -> List[float]:
    """Standard EMA with SMA seed."""
    if len(prices) < period:
        return prices[:]
    k      = 2 / (period + 1)
    result = [None] * len(prices)
    # Seed with SMA of first `period` values
    sma    = sum(prices[:period]) / period
    result[period - 1] = sma
    for i in range(period, len(prices)):
        result[i] = prices[i] * k + result[i - 1] * (1 - k)
    # Fill warmup with the first valid EMA value
    for i in range(period - 1):
        result[i] = result[period - 1]
    return result


def _vwap(prices: List[float], volumes: List[float]) -> List[float]:
    """
    Cumulative VWAP (resets each session).
    Using close as typical price since we work with premium sum.
    """
    cum_pv = 0.0
    cum_v  = 0.0
    result = []
    for p, v in zip(prices, volumes):
        cum_pv += p * v
        cum_v  += v
        result.append(cum_pv / cum_v if cum_v else p)
    return result


def _linear_slope_normalized(values: List[float]) -> float:
    """
    Fit a line to `values`; return slope normalised to [0, 1].
    Returns 0 if flat or declining, up to 1 for steep positive slope.
    """
    n = len(values)
    if n < 2:
        return 0.0
    xs  = list(range(n))
    x_m = sum(xs) / n
    y_m = sum(values) / n
    num = sum((x - x_m) * (y - y_m) for x, y in zip(xs, values))
    den = sum((x - x_m) ** 2 for x in xs)
    slope = (num / den) if den else 0.0
    # Normalise: clamp to [0, 1] using a scale factor based on mean
    scale = abs(y_m) if y_m else 1.0
    norm  = slope / scale
    return max(0.0, min(1.0, norm))


def _make_label(ce_sym: str, pe_sym: str) -> str:
    """'NSE:NIFTY25JUL2524000CE' → '24000C', combine → '24000C/23800P'"""
    return f"{_strike_abbr(ce_sym, 'C')}/{_strike_abbr(pe_sym, 'P')}"


def _strike_abbr(sym: str, suffix: str) -> str:
    """
    Extract strike from a Fyers option symbol.
    Works for both expiry formats:
      NSE:NIFTY26JUN24000CE   (no year, 5-char expiry)
      NSE:NIFTY26JUN2524000CE (with year, 7-char expiry)
    Strike is always the last 5 chars before CE/PE.
    """
    base = sym.split(":")[-1]   # NIFTY26JUN24000CE
    body = base[:-2]             # strip CE/PE
    strike = body[-5:]           # last 5 chars are always the strike
    return f"{strike}{suffix}"
