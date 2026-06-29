"""
state.py
Persists the last-known EMA9/VWAP relationship for each pair across runs.
Stored as a JSON file so GitHub Actions can cache it between workflow runs.

State format:
{
  "last_run_epoch": 1234567890,
  "pairs": {
    "24000C/23800P": "above",   # "above" | "below"
    ...
  }
}
"""

import json
import logging
import os
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)

STATE_FILE        = os.environ.get("STATE_FILE",        "crossover_state.json")
BEARISH_STATE_FILE = os.environ.get("BEARISH_STATE_FILE", "bearish_state.json")
BEARISH_LOG_FILE   = os.environ.get("BEARISH_LOG_FILE",   "bearish_log.json")


def load() -> Dict[str, str]:
    """Return { label: 'above'|'below' } from last run. Empty dict if no state."""
    if not os.path.exists(STATE_FILE):
        logger.info("No state file found — first run, all pairs treated as new")
        return {}
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        pairs = data.get("pairs", {})
        age   = time.time() - data.get("last_run_epoch", 0)
        logger.info("State loaded: %d pairs, %.0f s old", len(pairs), age)
        # If state is older than 30 min (e.g. gap between sessions) reset it
        # so we don't ghost-miss crossovers from a fresh market open
        if age > 1800:
            logger.info("State too old (%.0f s) — resetting", age)
            return {}
        return pairs
    except Exception as e:
        logger.warning("Failed to load state: %s — starting fresh", e)
        return {}


def save(pair_states: Dict[str, str]) -> None:
    """Persist current { label: 'above'|'below' } map."""
    data = {
        "last_run_epoch": int(time.time()),
        "pairs": pair_states,
    }
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(data, f)
        logger.info("State saved: %d pairs → %s", len(pair_states), STATE_FILE)
    except Exception as e:
        logger.error("Failed to save state: %s", e)


def detect_crossovers(
    current_states: Dict[str, str],
    prev_states:    Dict[str, str],
) -> list:
    """
    Compare current vs previous EMA9/VWAP side for each pair.
    Returns list of { label, cross_dir } for pairs that flipped.

    cross_dir = "above"  →  EMA9 just crossed above VWAP (was below, now above)
    cross_dir = "below"  →  EMA9 just crossed below VWAP (was above, now below)
    """
    crossovers = []
    for label, cur in current_states.items():
        prev = prev_states.get(label)
        if prev is None:
            continue          # no prior state for this pair, skip
        if prev != cur:
            crossovers.append({
                "label":     label,
                "cross_dir": cur,   # the new side it crossed to
            })
    return crossovers


# ── bearish setup state ───────────────────────────────────────────────────────
# Stores the last candle index at which each pair triggered the bearish setup,
# so we alert only once per setup occurrence and not on every subsequent candle.

def load_bearish() -> Dict[str, int]:
    """Return { label: last_triggered_candle_idx }. Empty dict if no state."""
    if not os.path.exists(BEARISH_STATE_FILE):
        return {}
    try:
        with open(BEARISH_STATE_FILE) as f:
            data = json.load(f)
        age = time.time() - data.get("last_run_epoch", 0)
        if age > 1800:
            logger.info("Bearish state too old (%.0f s) — resetting", age)
            return {}
        return data.get("pairs", {})
    except Exception as e:
        logger.warning("Failed to load bearish state: %s", e)
        return {}


def save_bearish(triggered: Dict[str, int]) -> None:
    """Persist { label: candle_idx } of last-triggered bearish setup."""
    data = {
        "last_run_epoch": int(time.time()),
        "pairs": triggered,
    }
    try:
        with open(BEARISH_STATE_FILE, "w") as f:
            json.dump(data, f)
        logger.info("Bearish state saved: %d pairs → %s", len(triggered), BEARISH_STATE_FILE)
    except Exception as e:
        logger.error("Failed to save bearish state: %s", e)


def filter_new_bearish(matches: list, prev_triggered: Dict[str, int], current_idx: int) -> list:
    """
    From the list of bearish setup matches, return only those
    that were NOT already triggered at this same candle index in the last run.
    Also merges current matches into prev_triggered (mutates it in place).
    """
    new_alerts = []
    for m in matches:
        label       = m["label"]
        last_candle = prev_triggered.get(label, -1)
        if last_candle != current_idx:
            new_alerts.append(m)
            prev_triggered[label] = current_idx   # mark as alerted
    return new_alerts


# ── bearish setup history log ─────────────────────────────────────────────────
# In live mode we accumulate triggers across runs into a single log file.
# Resets each new trading day (date changes).

def _today_str() -> str:
    import datetime
    return datetime.date.today().isoformat()


def load_bearish_log() -> list:
    """Return list of all bearish triggers logged today. Empty list if none/stale."""
    if not os.path.exists(BEARISH_LOG_FILE):
        return []
    try:
        with open(BEARISH_LOG_FILE) as f:
            data = json.load(f)
        # Reset if log is from a previous day
        if data.get("date") != _today_str():
            logger.info("Bearish log is from previous day — resetting")
            return []
        return data.get("triggers", [])
    except Exception as e:
        logger.warning("Failed to load bearish log: %s", e)
        return []


def append_bearish_log(new_triggers: list, candle_time_str: str) -> None:
    """Append new bearish setup triggers to today's log file."""
    if not new_triggers:
        return
    existing = load_bearish_log()
    # Enrich each trigger with the candle_str if not already set
    for t in new_triggers:
        if "candle_str" not in t:
            t["candle_str"] = candle_time_str
    existing.extend(new_triggers)
    # Keep sorted by candle_time epoch if present, else by order added
    existing.sort(key=lambda x: x.get("candle_time", 0))
    data = {
        "date":     _today_str(),
        "triggers": existing,
    }
    try:
        with open(BEARISH_LOG_FILE, "w") as f:
            json.dump(data, f)
        logger.info("Bearish log updated: %d total triggers today", len(existing))
    except Exception as e:
        logger.error("Failed to save bearish log: %s", e)
