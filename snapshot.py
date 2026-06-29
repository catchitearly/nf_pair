"""
snapshot.py
Writes two files into docs/ after each live run:
  docs/data.json   — all pair series + ranked lists + crossovers
  docs/index.html  — self-contained dashboard that reads data.json (no server needed)

GitHub Pages serves docs/ as the static site.
"""

import json
import logging
import os
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

DOCS_DIR = os.path.join(os.path.dirname(__file__), "docs")


def write(
    pair_cache:   Dict[str, dict],
    last_idx:     int,
    atm:          int,
    candle_time:  str,
    current_data: Dict[str, dict],
    crossovers:   List[dict],
    mode:         str = "live",
) -> None:
    os.makedirs(DOCS_DIR, exist_ok=True)

    # ── build ranked lists ────────────────────────────────────────────────────
    import engine, config
    case1, case2 = engine.ranked_pairs_at(last_idx)

    # ── build chart series for every pair (up to last_idx) ───────────────────
    charts = {}
    for label, series in pair_cache.items():
        n = min(last_idx + 1, len(series["times"]))
        charts[label] = {
            "times": series["times"][:n],
            "price": [round(v, 2) for v in series["price"][:n]],
            "ema9":  [round(v, 2) for v in series["ema9"][:n]],
            "vwap":  [round(v, 2) for v in series["vwap"][:n]],
        }

    # ── collect all candle timestamps (for slider) ────────────────────────────
    all_times = []
    for series in pair_cache.values():
        all_times = series["times"][:last_idx + 1]
        break

    # Bearish setup history for Tab 3
    # Backtest: scan all candles in one pass (full history available)
    # Live:     read accumulated log from state (built up across 5-min runs)
    import engine as _engine
    if mode == "backtest":
        bearish_history = _engine.detect_bearish_setup_all()
    else:
        import state as _state
        bearish_history = _state.load_bearish_log()

    # Backtest / live trades for Tab 4
    bt_result = _engine.run_backtest(last_idx)

    payload = {
        "meta": {
            "atm":         atm,
            "candle_time": candle_time,
            "candle_count": last_idx + 1,
            "candle_times": all_times,
            "mode":        mode,
            "widening_window": config.WIDENING_WINDOW,
        },
        "case1":           case1,
        "case2":           case2,
        "crossovers":      crossovers,
        "bearish_history": bearish_history,
        "trades":          bt_result["trades"],
        "equity_curve":    bt_result["equity_curve"],
        "bt_summary":      bt_result["summary"],
        "charts":          charts,
    }

    data_path = os.path.join(DOCS_DIR, "data.json")
    with open(data_path, "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    size_kb = os.path.getsize(data_path) / 1024
    logger.info("Snapshot written → %s (%.1f KB)", data_path, size_kb)

    _write_html()


def _write_html() -> None:
    """Copy dashboard/index.html into docs/, patching it to load data.json instead of /api/*"""
    src = os.path.join(os.path.dirname(__file__), "dashboard", "index.html")
    dst = os.path.join(DOCS_DIR, "index.html")

    with open(src) as f:
        html = f.read()

    # Inject the static-mode bootstrap script right before </body>
    static_script = """
<script>
// ── STATIC MODE: load data.json instead of /api/* ────────────────────────────
(async function () {
  showOverlay('Loading snapshot data…');

  let payload;
  try {
    const r = await fetch('data.json');
    payload  = await r.json();
  } catch (e) {
    showOverlay('Failed to load data.json — ' + e.message);
    return;
  }

  const meta = payload.meta;

  // Populate globals the live dashboard normally gets from /api/status + /api/data
  candleTimes  = meta.candle_times;
  case1Data    = payload.case1;
  case2Data    = payload.case2;
  bearish3Data = payload.bearish_history || [];
  tradesData   = payload.trades          || [];
  equityData   = payload.equity_curve    || [];
  btSummary    = payload.bt_summary      || {};
  currentIdx   = meta.candle_count - 1;

  // Patch fetch so chart clicks work against embedded data
  window._staticCharts = payload.charts;
  window._staticMeta   = meta;

  // Fill status bar
  document.getElementById('atm-val').textContent           = meta.atm;
  document.getElementById('candle-count-lbl').textContent  = meta.candle_count;

  // Slider
  const slider   = document.getElementById('timeline-slider');
  slider.max     = meta.candle_count - 1;
  slider.value   = meta.candle_count - 1;
  slider.oninput = function(e) {
    const idx = parseInt(e.target.value);
    currentIdx = idx;
    updateTimeDisplay(idx);
    _staticSliderFilter(idx, payload);
  };

  updateTimeDisplay(currentIdx);

  // Mode badge
  document.getElementById('btn-live').classList.add('active');
  document.getElementById('btn-backtest').classList.remove('active');
  document.getElementById('date-input').disabled = true;
  document.getElementById('load-btn').style.display = 'none';

  // Crossovers
  renderCrossovers(payload.crossovers || []);

  // Render pair lists
  document.getElementById('tab1-count').textContent = case1Data.length;
  document.getElementById('tab2-count').textContent = case2Data.length;
  document.getElementById('tab3-count').textContent = bearish3Data.length;
  document.getElementById('tab4-count').textContent = tradesData.length;
  renderPairList();

  // Override selectPair chart fetch to use embedded data
  window._origSelectPair = window.selectPair;
  window.selectPair = async function(label, rowData, rank) {
    selectedPair = label;
    renderPairList();
    document.getElementById('chart-pair-title').textContent = label;
    const pctEl = document.getElementById('stat-pct');
    pctEl.textContent = (rowData.pct_diff >= 0 ? '+' : '') + rowData.pct_diff.toFixed(2) + '%';
    pctEl.className   = 'val ' + (rowData.ema_gt_vwap ? 'up' : 'down');
    await _staticDrawChart(label, currentIdx);
  };

  setStatus('ok', 'snapshot');
  hideOverlay();
})();

function _staticSliderFilter(idx, payload) {
  // For the static snapshot we can't recompute ranks at arbitrary idx.
  // The pair list always shows the final snapshot ranking.
  renderCrossovers(payload.crossovers || []);
  if (currentTab === 3) {
    renderBearishList();
  } else {
    renderPairList();
  }
  // Redraw chart for whatever is selected (pair key or bearish key)
  if (selectedPair) {
    // Extract real pair label from composite key (label@epoch) used in Tab 3
    const label = selectedPair.includes('@') ? selectedPair.split('@')[0] : selectedPair;
    _staticDrawChart(label, idx);
  }
}

async function _staticDrawChart(label, idx) {
  const series = (window._staticCharts || {})[label];
  if (!series) return;

  const n      = Math.min(idx + 1, series.times.length);
  const times  = series.times.slice(0, n);
  const price  = series.price.slice(0, n);
  const ema9   = series.ema9.slice(0, n);
  const vwap   = series.vwap.slice(0, n);

  const labels = times.map(t => {
    const dt = new Date(t * 1000);
    return String(dt.getHours()).padStart(2,'0') + ':' + String(dt.getMinutes()).padStart(2,'0');
  });

  const last = n - 1;
  document.getElementById('stat-price').textContent = price[last]?.toFixed(2) ?? '—';
  document.getElementById('stat-ema').textContent   = ema9[last]?.toFixed(2)  ?? '—';
  document.getElementById('stat-vwap').textContent  = vwap[last]?.toFixed(2)  ?? '—';
  const isUp = ema9[last] > vwap[last];
  document.getElementById('stat-ema').className  = 'val ' + (isUp ? 'up' : 'down');
  document.getElementById('stat-vwap').className = 'val ' + (isUp ? 'up' : 'down');

  hideEmptyState();
  const ctx = document.getElementById('chart-canvas').getContext('2d');
  if (chart) chart.destroy();
  chart = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [
        { label:'Price', data:price, borderColor:'#8ab4f8', backgroundColor:'rgba(138,180,248,.06)', borderWidth:1.5, pointRadius:0, fill:true,  tension:0.2 },
        { label:'EMA9',  data:ema9,  borderColor:'#00d4aa', borderWidth:2,   pointRadius:0, fill:false, tension:0.2 },
        { label:'VWAP',  data:vwap,  borderColor:'#ff5f7e', borderWidth:2,   pointRadius:0, fill:false, tension:0.2, borderDash:[4,3] },
      ]
    },
    options: {
      responsive:true, maintainAspectRatio:false,
      interaction:{ mode:'index', intersect:false },
      plugins:{
        legend:{ labels:{ color:'#5a6282', font:{family:"'JetBrains Mono',monospace",size:11}, boxWidth:20, padding:16 }},
        tooltip:{ backgroundColor:'#1a1f2e', borderColor:'#2a3045', borderWidth:1, titleColor:'#e8eaf0', bodyColor:'#8ab4f8', titleFont:{family:"'JetBrains Mono',monospace",size:11}, bodyFont:{family:"'JetBrains Mono',monospace",size:11}, padding:10 }
      },
      scales:{
        x:{ ticks:{color:'#5a6282',font:{family:"'JetBrains Mono',monospace",size:10},maxTicksLimit:12}, grid:{color:'#1e2435'} },
        y:{ ticks:{color:'#5a6282',font:{family:"'JetBrains Mono',monospace",size:10}},                  grid:{color:'#1e2435'} }
      }
    }
  });
}
</script>
"""

    html = html.replace("</body>", static_script + "\n</body>")

    with open(dst, "w") as f:
        f.write(html)
    logger.info("Static dashboard written → %s", dst)
