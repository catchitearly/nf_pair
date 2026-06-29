"""
fetcher.py
Handles all Fyers API interactions:
  - ATM strike detection at 9:15 AM
  - Symbol list construction
  - Historical 5-min candle fetch (backtest)
  - Live quote polling (live mode)
"""

import datetime
import time
import logging
from typing import Dict, List, Optional, Tuple

from fyers_apiv3 import fyersModel

import config

logger = logging.getLogger(__name__)


# ── module-level state ────────────────────────────────────────────────────────
fyers: Optional[fyersModel.FyersModel] = None

# { symbol: [ {t, o, h, l, c, v}, ... ] }  – sorted by time ascending
candle_store: Dict[str, List[dict]] = {}

atm_strike:   Optional[int]  = None
ce_symbols:   List[str]      = []
pe_symbols:   List[str]      = []
_mode:        str             = "backtest"   # "backtest" | "live"


# ── init ──────────────────────────────────────────────────────────────────────

def init(mode: str = "backtest") -> None:
    global fyers, _mode
    _mode = mode
    fyers = fyersModel.FyersModel(
        client_id=config.CLIENT_ID,
        token=config.ACCESS_TOKEN,
        log_path=""
    )
    logger.info("Fyers client initialised | mode=%s", mode)


# ── ATM detection ─────────────────────────────────────────────────────────────

def fetch_atm_strike(date: Optional[datetime.date] = None) -> int:
    """
    Return ATM strike = Nifty spot at 9:15 AM rounded to nearest STRIKE_STEP.
    Always uses IST timezone for epoch conversion.
    """
    global atm_strike

    if _mode == "backtest" and date:
        dt_str = date.strftime("%Y-%m-%d")
        logger.info("Fetching spot | symbol=%s date=%s", config.INDEX_SYMBOL, dt_str)

        data = fyers.history({
            "symbol":      config.INDEX_SYMBOL,
            "resolution":  "1",
            "date_format": "1",
            "range_from":  dt_str,
            "range_to":    dt_str,
            "cont_flag":   "1"
        })

        logger.info("Spot response keys=%s candle_count=%s",
                    list(data.keys()), len(data.get("candles", [])))

        candles = data.get("candles", [])
        if not candles:
            # Fallback: try with 5-min resolution
            logger.warning("No 1-min spot candle, retrying with 5-min resolution…")
            data = fyers.history({
                "symbol":      config.INDEX_SYMBOL,
                "resolution":  "5",
                "date_format": "1",
                "range_from":  dt_str,
                "range_to":    dt_str,
                "cont_flag":   "1"
            })
            logger.info("5-min spot response: %s", data)
            candles = data.get("candles", [])

        if not candles:
            raise ValueError(
                f"No spot data returned for {date} at 09:15 IST.\n"
                f"Last API response: {data}\n"
                f"Check: (1) ACCESS_TOKEN is valid, (2) {date} was a trading day, "
                f"(3) INDEX_SYMBOL '{config.INDEX_SYMBOL}' is correct."
            )

        spot = candles[0][1]   # open of first candle
    else:
        resp = fyers.quotes({"symbols": config.INDEX_SYMBOL})
        logger.info("Live quote response: %s", resp)
        spot = resp["d"][0]["v"]["lp"]

    strike = _round_to_step(spot, config.STRIKE_STEP)
    atm_strike = strike
    logger.info("Spot=%.2f  ATM=%d", spot, strike)
    return strike


# ── symbol construction ───────────────────────────────────────────────────────

def build_symbols(strike: int, expiry: Optional[str] = None) -> Tuple[List[str], List[str]]:
    """
    Build CE and PE symbol lists for ATM ± STRIKE_RANGE.

    Fyers weekly Nifty option format:  NSE:NIFTY26JUN24000CE
      = "NSE:NIFTY" + expiry_str + strike + "CE/PE"
    
    expiry_str accepted formats (passed via --expiry or config.EXPIRY_DATE):
      "26JUN"          →  used as-is  (weekly, current year implied by Fyers)
      "26JUN25"        →  used as-is
      "26-JUN-2025"    →  reformatted to "26JUN25"
    """
    global ce_symbols, pe_symbols

    exp_str = _normalise_expiry(expiry or config.EXPIRY_DATE)
    strikes = range(
        strike - config.STRIKE_RANGE,
        strike + config.STRIKE_RANGE + config.STRIKE_STEP,
        config.STRIKE_STEP
    )
    ce_syms = [f"NSE:NIFTY{exp_str}{s}CE" for s in strikes]
    pe_syms = [f"NSE:NIFTY{exp_str}{s}PE" for s in strikes]

    ce_symbols = ce_syms
    pe_symbols = pe_syms
    logger.info("Built %d CE + %d PE symbols | expiry=%s | sample: %s",
                len(ce_syms), len(pe_syms), exp_str, ce_syms[len(ce_syms)//2])
    return ce_syms, pe_syms


# ── historical fetch (backtest) ───────────────────────────────────────────────

def fetch_historical(date: datetime.date) -> None:
    """
    Fetch 5-min OHLCV for all CE+PE symbols for a given date.
    Populates candle_store.
    """
    global candle_store
    candle_store = {}

    dt_str = date.strftime("%Y-%m-%d")

    all_syms = ce_symbols + pe_symbols
    total    = len(all_syms)

    for i, sym in enumerate(all_syms, 1):
        logger.info("Fetching %d/%d  %s", i, total, sym)
        try:
            data = fyers.history({
                "symbol":      sym,
                "resolution":  config.CANDLE_RESOLUTION,
                "date_format": "1",
                "range_from":  dt_str,
                "range_to":    dt_str,
                "cont_flag":   "1"
            })
            candles = data.get("candles", [])
            if not candles:
                logger.warning("Empty candles for %s | response: %s", sym, data)
            candle_store[sym] = [
                {"t": int(c[0]), "o": c[1], "h": c[2], "l": c[3], "c": c[4], "v": c[5]}
                for c in candles
            ]
        except Exception as exc:
            logger.warning("Failed to fetch %s: %s", sym, exc)
            candle_store[sym] = []

        time.sleep(0.12)   # stay within Fyers rate limit (~10 req/s)

    non_empty = sum(1 for v in candle_store.values() if v)
    logger.info("Historical fetch complete | %d/%d symbols have data", non_empty, total)


# ── live polling ──────────────────────────────────────────────────────────────

def append_live_candles() -> None:
    today = datetime.date.today()
    fetch_historical(today)
    logger.info("Live candle refresh complete")


# ── helpers ───────────────────────────────────────────────────────────────────


def _round_to_step(value: float, step: int) -> int:
    return int(round(value / step) * step)


def _normalise_expiry(raw: str) -> str:
    """
    Accept multiple expiry string formats and return what Fyers expects.

    Fyers weekly Nifty:  "26JUN"  or  "26JUN25"
    We pass it through if it already looks right; reformat if it's long-form.
    """
    raw = raw.strip().upper()

    # Already short-form: "26JUN" or "26JUN25"
    if len(raw) in (5, 7) and raw[:2].isdigit() and raw[2:5].isalpha():
        return raw

    # Long-form: "26-JUN-2025"  →  "26JUN25"
    try:
        dt = datetime.datetime.strptime(raw, "%d-%b-%Y")
        return dt.strftime("%d%b%y").upper()
    except ValueError:
        pass

    # Fallback: return as-is and let Fyers reject it with a clear message
    logger.warning("Unrecognised expiry format '%s' — passing through unchanged", raw)
    return raw
