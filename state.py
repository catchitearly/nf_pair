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

STATE_FILE = os.environ.get("STATE_FILE", "crossover_state.json")


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
