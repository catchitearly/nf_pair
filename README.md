# Nifty Options · EMA/VWAP Dashboard

Real-time and historical dashboard for Nifty option pairs using EMA9 vs VWAP analysis.

---

## Setup

```bash
cd nifty_options
pip install -r requirements.txt
```

### 1. Set credentials in `config.py`

```python
CLIENT_ID    = "XXXXXXXXXXX-100"   # Fyers app client ID
ACCESS_TOKEN = "eyJ..."            # Fresh access token (generate daily)
```

### 2. Set expiry (optional)

Leave `EXPIRY_DATE = ""` to auto-pick the nearest weekly Thursday expiry.
Or set explicitly: `EXPIRY_DATE = "24-JUL-2025"`

---

## Run

### Backtest mode (replay a historical day)

```bash
python main.py --mode backtest --date 2025-07-10
```

### Live mode (today's session, auto-refreshes every 5 min)

```bash
python main.py --mode live
```

### Extra options

```bash
# Custom expiry
python main.py --mode backtest --date 2025-07-10 --expiry "24-JUL-2025"

# Custom widening window (default 5 candles)
python main.py --mode backtest --date 2025-07-10 --window 10
```

---

## Dashboard → http://localhost:8080

| Control | Description |
|---|---|
| BACKTEST / LIVE toggle | Switch modes |
| Date picker | Select backtest date |
| LOAD button | Fetch data + compute all pairs |
| Timeline slider | Scrub through 9:15 → 3:30 |
| WINDOW dropdown | Widening candle window (3 / 5 / 10) |
| Tab 1: EMA9 > VWAP | Bullish pairs ranked by % diff |
| Tab 2: EMA9 < VWAP | Bearish pairs ranked by % diff |
| 🔥 flame icon | Pair where gap is widening steadily |
| Click any pair | Shows Price / EMA9 / VWAP line chart |

---

## Architecture

```
main.py       CLI entry point; bootstraps pipeline
config.py     Credentials + constants
fetcher.py    Fyers API: ATM detection, symbol building, OHLCV fetch
engine.py     EMA9, VWAP, 169 pair series, ranking
server.py     HTTP server + /api/* endpoints
dashboard/
  index.html  Self-contained frontend (Chart.js)
```

## API endpoints (for debugging)

```
GET  /api/status              → mode, ATM, candle_count, timestamps
GET  /api/data?idx=<n>        → ranked case1 + case2 at candle n
GET  /api/chart?pair=X&idx=n  → price/ema9/vwap series for one pair
POST /api/refresh             → trigger live candle refresh
```

## Pair naming

`24000C/23800P` = ATM CE 24000 + PE 23800 (premium sum as pair price)

## Rank score formula

```
rank_score = |pct_diff| × (1 + 0.5 × widening_score)
```

Where `widening_score` = normalised slope of `|EMA9 - VWAP|` over the last N candles.
