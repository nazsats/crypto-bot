"""
dapp/solana/paper_ledger.py — Paper trading ledger for Solana / Pump.fun.

Uses REAL Pump.fun price data but never executes real transactions.
Think of it as a flight simulator for the bot — all the data is real,
but no money is at risk.

How it works:
  - buy()  → records a virtual position at the current real price
  - sell() → closes the position, calculates real P&L based on live price
  - check_exits() → same stop-loss / ladder logic as live trading
  - /portfolio on Telegram shows live unrealised P&L

Devnet wallet:
  - You can optionally set SOL_DEVNET_RPC_URL in .env and SOLANA_DEVNET_KEY
  - Run `solana airdrop 2 <your_address> --url devnet` to get free devnet SOL
  - The ledger doesn't need this to function — it's purely virtual accounting
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from config import (
    PUMPFUN_BUY_SOL,
    PUMPFUN_STOP_LOSS_PCT,
    PUMPFUN_LADDER_ENABLED,
    PUMPFUN_TAKE_PROFIT_LADDER,
    PUMPFUN_TAKE_PROFIT_PCT,
)
from utils.logger import get_logger
import utils.telegram_notifier as tg

log = get_logger("paper_ledger")


@dataclass
class PaperPosition:
    mint: str
    symbol: str
    name: str
    sol_spent: float
    entry_price_sol: float
    token_amount: float          # virtual token amount
    stop_loss_sol: float
    take_profit_sol: float
    opened_at: float = field(default_factory=time.time)
    ladder_levels_hit: list = field(default_factory=list)

    def __post_init__(self):
        if not self.ladder_levels_hit:
            self.ladder_levels_hit = [False] * len(PUMPFUN_TAKE_PROFIT_LADDER)


class PaperLedger:
    """
    Virtual trading ledger. Tracks paper positions and calculates P&L
    using real-time Pump.fun prices.
    """

    def __init__(self, starting_sol: float = 1.0):
        self.positions: dict[str, PaperPosition] = {}
        self.starting_sol   = starting_sol
        self.virtual_sol    = starting_sol    # running balance
        self.total_trades   = 0
        self.winning_trades = 0
        self.realised_pnl   = 0.0            # SOL profit/loss from closed trades

    # ──────────────────────────────────────────────────────────────────
    # BUY
    # ──────────────────────────────────────────────────────────────────

    def buy(self, token) -> bool:
        """Record a paper buy. token is a PumpToken dataclass."""
        if token.mint in self.positions:
            log.info(f"[PAPER] Already holding {token.symbol}, skipping duplicate buy")
            return False

        amount_sol = PUMPFUN_BUY_SOL
        if self.virtual_sol < amount_sol:
            log.warning(f"[PAPER] Not enough virtual SOL ({self.virtual_sol:.3f} < {amount_sol})")
            return False

        if token.price_sol <= 0:
            log.warning(f"[PAPER] {token.symbol} has zero price, skipping")
            return False

        # Virtual token amount: how many tokens do we get for amount_sol?
        token_amount = amount_sol / token.price_sol

        pos = PaperPosition(
            mint=token.mint,
            symbol=token.symbol,
            name=token.name,
            sol_spent=amount_sol,
            entry_price_sol=token.price_sol,
            token_amount=token_amount,
            stop_loss_sol=token.price_sol * (1 - PUMPFUN_STOP_LOSS_PCT),
            take_profit_sol=token.price_sol * PUMPFUN_TAKE_PROFIT_PCT,
        )

        self.positions[token.mint] = pos
        self.virtual_sol -= amount_sol
        self.total_trades += 1

        log.info(
            f"[PAPER] BUY {token.symbol} @ {token.price_sol:.8f} SOL "
            f"| spent {amount_sol} SOL | balance {self.virtual_sol:.4f} SOL"
        )
        tg.alert_paper_buy(
            symbol=token.symbol,
            name=token.name,
            market_cap=token.market_cap_usd,
            price_sol=token.price_sol,
            amount_sol=amount_sol,
        )
        return True

    # ──────────────────────────────────────────────────────────────────
    # SELL
    # ──────────────────────────────────────────────────────────────────

    def sell(self, mint: str, current_price_sol: float, reason: str = "manual") -> bool:
        pos = self.positions.get(mint)
        if not pos:
            return False

        proceeds   = pos.token_amount * current_price_sol
        pnl_sol    = proceeds - pos.sol_spent
        pnl_pct    = pnl_sol / pos.sol_spent * 100

        self.virtual_sol  += proceeds
        self.realised_pnl += pnl_sol
        if pnl_sol >= 0:
            self.winning_trades += 1

        log.info(
            f"[PAPER] SELL {pos.symbol} @ {current_price_sol:.8f} SOL "
            f"| PnL {pnl_pct:+.1f}% ({pnl_sol:+.4f} SOL) | reason: {reason}"
        )
        tg.alert_paper_sell(
            symbol=pos.symbol,
            entry_sol=pos.entry_price_sol,
            exit_sol=current_price_sol,
            amount_sol=pos.sol_spent,
            reason=reason,
        )
        del self.positions[mint]
        return True

    def sell_partial(self, mint: str, fraction: float,
                     current_price_sol: float, reason: str) -> bool:
        """Sell a fraction of a position (for ladder exits)."""
        pos = self.positions.get(mint)
        if not pos:
            return False

        sell_tokens = pos.token_amount * fraction
        proceeds    = sell_tokens * current_price_sol
        cost_basis  = pos.sol_spent * fraction
        pnl_sol     = proceeds - cost_basis
        pnl_pct     = pnl_sol / cost_basis * 100 if cost_basis > 0 else 0

        pos.token_amount -= sell_tokens
        pos.sol_spent    -= cost_basis
        self.virtual_sol += proceeds
        self.realised_pnl += pnl_sol

        log.info(
            f"[PAPER] PARTIAL SELL {pos.symbol} ({fraction*100:.0f}%) @ "
            f"{current_price_sol:.8f} SOL | PnL {pnl_pct:+.1f}% | {reason}"
        )
        tg.alert_paper_sell(
            symbol=pos.symbol,
            entry_sol=pos.entry_price_sol,
            exit_sol=current_price_sol,
            amount_sol=cost_basis,
            reason=f"{reason} ({fraction*100:.0f}% partial)",
        )
        if pos.token_amount <= 0:
            del self.positions[mint]
        return True

    # ──────────────────────────────────────────────────────────────────
    # CHECK EXITS (same logic as live trading)
    # ──────────────────────────────────────────────────────────────────

    def check_exits(self, get_token_fn):
        """
        Check all paper positions for stop-loss / ladder exits.
        get_token_fn: callable(mint) → PumpToken | None  (from PumpFunSniper)
        """
        for mint in list(self.positions.keys()):
            pos   = self.positions.get(mint)
            if not pos:
                continue
            token = get_token_fn(mint)
            if not token:
                continue

            price = token.price_sol

            # Stop-loss
            if price <= pos.stop_loss_sol:
                self.sell(mint, price, reason="STOP_LOSS")
                continue

            # Pre-graduation
            if token.market_cap_usd >= 80_000 and not token.complete:
                self.sell(mint, price, reason="PRE_GRADUATION")
                continue

            # Ladder
            if PUMPFUN_LADDER_ENABLED and pos.entry_price_sol > 0:
                mult = price / pos.entry_price_sol
                for i, (target_mult, fraction) in enumerate(PUMPFUN_TAKE_PROFIT_LADDER):
                    if pos.ladder_levels_hit[i]:
                        continue
                    if mult >= target_mult:
                        self.sell_partial(mint, fraction, price,
                                          reason=f"LADDER_L{i+1}_{target_mult}x")
                        if mint in self.positions:
                            self.positions[mint].ladder_levels_hit[i] = True
            else:
                if price >= pos.take_profit_sol:
                    self.sell(mint, price, reason="TAKE_PROFIT")

    # ──────────────────────────────────────────────────────────────────
    # PORTFOLIO SUMMARY
    # ──────────────────────────────────────────────────────────────────

    def portfolio_summary(self, get_token_fn) -> dict:
        """Build a summary dict for /portfolio Telegram command."""
        positions_data = []
        unrealised_pnl = 0.0

        for mint, pos in self.positions.items():
            token = get_token_fn(mint)
            current = token.price_sol if token else pos.entry_price_sol
            pnl_pct = (current - pos.entry_price_sol) / pos.entry_price_sol * 100
            pnl_sol = (current - pos.entry_price_sol) / pos.entry_price_sol * pos.sol_spent
            unrealised_pnl += pnl_sol

            positions_data.append({
                "symbol":        pos.symbol,
                "entry_price":   pos.entry_price_sol,
                "current_price": current,
                "sol_spent":     pos.sol_spent,
                "pnl_pct":       pnl_pct,
                "pnl_sol":       pnl_sol,
            })

        win_rate = (
            self.winning_trades / self.total_trades * 100
            if self.total_trades > 0 else 0
        )

        return {
            "positions":      positions_data,
            "virtual_sol":    self.virtual_sol,
            "starting_sol":   self.starting_sol,
            "realised_pnl":   self.realised_pnl,
            "unrealised_pnl": unrealised_pnl,
            "total_pnl":      self.realised_pnl + unrealised_pnl,
            "total_trades":   self.total_trades,
            "win_rate":       win_rate,
        }

    def log_summary(self, get_token_fn):
        """Print a summary to the terminal log."""
        s = self.portfolio_summary(get_token_fn)
        log.info(
            f"[PAPER] Balance: {s['virtual_sol']:.4f} SOL | "
            f"Realised PnL: {s['realised_pnl']:+.4f} SOL | "
            f"Unrealised: {s['unrealised_pnl']:+.4f} SOL | "
            f"Trades: {s['total_trades']} | Win rate: {s['win_rate']:.0f}%"
        )
