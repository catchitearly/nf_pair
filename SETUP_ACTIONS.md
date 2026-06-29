# GitHub Actions · Live Alert Setup Guide

Complete setup to receive Telegram alerts when EMA9 crosses below VWAP on any Nifty option pair — triggered every 5 minutes via cronjob.org.

---

## Architecture

```
cronjob.org  →  GitHub API (repository_dispatch)
                     ↓
              GitHub Actions runner
                     ↓
              run_live.py
              ├── fetcher.py   → Fyers API (today's candles)
              ├── engine.py    → EMA9 + VWAP for 169 pairs
              ├── state.py     → detect crossovers vs last run
              └── notifier.py  → Telegram bot alert
```

State (which pairs were above/below VWAP last run) is cached between GitHub Actions runs using `actions/cache`.

---

## Step 1 — Create a GitHub repo

```bash
git init nifty_options
cd nifty_options
# copy all project files here
git add .
git commit -m "initial"
gh repo create nifty-options --private --source=. --push
```

---

## Step 2 — Add GitHub Actions secrets

Go to your repo → **Settings → Secrets and variables → Actions → New repository secret**

Add these 4 secrets:

| Secret name | Value |
|---|---|
| `FYERS_CLIENT_ID` | Your Fyers app client ID e.g. `XXXXXXXXXXX-100` |
| `FYERS_ACCESS_TOKEN` | Fresh access token (must refresh daily — see Step 5) |
| `TELEGRAM_BOT_TOKEN` | Token from @BotFather e.g. `123456:ABCdef...` |
| `TELEGRAM_CHAT_ID` | Your chat ID (get it from @userinfobot) |

---

## Step 3 — Create a Telegram bot

1. Open Telegram → search **@BotFather** → `/newbot`
2. Choose a name and username → copy the **bot token**
3. Send any message to your new bot
4. Get your **chat ID**: open `https://api.telegram.org/bot<TOKEN>/getUpdates` in browser
   - Find `"chat":{"id":XXXXXXXXX}` — that number is your `TELEGRAM_CHAT_ID`

---

## Step 4 — Set up cronjob.org

1. Sign up at **https://cronjob.org** (free tier allows 5-min intervals)
2. Create a new job:
   - **URL**: `https://api.github.com/repos/YOUR_USER/YOUR_REPO/dispatches`
   - **Method**: `POST`
   - **Headers**:
     ```
     Authorization: Bearer YOUR_GITHUB_PAT
     Accept: application/vnd.github+json
     Content-Type: application/json
     X-GitHub-Api-Version: 2022-11-28
     ```
   - **Body**:
     ```json
     {
       "event_type": "nifty-alert",
       "client_payload": {
         "expiry": "26JUN",
         "alert": "below"
       }
     }
     ```
   - **Schedule**: Every 5 minutes

3. Create a **GitHub PAT** (Personal Access Token):
   - GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens
   - Permissions needed: `Actions: Read and Write`, `Secrets: Read`
   - Paste this PAT into the cronjob.org Authorization header above

> **Timezone note**: cronjob.org runs in UTC. Indian market is UTC+5:30.
> Market hours 09:15–15:30 IST = 03:45–10:00 UTC.
> The script itself checks IST time and exits silently outside market hours,
> so it's safe to run every 5 min all day.

---

## Step 5 — Daily token refresh (important!)

Fyers access tokens expire daily. You must refresh before market open each day.

### Option A — Manual (simplest)
Each morning before 9:15 AM IST:
1. Generate token via Fyers dashboard or your auth script
2. Go to GitHub repo → Settings → Secrets → `FYERS_ACCESS_TOKEN` → Update

### Option B — Semi-automated
Run `refresh_token.py` locally each morning:
```bash
pip install fyers-apiv3 pyotp PyNaCl cryptography
python refresh_token.py \
  --client-id   YOUR_CLIENT_ID \
  --secret-key  YOUR_SECRET_KEY \
  --totp-key    YOUR_TOTP_BASE32_KEY \
  --pin         YOUR_4_DIGIT_PIN \
  --gh-token    YOUR_GITHUB_PAT \
  --gh-repo     username/nifty-options
```
This generates a token and pushes it directly to your GitHub secret.

### Option C — Automated via another GitHub Action
Create a second workflow `refresh_token.yml` that runs at 09:00 IST (03:30 UTC) Mon–Fri,
using a long-lived API key (if Fyers supports it) or a stored refresh token.

---

## Step 6 — Test manually

Trigger a test run from GitHub UI:
1. Go to repo → **Actions** → **Nifty Options · Live Crossover Alert**
2. Click **Run workflow**
3. Set `dry_run = true` to print alerts without sending Telegram
4. Set `expiry = 26JUN`
5. Click **Run workflow**

Check the run logs for output.

---

## Alert message format

You'll receive Telegram messages like:

```
⚡ Nifty Options · EMA9 Crossover Alert
🕐 Candle: 10:45  |  ATM: 24000

🔴 24000C/23800P  crossed BELOW VWAP
   Price: 312.50  EMA9: 308.20  VWAP: 309.15  Δ: -0.31%

🔴 24100C/24000P 🔥  crossed BELOW VWAP
   Price: 285.00  EMA9: 281.60  VWAP: 283.40  Δ: -0.64%
```

- 🔴 = EMA9 crossed below VWAP (bearish)
- 🟢 = EMA9 crossed above VWAP (bullish) — only if `alert=both`
- 🔥 = widening gap (steady momentum)

---

## Workflow inputs reference

| Input | Values | Default | Description |
|---|---|---|---|
| `expiry` | `26JUN`, `26JUN25` | `26JUN` | Option expiry string |
| `alert` | `below`, `above`, `both` | `below` | Which crossovers to notify |
| `dry_run` | `true`, `false` | `false` | Print only, no Telegram |

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `FYERS_ACCESS_TOKEN` error | Token expired — refresh it (Step 5) |
| No candle data | Check if it's a trading day; verify expiry string |
| Telegram not received | Check `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` secrets; make sure you sent a message to the bot first |
| `repository_dispatch` not triggering | Verify PAT has `Actions: write` permission and the `event_type` matches `nifty-alert` |
| Same alert repeated | State cache may have been evicted — this is normal after a gap of >30 min |
