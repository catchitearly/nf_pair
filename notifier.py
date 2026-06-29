"""
notifier.py
Sends Telegram messages via Bot API.
Credentials come from environment variables (set as GitHub Actions secrets):
  TELEGRAM_BOT_TOKEN  — bot token from @BotFather
  TELEGRAM_CHAT_ID    — your chat/channel ID
"""

import json
import logging
import os
import urllib.request
import urllib.parse
import urllib.error

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def _token() -> str:
    t = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not t:
        raise EnvironmentError("TELEGRAM_BOT_TOKEN not set")
    return t


def _chat_id() -> str:
    c = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not c:
        raise EnvironmentError("TELEGRAM_CHAT_ID not set")
    return c


def send(text: str) -> bool:
    """Send a plain-text message. Returns True on success."""
    try:
        url     = TELEGRAM_API.format(token=_token())
        payload = json.dumps({
            "chat_id":    _chat_id(),
            "text":       text,
            "parse_mode": "HTML",
        }).encode()
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if result.get("ok"):
                logger.info("Telegram sent: %s", text[:60])
                return True
            else:
                logger.warning("Telegram error: %s", result)
                return False
    except urllib.error.URLError as e:
        logger.error("Telegram network error: %s", e)
        return False
    except Exception as e:
        logger.error("Telegram unexpected error: %s", e)
        return False


def send_crossover_alert(crossovers: list, candle_time: str, atm: int) -> None:
    """
    Format and send a crossover alert message.
    `crossovers` is a list of dicts with keys:
      label, cross_dir, pct_diff, price, ema9, vwap, widening_score
    """
    if not crossovers:
        return

    lines = [
        f"⚡ <b>Nifty Options · EMA9 Crossover Alert</b>",
        f"🕐 Candle: <b>{candle_time}</b>  |  ATM: <b>{atm}</b>",
        "",
    ]

    for c in crossovers:
        if c["cross_dir"] == "below":
            emoji = "🔴"
            direction = "crossed BELOW VWAP"
        else:
            emoji = "🟢"
            direction = "crossed ABOVE VWAP"

        widening = " 🔥" if c.get("widening_score", 0) > 0.3 else ""
        sign     = "+" if c["pct_diff"] >= 0 else ""

        lines += [
            f"{emoji} <b>{c['label']}</b>{widening}  {direction}",
            f"   Price: <code>{c['price']:.2f}</code>  "
            f"EMA9: <code>{c['ema9']:.2f}</code>  "
            f"VWAP: <code>{c['vwap']:.2f}</code>  "
            f"Δ: <code>{sign}{c['pct_diff']:.2f}%</code>",
            "",
        ]

    send("\n".join(lines).strip())
