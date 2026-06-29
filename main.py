"""
main.py
Entry point.

Usage:
  # Backtest a specific date
  python main.py --mode backtest --date 2025-07-10

  # Live mode (today's session)
  python main.py --mode live

  # Backtest with custom expiry and widening window
  python main.py --mode backtest --date 2025-07-10 --expiry "24-JUL-2025" --window 10
"""

import argparse
import datetime
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main")


def parse_args():
    p = argparse.ArgumentParser(description="Nifty Options Dashboard")
    p.add_argument("--mode",   choices=["backtest", "live"], default="backtest")
    p.add_argument("--date",   default=None,
                   help="Date for backtest YYYY-MM-DD (default: today)")
    p.add_argument("--expiry", default=None,
                   help="Expiry override e.g. '24-JUL-2025' (default: nearest Thursday)")
    p.add_argument("--window", type=int, default=None,
                   help="Widening candle window 3/5/10 (default: config.WIDENING_WINDOW)")
    return p.parse_args()


def main():
    args = parse_args()

    # Late import so config patches apply
    import config
    if args.window:
        config.WIDENING_WINDOW = args.window
    if args.expiry:
        config.EXPIRY_DATE = args.expiry

    import fetcher
    import engine
    import server

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

    logger.info("Step 3/3 → Fetching %s candle data …",
                "historical" if mode == "backtest" else "live")
    fetcher.fetch_historical(date)

    logger.info("Computing EMA9 / VWAP for all pairs …")
    engine.build_all_pairs()

    logger.info("All ready — starting dashboard server …")
    server.start(mode)


if __name__ == "__main__":
    main()
