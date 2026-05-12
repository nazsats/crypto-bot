"""
cex/cex_ledger.py — Internal CEX paper trading ledger.

Works WITHOUT any exchange API key.
Uses real live prices from Binance/CoinGecko to simulate trades.

This is the default mode (CEX_MODE=internal_paper).
When you're ready for Bybit Demo, switch to CEX_MODE=bybit_demo in .env.

Tracks:
  - Virtual USDT balance
  - Open positions with live mark-to-market PnL
  - Trade history (closed trades)
  - Win rate, total P&L, best/worst trade
"""

from __future__ import annotations

import time
import threading
from dataclasses import dataclass, field
from typing import Optional

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from utils.logger import get_logger

log = get_logger("cex_ledger")


@dataclass
class CEXPosition:
    symbol:          str
    entry_price:     float       # USD
    current_price:   float       # USD (updated live)
    usdt_spent:      float       # how much USDT invested
    quantity:        float       # how many coins held
    opened_at:       float = field(default_factory=time.time)
    take_profit_pct: float = 20.0   # exit at +20%
    stop_loss_pct:   float = 10.0   # exit at -10%
    signal_score:    float = 0.0    # sentiment score that triggered buy
    source:          str   = ""     # e.g. "sentiment+gainer"

    @property
    def pnl_pct(self) -> float:
        if self.entry_price == 0:
            return 0.0
        return (self.current_price - self.entry_price) / self.entry_price * 100

    @property
    def pnl_usdt(self) -> float:
        return self.quantity * (self.current_price - self.entry_price)

    @property
    def current_value(self) -> float:
        return self.quantity * self.current_price

    @property
    def should_take_profit(self) -> bool:
        return self.pnl_pct >= self.take_profit_pct

    @property
    def should_stop_loss(self) -> bool:
        return self.pnl_pct <= -self.stop_loss_pct

    @property
    def age_minutes(self) -> float:
        return (time.time() - self.opened_at) / 60


@dataclass
class ClosedTrade:
    symbol:      str
    entry_price: float
    exit_price:  float
    usdt_spent:  float
    pnl_usdt:    float
    pnl_pct:     float
    reason:      str       # "take_profit" | "stop_loss" | "manual"
    opened_at:   float
    closed_at:   float = field(default_factory=time.time)

    @property
    def duration_minutes(self) -> float:
        return (self.closed_at - self.opened_at) / 60

    @property
    def won(self) -> bool:
        return self.pnl_usdt > 0


class CEXLedger:
    """
    Internal paper trading ledger for CEX trades.

    Usage:
        ledger = CEXLedger(starting_usdt=1000.0)
        ledger.buy("BTC", price=65000.0, usdt_amount=50.0, score=0.75)
        ledger.check_exits(prices)         # pass {symbol: current_price}
        summary = ledger.portfolio_summary(prices)
    """

    def __init__(self, starting_usdt: float = 1000.0,
                 take_profit_pct: float = 20.0,
                 stop_loss_pct: float   = 10.0):
        self.starting_usdt   = starting_usdt
        self.virtual_usdt    = starting_usdt
        self.take_profit_pct = take_profit_pct
        self.stop_loss_pct   = stop_loss_pct
        self.positions: dict[str, CEXPosition] = {}   # symbol → position
        self.closed_trades: list[ClosedTrade]  = []
        self._lock = threading.Lock()

    # ── Buy ──────────────────────────────────────────────────────────────────

    def buy(self, symbol: str, price: float, usdt_amount: float,
            score: float = 0.0, source: str = "") -> Optional[CEXPosition]:
        """
        Open a paper long position.

        Args:
            symbol:      coin symbol, e.g. "BTC"
            price:       current USD price
            usdt_amount: how much virtual USDT to spend
            score:       sentiment score that triggered this buy
            source:      signal source description

        Returns:
            CEXPosition if successful, None if insufficient balance or already held.
        """
        with self._lock:
            if symbol in self.positions:
                log.info(f"[CEX Ledger] Already holding {symbol} — skipping buy")
                return None
            if usdt_amount > self.virtual_usdt:
                log.warning(
                    f"[CEX Ledger] Insufficient balance: need ${usdt_amount:.2f}, "
                    f"have ${self.virtual_usdt:.2f}"
                )
                return None
            if price <= 0:
                log.warning(f"[CEX Ledger] Invalid price {price} for {symbol}")
                return None

            quantity = usdt_amount / price
            self.virtual_usdt -= usdt_amount

            pos = CEXPosition(
                symbol          = symbol,
                entry_price     = price,
                current_price   = price,
                usdt_spent      = usdt_amount,
                quantity        = quantity,
                take_profit_pct = self.take_profit_pct,
                stop_loss_pct   = self.stop_loss_pct,
                signal_score    = score,
                source          = source,
            )
            self.positions[symbol] = pos

            log.info(
                f"[CEX Ledger] PAPER BUY  {symbol}  "
                f"${price:,.4f}  qty={quantity:.6f}  "
                f"spent=${usdt_amount:.2f}  score={score:.2f}"
            )
            return pos

    # ── Sell ─────────────────────────────────────────────────────────────────

    def sell(self, symbol: str, current_price: float,
             reason: str = "manual") -> Optional[ClosedTrade]:
        """
        Close a position at current_price.

        Args:
            symbol:        coin symbol
            current_price: exit price in USD
            reason:        "take_profit" | "stop_loss" | "manual"

        Returns:
            ClosedTrade if position existed, None otherwise.
        """
        with self._lock:
            pos = self.positions.get(symbol)
            if not pos:
                log.warning(f"[CEX Ledger] No position for {symbol} to sell")
                return None

            exit_value = pos.quantity * current_price
            pnl_usdt   = exit_value - pos.usdt_spent
            pnl_pct    = (current_price - pos.entry_price) / pos.entry_price * 100

            self.virtual_usdt += exit_value
            del self.positions[symbol]

            trade = ClosedTrade(
                symbol      = symbol,
                entry_price = pos.entry_price,
                exit_price  = current_price,
                usdt_spent  = pos.usdt_spent,
                pnl_usdt    = round(pnl_usdt, 4),
                pnl_pct     = round(pnl_pct, 2),
                reason      = reason,
                opened_at   = pos.opened_at,
            )
            self.closed_trades.append(trade)

            sign = "+" if pnl_usdt >= 0 else ""
            log.info(
                f"[CEX Ledger] PAPER SELL {symbol}  "
                f"entry=${pos.entry_price:,.4f} → exit=${current_price:,.4f}  "
                f"PnL={sign}{pnl_pct:.1f}% ({sign}${pnl_usdt:.2f})  reason={reason}"
            )
            return trade

    # ── Update prices + auto-exit ─────────────────────────────────────────────

    def update_prices(self, prices: dict[str, float]):
        """Mark-to-market all positions with latest prices."""
        with self._lock:
            for symbol, pos in self.positions.items():
                price = prices.get(symbol)
                if price and price > 0:
                    pos.current_price = price

    def check_exits(self, prices: dict[str, float]) -> list[ClosedTrade]:
        """
        Update prices then check every position for TP/SL triggers.
        Returns list of trades that were closed this call.
        """
        self.update_prices(prices)
        closed: list[ClosedTrade] = []

        symbols_to_check = list(self.positions.keys())
        for symbol in symbols_to_check:
            pos = self.positions.get(symbol)
            if not pos:
                continue
            if pos.should_take_profit:
                trade = self.sell(symbol, pos.current_price, reason="take_profit")
                if trade:
                    closed.append(trade)
            elif pos.should_stop_loss:
                trade = self.sell(symbol, pos.current_price, reason="stop_loss")
                if trade:
                    closed.append(trade)

        return closed

    # ── Portfolio summary ─────────────────────────────────────────────────────

    def portfolio_summary(self, prices: Optional[dict[str, float]] = None) -> dict:
        """
        Returns a dict with full portfolio stats for the dashboard.
        Pass current prices to get live unrealised PnL.
        """
        if prices:
            self.update_prices(prices)

        with self._lock:
            positions_list = []
            unrealised_pnl = 0.0
            for pos in self.positions.values():
                positions_list.append({
                    "symbol":        pos.symbol,
                    "entry_price":   round(pos.entry_price, 6),
                    "current_price": round(pos.current_price, 6),
                    "pnl_pct":       round(pos.pnl_pct, 2),
                    "pnl_usdt":      round(pos.pnl_usdt, 4),
                    "usdt_spent":    round(pos.usdt_spent, 2),
                    "quantity":      round(pos.quantity, 6),
                    "age_minutes":   round(pos.age_minutes, 1),
                    "signal_score":  pos.signal_score,
                    "source":        pos.source,
                    "take_profit_at": pos.take_profit_pct,
                    "stop_loss_at":   -pos.stop_loss_pct,
                })
                unrealised_pnl += pos.pnl_usdt

            realised_pnl = sum(t.pnl_usdt for t in self.closed_trades)
            total_trades = len(self.closed_trades)
            wins         = sum(1 for t in self.closed_trades if t.won)
            win_rate     = (wins / total_trades * 100) if total_trades > 0 else 0.0

            best  = max((t.pnl_pct for t in self.closed_trades), default=0.0)
            worst = min((t.pnl_pct for t in self.closed_trades), default=0.0)

            history = [
                {
                    "symbol":      t.symbol,
                    "entry_price": round(t.entry_price, 6),
                    "exit_price":  round(t.exit_price, 6),
                    "pnl_pct":     t.pnl_pct,
                    "pnl_usdt":    t.pnl_usdt,
                    "reason":      t.reason,
                    "duration_min": round(t.duration_minutes, 1),
                }
                for t in reversed(self.closed_trades[-20:])
            ]

            return {
                "mode":           "internal_paper",
                "virtual_usdt":   round(self.virtual_usdt, 2),
                "starting_usdt":  self.starting_usdt,
                "realised_pnl":   round(realised_pnl, 4),
                "unrealised_pnl": round(unrealised_pnl, 4),
                "total_pnl":      round(realised_pnl + unrealised_pnl, 4),
                "total_trades":   total_trades,
                "win_rate":       round(win_rate, 1),
                "wins":           wins,
                "losses":         total_trades - wins,
                "best_trade_pct": round(best, 2),
                "worst_trade_pct":round(worst, 2),
                "positions":      positions_list,
                "history":        history,
                "open_positions": len(positions_list),
            }
