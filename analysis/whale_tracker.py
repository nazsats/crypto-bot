"""
analysis/whale_tracker.py — Whale wallet copy trading for Solana.

Monitors a list of known profitable wallets on Solana (configured in
config.py). When a whale buys a Pump.fun token, the bot automatically
copies the trade.

Data source: Solscan public API (free, no key needed for basic use)
             or Helius API (free tier, faster + more reliable).

How it works:
  1. Every N seconds, fetch the last 5 transactions for each whale wallet
  2. Detect if any transaction is a Pump.fun buy (interacts with pump.fun program)
  3. Extract the token mint address from the transaction
  4. Trigger a buy of that same token

Pump.fun program ID: 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

import requests

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from utils.logger import get_logger

log = get_logger("whale_tracker")

PUMPFUN_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
SOLSCAN_API        = "https://pro-api.solscan.io/v2.0"
SOLSCAN_PUBLIC_API = "https://api.solscan.io/v2"   # public fallback


class WhaleTracker:
    """
    Polls Solscan for new transactions from known whale wallets.
    Calls on_whale_buy(wallet, mint, sol_amount) when a Pump.fun buy is detected.
    """

    def __init__(
        self,
        whale_wallets: list[str],
        on_whale_buy: Callable[[str, str, float], None],
        poll_interval: int = 30,          # seconds between checks
        min_sol_amount: float = 0.5,      # ignore buys smaller than this
        solscan_api_key: str = "",
    ):
        self.whale_wallets  = whale_wallets
        self.on_whale_buy   = on_whale_buy
        self.poll_interval  = poll_interval
        self.min_sol_amount = min_sol_amount
        self._headers       = {"token": solscan_api_key} if solscan_api_key else {}

        # Track last seen transaction signature per wallet to avoid re-triggering
        self._last_sig: dict[str, str] = {}

        self._thread: Optional[threading.Thread] = None
        self._running = False

    # ─────────────────────────────────────────────────────────────────────
    # PUBLIC
    # ─────────────────────────────────────────────────────────────────────

    def start(self):
        if not self.whale_wallets:
            log.warning("No whale wallets configured — whale tracker disabled")
            return
        self._running = True
        self._thread  = threading.Thread(
            target=self._poll_loop,
            daemon=True,
            name="WhaleTracker",
        )
        self._thread.start()
        log.info(f"Whale tracker started — watching {len(self.whale_wallets)} wallets")

    def stop(self):
        self._running = False
        log.info("Whale tracker stopped")

    # ─────────────────────────────────────────────────────────────────────
    # INTERNAL
    # ─────────────────────────────────────────────────────────────────────

    def _poll_loop(self):
        while self._running:
            for wallet in self.whale_wallets:
                try:
                    self._check_wallet(wallet)
                except Exception as e:
                    log.warning(f"Whale check error [{wallet[:12]}]: {e}")
                time.sleep(1)   # small delay between wallet checks
            time.sleep(self.poll_interval)

    def _check_wallet(self, wallet: str):
        """Fetch recent transactions and detect Pump.fun buys."""
        txs = self._get_recent_txs(wallet, limit=5)
        if not txs:
            return

        last_known = self._last_sig.get(wallet, "")

        for tx in txs:
            sig = tx.get("signature") or tx.get("txHash", "")
            if sig == last_known:
                break   # reached transactions we've already seen

            # Check if this is a Pump.fun interaction
            if not self._is_pumpfun_tx(tx):
                continue

            sol_amount = self._extract_sol_amount(tx)
            if sol_amount < self.min_sol_amount:
                log.debug(
                    f"Whale [{wallet[:12]}] Pump.fun buy too small: {sol_amount:.3f} SOL"
                )
                continue

            mint = self._extract_token_mint(tx, wallet)
            if not mint:
                continue

            log.info(
                f"WHALE COPY: {wallet[:12]}... bought {mint[:12]}... "
                f"for {sol_amount:.3f} SOL"
            )
            self._last_sig[wallet] = sig
            self.on_whale_buy(wallet, mint, sol_amount)
            break   # only copy the most recent buy per poll cycle

        # Update last seen even if no buy detected
        if txs:
            first_sig = txs[0].get("signature") or txs[0].get("txHash", "")
            if first_sig and wallet not in self._last_sig:
                self._last_sig[wallet] = first_sig

    def _get_recent_txs(self, wallet: str, limit: int = 5) -> list[dict]:
        """Fetch recent transactions for a wallet from Solscan."""
        # Try public Solscan API (no key needed)
        try:
            resp = requests.get(
                f"{SOLSCAN_PUBLIC_API}/account/transactions",
                params={"account": wallet, "limit": limit},
                headers=self._headers,
                timeout=10,
            )
            if resp.ok:
                data = resp.json()
                return data.get("data", data) if isinstance(data, dict) else data
        except Exception as e:
            log.debug(f"Solscan API error: {e}")

        # Fallback: use public Solana RPC getSignaturesForAddress
        return self._get_txs_via_rpc(wallet, limit)

    def _get_txs_via_rpc(self, wallet: str, limit: int) -> list[dict]:
        """Fallback: use free public Solana RPC to get transaction signatures."""
        try:
            import sys, os
            sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
            from config import SOL_RPC_URL

            resp = requests.post(SOL_RPC_URL, json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getSignaturesForAddress",
                "params": [wallet, {"limit": limit}],
            }, timeout=10)
            resp.raise_for_status()
            sigs = resp.json().get("result", [])
            return [{"signature": s["signature"], "err": s.get("err")} for s in sigs]
        except Exception as e:
            log.debug(f"RPC tx fetch error: {e}")
            return []

    def _is_pumpfun_tx(self, tx: dict) -> bool:
        """Check if a transaction interacts with the Pump.fun program."""
        # Solscan format
        programs = tx.get("parsedInstruction", [])
        for prog in programs:
            if isinstance(prog, dict):
                prog_id = prog.get("programId", "") or prog.get("program", "")
                if PUMPFUN_PROGRAM_ID in str(prog_id):
                    return True

        # Also check raw log messages
        log_msgs = tx.get("logMessages", [])
        for msg in log_msgs:
            if PUMPFUN_PROGRAM_ID in str(msg):
                return True

        # Fallback: check if the tx description contains "pump"
        desc = str(tx).lower()
        return "pump" in desc and "buy" in desc

    def _extract_sol_amount(self, tx: dict) -> float:
        """Extract how much SOL was spent in the transaction."""
        # Try fee + lamport change
        lamports = (
            tx.get("lamport")
            or tx.get("sol")
            or tx.get("solAmount")
            or 0
        )
        if isinstance(lamports, (int, float)) and lamports > 1000:
            return lamports / 1e9   # convert lamports to SOL
        return 0.0

    def _extract_token_mint(self, tx: dict, buyer_wallet: str) -> Optional[str]:
        """
        Extract the token mint address that was bought.
        Looks in token balances changed by the transaction.
        """
        # Solscan provides tokenTransfers in transaction detail
        token_transfers = tx.get("tokenTransfers", []) or tx.get("tokenBalaneChanges", [])
        for transfer in token_transfers:
            owner = transfer.get("owner", "") or transfer.get("account", "")
            if owner == buyer_wallet:
                mint = transfer.get("token", {}).get("tokenAddress") or transfer.get("mint", "")
                if mint and len(mint) > 30:   # valid Solana address length
                    return mint

        # Fallback: look for any 32-44 char base58 string that could be a mint
        import re
        text = str(tx)
        # Solana addresses are base58, 32-44 chars, match conservatively
        candidates = re.findall(r'[1-9A-HJ-NP-Za-km-z]{43,44}', text)
        for c in candidates:
            if c != buyer_wallet and c != PUMPFUN_PROGRAM_ID:
                return c

        return None
