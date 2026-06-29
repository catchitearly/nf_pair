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




def detect_bearish_setup(last_idx: int) -> List[dict]:
    """
    Detect pairs matching the bearish confirmation setup at the current candle.

    Indexing (all values are candle CLOSE prices):
      [-2] = two candles ago  (index: last_idx - 2)
      [-1] = previous candle  (index: last_idx - 1)
      [0]  = current candle   (index: last_idx)

    All 4 conditions must hold:
      1. ema9[-2] > vwap[-2]  AND  ema9[-1] < vwap[-1]
            → EMA9 crossed DOWN through VWAP at the [-1] candle
      2. price[-1] < ema9[-1]
            → price was already below EMA9 when the cross happened
      3. ema9[-1] < vwap[-1]
            → cross is confirmed complete at [-1] (same as condition 1 rhs)
      4. price[0] < price[-1]
            → current close is lower than previous close (selling continues)

    Returns list of matching pair dicts with full context for Telegram alert.
    """
    if not _pair_cache:
        return []

    # Need at least 3 candles: [-2], [-1], [0]
    if last_idx < 2:
        logger.debug("detect_bearish_setup: not enough candles (last_idx=%d)", last_idx)
        return []

    matches = []
    idx_0  = last_idx        # [0]  current
    idx_m1 = last_idx - 1   # [-1] previous
    idx_m2 = last_idx - 2   # [-2] two ago

    for label, series in _pair_cache.items():
        n = len(series["times"])
        if n <= idx_0:
            continue   # series too short

        price  = series["price"]
        ema9   = series["ema9"]
        vwap   = series["vwap"]

        # Guard: skip if any VWAP is zero
        if vwap[idx_m2] == 0 or vwap[idx_m1] == 0:
            continue

        # Condition 1 & 3: EMA9 crossed DOWN at [-1]
        #   ema9[-2] was above vwap[-2]  →  ema9[-1] is below vwap[-1]
        ema_was_above = ema9[idx_m2] > vwap[idx_m2]
        ema_now_below = ema9[idx_m1] < vwap[idx_m1]
        if not (ema_was_above and ema_now_below):
            continue

        # Condition 2: price[-1] < ema9[-1]
        if not (price[idx_m1] < ema9[idx_m1]):
            continue

        # Condition 4: price[0] < price[-1]
        if not (price[idx_0] < price[idx_m1]):
            continue

        # All 4 passed — build result row
        pct_diff = (ema9[idx_0] - vwap[idx_0]) / vwap[idx_0] * 100 if vwap[idx_0] else 0

        matches.append({
            "label":       label,
            "ce_sym":      series["ce_sym"],
            "pe_sym":      series["pe_sym"],
            # [0] current candle
            "price_0":     round(price[idx_0],   2),
            "ema9_0":      round(ema9[idx_0],    2),
            "vwap_0":      round(vwap[idx_0],    2),
            # [-1] previous candle
            "price_m1":    round(price[idx_m1],  2),
            "ema9_m1":     round(ema9[idx_m1],   2),
            "vwap_m1":     round(vwap[idx_m1],   2),
            # [-2] two candles ago
            "ema9_m2":     round(ema9[idx_m2],   2),
            "vwap_m2":     round(vwap[idx_m2],   2),
            # summary
            "pct_diff":    round(pct_diff, 4),
            "price_drop":  round(price[idx_0] - price[idx_m1], 2),
        })

    logger.info("detect_bearish_setup: %d matches at idx=%d", len(matches), last_idx)
    return matches

def detect_bearish_setup_all() -> list:
    """
    Scan ALL candles in the session and return every instance where the
    bearish setup conditions were met, with the candle time included.
    Used for backtest Tab 3 — shows full chronological history for the day.

    Same 4 conditions as detect_bearish_setup but iterated over every idx >= 2.
    Multiple hits for the same pair at different candles are all included.

    Returns list sorted by candle epoch time (ascending — earliest first).
    Each row includes candle_time (epoch) and candle_time_str (HH:MM).
    """
    if not _pair_cache:
        return []

    all_matches = []

    for label, series in _pair_cache.items():
        n     = len(series["times"])
        price = series["price"]
        ema9  = series["ema9"]
        vwap  = series["vwap"]
        times = series["times"]

        for idx_0 in range(2, n):
            idx_m1 = idx_0 - 1
            idx_m2 = idx_0 - 2

            if vwap[idx_m2] == 0 or vwap[idx_m1] == 0:
                continue

            # Condition 1 & 3: EMA9 crossed DOWN at [-1]
            if not (ema9[idx_m2] > vwap[idx_m2] and ema9[idx_m1] < vwap[idx_m1]):
                continue

            # Condition 2: price[-1] < ema9[-1]
            if not (price[idx_m1] < ema9[idx_m1]):
                continue

            # Condition 4: price[0] < price[-1]
            if not (price[idx_0] < price[idx_m1]):
                continue

            pct_diff = (ema9[idx_0] - vwap[idx_0]) / vwap[idx_0] * 100 if vwap[idx_0] else 0

            # Format candle time as HH:MM IST
            try:
                import datetime, zoneinfo
                tz = zoneinfo.ZoneInfo("Asia/Kolkata")
            except Exception:
                import datetime
                tz = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
            dt  = datetime.datetime.fromtimestamp(times[idx_0], tz=tz)
            tstr = dt.strftime("%H:%M")

            all_matches.append({
                "label":        label,
                "candle_time":  times[idx_0],      # epoch for sorting
                "candle_str":   tstr,              # HH:MM for display
                "candle_idx":   idx_0,
                # [0] current
                "price_0":      round(price[idx_0],  2),
                "ema9_0":       round(ema9[idx_0],   2),
                "vwap_0":       round(vwap[idx_0],   2),
                # [-1] previous
                "price_m1":     round(price[idx_m1], 2),
                "ema9_m1":      round(ema9[idx_m1],  2),
                "vwap_m1":      round(vwap[idx_m1],  2),
                # summary
                "pct_diff":     round(pct_diff, 4),
                "price_drop":   round(price[idx_0] - price[idx_m1], 2),
            })

    # Sort chronologically
    all_matches.sort(key=lambda x: x["candle_time"])
    logger.info("detect_bearish_setup_all: %d total triggers across session", len(all_matches))
    return all_matches




def run_backtest(last_idx: Optional[int] = None) -> dict:
    """
    Simulate short trades on pairs qualifying the Bearish Setup + entry filter.

    Entry conditions (at candle [0]):
      - All 4 bearish setup conditions met
      - price[0] < config.BT_MAX_ENTRY_PRICE      (default 480)
      - abs(price[0] - ema9[0]) <= config.BT_EMA_PROXIMITY  (default 10)
      - No open trade already exists for this pair
      - Candle time <= 14:30 IST (no new entries after 2:30 PM)

    Exit conditions (checked on each subsequent candle):
      - SL: price[candle] >= ema9[candle]  → exit at that candle close
      - Hard close: candle time >= 15:00 IST → exit at that candle close

    Returns:
      {
        "trades":        [ trade_dict, ... ],   sorted by entry_time
        "equity_curve":  [ { "time": HH:MM, "epoch": int, "cumulative": float }, ... ]
        "summary":       { total, wins, losses, open, total_pnl }
      }
    """
    if not _pair_cache:
        return {"trades": [], "equity_curve": [], "summary": {}}

    try:
        import datetime, zoneinfo
        tz = zoneinfo.ZoneInfo("Asia/Kolkata")
    except Exception:
        import datetime
        tz = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

    max_price   = config.BT_MAX_ENTRY_PRICE
    ema_prox    = config.BT_EMA_PROXIMITY

    # Cut-off epochs: no entry after 14:30, hard close at 15:00
    def _hhmm_epoch(series_times, hh, mm):
        """Find epoch for HH:MM on the same date as the first candle."""
        if not series_times:
            return None
        base = datetime.datetime.fromtimestamp(series_times[0], tz=tz)
        target = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
        return int(target.timestamp())

    trades      = []   # list of trade dicts
    open_trades = {}   # label → trade dict (mutable, updated as we scan)

    # Determine scan range
    n_max = 0
    for series in _pair_cache.values():
        n_max = max(n_max, len(series["times"]))
    scan_end = (last_idx + 1) if last_idx is not None else n_max

    # Get cut-off epochs from first available series
    first_times = next(iter(_pair_cache.values()))["times"]
    no_entry_epoch  = _hhmm_epoch(first_times, 14, 30)
    hard_close_epoch = _hhmm_epoch(first_times, 15,  0)

    def _fmt(epoch):
        return datetime.datetime.fromtimestamp(epoch, tz=tz).strftime("%H:%M")

    # Scan candle by candle across all pairs simultaneously
    for idx in range(2, scan_end):

        for label, series in _pair_cache.items():
            n     = len(series["times"])
            if idx >= n:
                continue

            price  = series["price"]
            ema9   = series["ema9"]
            vwap   = series["vwap"]
            times  = series["times"]

            t_epoch = times[idx]

            # ── Check exit for open trade ──────────────────────────────────
            if label in open_trades:
                trade = open_trades[label]
                sl_hit    = price[idx] >= ema9[idx]
                hard_close = (hard_close_epoch and t_epoch >= hard_close_epoch)

                if sl_hit or hard_close:
                    trade["exit_price"]  = round(price[idx], 2)
                    trade["exit_time"]   = _fmt(t_epoch)
                    trade["exit_epoch"]  = t_epoch
                    trade["pnl"]         = round(trade["entry_price"] - trade["exit_price"], 2)
                    trade["status"]      = "SL" if sl_hit else "Closed@3PM"
                    del open_trades[label]

            # ── Check entry for new trade ──────────────────────────────────
            else:
                # No new entries after 14:30
                if no_entry_epoch and t_epoch > no_entry_epoch:
                    continue

                idx_m1 = idx - 1
                idx_m2 = idx - 2

                if vwap[idx_m2] == 0 or vwap[idx_m1] == 0:
                    continue

                # Bearish setup conditions
                c1 = ema9[idx_m2] > vwap[idx_m2] and ema9[idx_m1] < vwap[idx_m1]
                c2 = price[idx_m1] < ema9[idx_m1]
                c4 = price[idx] < price[idx_m1]
                if not (c1 and c2 and c4):
                    continue

                # Entry filters
                if price[idx] >= max_price:
                    continue
                if abs(price[idx] - ema9[idx]) > ema_prox:
                    continue

                trade = {
                    "label":        label,
                    "entry_price":  round(price[idx], 2),
                    "entry_time":   _fmt(t_epoch),
                    "entry_epoch":  t_epoch,
                    "entry_idx":    idx,
                    "exit_price":   None,
                    "exit_time":    None,
                    "exit_epoch":   None,
                    "pnl":          None,
                    "status":       "Open",
                    "ema9_entry":   round(ema9[idx], 2),
                    "vwap_entry":   round(vwap[idx], 2),
                }
                open_trades[label] = trade
                trades.append(trade)   # same dict — mutations reflect here

    # Any still-open trades at end of scan = "Open" (live mode) or closed
    # In live/partial-day scans they stay as Open with current P&L
    for label, trade in open_trades.items():
        series = _pair_cache.get(label)
        if series:
            last = min(scan_end - 1, len(series["times"]) - 1)
            cur_price = series["price"][last]
            trade["pnl"]    = round(trade["entry_price"] - cur_price, 2)
            trade["status"] = "Open"
            trade["exit_price"] = round(cur_price, 2)
            trade["exit_time"]  = _fmt(series["times"][last]) + "*"

    # Sort trades by entry time
    trades.sort(key=lambda x: x["entry_epoch"])

    # Build equity curve (cumulative P&L at each exit point)
    closed = [t for t in trades if t["status"] != "Open"]
    closed.sort(key=lambda x: x.get("exit_epoch") or 0)
    cum = 0.0
    equity_curve = []
    for t in closed:
        cum += t["pnl"] or 0
        equity_curve.append({
            "time":       t["exit_time"].rstrip("*"),
            "epoch":      t.get("exit_epoch", 0),
            "cumulative": round(cum, 2),
            "label":      t["label"],
            "pnl":        t["pnl"],
        })

    # Summary
    closed_pnl = [t["pnl"] for t in trades if t["status"] != "Open" and t["pnl"] is not None]
    open_pnl   = [t["pnl"] for t in trades if t["status"] == "Open"  and t["pnl"] is not None]
    summary = {
        "total":     len(trades),
        "closed":    len(closed_pnl),
        "open":      len(open_pnl),
        "wins":      sum(1 for p in closed_pnl if p > 0),
        "losses":    sum(1 for p in closed_pnl if p <= 0),
        "total_pnl": round(sum(closed_pnl) + sum(open_pnl), 2),
        "closed_pnl": round(sum(closed_pnl), 2),
    }

    logger.info("Backtest: %d trades | PnL=%.2f | W=%d L=%d Open=%d",
                summary["total"], summary["total_pnl"],
                summary["wins"], summary["losses"], summary["open"])

    return {"trades": trades, "equity_curve": equity_curve, "summary": summary}

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
