"""
utils/telegram_notifier.py — Send alerts to your Telegram bot.

Uses the Telegram Bot HTTP API directly (no extra library needed).
The bot sends you messages; you receive them on your phone/desktop.
"""

from __future__ import annotations

import html
import sys
import os
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_ENABLED
from utils.logger import get_logger

log = get_logger("telegram")

_BASE_URL  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
_MAX_LEN   = 4000   # Telegram hard limit is 4096; stay under it


def _esc(text: str) -> str:
    """Escape special HTML characters in dynamic data (token names, symbols, etc.)."""
    return html.escape(str(text))


# ─────────────────────────────────────────────────────────────────────────────
# CORE SENDER
# ─────────────────────────────────────────────────────────────────────────────

def _send(text: str, parse_mode: str = "HTML") -> bool:
    """
    Send a message. Automatically splits messages that exceed Telegram's limit.
    Returns True if all parts sent successfully.
    """
    if not TELEGRAM_ENABLED:
        log.debug("Telegram not enabled — skipping message")
        return False

    # Split long messages into chunks
    chunks = [text[i:i+_MAX_LEN] for i in range(0, len(text), _MAX_LEN)]
    all_ok = True

    for chunk in chunks:
        try:
            resp = requests.post(
                _BASE_URL,
                json={
                    "chat_id":    TELEGRAM_CHAT_ID,
                    "text":       chunk,
                    "parse_mode": parse_mode,
                },
                timeout=10,
            )
            if not resp.ok:
                # If HTML parsing fails, retry as plain text
                if resp.status_code == 400 and "parse" in resp.text.lower():
                    log.warning("HTML parse error — retrying as plain text")
                    resp2 = requests.post(
                        _BASE_URL,
                        json={"chat_id": TELEGRAM_CHAT_ID, "text": chunk},
                        timeout=10,
                    )
                    if not resp2.ok:
                        log.warning(f"Telegram send failed (plain): {resp2.status_code} {resp2.text[:200]}")
                        all_ok = False
                else:
                    log.warning(f"Telegram send failed: {resp.status_code} {resp.text[:300]}")
                    all_ok = False
        except Exception as e:
            log.warning(f"Telegram error: {e}")
            all_ok = False

    return all_ok


# ─────────────────────────────────────────────────────────────────────────────
# PUMP.FUN SCAN RESULTS — called directly from scan_pumpfun()
# ─────────────────────────────────────────────────────────────────────────────

def alert_pumpfun_scan(tokens: list) -> bool:
    """
    Send formatted Pump.fun launch list to Telegram.
    Handles special characters in token names safely.
    """
    if not tokens:
        return _send("🔍 <b>Pump.fun Scan</b>\nNo launches found.")

    lines = ["🔍 <b>Pump.fun Latest Launches</b>", "━━━━━━━━━━━━━━━━━━━━"]
    for t in tokens:
        status = "✅ Grad" if t.complete else "🆕 Live"
        symbol = _esc(t.symbol)
        name   = _esc(t.name[:20])
        lines.append(
            f"{status} <b>{symbol}</b> — {name}\n"
            f"  MCap: <b>${t.market_cap_usd:,.0f}</b> | "
            f"Price: {t.price_sol:.8f} SOL"
        )

    return _send("\n".join(lines))


# ─────────────────────────────────────────────────────────────────────────────
# PAPER TRADING ALERTS
# ─────────────────────────────────────────────────────────────────────────────

def alert_paper_buy(symbol: str, name: str, market_cap: float,
                    price_sol: float, amount_sol: float):
    text = (
        f"📋 <b>PAPER BUY — Pump.fun</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Token:      <b>{_esc(symbol)}</b> ({_esc(name)})\n"
        f"Market Cap: <b>${market_cap:,.0f}</b>\n"
        f"Price:      {price_sol:.8f} SOL\n"
        f"Spent:      {amount_sol} SOL (virtual)\n"
        f"<i>Paper trade — no real money spent</i>"
    )
    _send(text)


def alert_paper_sell(symbol: str, entry_sol: float, exit_sol: float,
                     amount_sol: float, reason: str):
    pnl_pct = (exit_sol - entry_sol) / entry_sol * 100 if entry_sol > 0 else 0
    pnl_sol = (exit_sol - entry_sol) / entry_sol * amount_sol
    emoji   = "✅" if pnl_pct >= 0 else "❌"
    text = (
        f"{emoji} <b>PAPER SELL — {_esc(symbol)}</b>\n"
        f"Entry: {entry_sol:.8f} SOL\n"
        f"Exit:  {exit_sol:.8f} SOL\n"
        f"PnL:   <b>{pnl_pct:+.1f}%</b> ({pnl_sol:+.4f} SOL)\n"
        f"Reason: {_esc(reason)}\n"
        f"<i>Paper trade — no real money</i>"
    )
    _send(text)


def alert_paper_portfolio(positions: list[dict], total_pnl_sol: float):
    """Send paper trading portfolio summary."""
    if not positions:
        _send("📭 <b>Paper Portfolio</b>\nNo open positions.")
        return

    lines = [f"📋 <b>Paper Portfolio</b>", "━━━━━━━━━━━━━━━━━━━━"]
    for p in positions:
        pnl_pct = p.get("pnl_pct", 0)
        emoji   = "🟢" if pnl_pct >= 0 else "🔴"
        lines.append(
            f"{emoji} <b>{_esc(p['symbol'])}</b>\n"
            f"   Entry: {p['entry_price']:.8f} SOL\n"
            f"   Now:   {p['current_price']:.8f} SOL\n"
            f"   PnL:   <b>{pnl_pct:+.1f}%</b>\n"
            f"   Size:  {p['sol_spent']} SOL"
        )

    pnl_emoji = "✅" if total_pnl_sol >= 0 else "❌"
    lines.append(f"\n{pnl_emoji} Total PnL: <b>{total_pnl_sol:+.4f} SOL</b>")
    _send("\n".join(lines))


# ─────────────────────────────────────────────────────────────────────────────
# STANDARD ALERTS
# ─────────────────────────────────────────────────────────────────────────────

def alert_bot_started(dry_run: bool, paper_mode: bool, pumpfun_on: bool,
                      eth_on: bool, base_on: bool):
    if paper_mode:
        mode = "📋 PAPER TRADING (devnet — no real money)"
    elif dry_run:
        mode = "🔍 DRY-RUN (analysis only)"
    else:
        mode = "🔴 LIVE TRADING"

    text = (
        f"🤖 <b>Crypto Narrative Bot Started</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Mode:       <b>{mode}</b>\n"
        f"Pump.fun:   {'✅ ON' if pumpfun_on else '❌ OFF'}\n"
        f"Ethereum:   {'✅ ON' if eth_on else '❌ OFF'}\n"
        f"Base Chain: {'✅ ON' if base_on else '❌ OFF'}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Sources: CoinGecko · CMC · CoinTelegraph · CoinDesk · Binance · WatcherGuru"
    )
    _send(text)


def alert_bot_stopped():
    _send("🛑 <b>Crypto Narrative Bot stopped.</b>")


def alert_hype_detected(token_name: str, score: float, source: str = ""):
    src  = f" via {_esc(source)}" if source else ""
    text = (
        f"🔥 <b>Hype Detected{src}</b>\n"
        f"Token:  <code>{_esc(token_name)}</code>\n"
        f"Score:  <b>{score:.2f}</b> / 1.00\n"
        f"Action: Searching Pump.fun..."
    )
    _send(text)


def alert_buy_signal(coin: str, score: float, narrative: str, source: str = "pump.fun"):
    text = (
        f"🟢 <b>BUY SIGNAL — {_esc(coin)}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Score:     <b>{score:.2f}</b>\n"
        f"Narrative: {_esc(narrative)}\n"
        f"Exchange:  {_esc(source)}"
    )
    _send(text)


def alert_pumpfun_buy(symbol: str, name: str, market_cap: float,
                      price_sol: float, amount_sol: float, dry_run: bool):
    label = "🔍 DRY-RUN BUY" if dry_run else "💸 BUY EXECUTED"
    text = (
        f"{label} — Pump.fun\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Token:      <b>{_esc(symbol)}</b> ({_esc(name)})\n"
        f"Market Cap: <b>${market_cap:,.0f}</b>\n"
        f"Price:      {price_sol:.8f} SOL\n"
        f"Spent:      {amount_sol} SOL"
    )
    _send(text)


def alert_sell_signal(coin: str, score: float, reason: str = ""):
    text = (
        f"🔴 <b>SELL SIGNAL — {_esc(coin)}</b>\n"
        f"Score:  <b>{score:.2f}</b>\n"
        f"Reason: {_esc(reason) or 'Sentiment dropped below threshold'}"
    )
    _send(text)


def alert_exit(coin: str, pnl_pct: float, reason: str):
    emoji = "✅" if pnl_pct >= 0 else "❌"
    text = (
        f"{emoji} <b>EXIT — {_esc(coin)}</b>\n"
        f"P&L:    <b>{pnl_pct:+.1f}%</b>\n"
        f"Reason: {_esc(reason)}"
    )
    _send(text)


def alert_cycle_summary(cycle_num: int, posts_fetched: int,
                        coins_analyzed: int, signals: dict[str, str]):
    if not signals:
        signals_text = "No signals this cycle."
    else:
        lines = []
        for coin, signal in signals.items():
            emoji = {"BUY": "🟢", "SELL": "🔴", "HOLD": "⚪"}.get(signal, "•")
            lines.append(f"  {emoji} {_esc(coin)}: {signal}")
        signals_text = "\n".join(lines)

    text = (
        f"📊 <b>Cycle #{cycle_num} Summary</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Posts fetched:   {posts_fetched}\n"
        f"Coins analyzed:  {coins_analyzed}\n"
        f"\n<b>Signals:</b>\n{signals_text}"
    )
    _send(text)


def alert_error(message: str):
    text = f"⚠️ <b>Bot Error</b>\n<code>{_esc(message[:400])}</code>"
    _send(text)


def alert_trending_coins(trending: list[str]):
    if not trending:
        return
    coins_text = " · ".join(f"<code>{_esc(c)}</code>" for c in trending[:10])
    _send(f"📈 <b>Trending Now</b>\n{coins_text}")


# ─────────────────────────────────────────────────────────────────────────────
# SETUP TEST
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Testing Telegram connection...")
    if not TELEGRAM_ENABLED:
        print("ERROR: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set in .env")
        sys.exit(1)

    ok = _send(
        "✅ <b>Telegram bot is working!</b>\n"
        "Your Crypto Narrative Bot is connected.\n"
        "You will receive alerts here."
    )
    if ok:
        print("SUCCESS — check your Telegram.")
    else:
        print("FAILED — check your token and chat ID in .env")
