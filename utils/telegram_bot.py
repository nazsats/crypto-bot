"""
utils/telegram_bot.py — Telegram command handler with inline buttons.

Every alert and response uses clickable inline keyboard buttons.
No need to type commands — just tap buttons on your phone.

Main menu buttons:
  [📊 Status] [💼 Portfolio] [📈 Trending]
  [🔍 Scan]   [⏸ Pause]     [▶ Resume]

Alert buttons (on buy alerts):
  [✅ Confirm Buy] [❌ Skip]

Position buttons (on portfolio):
  [💸 Sell <TOKEN>] for each position
"""

from __future__ import annotations

import html
import json
import threading
import time
import requests

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_ENABLED
from utils.logger import get_logger

log = get_logger("tg_bot")

_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


# ─────────────────────────────────────────────────────────────────────────────
# LOW-LEVEL SEND HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _send(chat_id: str, text: str, reply_markup: dict = None):
    """Send a message with optional inline keyboard."""
    payload = {
        "chat_id":    chat_id,
        "text":       text,
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        resp = requests.post(f"{_BASE}/sendMessage", json=payload, timeout=10)
        if not resp.ok:
            # Retry without HTML if parse error
            if resp.status_code == 400 and "parse" in resp.text.lower():
                payload["parse_mode"] = None
                payload.pop("parse_mode")
                requests.post(f"{_BASE}/sendMessage", json=payload, timeout=10)
            else:
                log.warning(f"Telegram send failed: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        log.warning(f"Telegram send error: {e}")


def _answer_callback(callback_query_id: str, text: str = ""):
    """Acknowledge a button press (removes loading spinner)."""
    try:
        requests.post(f"{_BASE}/answerCallbackQuery", json={
            "callback_query_id": callback_query_id,
            "text": text,
        }, timeout=5)
    except Exception:
        pass


def _get_updates(offset: int) -> list[dict]:
    try:
        resp = requests.get(
            f"{_BASE}/getUpdates",
            params={"offset": offset, "timeout": 20,
                    "allowed_updates": ["message", "callback_query"]},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("result", [])
    except Exception as e:
        log.warning(f"Telegram getUpdates error: {e}")
        return []


def _only_owner(chat_id: str) -> bool:
    return str(chat_id) == str(TELEGRAM_CHAT_ID)


# ─────────────────────────────────────────────────────────────────────────────
# KEYBOARD LAYOUTS
# ─────────────────────────────────────────────────────────────────────────────

MAIN_MENU = {
    "inline_keyboard": [
        [
            {"text": "📊 Status",    "callback_data": "cmd_status"},
            {"text": "💼 Portfolio", "callback_data": "cmd_portfolio"},
            {"text": "📈 Trending",  "callback_data": "cmd_trending"},
        ],
        [
            {"text": "🔍 Scan Pump.fun", "callback_data": "cmd_scan"},
            {"text": "⏸ Pause",         "callback_data": "cmd_pause"},
            {"text": "▶ Resume",         "callback_data": "cmd_resume"},
        ],
    ]
}

def _sell_keyboard(positions: dict) -> dict:
    """Build a keyboard with one Sell button per open position."""
    buttons = []
    for mint, pos in positions.items():
        buttons.append([{
            "text": f"💸 Sell {html.escape(pos.symbol)}",
            "callback_data": f"sell_{mint}",
        }])
    buttons.append([{"text": "🔙 Back to Menu", "callback_data": "cmd_menu"}])
    return {"inline_keyboard": buttons}

def _confirm_buy_keyboard(token_name: str, sol: float) -> dict:
    return {"inline_keyboard": [[
        {"text": "✅ Buy Now",  "callback_data": f"confirm_buy_{token_name}_{sol}"},
        {"text": "❌ Skip",    "callback_data": "skip_buy"},
    ]]}

BACK_MENU = {"inline_keyboard": [[
    {"text": "🔙 Main Menu", "callback_data": "cmd_menu"}
]]}


# ─────────────────────────────────────────────────────────────────────────────
# COMMAND HANDLERS
# ─────────────────────────────────────────────────────────────────────────────

def _cmd_menu(chat_id: str, _state=None):
    _send(chat_id,
        "🤖 <b>Crypto Narrative Bot</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Choose an action below 👇",
        reply_markup=MAIN_MENU,
    )


def _cmd_start(chat_id: str, _state=None):
    _send(chat_id,
        "👋 <b>Welcome to your Crypto Bot!</b>\n\n"
        "Use the buttons below to control everything.\n"
        "You can also type commands:\n\n"
        "/status · /portfolio · /scan\n"
        "/trending · /pause · /resume\n"
        "/buy &lt;token&gt; &lt;sol&gt; · /sell &lt;mint&gt;",
        reply_markup=MAIN_MENU,
    )


def _cmd_status(chat_id: str, state):
    paused_str = "⏸ PAUSED" if state.paused else "▶ RUNNING"
    mode_str   = "📋 PAPER" if state.paper_trading else ("🔍 DRY-RUN" if state.dry_run else "🔴 LIVE")

    signals = state.latest_signals
    if signals:
        sig_lines = "\n".join(
            f"  {'🟢' if s=='BUY' else '🔴' if s=='SELL' else '⚪'} {c}: {s}"
            for c, s in signals.items()
        )
    else:
        sig_lines = "  Waiting for first cycle..."

    _send(chat_id,
        f"📊 <b>Bot Status</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"State:       {paused_str}\n"
        f"Mode:        {mode_str}\n"
        f"Uptime:      {state.uptime_str()}\n"
        f"Cycles run:  {state.cycle_count}\n"
        f"Last cycle:  {state.last_cycle_ago()}\n"
        f"Posts/cycle: {state.posts_last_cycle}\n"
        f"\n<b>Latest Signals:</b>\n{sig_lines}",
        reply_markup=BACK_MENU,
    )


def _cmd_portfolio(chat_id: str, state):
    # ── Paper trading ────────────────────────────────────────────────
    if state.paper_trading and state.paper_ledger:
        ledger  = state.paper_ledger
        pumpfun = state.pumpfun
        summary = ledger.portfolio_summary(
            pumpfun.get_token_by_mint if pumpfun else lambda m: None
        )
        lines = ["📋 <b>Paper Portfolio</b>", "━━━━━━━━━━━━━━━━━━━━"]

        if summary["positions"]:
            for p in summary["positions"]:
                emoji = "🟢" if p["pnl_pct"] >= 0 else "🔴"
                lines.append(
                    f"{emoji} <b>{html.escape(p['symbol'])}</b>\n"
                    f"   Entry: {p['entry_price']:.8f} SOL\n"
                    f"   Now:   {p['current_price']:.8f} SOL\n"
                    f"   PnL:   <b>{p['pnl_pct']:+.1f}%</b> | {p['sol_spent']:.4f} SOL"
                )
        else:
            lines.append("No open positions.")

        pnl_e = "✅" if summary["total_pnl"] >= 0 else "❌"
        lines += [
            "━━━━━━━━━━━━━━━━━━━━",
            f"💰 Balance:     <b>{summary['virtual_sol']:.4f} SOL</b>",
            f"{pnl_e} Total PnL:  <b>{summary['total_pnl']:+.4f} SOL</b>",
            f"🎯 Win rate:    {summary['win_rate']:.0f}% ({summary['total_trades']} trades)",
        ]
        _send(chat_id, "\n".join(lines), reply_markup=BACK_MENU)
        return

    # ── Live positions ───────────────────────────────────────────────
    pumpfun = state.pumpfun
    if not pumpfun or not pumpfun.positions:
        _send(chat_id, "📭 <b>Portfolio</b>\nNo open positions.", reply_markup=BACK_MENU)
        return

    lines = ["💼 <b>Live Positions</b>", "━━━━━━━━━━━━━━━━━━━━"]
    for mint, pos in pumpfun.positions.items():
        current = pumpfun.get_token_by_mint(mint)
        if current:
            pnl_pct = (current.price_sol - pos.entry_price_sol) / pos.entry_price_sol * 100
            emoji   = "🟢" if pnl_pct >= 0 else "🔴"
            lines.append(
                f"{emoji} <b>{html.escape(pos.symbol)}</b>\n"
                f"   Entry: {pos.entry_price_sol:.8f} SOL\n"
                f"   Now:   {current.price_sol:.8f} SOL\n"
                f"   PnL:   <b>{pnl_pct:+.1f}%</b>"
            )
        else:
            lines.append(f"• <b>{html.escape(pos.symbol)}</b> — price unavailable")

    _send(chat_id, "\n".join(lines),
          reply_markup=_sell_keyboard(pumpfun.positions))


def _cmd_buy(chat_id: str, args: list[str], state):
    if len(args) < 2:
        _send(chat_id,
            "💸 <b>Manual Buy</b>\n"
            "Usage: <code>/buy TOKEN SOL_AMOUNT</code>\n"
            "Example: <code>/buy PEPE 0.05</code>",
            reply_markup=BACK_MENU,
        )
        return

    token_query = args[0]
    try:
        sol_amount = float(args[1])
    except ValueError:
        _send(chat_id, "❌ Invalid SOL amount. Example: <code>/buy PEPE 0.05</code>")
        return

    if sol_amount > 1.0:
        _send(chat_id, "⚠️ Max manual buy is 1.0 SOL for safety.")
        return

    pumpfun = state.pumpfun
    if not pumpfun:
        _send(chat_id, "❌ Pump.fun not initialized.")
        return

    _send(chat_id, f"🔍 Searching Pump.fun for <b>{html.escape(token_query)}</b>...")

    token = pumpfun.find_token(token_query)
    if not token:
        _send(chat_id, f"❌ <code>{html.escape(token_query)}</code> not found on Pump.fun.",
              reply_markup=BACK_MENU)
        return

    if token.complete:
        _send(chat_id, f"⚠️ <b>{html.escape(token.symbol)}</b> already graduated to Raydium.",
              reply_markup=BACK_MENU)
        return

    _send(chat_id,
        f"🎯 Found: <b>{html.escape(token.symbol)}</b> — {html.escape(token.name)}\n"
        f"Market cap: <b>${token.market_cap_usd:,.0f}</b>\n"
        f"Price:      {token.price_sol:.8f} SOL\n\n"
        f"Buy <b>{sol_amount} SOL</b> worth?",
        reply_markup=_confirm_buy_keyboard(token.symbol, sol_amount),
    )


def _handle_confirm_buy(chat_id: str, data: str, state):
    """Handle the ✅ Buy Now button press."""
    parts      = data.split("_", 3)   # confirm_buy_SYMBOL_SOL
    token_name = parts[2] if len(parts) > 2 else ""
    sol_amount = float(parts[3]) if len(parts) > 3 else 0.05

    pumpfun = state.pumpfun
    if not pumpfun:
        _send(chat_id, "❌ Pump.fun not initialized.")
        return

    token = pumpfun.find_token(token_name)
    if not token:
        _send(chat_id, f"❌ {html.escape(token_name)} no longer available.", reply_markup=BACK_MENU)
        return

    import config as cfg
    original = cfg.PUMPFUN_BUY_SOL
    cfg.PUMPFUN_BUY_SOL = sol_amount

    if state.paper_trading and state.paper_ledger:
        success = state.paper_ledger.buy(token)
        result_msg = f"📋 Paper buy recorded: <b>{html.escape(token.symbol)}</b> for {sol_amount} SOL"
    else:
        success = pumpfun.buy(token)
        result_msg = (
            f"✅ <b>Buy executed!</b> {html.escape(token.symbol)} for {sol_amount} SOL"
            if success else
            f"❌ Buy failed — check logs."
        )

    cfg.PUMPFUN_BUY_SOL = original
    _send(chat_id, result_msg, reply_markup=BACK_MENU)


def _cmd_sell(chat_id: str, args: list[str], state):
    pumpfun = state.pumpfun

    if not args:
        if pumpfun and pumpfun.positions:
            _send(chat_id, "Choose a position to sell:",
                  reply_markup=_sell_keyboard(pumpfun.positions))
        else:
            _send(chat_id, "📭 No open positions to sell.", reply_markup=BACK_MENU)
        return

    mint = args[0]
    if not pumpfun or mint not in pumpfun.positions:
        _send(chat_id, f"❌ No open position for that mint.", reply_markup=BACK_MENU)
        return

    pos = pumpfun.positions[mint]
    _send(chat_id, f"💸 Selling <b>{html.escape(pos.symbol)}</b>...")
    success = pumpfun.sell(mint)

    if success:
        _send(chat_id, f"✅ Sold <b>{html.escape(pos.symbol)}</b>", reply_markup=BACK_MENU)
    elif state.dry_run or state.paper_trading:
        _send(chat_id, f"📋 Paper sell recorded for <b>{html.escape(pos.symbol)}</b>", reply_markup=BACK_MENU)
    else:
        _send(chat_id, f"❌ Sell failed — check logs.", reply_markup=BACK_MENU)


def _handle_sell_button(chat_id: str, mint: str, state):
    """Handle a Sell <TOKEN> button press from portfolio view."""
    pumpfun = state.pumpfun
    if not pumpfun or mint not in pumpfun.positions:
        _send(chat_id, "❌ Position not found (may have been auto-sold).", reply_markup=BACK_MENU)
        return
    pos = pumpfun.positions[mint]
    _send(chat_id, f"💸 Selling <b>{html.escape(pos.symbol)}</b>...")
    pumpfun.sell(mint)
    _send(chat_id, f"✅ <b>{html.escape(pos.symbol)}</b> sold.", reply_markup=BACK_MENU)


def _cmd_pause(chat_id: str, state):
    state.pause()
    _send(chat_id,
        "⏸ <b>Auto-trading paused.</b>\n"
        "The bot keeps monitoring — it just won't open new positions.\n"
        "Tap ▶ Resume when ready.",
        reply_markup={"inline_keyboard": [[
            {"text": "▶ Resume Trading", "callback_data": "cmd_resume"},
            {"text": "📊 Status",        "callback_data": "cmd_status"},
        ]]}
    )


def _cmd_resume(chat_id: str, state):
    state.resume()
    _send(chat_id, "▶ <b>Auto-trading resumed!</b>", reply_markup=MAIN_MENU)


def _cmd_scan(chat_id: str, state):
    pumpfun = state.pumpfun
    if not pumpfun:
        _send(chat_id, "❌ Pump.fun not initialized.", reply_markup=BACK_MENU)
        return

    _send(chat_id, "🔍 Scanning Pump.fun — please wait...")
    tokens = pumpfun.get_new_launches(limit=12)

    if not tokens:
        _send(chat_id, "No launches found.", reply_markup=BACK_MENU)
        return

    lines = ["🚀 <b>Latest Pump.fun Launches</b>", "━━━━━━━━━━━━━━━━━━━━"]
    buy_buttons = []

    for t in tokens[:12]:
        status = "✅ Grad" if t.complete else "🆕 Live"
        lines.append(
            f"{status} <b>{html.escape(t.symbol)}</b> — {html.escape(t.name[:16])}\n"
            f"  MCap: <b>${t.market_cap_usd:,.0f}</b> | {t.price_sol:.8f} SOL"
        )
        if not t.complete and t.market_cap_usd < 50_000:
            buy_buttons.append([{
                "text": f"🛒 Buy {html.escape(t.symbol)} (0.05 SOL)",
                "callback_data": f"confirm_buy_{t.symbol}_0.05",
            }])

    buy_buttons.append([{"text": "🔙 Main Menu", "callback_data": "cmd_menu"}])
    _send(chat_id, "\n".join(lines), reply_markup={"inline_keyboard": buy_buttons})


def _cmd_trending(chat_id: str, _state=None):
    try:
        resp = requests.get(
            "https://api.coingecko.com/api/v3/search/trending",
            headers={"accept": "application/json"}, timeout=10,
        )
        resp.raise_for_status()
        coins = resp.json().get("coins", [])

        lines = ["📈 <b>Trending on CoinGecko</b>", "━━━━━━━━━━━━━━━━━━━━"]
        buy_buttons = []
        for i, entry in enumerate(coins[:7], 1):
            item   = entry.get("item", {})
            name   = html.escape(item.get("name", "?"))
            symbol = html.escape(item.get("symbol", "?"))
            rank   = item.get("market_cap_rank", "?")
            lines.append(f"{i}. <b>{name}</b> (<code>{symbol}</code>) — Rank #{rank}")
            buy_buttons.append([{
                "text": f"🛒 Snipe {symbol} on Pump.fun",
                "callback_data": f"confirm_buy_{symbol}_0.05",
            }])

        buy_buttons.append([{"text": "🔙 Main Menu", "callback_data": "cmd_menu"}])
        _send(chat_id, "\n".join(lines), reply_markup={"inline_keyboard": buy_buttons})
    except Exception as e:
        _send(chat_id, f"❌ Could not fetch trending: {e}", reply_markup=BACK_MENU)


# ─────────────────────────────────────────────────────────────────────────────
# DISPATCHER
# ─────────────────────────────────────────────────────────────────────────────

def _dispatch_message(message: dict, state):
    chat_id = str(message.get("chat", {}).get("id", ""))
    text    = message.get("text", "").strip()

    if not text or not text.startswith("/"):
        return
    if not _only_owner(chat_id):
        _send(chat_id, "Unauthorized.")
        return

    parts   = text.split()
    command = parts[0].lower().split("@")[0]
    args    = parts[1:]

    handlers = {
        "/start":     lambda: _cmd_start(chat_id),
        "/help":      lambda: _cmd_start(chat_id),
        "/menu":      lambda: _cmd_menu(chat_id),
        "/status":    lambda: _cmd_status(chat_id, state),
        "/portfolio": lambda: _cmd_portfolio(chat_id, state),
        "/buy":       lambda: _cmd_buy(chat_id, args, state),
        "/sell":      lambda: _cmd_sell(chat_id, args, state),
        "/pause":     lambda: _cmd_pause(chat_id, state),
        "/resume":    lambda: _cmd_resume(chat_id, state),
        "/scan":      lambda: _cmd_scan(chat_id, state),
        "/trending":  lambda: _cmd_trending(chat_id),
    }

    handler = handlers.get(command)
    if handler:
        try:
            handler()
        except Exception as e:
            log.error(f"Command {command} error: {e}", exc_info=True)
            _send(chat_id, f"⚠️ Error: {html.escape(str(e))}")
    else:
        _send(chat_id, f"Unknown command. Use /menu to see options.", reply_markup=MAIN_MENU)


def _dispatch_callback(callback_query: dict, state):
    """Handle button presses (inline keyboard callbacks)."""
    query_id = callback_query.get("id", "")
    chat_id  = str(callback_query.get("message", {}).get("chat", {}).get("id", ""))
    data     = callback_query.get("data", "")

    if not _only_owner(chat_id):
        _answer_callback(query_id, "Unauthorized")
        return

    _answer_callback(query_id)   # dismiss loading spinner immediately

    # Route callback data
    if data == "cmd_menu":          _cmd_menu(chat_id)
    elif data == "cmd_status":      _cmd_status(chat_id, state)
    elif data == "cmd_portfolio":   _cmd_portfolio(chat_id, state)
    elif data == "cmd_trending":    _cmd_trending(chat_id)
    elif data == "cmd_scan":        _cmd_scan(chat_id, state)
    elif data == "cmd_pause":       _cmd_pause(chat_id, state)
    elif data == "cmd_resume":      _cmd_resume(chat_id, state)
    elif data == "skip_buy":        _send(chat_id, "⏭ Skipped.", reply_markup=BACK_MENU)
    elif data.startswith("confirm_buy_"):  _handle_confirm_buy(chat_id, data, state)
    elif data.startswith("sell_"):
        mint = data[5:]             # strip "sell_" prefix
        _handle_sell_button(chat_id, mint, state)
    else:
        _send(chat_id, f"Unknown action: {data}")


# ─────────────────────────────────────────────────────────────────────────────
# POLLING LOOP
# ─────────────────────────────────────────────────────────────────────────────

def _polling_loop(state):
    log.info("Telegram bot polling started")
    offset = 0
    while True:
        try:
            updates = _get_updates(offset)
            for update in updates:
                offset = update["update_id"] + 1
                if "message" in update:
                    _dispatch_message(update["message"], state)
                elif "callback_query" in update:
                    _dispatch_callback(update["callback_query"], state)
        except Exception as e:
            log.warning(f"Polling loop error: {e}")
            time.sleep(5)


def start_command_handler(state) -> threading.Thread:
    if not TELEGRAM_ENABLED:
        log.warning("Telegram not configured — command handler disabled")
        return None

    thread = threading.Thread(
        target=_polling_loop,
        args=(state,),
        daemon=True,
        name="TelegramCmdHandler",
    )
    thread.start()
    log.info("Telegram command handler started")
    return thread
