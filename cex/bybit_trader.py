"""
cex/bybit_trader.py — CEX trader supporting three modes:

  INTERNAL_PAPER  (default, no API key needed)
    → Simulates trades internally using live Binance prices.
    → Start here — zero risk, instant setup.

  BYBIT_DEMO      (Bybit Demo account, fake USDT)
    → Uses Bybit's built-in testnet via ccxt.
    → Requires Bybit Demo API key (free, from demo.bybit.com).
    → Trades look exactly like live — same order book, same UI.

  BYBIT_LIVE      (real Bybit account, real money)
    → Flip CEX_MODE=bybit_live when confident in paper results.
    → Requires real Bybit API key.

Strategy — Top Gainer + Sentiment Fusion:
  Every cycle the main bot calls receive_signals(signals).
  CEXTrader cross-checks signals with state.cex_gainers.
  If a coin is:
    ① In top gainers (up > CEX_MIN_GAIN_PCT in 24h), AND
    ② Has a BUY sentiment signal, AND
    ③ We have fewer than CEX_MAX_POSITIONS open
  → Execute a paper/demo/live buy for CEX_USDT_PER_TRADE USDT.

Exits are checked every 60 seconds:
  → Take profit at +CEX_TAKE_PROFIT_PCT %
  → Stop loss  at -CEX_STOP_LOSS_PCT %
"""

from __future__ import annotations

import threading
import time
from typing import Optional

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from utils.logger import get_logger
from utils.terminal_ui import log_activity
from cex.cex_ledger import CEXLedger
from cex.top_gainers import TopGainersFetcher, GainerCoin

log = get_logger("bybit_trader")

# ── Try importing ccxt (optional — only needed for Bybit Demo/Live) ──────────
try:
    import ccxt
    CCXT_AVAILABLE = True
except ImportError:
    CCXT_AVAILABLE = False
    log.info("ccxt not installed — Bybit Demo/Live mode unavailable. Using internal paper mode.")


class CEXTrader:
    """
    Unified CEX trader. Handles internal paper, Bybit Demo, and Bybit Live.

    Args:
        mode:            "internal_paper" | "bybit_demo" | "bybit_live"
        starting_usdt:   virtual USDT balance for paper mode
        usdt_per_trade:  how much USDT to spend per trade
        take_profit_pct: exit long at this % gain (e.g. 20 = +20%)
        stop_loss_pct:   exit long at this % loss (e.g. 10 = -10%)
        max_positions:   max concurrent open trades
        min_gain_pct:    minimum 24h gain% for a coin to qualify as a gainer
        min_sentiment:   minimum AI sentiment score to trigger a buy (0.0-1.0)
        api_key:         Bybit API key (Demo or Live)
        api_secret:      Bybit API secret
    """

    def __init__(
        self,
        mode:            str   = "internal_paper",
        starting_usdt:   float = 1000.0,
        usdt_per_trade:  float = 50.0,
        take_profit_pct: float = 20.0,
        stop_loss_pct:   float = 10.0,
        max_positions:   int   = 5,
        min_gain_pct:    float = 5.0,
        min_sentiment:   float = 0.65,
        api_key:         str   = "",
        api_secret:      str   = "",
    ):
        self.mode            = mode.lower()
        self.usdt_per_trade  = usdt_per_trade
        self.max_positions   = max_positions
        self.min_gain_pct    = min_gain_pct
        self.min_sentiment   = min_sentiment

        # Internal paper ledger (used for all modes as local record)
        self.ledger = CEXLedger(
            starting_usdt   = starting_usdt,
            take_profit_pct = take_profit_pct,
            stop_loss_pct   = stop_loss_pct,
        )

        # ccxt exchange (Bybit Demo or Live)
        self._exchange: Optional[object] = None
        if self.mode in ("bybit_demo", "bybit_live") and api_key and api_secret:
            self._exchange = self._init_bybit(api_key, api_secret)
        elif self.mode in ("bybit_demo", "bybit_live"):
            log.warning(
                f"CEX_MODE={self.mode} but no API keys found — "
                "falling back to internal_paper mode."
            )
            self.mode = "internal_paper"

        # Price fetcher helper
        self._price_fetcher = TopGainersFetcher()

        # Background exit-checker
        self._stop_event = threading.Event()
        self._exit_thread: Optional[threading.Thread] = None

        log.info(
            f"CEXTrader initialized — mode={self.mode}  "
            f"budget=${usdt_per_trade}/trade  "
            f"TP=+{take_profit_pct}%  SL=-{stop_loss_pct}%  "
            f"max={max_positions} positions"
        )

    # ── Init Bybit via ccxt ──────────────────────────────────────────────────

    def _init_bybit(self, api_key: str, api_secret: str):
        if not CCXT_AVAILABLE:
            log.error("ccxt not installed. Run: pip install ccxt")
            return None
        try:
            exchange = ccxt.bybit({
                "apiKey":    api_key,
                "secret":    api_secret,
                "options":   {"defaultType": "spot"},
            })
            if self.mode == "bybit_demo":
                exchange.set_sandbox_mode(True)
                log.info("Bybit DEMO mode activated (testnet.bybit.com)")
            else:
                log.info("Bybit LIVE mode activated — REAL MONEY")
            # Test connection
            exchange.fetch_balance()
            log.info("Bybit API connection successful")
            return exchange
        except Exception as e:
            log.error(f"Bybit API connection failed: {e} — falling back to internal_paper")
            self.mode = "internal_paper"
            return None

    # ── Start background exit checker ────────────────────────────────────────

    def start(self, state) -> threading.Thread:
        """Start background thread that checks exits every 60 seconds."""
        self._stop_event.clear()
        self._state = state

        def _loop():
            while not self._stop_event.is_set():
                self._stop_event.wait(60)   # check exits every 60s
                if not self._stop_event.is_set():
                    try:
                        self._run_exit_check()
                    except Exception as e:
                        log.error(f"CEX exit check error: {e}")

        self._exit_thread = threading.Thread(target=_loop, daemon=True, name="CEXExitChecker")
        self._exit_thread.start()
        log.info("CEX exit checker started (60s interval)")
        return self._exit_thread

    def stop(self):
        self._stop_event.set()

    # ── Receive signals from main bot ────────────────────────────────────────

    def receive_signals(self, signals: dict[str, str], gainers: list[GainerCoin]):
        """
        Called by the main bot after each sentiment cycle.

        Args:
            signals: {symbol: "BUY"/"SELL"/"HOLD"} from sentiment engine
            gainers: current top gainers list from TopGainersFetcher

        Logic:
            For each BUY signal, check if coin is also a top gainer.
            If yes → buy (respecting max_positions cap).
        """
        if not signals or not gainers:
            return

        open_count = len(self.ledger.positions)
        if open_count >= self.max_positions:
            log.info(f"CEX max positions reached ({open_count}/{self.max_positions}) — skipping")
            return

        # Build gainer lookup: symbol → GainerCoin
        gainer_map = {g.symbol.upper(): g for g in gainers}

        for symbol, signal in signals.items():
            if signal != "BUY":
                continue
            sym = symbol.upper()
            gainer = gainer_map.get(sym)
            if not gainer:
                continue
            if gainer.change_24h < self.min_gain_pct:
                log.debug(f"CEX: {sym} BUY signal but gain={gainer.change_24h:.1f}% < min {self.min_gain_pct}%")
                continue
            if sym in self.ledger.positions:
                continue

            log.info(
                f"CEX SIGNAL MATCH: {sym}  "
                f"signal=BUY  gain={gainer.change_24h:+.1f}%  "
                f"price=${gainer.price_usd:,.4f}  volume=${gainer.volume_24h:,.0f}"
            )
            self._execute_buy(sym, gainer)

            open_count += 1
            if open_count >= self.max_positions:
                break

        # Also handle SELL signals — close any open positions
        for symbol, signal in signals.items():
            if signal == "SELL":
                sym = symbol.upper()
                if sym in self.ledger.positions:
                    price = self._get_price(sym) or self.ledger.positions[sym].current_price
                    self._execute_sell(sym, price, reason="sentiment_sell")

    # ── Execute buy ──────────────────────────────────────────────────────────

    def _execute_buy(self, symbol: str, gainer: GainerCoin):
        price = gainer.price_usd
        if price <= 0:
            price = self._get_price(symbol) or 0
        if price <= 0:
            log.warning(f"CEX: Cannot buy {symbol} — price unavailable")
            return

        source_desc = (
            f"top-gainer({gainer.change_24h:+.1f}%,vol=${gainer.volume_24h/1e6:.1f}M) "
            f"+ sentiment_BUY"
        )

        if self.mode == "internal_paper":
            pos = self.ledger.buy(
                symbol      = symbol,
                price       = price,
                usdt_amount = self.usdt_per_trade,
                source      = source_desc,
            )
            if pos:
                log_activity(
                    "BUY",
                    f"[CEX Paper] {symbol} @ ${price:,.4f}  "
                    f"gain={gainer.change_24h:+.1f}%  "
                    f"spent=${self.usdt_per_trade:.0f} USDT"
                )
        else:
            # Bybit Demo or Live via ccxt
            try:
                amount = self.usdt_per_trade / price   # coin quantity
                order  = self._exchange.create_market_buy_order(
                    f"{symbol}/USDT", amount
                )
                # Mirror in internal ledger for dashboard tracking
                self.ledger.buy(symbol, price, self.usdt_per_trade, source=source_desc)
                log_activity(
                    "BUY",
                    f"[CEX {self.mode.upper()}] {symbol} @ ${price:,.4f}  "
                    f"order_id={order.get('id','?')}"
                )
                log.info(f"Bybit order placed: {order}")
            except Exception as e:
                log.error(f"Bybit buy failed for {symbol}: {e}")
                log_activity("ERROR", f"CEX buy failed: {symbol} — {e}")

    # ── Execute sell ─────────────────────────────────────────────────────────

    def _execute_sell(self, symbol: str, price: float, reason: str = "manual"):
        pos = self.ledger.positions.get(symbol)
        if not pos:
            return

        if self.mode == "internal_paper":
            trade = self.ledger.sell(symbol, price, reason=reason)
            if trade:
                sign = "+" if trade.pnl_usdt >= 0 else ""
                log_activity(
                    "SELL",
                    f"[CEX Paper] {symbol} @ ${price:,.4f}  "
                    f"PnL={sign}{trade.pnl_pct:.1f}% ({sign}${trade.pnl_usdt:.2f})  "
                    f"reason={reason}"
                )
        else:
            try:
                order = self._exchange.create_market_sell_order(
                    f"{symbol}/USDT", pos.quantity
                )
                trade = self.ledger.sell(symbol, price, reason=reason)
                if trade:
                    sign = "+" if trade.pnl_usdt >= 0 else ""
                    log_activity(
                        "SELL",
                        f"[CEX {self.mode.upper()}] {symbol} @ ${price:,.4f}  "
                        f"PnL={sign}{trade.pnl_pct:.1f}%  reason={reason}"
                    )
                log.info(f"Bybit sell order: {order}")
            except Exception as e:
                log.error(f"Bybit sell failed for {symbol}: {e}")
                log_activity("ERROR", f"CEX sell failed: {symbol} — {e}")

    # ── Manual buy (from dashboard) ───────────────────────────────────────────

    def manual_buy(
        self,
        symbol:          str,
        usdt_amount:     float         = None,
        take_profit_pct: Optional[float] = None,
        stop_loss_pct:   Optional[float] = None,
    ) -> dict:
        """
        Buy any coin manually from the dashboard — bypasses signal/gainer checks.

        Args:
            symbol:          Coin symbol, e.g. "BTC" or "BTC/USDT"
            usdt_amount:     How much USDT to spend (defaults to usdt_per_trade)
            take_profit_pct: Override TP % for this position (None = use default)
            stop_loss_pct:   Override SL % for this position (None = use default)

        Returns:
            {"ok": True/False, "symbol": str, "price": float, "error": str or None}
        """
        sym = symbol.upper().replace("/USDT", "").replace("USDT", "")
        amount = usdt_amount or self.usdt_per_trade

        if sym in self.ledger.positions:
            return {"ok": False, "symbol": sym, "error": f"Already holding {sym}"}

        if len(self.ledger.positions) >= self.max_positions:
            return {"ok": False, "symbol": sym, "error": f"Max positions reached ({self.max_positions})"}

        price = self._get_price(sym)
        if not price or price <= 0:
            return {"ok": False, "symbol": sym, "error": f"Cannot fetch price for {sym}"}

        # Override TP/SL on the ledger temporarily if custom values provided
        old_tp = self.ledger.take_profit_pct
        old_sl = self.ledger.stop_loss_pct
        if take_profit_pct is not None:
            self.ledger.take_profit_pct = take_profit_pct
        if stop_loss_pct is not None:
            self.ledger.stop_loss_pct = stop_loss_pct

        if self.mode == "internal_paper":
            pos = self.ledger.buy(
                symbol      = sym,
                price       = price,
                usdt_amount = amount,
                source      = "manual",
            )
        else:
            try:
                qty   = amount / price
                order = self._exchange.create_market_buy_order(f"{sym}/USDT", qty)
                pos   = self.ledger.buy(sym, price, amount, source="manual")
                log.info(f"Bybit manual buy order: {order}")
            except Exception as e:
                # Restore TP/SL
                self.ledger.take_profit_pct = old_tp
                self.ledger.stop_loss_pct   = old_sl
                return {"ok": False, "symbol": sym, "error": str(e)}

        # Restore TP/SL defaults
        self.ledger.take_profit_pct = old_tp
        self.ledger.stop_loss_pct   = old_sl

        if pos:
            log_activity(
                "BUY",
                f"[CEX Manual] {sym} @ ${price:,.4f}  "
                f"spent=${amount:.0f} USDT  "
                f"TP=+{take_profit_pct or old_tp:.0f}%  SL=-{stop_loss_pct or old_sl:.0f}%"
            )
            return {"ok": True, "symbol": sym, "price": price, "usdt_spent": amount}
        else:
            return {"ok": False, "symbol": sym, "error": "Ledger buy failed (check balance)"}

    # ── Update TP/SL for an open position ─────────────────────────────────────

    def update_position(self, symbol: str, take_profit_pct: float = None, stop_loss_pct: float = None) -> dict:
        """Update take-profit and/or stop-loss for an existing open position."""
        sym = symbol.upper().replace("/USDT", "").replace("USDT", "")
        pos = self.ledger.positions.get(sym)
        if not pos:
            return {"ok": False, "symbol": sym, "error": f"No open position for {sym}"}

        if take_profit_pct is not None:
            pos.take_profit_pct = take_profit_pct
        if stop_loss_pct is not None:
            pos.stop_loss_pct = stop_loss_pct

        log_activity(
            "INFO",
            f"[CEX] Updated {sym} thresholds — "
            f"TP=+{pos.take_profit_pct:.0f}%  SL=-{pos.stop_loss_pct:.0f}%"
        )
        return {"ok": True, "symbol": sym, "take_profit_pct": pos.take_profit_pct, "stop_loss_pct": pos.stop_loss_pct}

    # ── Manual sell (from dashboard / Telegram) ───────────────────────────────

    def manual_sell(self, symbol: str) -> bool:
        """Called from /api/cex/sell endpoint or Telegram /sell command."""
        sym   = symbol.upper()
        price = self._get_price(sym)
        if not price:
            pos = self.ledger.positions.get(sym)
            price = pos.current_price if pos else 0
        if not price:
            return False
        self._execute_sell(sym, price, reason="manual")
        return True

    # ── Exit checker (background) ─────────────────────────────────────────────

    def _run_exit_check(self):
        if not self.ledger.positions:
            return
        symbols = list(self.ledger.positions.keys())
        prices  = self._price_fetcher.get_prices_batch(symbols)
        if not prices:
            return

        closed = self.ledger.check_exits(prices)
        for trade in closed:
            sign = "+" if trade.pnl_usdt >= 0 else ""
            log_activity(
                "SELL",
                f"[CEX Auto] {trade.symbol}  "
                f"PnL={sign}{trade.pnl_pct:.1f}% ({sign}${trade.pnl_usdt:.2f})  "
                f"reason={trade.reason}"
            )

        # Update state snapshot for dashboard
        if hasattr(self, "_state"):
            all_prices = self._price_fetcher.get_prices_batch(list(self.ledger.positions.keys()))
            self._state.cex_portfolio = self.ledger.portfolio_summary(all_prices)

    # ── Price helper ──────────────────────────────────────────────────────────

    def _get_price(self, symbol: str) -> Optional[float]:
        """Fetch live price for a symbol in USD."""
        prices = self._price_fetcher.get_prices_batch([symbol])
        return prices.get(symbol.upper())

    # ── Portfolio snapshot ────────────────────────────────────────────────────

    def get_portfolio(self) -> dict:
        """Returns full portfolio summary for the dashboard."""
        symbols = list(self.ledger.positions.keys())
        prices  = self._price_fetcher.get_prices_batch(symbols) if symbols else {}
        return self.ledger.portfolio_summary(prices)

    # ── Status summary (for logging) ─────────────────────────────────────────

    def status_summary(self) -> str:
        p = self.ledger.portfolio_summary()
        return (
            f"CEX [{self.mode.upper()}]  "
            f"balance=${p['virtual_usdt']:.2f}  "
            f"open={p['open_positions']}  "
            f"realised_pnl=${p['realised_pnl']:+.2f}  "
            f"win_rate={p['win_rate']:.0f}%  "
            f"trades={p['total_trades']}"
        )
