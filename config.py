# ─────────────────────────────────────────────
#  Fyers API credentials
# ─────────────────────────────────────────────
CLIENT_ID    = "YOUR_CLIENT_ID"       # e.g. "XXXXXXXXXXX-100"
ACCESS_TOKEN = "YOUR_ACCESS_TOKEN"    # paste fresh token here

# ─────────────────────────────────────────────
#  Nifty options universe
# ─────────────────────────────────────────────
INDEX_SYMBOL   = "NSE:NIFTY50-INDEX"  # Fyers symbol for Nifty spot
EXPIRY_DATE    = "26721"                   # e.g. "26JUN" or "26JUN25" or "26-JUN-2025"; leave "" to auto-pick nearest Thursday
STRIKE_RANGE   = 600                  # ATM ± 600
STRIKE_STEP    = 100
OPTION_EXPIRY_FORMAT = "%d%b%y"       # Fyers format: 24JUL25

# ─────────────────────────────────────────────
#  Candle settings
# ─────────────────────────────────────────────
CANDLE_RESOLUTION = "5"               # 5-min candles
MARKET_OPEN  = "09:15"
MARKET_CLOSE = "15:30"

# ─────────────────────────────────────────────
#  Indicator settings
# ─────────────────────────────────────────────
EMA_PERIOD         = 9
WIDENING_WINDOW    = 5        # configurable: candles to measure steady widening (3, 5, or 10)
WIDENING_BOOST     = 0.5      # rank score multiplier for steady-widening pairs
MIN_OPTION_PRICE   = 10.0     # skip CE/PE strikes with latest close below this before pairing

# ── Backtest / trade settings ─────────────────────────────────────────────────
BT_MAX_ENTRY_PRICE = 480.0    # only short pairs with price[0] < this
BT_EMA_PROXIMITY   = 10.0     # only enter if abs(price[0] - ema9[0]) <= this

# ─────────────────────────────────────────────
#  Server settings
# ─────────────────────────────────────────────
HOST = "localhost"
PORT = 8080
LIVE_REFRESH_SECONDS = 300    # auto-refresh every 5 min in live mode
