# Nifty Options Pair Dashboard

Fetches Nifty ATM ± 400 strike option data from Fyers, computes pair premiums, VWAP & EMA9, detects crossovers, sends Telegram alerts, and publishes an interactive dashboard to GitHub Pages.

---

## How It Works

```
Manual trigger (GitHub Actions)
  → Fetch Nifty spot → Round to ATM (nearest 100)
  → Build 5 CE+PE pairs (ATM, ±100, ±200, ±300, ±400)
  → Fetch 2-day 5-min OHLCV for each symbol via Fyers
  → Combine pair premiums → compute VWAP + EMA9
  → Detect crossovers → send Telegram alerts
  → Generate HTML dashboard → deploy to GitHub Pages
```

---

## Setup

### 1. Fork / Clone this repo

### 2. Enable GitHub Pages
- Go to **Settings → Pages**
- Source: **Deploy from a branch**
- Branch: `gh-pages` / `/ (root)`

### 3. Add GitHub Secrets
Go to **Settings → Secrets and variables → Actions → New repository secret**

| Secret Name | Where to get it |
|---|---|
| `FYERS_CLIENT_ID` | Fyers API dashboard → My Apps |
| `FYERS_ACCESS_TOKEN` | Generate daily via Fyers auth flow |
| `TELEGRAM_BOT_TOKEN` | Create a bot via [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_ID` | Get via [@userinfobot](https://t.me/userinfobot) or group ID |

### 4. Update Expiry Date
In `scripts/fetch_and_analyze.py`, update this line to the current Tuesday expiry:
```python
EXPIRY_DATE = "26623"   # Format: YYMMDD  e.g. 25 June 2026 = 26625 (if Tuesday)
```

### 5. Run Manually
- Go to **Actions → Nifty Options Dashboard → Run workflow**

### 6. View Dashboard
After the workflow completes, visit:
```
https://<your-username>.github.io/<repo-name>/
```

---

## Pair Structure

| Pair | CE Strike | PE Strike |
|------|-----------|-----------|
| ATM Straddle | ATM | ATM |
| ATM ± 100 | ATM + 100 | ATM − 100 |
| ATM ± 200 | ATM + 200 | ATM − 200 |
| ATM ± 300 | ATM + 300 | ATM − 300 |
| ATM ± 400 | ATM + 400 | ATM − 400 |

---

## Crossover Alerts (Telegram)

Alerts are sent when:
- **Price crosses ↑/↓ VWAP**
- **Price crosses ↑/↓ EMA9**
- **EMA9 crosses ↑/↓ VWAP**

Example message:
```
🚨 Nifty Options Crossover Alerts

🟢 ATM Straddle (24400)
  Signal : Price crossed ↑ VWAP
  Premium: ₹312.50
  Time   : 17-Jun 10:25 AM
```

---

## Notes on Fyers Access Token

Fyers access tokens expire daily. For automation:
- Either regenerate the token manually before each run and update the secret
- Or extend the script to handle TOTP-based token refresh (requires storing TOTP secret securely)
