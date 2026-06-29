"""
run_live.py
Standalone script for GitHub Actions / cron execution.
No HTTP server, no dashboard — just:
  1. Fetch today's Nifty spot → ATM
  2. Fetch 5-min candles for all 26 option symbols
  3. Compute EMA9 + VWAP for all 169 pairs
  4. Compare current EMA9/VWAP side against saved state
  5. Send Telegram alert for any pairs that crossed BELOW VWAP
  6. Save updated state

Usage:
  python run_live.py --expiry "26JUN"

Environment variables required:
  FYERS_CLIENT_ID     — Fyers app client ID
  FYERS_ACCESS_TOKEN  — Fyers access token (refresh daily via separate auth script)
  TELEGRAM_BOT_TOKEN  — Telegram bot token
  TELEGRAM_CHAT_ID    — Telegram chat/channel ID

Optional:
  STATE_FILE          — path to JSON state file (default: crossover_state.json)
  WIDENING_WINDOW     — candle window for widening score (default: 5)
"""

import argparse
import datetime
import logging
import os
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("run_live")


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--expiry",  required=True,
                   help="Expiry string e.g. '26JUN' or '26JUN25'")
    p.add_argument("--window",  type=int, default=None,
                   help="Widening candle window (default: 5)")
    p.add_argument("--alert",   choices=["below", "above", "both"], default="below",
                   help="Which crossover direction to alert on (default: below)")
    p.add_argument("--dry-run", action="store_true",
                   help="Print alerts to stdout instead of sending Telegram")
    return p.parse_args()


def _check_market_hours() -> bool:
    """Return True if current IST time is within trading hours 09:15–15:30."""
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo("Asia/Kolkata")
    except ImportError:
        # Python < 3.9 fallback: use UTC+5:30
        tz = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

    now  = datetime.datetime.now(tz)
    open_  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    close_ = now.replace(hour=15, minute=30, second=0, microsecond=0)

    if now.weekday() >= 5:
        logger.info("Weekend — market closed")
        return False
    if not (open_ <= now <= close_):
        logger.info("Outside market hours (IST %s)", now.strftime("%H:%M"))
        return False
    return True


def _candle_time_str(candle_times: list) -> str:
    """Return HH:MM string of the latest candle."""
    if not candle_times:
        return "—"
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo("Asia/Kolkata")
    except ImportError:
        tz = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    dt = datetime.datetime.fromtimestamp(candle_times[-1], tz=tz)
    return dt.strftime("%H:%M")


def main():
    args = parse_args()

    # ── patch config from environment ─────────────────────────────────────────
    import config
    config.CLIENT_ID    = os.environ.get("FYERS_CLIENT_ID",    config.CLIENT_ID)
    config.ACCESS_TOKEN = os.environ.get("FYERS_ACCESS_TOKEN", config.ACCESS_TOKEN)
    config.EXPIRY_DATE  = args.expiry
    if args.window:
        config.WIDENING_WINDOW = args.window
    elif os.environ.get("WIDENING_WINDOW"):
        config.WIDENING_WINDOW = int(os.environ["WIDENING_WINDOW"])

    # ── guard: only run during market hours ───────────────────────────────────
    if not _check_market_hours():
        logger.info("Exiting — outside market hours")
        sys.exit(0)

    import fetcher
    import engine
    import state
    import notifier

    today = datetime.date.today()

    # ── Step 1: ATM ───────────────────────────────────────────────────────────
    logger.info("Detecting ATM strike …")
    fetcher.init("live")
    atm = fetcher.fetch_atm_strike()
    logger.info("ATM = %d", atm)

    # ── Step 2: Symbols ───────────────────────────────────────────────────────
    fetcher.build_symbols(atm, args.expiry)

    # ── Step 3: Candles ───────────────────────────────────────────────────────
    logger.info("Fetching today's candles …")
    fetcher.fetch_historical(today)

    non_empty = sum(1 for v in fetcher.candle_store.values() if v)
    if non_empty == 0:
        logger.error("No candle data fetched — aborting")
        sys.exit(1)
    logger.info("%d/%d symbols have data", non_empty, len(fetcher.ce_symbols) + len(fetcher.pe_symbols))

    # ── Step 4: Compute pairs ─────────────────────────────────────────────────
    logger.info("Computing EMA9 / VWAP for all pairs …")
    pair_cache = engine.build_all_pairs()
    logger.info("Built %d pair series", len(pair_cache))

    if not pair_cache:
        logger.error("No pairs computed — aborting")
        sys.exit(1)

    # ── Step 5: Current state snapshot (last candle) ──────────────────────────
    # Find the latest candle index across all pairs
    last_idx = max(len(s["times"]) - 1 for s in pair_cache.values())
    candle_times = next(iter(pair_cache.values()))["times"]
    ctime = _candle_time_str(candle_times)
    logger.info("Latest candle index=%d  time=%s", last_idx, ctime)

    # Build current side map: { label: "above"|"below" }
    current_sides: dict = {}
    current_data:  dict = {}   # label → full row data for notification

    for label, series in pair_cache.items():
        idx  = min(last_idx, len(series["times"]) - 1)
        ema9 = series["ema9"][idx]
        vwap = series["vwap"][idx]
        if vwap == 0:
            continue
        side = "above" if ema9 > vwap else "below"
        current_sides[label] = side

        diff     = ema9 - vwap
        pct_diff = diff / vwap * 100
        w        = config.WIDENING_WINDOW
        start    = max(0, idx - w + 1)
        abs_diffs = [abs(series["ema9"][i] - series["vwap"][i]) for i in range(start, idx + 1)]
        widening  = engine._linear_slope_normalized(abs_diffs)

        current_data[label] = {
            "label":         label,
            "cross_dir":     side,
            "pct_diff":      round(pct_diff, 4),
            "price":         round(series["price"][idx], 2),
            "ema9":          round(ema9, 2),
            "vwap":          round(vwap, 2),
            "widening_score": round(widening, 4),
        }

    # ── Step 6: Load previous state & detect crossovers ──────────────────────
    prev_sides   = state.load()
    raw_crossovers = state.detect_crossovers(current_sides, prev_sides)

    # Filter by alert direction
    crossovers = []
    for c in raw_crossovers:
        if args.alert == "both":
            crossovers.append(c)
        elif c["cross_dir"] == args.alert:
            crossovers.append(c)

    # Enrich with full data
    enriched = []
    for c in crossovers:
        row = current_data.get(c["label"], {})
        row.update(c)
        enriched.append(row)

    logger.info("Crossovers detected: %d (filtered to alert direction '%s': %d)",
                len(raw_crossovers), args.alert, len(enriched))

    # ── Step 7: Send alerts ───────────────────────────────────────────────────
    if enriched:
        if args.dry_run:
            print("\n── DRY RUN ALERT ──────────────────────────")
            for c in enriched:
                sign = "+" if c["pct_diff"] >= 0 else ""
                print(f"  {'↑' if c['cross_dir']=='above' else '↓'} {c['label']}  "
                      f"crossed {c['cross_dir']} VWAP  "
                      f"Δ{sign}{c['pct_diff']:.2f}%  "
                      f"EMA9={c['ema9']:.2f}  VWAP={c['vwap']:.2f}")
            print("────────────────────────────────────────────\n")
        else:
            notifier.send_crossover_alert(enriched, ctime, atm)
    else:
        logger.info("No crossovers to alert")

    # ── Step 8: Save state ────────────────────────────────────────────────────
    state.save(current_sides)
    logger.info("Done")


if __name__ == "__main__":
    main()
