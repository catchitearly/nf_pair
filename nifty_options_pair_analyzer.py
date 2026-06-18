"""
Nifty Options Pair Analyzer
- Fetches ATM ± 400 strikes (CE + PE) from Fyers API
- Combines into 5 pairs (straddle + strangles)
- Computes VWAP and EMA9 per pair
- Detects crossovers and sends Telegram alerts
- Generates standalone HTML dashboard for GitHub Pages
"""

import os
import json
import math
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from fyers_apiv3 import fyersModel

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
def _require_env(key: str) -> str:
    """Fetch a required env var with a clear error instead of a bare KeyError,
    so a missing GitHub Actions secret is obvious in the run log."""
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(
            f"Missing required environment variable '{key}'. "
            f"Add it under Settings → Secrets and variables → Actions, "
            f"and pass it in the workflow's `env:` block for this step."
        )
    return val


CLIENT_ID    = _require_env("FYERS_CLIENT_ID")
ACCESS_TOKEN = _require_env("FYERS_ACCESS_TOKEN")
TG_BOT_TOKEN = _require_env("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID   = _require_env("TELEGRAM_CHAT_ID")

EXPIRY_DATE     = "26623"          # YYMDD (no leading zero on month) — update every week
INDEX_SYMBOL    = "NSE:NIFTY50-INDEX"
STRIKE_STEP     = 100
OTM_RANGE       = 400              # ATM ± 400
EMA_PERIOD      = 9
RESOLUTION      = "5"             # 5-minute candles
DAYS_BACK       = 2

# ─────────────────────────────────────────────
# FYERS CLIENT
# ─────────────────────────────────────────────
fyers = fyersModel.FyersModel(
    client_id=CLIENT_ID,
    token=ACCESS_TOKEN,
    is_async=False,
    log_path=""
)

# ─────────────────────────────────────────────
# STEP 1: GET NIFTY SPOT & COMPUTE ATM
# ─────────────────────────────────────────────
def get_atm_strike() -> int:
    resp = fyers.quotes({"symbols": INDEX_SYMBOL})
    if resp.get("s") != "ok" or not resp.get("d"):
        # Surfaces the real Fyers error (e.g. expired ACCESS_TOKEN) instead of
        # a confusing KeyError/IndexError a few lines down.
        raise RuntimeError(f"Fyers quotes() call failed: {resp}")
    ltp  = resp["d"][0]["v"]["lp"]
    atm  = round(ltp / STRIKE_STEP) * STRIKE_STEP
    print(f"Nifty LTP: {ltp:.2f}  →  ATM Strike: {atm}")
    return atm


# ─────────────────────────────────────────────
# STEP 2: BUILD OPTION SYMBOL STRINGS
# ─────────────────────────────────────────────
def build_symbol(strike: int, opt_type: str) -> str:
    # Fyers convention is EXPIRY + STRIKE + CE/PE, e.g. NSE:NIFTY2662324400CE
    # (the strike comes BEFORE the CE/PE suffix, not after it).
    return f"NSE:NIFTY{EXPIRY_DATE}{strike}{opt_type}"


def build_pairs(atm: int) -> list[dict]:
    pairs = []
    for offset in range(0, OTM_RANGE + STRIKE_STEP, STRIKE_STEP):
        ce_strike = atm + offset
        pe_strike = atm - offset
        label = (
            f"ATM Straddle ({atm})"
            if offset == 0
            else f"ATM±{offset} ({ce_strike}CE / {pe_strike}PE)"
        )
        pairs.append({
            "label":     label,
            "ce_strike": ce_strike,
            "pe_strike": pe_strike,
            "ce_sym":    build_symbol(ce_strike, "CE"),
            "pe_sym":    build_symbol(pe_strike, "PE"),
        })
    return pairs


# ─────────────────────────────────────────────
# STEP 3: FETCH HISTORICAL DATA
# ─────────────────────────────────────────────
def date_range() -> tuple[str, str]:
    today = datetime.now()
    # Walk back to find 2 actual trading days (skip Sat/Sun)
    trading_days = []
    d = today
    while len(trading_days) < DAYS_BACK:
        if d.weekday() < 5:          # Mon–Fri
            trading_days.append(d)
        d -= timedelta(days=1)
    start = trading_days[-1].strftime("%Y-%m-%d")
    end   = trading_days[0].strftime("%Y-%m-%d")
    return start, end


def fetch_ohlcv(symbol: str, date_from: str, date_to: str) -> pd.DataFrame:
    data = {
        "symbol":     symbol,
        "resolution": RESOLUTION,
        "date_format": "1",
        "range_from": date_from,
        "range_to":   date_to,
        "cont_flag":  "1",
    }
    resp = fyers.history(data)
    if resp.get("s") != "ok" or not resp.get("candles"):
        print(f"  ⚠  No data for {symbol}  ({resp.get('s')}: {resp.get('message', '')})")
        return pd.DataFrame()

    df = pd.DataFrame(
        resp["candles"],
        columns=["timestamp", "open", "high", "low", "close", "volume"]
    )
    df["datetime"] = pd.to_datetime(df["timestamp"], unit="s").dt.tz_localize("UTC").dt.tz_convert("Asia/Kolkata")
    df.set_index("datetime", inplace=True)
    df.drop(columns=["timestamp"], inplace=True)
    return df


# ─────────────────────────────────────────────
# STEP 4: COMPUTE VWAP & EMA9 ON COMBINED PAIR
# ─────────────────────────────────────────────
def compute_indicators(ce_df: pd.DataFrame, pe_df: pd.DataFrame) -> pd.DataFrame:
    # Align on common timestamps
    combined = pd.DataFrame(index=ce_df.index)
    combined["ce_close"]  = ce_df["close"]
    combined["pe_close"]  = pe_df["close"]
    combined["ce_volume"] = ce_df["volume"]
    combined["pe_volume"] = pe_df["volume"]
    combined.dropna(inplace=True)

    combined["premium"]   = combined["ce_close"] + combined["pe_close"]
    combined["volume"]    = combined["ce_volume"] + combined["pe_volume"]

    # VWAP = cumulative(price * volume) / cumulative(volume)
    # Reset per trading day
    combined["date"] = combined.index.date
    vwap_list = []
    for _, day_df in combined.groupby("date"):
        tp_vol  = day_df["premium"] * day_df["volume"]
        cum_tpv = tp_vol.cumsum()
        cum_vol = day_df["volume"].cumsum()
        vwap    = cum_tpv / cum_vol
        vwap_list.append(vwap)
    combined["vwap"] = pd.concat(vwap_list)

    # EMA9
    combined["ema9"] = combined["premium"].ewm(span=EMA_PERIOD, adjust=False).mean()

    combined.drop(columns=["date"], inplace=True)
    return combined


# ─────────────────────────────────────────────
# STEP 5: DETECT CROSSOVERS
# ─────────────────────────────────────────────
def detect_crossovers(df: pd.DataFrame, pair_label: str) -> list[dict]:
    """Flags only EMA9 crossing VWAP. Price-vs-VWAP and price-vs-EMA9
    crossovers are intentionally not checked anymore."""
    alerts  = []
    vwap    = df["vwap"].values
    ema9    = df["ema9"].values
    premium = df["premium"].values
    times   = df.index

    for i in range(1, len(df)):
        ts = times[i].strftime("%d-%b %I:%M %p")

        if ema9[i - 1] < vwap[i - 1] and ema9[i] >= vwap[i]:
            alerts.append({"time": ts, "pair": pair_label, "type": "EMA9 crossed ↑ VWAP", "price": premium[i], "y": ema9[i]})
        elif ema9[i - 1] > vwap[i - 1] and ema9[i] <= vwap[i]:
            alerts.append({"time": ts, "pair": pair_label, "type": "EMA9 crossed ↓ VWAP", "price": premium[i], "y": ema9[i]})

    return alerts


# ─────────────────────────────────────────────
# STEP 6: SEND TELEGRAM ALERT
# ─────────────────────────────────────────────
TG_MAX_CHARS = 4000   # Telegram limit is 4096; stay safely under

def _tg_post(text: str) -> bool:
    """Send a single message to Telegram. Returns True on success."""
    url     = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    r = requests.post(url, json=payload, timeout=10)
    if r.status_code != 200:
        print(f"❌ Telegram error: {r.text}")
        return False
    return True


def send_telegram(alerts: list[dict]):
    if not alerts:
        print("No crossover alerts to send.")
        return

    # ── Summary message (always 1 message) ──────────────────
    bull = sum(1 for a in alerts if "↑" in a["type"])
    bear = len(alerts) - bull

    # Count per pair
    from collections import Counter
    pair_counts = Counter(a["pair"] for a in alerts)
    pair_lines  = "\n".join(
        f"  • {pair}: {cnt}" for pair, cnt in pair_counts.most_common()
    )

    summary = (
        f"📊 *Nifty Options — Crossover Summary*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Total alerts : {len(alerts)}\n"
        f"🟢 Bullish   : {bull}\n"
        f"🔴 Bearish   : {bear}\n\n"
        f"*By Pair:*\n{pair_lines}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"_Details follow in next message(s)_"
    )
    _tg_post(summary)

    # ── Detail messages — chunked to stay under 4000 chars ──
    # Build individual alert lines first
    alert_lines = []
    for a in alerts:
        arrow = "🟢" if "↑" in a["type"] else "🔴"
        alert_lines.append(
            f"{arrow} *{a['pair']}*\n"
            f"  {a['type']} | ₹{a['price']:.2f} | {a['time']}"
        )

    # Pack lines into chunks
    chunk_header = "🚨 *Crossover Alerts*\n━━━━━━━━━━━━━━━━━━━━\n"
    current_chunk = chunk_header
    chunk_num     = 0
    sent_chunks   = 0

    for line in alert_lines:
        candidate = current_chunk + line + "\n\n"
        if len(candidate) > TG_MAX_CHARS:
            # Send current chunk and start a new one
            if _tg_post(current_chunk.rstrip()):
                sent_chunks += 1
            chunk_num    += 1
            current_chunk = chunk_header + line + "\n\n"
        else:
            current_chunk = candidate

    # Send remaining chunk
    if current_chunk.strip() != chunk_header.strip():
        if _tg_post(current_chunk.rstrip()):
            sent_chunks += 1

    print(f"✅ Telegram: summary + {sent_chunks} detail message(s) sent ({len(alerts)} alerts)")


# ─────────────────────────────────────────────
# STEP 7: GENERATE STANDALONE HTML DASHBOARD
# ─────────────────────────────────────────────
def generate_html(pairs_data: list[dict], run_time: str) -> str:
    """Build a self-contained HTML file with embedded Plotly charts."""

    # Build per-pair chart spec as JSON for the HTML template
    charts = []
    for pd_item in pairs_data:
        label = pd_item["label"]
        df    = pd_item["df"]
        cross = pd_item["crossovers"]

        if df.empty:
            charts.append({"label": label, "error": True})
            continue

        times   = [t.strftime("%d-%b %H:%M") for t in df.index]
        premium = df["premium"].round(2).tolist()
        vwap    = df["vwap"].round(2).tolist()
        ema9    = df["ema9"].round(2).tolist()

        # Crossover markers
        cx_times  = [c["time"] for c in cross]
        cx_prices = [c["price"] for c in cross]
        cx_labels = [c["type"] for c in cross]
        cx_y      = [c["y"] for c in cross]

        charts.append({
            "label":     label,
            "error":     False,
            "times":     times,
            "premium":   premium,
            "vwap":      vwap,
            "ema9":      ema9,
            "cx_times":  cx_times,
            "cx_prices": cx_prices,
            "cx_labels": cx_labels,
            "cx_y":      cx_y,
        })

    charts_json = json.dumps(charts)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Nifty Options Pair Dashboard</title>
<script src="https://cdn.plot.ly/plotly-2.32.0.min.js"></script>
<style>
  :root {{
    --bg:       #0d1117;
    --surface:  #161b22;
    --border:   #30363d;
    --text:     #e6edf3;
    --muted:    #8b949e;
    --green:    #3fb950;
    --red:      #f85149;
    --blue:     #58a6ff;
    --yellow:   #d29922;
    --purple:   #bc8cff;
    --accent:   #1f6feb;
  }}

  * {{ box-sizing: border-box; margin: 0; padding: 0; }}

  body {{
    background: var(--bg);
    color: var(--text);
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    min-height: 100vh;
  }}

  header {{
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 18px 32px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    position: sticky;
    top: 0;
    z-index: 100;
  }}

  .header-left h1 {{
    font-size: 1.25rem;
    font-weight: 600;
    letter-spacing: -0.02em;
    color: var(--text);
  }}

  .header-left h1 span {{
    color: var(--blue);
  }}

  .header-meta {{
    font-size: 0.78rem;
    color: var(--muted);
    margin-top: 2px;
  }}

  .badge {{
    background: var(--accent);
    color: #fff;
    font-size: 0.7rem;
    font-weight: 600;
    padding: 3px 10px;
    border-radius: 20px;
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }}

  .legend-bar {{
    display: flex;
    gap: 20px;
    padding: 10px 32px;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    flex-wrap: wrap;
  }}

  .legend-item {{
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 0.78rem;
    color: var(--muted);
  }}

  .legend-dot {{
    width: 24px;
    height: 3px;
    border-radius: 2px;
  }}

  .main {{
    padding: 24px 32px;
    display: flex;
    flex-direction: column;
    gap: 24px;
    max-width: 1400px;
    margin: 0 auto;
  }}

  .pair-card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    overflow: hidden;
  }}

  .pair-header {{
    padding: 14px 20px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    border-bottom: 1px solid var(--border);
  }}

  .pair-title {{
    font-size: 0.9rem;
    font-weight: 600;
    color: var(--text);
  }}

  .pair-stats {{
    display: flex;
    gap: 16px;
  }}

  .stat {{
    text-align: right;
  }}

  .stat-label {{
    font-size: 0.65rem;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }}

  .stat-value {{
    font-size: 0.85rem;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }}

  .green {{ color: var(--green); }}
  .red   {{ color: var(--red);   }}
  .blue  {{ color: var(--blue);  }}

  .chart-wrap {{
    padding: 0;
  }}

  .alerts-section {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    overflow: hidden;
  }}

  .alerts-header {{
    padding: 14px 20px;
    border-bottom: 1px solid var(--border);
    font-size: 0.9rem;
    font-weight: 600;
  }}

  .alerts-body {{
    padding: 12px 20px;
    display: flex;
    flex-direction: column;
    gap: 8px;
    max-height: 300px;
    overflow-y: auto;
  }}

  .alert-row {{
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 8px 12px;
    background: var(--bg);
    border-radius: 6px;
    font-size: 0.8rem;
  }}

  .alert-icon {{ font-size: 1rem; }}

  .alert-info {{ flex: 1; }}

  .alert-pair {{
    font-weight: 600;
    color: var(--text);
  }}

  .alert-type {{
    color: var(--muted);
    font-size: 0.75rem;
    margin-top: 1px;
  }}

  .alert-price {{
    font-variant-numeric: tabular-nums;
    font-weight: 700;
    font-size: 0.85rem;
  }}

  .alert-time {{
    color: var(--muted);
    font-size: 0.72rem;
    white-space: nowrap;
  }}

  .no-alerts {{
    color: var(--muted);
    font-size: 0.82rem;
    padding: 12px 0;
    text-align: center;
  }}

  .error-card {{
    padding: 32px;
    text-align: center;
    color: var(--muted);
    font-size: 0.85rem;
  }}

  @media (max-width: 640px) {{
    .main {{ padding: 16px; }}
    header {{ padding: 14px 16px; }}
    .pair-stats {{ display: none; }}
    .legend-bar {{ padding: 10px 16px; }}
  }}
</style>
</head>
<body>

<header>
  <div class="header-left">
    <h1>Nifty Options <span>Pair Dashboard</span></h1>
    <div class="header-meta">Expiry {EXPIRY_DATE} · 5-min · Last 2 Trading Days · Updated {run_time}</div>
  </div>
  <span class="badge">Live Analysis</span>
</header>

<div class="legend-bar">
  <div class="legend-item">
    <div class="legend-dot" style="background:#58a6ff"></div> Combined Premium
  </div>
  <div class="legend-item">
    <div class="legend-dot" style="background:#d29922"></div> VWAP
  </div>
  <div class="legend-item">
    <div class="legend-dot" style="background:#bc8cff"></div> EMA 9
  </div>
  <div class="legend-item">
    <div class="legend-dot" style="background:#3fb950; width:10px; height:10px; border-radius:50%"></div> EMA9 crossed above VWAP (Bullish)
  </div>
  <div class="legend-item">
    <div class="legend-dot" style="background:#f85149; width:10px; height:10px; border-radius:50%"></div> EMA9 crossed below VWAP (Bearish)
  </div>
</div>

<div class="main" id="main">
  <div id="charts-container"></div>
  <div class="alerts-section">
    <div class="alerts-header">🚨 Last 5 EMA9×VWAP Crossovers</div>
    <div class="alerts-body" id="alerts-body"></div>
  </div>
</div>

<script>
const CHARTS = {charts_json};

const plotlyLayout = (label, annotations = []) => ({{
  paper_bgcolor: 'transparent',
  plot_bgcolor:  'transparent',
  margin:        {{ t: 10, b: 40, l: 60, r: 20 }},
  height:        280,
  annotations:   annotations,
  xaxis: {{
    gridcolor:    '#21262d',
    tickfont:     {{ color: '#8b949e', size: 10 }},
    linecolor:    '#30363d',
    showgrid:     true,
    tickangle:    -30,
  }},
  yaxis: {{
    gridcolor:    '#21262d',
    tickfont:     {{ color: '#8b949e', size: 10 }},
    linecolor:    '#30363d',
    showgrid:     true,
    tickprefix:   '₹',
  }},
  legend: {{ bgcolor: 'transparent', font: {{ color: '#8b949e', size: 10 }} }},
  hovermode: 'x unified',
  hoverlabel: {{
    bgcolor:    '#161b22',
    bordercolor:'#30363d',
    font:       {{ color: '#e6edf3', size: 11 }},
  }},
}});

const allAlerts = [];

function buildCharts() {{
  const container = document.getElementById('charts-container');

  CHARTS.forEach((c, idx) => {{
    const card = document.createElement('div');
    card.className = 'pair-card';

    if (c.error) {{
      card.innerHTML = `
        <div class="pair-header"><span class="pair-title">${{c.label}}</span></div>
        <div class="error-card">⚠️ No data available for this pair</div>`;
      container.appendChild(card);
      return;
    }}

    const lastPremium = c.premium.at(-1);
    const lastVwap    = c.vwap.at(-1);
    const lastEma     = c.ema9.at(-1);
    const vsVwap      = lastPremium - lastVwap;
    const vsEma       = lastPremium - lastEma;

    card.innerHTML = `
      <div class="pair-header">
        <span class="pair-title">${{c.label}}</span>
        <div class="pair-stats">
          <div class="stat">
            <div class="stat-label">Premium</div>
            <div class="stat-value blue">₹${{lastPremium.toFixed(2)}}</div>
          </div>
          <div class="stat">
            <div class="stat-label">vs VWAP</div>
            <div class="stat-value ${{vsVwap >= 0 ? 'green' : 'red'}}">${{vsVwap >= 0 ? '+' : ''}}${{vsVwap.toFixed(2)}}</div>
          </div>
          <div class="stat">
            <div class="stat-label">vs EMA9</div>
            <div class="stat-value ${{vsEma >= 0 ? 'green' : 'red'}}">${{vsEma >= 0 ? '+' : ''}}${{vsEma.toFixed(2)}}</div>
          </div>
        </div>
      </div>
      <div class="chart-wrap"><div id="chart-${{idx}}"></div></div>`;

    container.appendChild(card);

    // Annotate only the most recent 5 EMA9×VWAP crossovers for this pair
    const n = c.cx_labels.length;
    const recentIdx = [];
    for (let k = Math.max(0, n - 5); k < n; k++) recentIdx.push(k);

    const annotations = recentIdx.map(k => {{
      const bull  = c.cx_labels[k].includes('↑');
      const color = bull ? '#3fb950' : '#f85149';

      allAlerts.push({{
        pairLabel: c.label,
        time:      c.cx_times[k],
        type:      c.cx_labels[k],
        price:     c.cx_prices[k],
        bull,
      }});

      return {{
        x: c.cx_times[k],
        y: c.cx_y[k],
        text: 'EMA9×VWAP' + (bull ? ' ▲' : ' ▼'),
        showarrow:  true,
        arrowhead:  3,
        arrowsize:  1,
        arrowwidth: 1.6,
        arrowcolor: color,
        ax: 0,
        ay: bull ? 28 : -28,
        font:       {{ color: color, size: 10 }},
        bgcolor:    '#161b22',
        bordercolor: color,
        borderwidth: 1,
        borderpad:   3,
      }};
    }});

    const traces = [
      {{
        x: c.times, y: c.premium, name: 'Premium',
        type: 'scatter', mode: 'lines',
        line: {{ color: '#58a6ff', width: 2 }},
      }},
      {{
        x: c.times, y: c.vwap, name: 'VWAP',
        type: 'scatter', mode: 'lines',
        line: {{ color: '#d29922', width: 1.5, dash: 'dot' }},
      }},
      {{
        x: c.times, y: c.ema9, name: 'EMA 9',
        type: 'scatter', mode: 'lines',
        line: {{ color: '#bc8cff', width: 1.5, dash: 'dash' }},
      }},
    ];

    Plotly.newPlot(`chart-${{idx}}`, traces, plotlyLayout(c.label, annotations), {{
      responsive:  true,
      displaylogo: false,
      modeBarButtonsToRemove: ['lasso2d', 'select2d'],
    }});
  }});
}}

function buildAlerts() {{
  const body = document.getElementById('alerts-body');
  if (allAlerts.length === 0) {{
    body.innerHTML = '<div class="no-alerts">No crossover alerts in this session</div>';
    return;
  }}

  // Most recent 5 crossovers across all pairs combined
  const recent = [...allAlerts]
    .sort((a, b) => b.time.localeCompare(a.time))
    .slice(0, 5);

  recent.forEach(a => {{
    const color = a.bull ? '#3fb950' : '#f85149';
    const arrow = a.bull ? '▲' : '▼';
    const row = document.createElement('div');
    row.className = 'alert-row';
    row.style.borderLeft = `3px solid ${{color}}`;
    row.innerHTML = `
      <span class="alert-icon" style="color:${{color}}">${{arrow}}</span>
      <div class="alert-info">
        <div class="alert-pair">${{a.pairLabel}}</div>
        <div class="alert-type" style="color:${{color}}">${{a.type}}</div>
      </div>
      <div class="alert-price ${{a.bull ? 'green' : 'red'}}">₹${{a.price.toFixed(2)}}</div>
      <div class="alert-time">${{a.time}}</div>`;
    body.appendChild(row);
  }});
}}

buildCharts();
buildAlerts();
</script>
</body>
</html>"""
    return html


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main():
    run_time = datetime.now().strftime("%d-%b-%Y %I:%M %p")
    print(f"\n{'─'*50}")
    print(f"  Nifty Options Analyzer  |  {run_time}")
    print(f"{'─'*50}\n")

    # 1. ATM Strike
    atm   = 24100 #get_atm_strike()
    pairs = build_pairs(atm)

    # 2. Date range
    date_from, date_to = date_range()
    print(f"Fetching data: {date_from} → {date_to}\n")

    # 3. Fetch + process each pair
    all_alerts   = []
    pairs_data   = []

    for pair in pairs:
        print(f"Processing: {pair['label']}")
        ce_df = fetch_ohlcv(pair["ce_sym"], date_from, date_to)
        pe_df = fetch_ohlcv(pair["pe_sym"], date_from, date_to)

        if ce_df.empty or pe_df.empty:
            pairs_data.append({"label": pair["label"], "df": pd.DataFrame(), "crossovers": []})
            continue

        df         = compute_indicators(ce_df, pe_df)
        crossovers = detect_crossovers(df, pair["label"])

        last = df.iloc[-1]
        print(f"  Premium={last['premium']:.2f}  VWAP={last['vwap']:.2f}  EMA9={last['ema9']:.2f}  Crosses={len(crossovers)}")

        all_alerts.extend(crossovers)
        pairs_data.append({"label": pair["label"], "df": df, "crossovers": crossovers})

    # 4. Telegram alerts
    print(f"\nTotal crossovers detected: {len(all_alerts)}")
    
    send_telegram(all_alerts)

    # 5. Generate HTML
    print("\nGenerating HTML dashboard...")
    html = generate_html(pairs_data, run_time)

    os.makedirs("docs", exist_ok=True)
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("✅ Dashboard written → docs/index.html")


if __name__ == "__main__":
    main()
