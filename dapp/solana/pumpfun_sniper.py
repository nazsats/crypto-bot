"""
dapp/solana/pumpfun_sniper.py — Pump.fun token sniper on Solana.

How Pump.fun works:
  - Tokens launch on a bonding curve (price rises as people buy)
  - At ~$69k market cap, the token "graduates" to Raydium DEX
  - Best entry: within the first few minutes of launch (lowest price)
  - Best exit: before or just after graduation (~$50-100k market cap)

This bot:
  1. Detects new token mentions in Reddit/CryptoPanic sentiment data
  2. Looks up the token on Pump.fun API to verify it exists + get mint address
  3. Buys via Pump.fun's bonding curve using the Jupiter aggregator API
  4. Monitors price and auto-sells at take-profit or stop-loss

Dependencies (install after C++ Build Tools):
  pip install solders solana

Free RPC options:
  - https://api.mainnet-beta.solana.com  (public, rate-limited)
  - https://helius.dev                   (free tier: 10M credits/month)
  - https://quicknode.com                (free tier available)
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from typing import Optional

import requests

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from config import (
    SOL_RPC_URL,
    SOLANA_PRIVATE_KEY,
    PUMPFUN_ENABLED,
    PUMPFUN_BUY_SOL,
    PUMPFUN_STOP_LOSS_PCT,
    PUMPFUN_TAKE_PROFIT_PCT,
    PUMPFUN_LADDER_ENABLED,
    PUMPFUN_TAKE_PROFIT_LADDER,
    RUG_SCAN_ENABLED,
)
from utils.logger import get_logger

log = get_logger("pumpfun")

# ─────────────────────────────────────────────
# PUMP.FUN PUBLIC API ENDPOINTS
# These are public REST endpoints — no API key needed
# ─────────────────────────────────────────────
PUMPFUN_API    = "https://frontend-api.pump.fun"
JUPITER_API    = "https://quote-api.jup.ag/v6"       # Free aggregator for swaps
SOL_MINT       = "So11111111111111111111111111111111111111112"  # Wrapped SOL


@dataclass
class PumpToken:
    mint: str
    name: str
    symbol: str
    market_cap_usd: float
    price_sol: float
    volume_24h: float
    created_timestamp: int
    bonding_curve: str   # bonding curve account address
    complete: bool       # True = graduated to Raydium


@dataclass
class SolPosition:
    mint: str
    symbol: str
    sol_spent: float
    entry_price_sol: float
    token_amount: int
    stop_loss_sol: float
    take_profit_sol: float
    opened_at: float
    ladder_levels_hit: list = None   # tracks which ladder levels have been sold

    def __post_init__(self):
        if self.ladder_levels_hit is None:
            self.ladder_levels_hit = [False] * len(PUMPFUN_TAKE_PROFIT_LADDER)


class PumpFunSniper:
    def __init__(self):
        self.positions: dict[str, SolPosition] = {}  # mint → position

        if not PUMPFUN_ENABLED:
            log.info("Pump.fun sniper disabled (PUMPFUN_ENABLED=False)")
            self.keypair = None
            return

        if not SOLANA_PRIVATE_KEY:
            log.error("SOLANA_PRIVATE_KEY not set — Pump.fun disabled")
            self.keypair = None
            return

        # Import solders only when actually enabled
        try:
            from solders.keypair import Keypair  # type: ignore
            self.keypair = Keypair.from_base58_string(SOLANA_PRIVATE_KEY)
            log.info(f"Solana wallet: {str(self.keypair.pubkey())[:12]}...")
        except ImportError:
            log.error("solders not installed. Run: pip install solders solana")
            self.keypair = None
        except Exception as e:
            log.error(f"Invalid Solana private key: {e}")
            self.keypair = None

    # ──────────────────────────────────────────
    # PUBLIC: search for a token on Pump.fun by name/symbol
    # ──────────────────────────────────────────
    def find_token(self, query: str) -> Optional[PumpToken]:
        """Search Pump.fun for a token by name or symbol."""
        try:
            resp = requests.get(
                f"{PUMPFUN_API}/coins/search",
                params={"q": query, "limit": 5, "includeNsfw": "false"},
                timeout=10,
            )
            resp.raise_for_status()
            results = resp.json()
            if not results:
                return None

            # Take the highest market cap result that matches
            best = max(results, key=lambda x: x.get("usd_market_cap", 0))
            return self._parse_token(best)
        except Exception as e:
            log.error(f"Pump.fun search failed for '{query}': {e}")
            return None

    def get_token_by_mint(self, mint: str) -> Optional[PumpToken]:
        """Get full token info by mint address."""
        try:
            resp = requests.get(f"{PUMPFUN_API}/coins/{mint}", timeout=10)
            resp.raise_for_status()
            return self._parse_token(resp.json())
        except Exception as e:
            log.error(f"Token lookup failed for {mint}: {e}")
            return None

    def get_new_launches(self, limit: int = 20) -> list[PumpToken]:
        """Fetch the latest token launches on Pump.fun."""
        try:
            resp = requests.get(
                f"{PUMPFUN_API}/coins",
                params={"limit": limit, "sort": "created_timestamp", "order": "DESC",
                        "includeNsfw": "false"},
                timeout=10,
            )
            resp.raise_for_status()
            tokens = [self._parse_token(t) for t in resp.json()]
            log.info(f"Pump.fun: {len(tokens)} new launches fetched")
            return tokens
        except Exception as e:
            log.error(f"Pump.fun new launches failed: {e}")
            return []

    # ──────────────────────────────────────────
    # PUBLIC: execute a buy via Jupiter aggregator
    # ──────────────────────────────────────────
    def buy(self, token: PumpToken) -> bool:
        # ── Rug scan before spending any money ──────────────────────────
        if RUG_SCAN_ENABLED:
            from analysis.rug_scanner import is_safe_to_buy_solana
            if not is_safe_to_buy_solana(token.mint):
                log.warning(f"RUG SCAN BLOCKED buy of {token.symbol} ({token.mint[:12]}...)")
                return False

        if not self.keypair:
            log.info(f"[DRY-RUN] Would BUY {token.symbol} (mint: {token.mint[:12]}...) "
                     f"for {PUMPFUN_BUY_SOL} SOL | mcap=${token.market_cap_usd:,.0f}")
            return False

        log.info(f"Buying {token.symbol} for {PUMPFUN_BUY_SOL} SOL via Jupiter...")

        # 1. Get quote from Jupiter
        quote = self._get_jupiter_quote(
            input_mint=SOL_MINT,
            output_mint=token.mint,
            amount_lamports=int(PUMPFUN_BUY_SOL * 1e9),
        )
        if not quote:
            return False

        # 2. Get swap transaction from Jupiter
        tx_b64 = self._get_jupiter_swap_tx(quote)
        if not tx_b64:
            return False

        # 3. Sign and send
        tx_sig = self._sign_and_send(tx_b64)
        if not tx_sig:
            return False

        log.info(f"BUY {token.symbol} confirmed: https://solscan.io/tx/{tx_sig}")

        # Track position
        out_amount = int(quote["outAmount"])
        self.positions[token.mint] = SolPosition(
            mint=token.mint,
            symbol=token.symbol,
            sol_spent=PUMPFUN_BUY_SOL,
            entry_price_sol=token.price_sol,
            token_amount=out_amount,
            stop_loss_sol=token.price_sol * (1 - PUMPFUN_STOP_LOSS_PCT),
            take_profit_sol=token.price_sol * PUMPFUN_TAKE_PROFIT_PCT,
            opened_at=time.time(),
        )
        return True

    def sell(self, mint: str) -> bool:
        pos = self.positions.get(mint)
        if not pos:
            log.debug(f"No position to sell for {mint[:12]}...")
            return False

        token = self.get_token_by_mint(mint)
        if not token:
            return False

        if not self.keypair:
            log.info(f"[DRY-RUN] Would SELL {pos.symbol} @ {token.price_sol:.8f} SOL")
            del self.positions[mint]
            return False

        quote = self._get_jupiter_quote(
            input_mint=mint,
            output_mint=SOL_MINT,
            amount_lamports=pos.token_amount,
        )
        if not quote:
            return False

        tx_b64 = self._get_jupiter_swap_tx(quote)
        if not tx_b64:
            return False

        tx_sig = self._sign_and_send(tx_b64)
        if tx_sig:
            pnl = token.price_sol - pos.entry_price_sol
            pnl_pct = pnl / pos.entry_price_sol * 100
            log.info(f"SELL {pos.symbol}: PnL={pnl_pct:+.1f}% | tx: {tx_sig}")
            del self.positions[mint]
            return True
        return False

    def _sell_partial(self, mint: str, token_amount: int) -> bool:
        """Sell a specific number of tokens (ladder partial exit)."""
        pos = self.positions.get(mint)
        if not pos or not self.keypair:
            return False
        quote = self._get_jupiter_quote(
            input_mint=mint,
            output_mint=SOL_MINT,
            amount_lamports=token_amount,
        )
        if not quote:
            return False
        tx_b64 = self._get_jupiter_swap_tx(quote)
        if not tx_b64:
            return False
        tx_sig = self._sign_and_send(tx_b64)
        if tx_sig:
            pos.token_amount -= token_amount
            log.info(f"PARTIAL SELL {pos.symbol}: {token_amount} tokens | tx={tx_sig}")
            if pos.token_amount <= 0:
                del self.positions[mint]
            return True
        return False

    def check_exits(self):
        """Check all open positions for stop-loss / take-profit ladder."""
        for mint in list(self.positions.keys()):
            pos   = self.positions[mint]
            token = self.get_token_by_mint(mint)
            if not token:
                continue

            # ── Stop-loss: full exit ─────────────────────────────────────
            if token.price_sol <= pos.stop_loss_sol:
                log.info(f"STOP_LOSS hit for {pos.symbol}")
                self.sell(mint)
                continue

            # ── Pre-graduation exit: sell everything before Raydium chaos ─
            if token.market_cap_usd >= 80_000 and not token.complete:
                log.info(f"PRE_GRADUATION exit for {pos.symbol}")
                self.sell(mint)
                continue

            # ── Take-profit ladder ───────────────────────────────────────
            if PUMPFUN_LADDER_ENABLED and pos.entry_price_sol > 0:
                price_mult = token.price_sol / pos.entry_price_sol
                for i, (target_mult, sell_fraction) in enumerate(PUMPFUN_TAKE_PROFIT_LADDER):
                    if pos.ladder_levels_hit[i]:
                        continue   # already sold this level
                    if price_mult >= target_mult:
                        sell_amount = int(pos.token_amount * sell_fraction)
                        log.info(
                            f"LADDER L{i+1}: {pos.symbol} hit {price_mult:.1f}x — "
                            f"selling {sell_fraction*100:.0f}% ({sell_amount} tokens)"
                        )
                        self._sell_partial(mint, sell_amount)
                        pos.ladder_levels_hit[i] = True
            else:
                # Legacy single take-profit
                if token.price_sol >= pos.take_profit_sol:
                    log.info(f"TAKE_PROFIT hit for {pos.symbol}")
                    self.sell(mint)

    # ──────────────────────────────────────────
    # PRIVATE: Jupiter API helpers
    # ──────────────────────────────────────────
    def _get_jupiter_quote(self, input_mint: str, output_mint: str,
                           amount_lamports: int) -> Optional[dict]:
        try:
            resp = requests.get(f"{JUPITER_API}/quote", params={
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": amount_lamports,
                "slippageBps": 1000,    # 10% slippage for memecoins
            }, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log.error(f"Jupiter quote failed: {e}")
            return None

    def _get_jupiter_swap_tx(self, quote: dict) -> Optional[str]:
        try:
            resp = requests.post(f"{JUPITER_API}/swap", json={
                "quoteResponse": quote,
                "userPublicKey": str(self.keypair.pubkey()),
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": 10_000,  # ~$0.001 priority fee
            }, timeout=15)
            resp.raise_for_status()
            return resp.json()["swapTransaction"]
        except Exception as e:
            log.error(f"Jupiter swap tx failed: {e}")
            return None

    def _sign_and_send(self, tx_b64: str) -> Optional[str]:
        try:
            from solders.transaction import VersionedTransaction  # type: ignore
            from solana.rpc.api import Client                      # type: ignore
            from solana.rpc.types import TxOpts                    # type: ignore
            from solders.commitment_config import CommitmentLevel  # type: ignore

            client = Client(SOL_RPC_URL)
            raw_tx = base64.b64decode(tx_b64)
            tx = VersionedTransaction.from_bytes(raw_tx)

            # Sign
            tx.sign([self.keypair])

            result = client.send_raw_transaction(
                bytes(tx),
                opts=TxOpts(skip_preflight=False,
                            preflight_commitment=CommitmentLevel.Confirmed)
            )
            return str(result.value)
        except ImportError:
            log.error("solana/solders not installed. Run: pip install solders solana")
            return None
        except Exception as e:
            log.error(f"Transaction send failed: {e}")
            return None

    def _parse_token(self, data: dict) -> PumpToken:
        return PumpToken(
            mint=data.get("mint", ""),
            name=data.get("name", "Unknown"),
            symbol=data.get("symbol", "???"),
            market_cap_usd=float(data.get("usd_market_cap", 0)),
            price_sol=float(data.get("price", 0)),
            volume_24h=float(data.get("volume_24h", 0)),
            created_timestamp=int(data.get("created_timestamp", 0)),
            bonding_curve=data.get("bonding_curve", ""),
            complete=bool(data.get("complete", False)),
        )
