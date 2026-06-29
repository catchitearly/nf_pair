"""
main.py
Entry point.

Usage:
  # Backtest a specific date (opens local dashboard)
  python main.py --mode backtest --date 2025-07-10 --expiry "26JUN"

  # Backtest without starting the server (writes docs/ snapshot only)
  python main.py --mode backtest --date 2025-07-10 --expiry "26JUN" --no-server

  # Live mode (today's session)
  python main.py --mode live --expiry "26JUN"

  # Custom widening window
  python main.py --mode backtest --date 2025-07-10 --expiry "26JUN" --window 10
"""

import argparse
import datetime
import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main")


def parse_args():
    p = argparse.ArgumentParser(description="Nifty Options Dashboard")
    p.add_argument("--mode",      choices=["backtest", "live"], default="backtest")
    p.add_argument("--date",      default=None,
                   help="Date for backtest YYYY-MM-DD (default: today)")
    p.add_argument("--expiry",    default=None,
                   help="Expiry string e.g. '26JUN' or '26JUN25'")
    p.add_argument("--window",    type=int, default=None,
                   help="Widening candle window 3/5/10 (default: config.WIDENING_WINDOW)")
    p.add_argument("--no-server", action="store_true",
                   help="Skip HTTP server; write docs/ snapshot and exit (for CI/Actions)")
    return p.parse_args()


def main():
    args = parse_args()

    # Patch config before any other imports
    import config
    if args.window:
        config.WIDENING_WINDOW = args.window
    if args.expiry:
        config.EXPIRY_DATE = args.expiry

    # Also allow env-var overrides (used by GitHub Actions)
    if os.environ.get("FYERS_CLIENT_ID"):
        config.CLIENT_ID = os.environ["FYERS_CLIENT_ID"]
    if os.environ.get("FYERS_ACCESS_TOKEN"):
        config.ACCESS_TOKEN = os.environ["FYERS_ACCESS_TOKEN"]

    import fetcher
    import engine

    mode = args.mode
    fetcher.init(mode)

    # ── resolve date ──────────────────────────────────────────────────────────
    if mode == "backtest":
        if args.date:
            date = datetime.datetime.strptime(args.date, "%Y-%m-%d").date()
        else:
            date = datetime.date.today()
        logger.info("Backtest date: %s", date)
    else:
        date = datetime.date.today()
        logger.info("Live mode: %s", date)

    # ── bootstrap pipeline ────────────────────────────────────────────────────
    logger.info("Step 1/3 → Detecting ATM strike …")
    atm = fetcher.fetch_atm_strike(date if mode == "backtest" else None)
    logger.info("ATM = %d", atm)

    logger.info("Step 2/3 → Building option symbols …")
    fetcher.build_symbols(atm, config.EXPIRY_DATE or None)

    logger.info("Step 3/3 → Fetching candle data …")
    date_from = fetcher._prev_trading_day(date)
    fetcher.fetch_historical(date_from, date_to=date)

    logger.info("Computing EMA9 / VWAP for all pairs …")
    pair_cache = engine.build_all_pairs()

    # ── snapshot for GitHub Pages (always written in --no-server mode) ────────
    if args.no_server or os.environ.get("GITHUB_ACTIONS"):
        import snapshot

        last_idx     = max((len(s["times"]) - 1 for s in pair_cache.values()), default=0)
        candle_times = next(iter(pair_cache.values()))["times"] if pair_cache else []

        try:
            import zoneinfo
            tz = zoneinfo.ZoneInfo("Asia/Kolkata")
        except ImportError:
            tz = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

        ctime = "--:--"
        if candle_times:
            dt = datetime.datetime.fromtimestamp(candle_times[last_idx], tz=tz)
            ctime = dt.strftime("%H:%M")

        # Build current_data dict for snapshot (same shape as run_live.py)
        current_data = {}
        for label, series in pair_cache.items():
            idx  = min(last_idx, len(series["times"]) - 1)
            ema9 = series["ema9"][idx]
            vwap = series["vwap"][idx]
            if vwap == 0:
                continue
            pct_diff = (ema9 - vwap) / vwap * 100
            current_data[label] = {
                "label":    label,
                "price":    round(series["price"][idx], 2),
                "ema9":     round(ema9, 2),
                "vwap":     round(vwap, 2),
                "pct_diff": round(pct_diff, 4),
            }

        snapshot.write(
            pair_cache   = pair_cache,
            last_idx     = last_idx,
            atm          = atm,
            candle_time  = ctime,
            current_data = current_data,
            crossovers   = [],    # no live crossover data in backtest snapshot
            mode         = mode,
        )
        logger.info("Snapshot written to docs/ — exiting")
        sys.exit(0)

    # ── local dashboard server ────────────────────────────────────────────────
    import server
    logger.info("Starting dashboard → http://%s:%d", config.HOST, config.PORT)
    server.start(mode)


if __name__ == "__main__":
    main()
