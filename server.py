"""
server.py
Minimal HTTP server exposing:
  GET /                          → dashboard/index.html
  GET /api/status                → { mode, atm, candle_count, candle_times }
  GET /api/data?idx=<int>        → { case1: [...], case2: [...] }
  GET /api/chart?pair=<label>&idx=<int> → { times, price, ema9, vwap }
  POST /api/refresh              → trigger live data refresh
"""

import json
import logging
import os
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

import config
import engine
import fetcher

logger = logging.getLogger(__name__)

DASHBOARD_DIR = Path(__file__).parent / "dashboard"


class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):  # suppress default stdout noise
        logger.debug(fmt, *args)

    # ── routing ───────────────────────────────────────────────────────────────

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path   = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            self._serve_file(DASHBOARD_DIR / "index.html", "text/html")
        elif path == "/api/status":
            self._json(self._status())
        elif path == "/api/data":
            idx = int(params.get("idx", ["0"])[0])
            self._json(self._data(idx))
        elif path == "/api/chart":
            pair = params.get("pair", [""])[0]
            idx  = int(params.get("idx", ["0"])[0])
            self._json(self._chart(pair, idx))
        else:
            self._not_found()

    def do_POST(self):
        if self.path == "/api/refresh":
            threading.Thread(target=fetcher.append_live_candles, daemon=True).start()
            self._json({"ok": True})
        else:
            self._not_found()

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors_headers()
        self.end_headers()

    # ── handlers ──────────────────────────────────────────────────────────────

    def _status(self) -> dict:
        cs = engine.candle_count()
        times = []
        for series in engine._pair_cache.values():
            times = series["times"]
            break
        return {
            "mode":         fetcher._mode,
            "atm":          fetcher.atm_strike,
            "candle_count": cs,
            "candle_times": times,
            "widening_window": config.WIDENING_WINDOW,
        }

    def _data(self, idx: int) -> dict:
        case1, case2 = engine.ranked_pairs_at(idx)
        return {"case1": case1, "case2": case2, "idx": idx}

    def _chart(self, pair: str, idx: int) -> dict:
        series = engine.get_pair_series(pair, idx)
        if not series:
            return {"error": f"Pair '{pair}' not found"}
        return series

    # ── helpers ───────────────────────────────────────────────────────────────

    def _json(self, obj: dict):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, path: Path, content_type: str):
        if not path.exists():
            self._not_found()
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _not_found(self):
        self.send_response(404)
        self.end_headers()

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")


# ── live-refresh scheduler ────────────────────────────────────────────────────

def _live_scheduler():
    """Background thread: refresh data every LIVE_REFRESH_SECONDS in live mode."""
    while True:
        time.sleep(config.LIVE_REFRESH_SECONDS)
        if fetcher._mode == "live":
            logger.info("Live scheduler: refreshing candles…")
            fetcher.append_live_candles()
            engine.build_all_pairs()
            logger.info("Live scheduler: rebuild complete")


def start(mode: str = "backtest"):
    if mode == "live":
        threading.Thread(target=_live_scheduler, daemon=True).start()

    httpd = HTTPServer((config.HOST, config.PORT), Handler)
    logger.info("Dashboard → http://%s:%d", config.HOST, config.PORT)
    httpd.serve_forever()
