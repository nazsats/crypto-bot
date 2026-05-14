"""
dapp/solana/pumpfun_websocket.py — Real-time Pump.fun token watcher.

Connects to pumpportal.fun WebSocket API and fires a callback the
INSTANT a new token is created on Pump.fun — before it shows up in
any API poll, before anyone else sees it.

This is the single biggest edge you can have on Pump.fun:
  Polling (old way):  sees token 3-5 minutes after launch
  WebSocket (this):   sees token within 1-2 SECONDS of launch

Usage:
  watcher = PumpFunWebSocket(on_new_token=my_callback)
  watcher.start()   # runs in background thread

The callback receives a PumpToken dataclass (same as pumpfun_sniper).
"""

from __future__ import annotations

import json
import threading
import time
from typing import Callable, Optional

import websocket  # pip install websocket-client

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from utils.logger import get_logger

log = get_logger("pumpfun_ws")

PUMPPORTAL_WSS = "wss://pumpportal.fun/api/data"

# Import the shared dataclass from the sniper
from dapp.solana.pumpfun_sniper import PumpToken


class PumpFunWebSocket:
    """
    Maintains a persistent WebSocket connection to pumpportal.fun.
    Automatically reconnects on disconnect.

    on_new_token: callable(PumpToken) — called for every new launch
    on_new_trade: callable(dict)      — called for every trade (optional)
    """

    def __init__(
        self,
        on_new_token: Optional[Callable[[PumpToken], None]] = None,
        on_new_trade: Optional[Callable[[dict], None]] = None,
        min_sol_in_dev_buy: float = 0.0,   # filter: skip if dev bought less than X SOL
    ):
        self.on_new_token    = on_new_token
        self.on_new_trade    = on_new_trade
        self.min_sol_in_dev_buy = min_sol_in_dev_buy

        self._ws: Optional[websocket.WebSocketApp] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._reconnect_delay = 5   # seconds between reconnect attempts

    # ─────────────────────────────────────────────────────────────────────
    # PUBLIC
    # ─────────────────────────────────────────────────────────────────────

    def start(self):
        """Start the WebSocket listener in a background daemon thread."""
        self._running = True
        self._thread = threading.Thread(
            target=self._run_forever,
            daemon=True,
            name="PumpFunWS",
        )
        self._thread.start()
        log.info("Pump.fun WebSocket watcher started (background thread)")

    def stop(self):
        self._running = False
        if self._ws:
            self._ws.close()
        log.info("Pump.fun WebSocket watcher stopped")

    def subscribe_token_trades(self, mint: str):
        """Subscribe to live trades for a specific token after buying it."""
        if self._ws:
            try:
                self._ws.send(json.dumps({
                    "method": "subscribeTokenTrade",
                    "keys": [mint],
                }))
                log.info(f"Subscribed to trade feed for {mint[:12]}...")
            except Exception as e:
                log.warning(f"Failed to subscribe to trades for {mint[:12]}: {e}")

    # ─────────────────────────────────────────────────────────────────────
    # INTERNAL
    # ─────────────────────────────────────────────────────────────────────

    def _run_forever(self):
        while self._running:
            try:
                self._connect()
            except Exception as e:
                log.warning(f"WebSocket error: {e}")
            if self._running:
                log.info(f"WebSocket disconnected. Reconnecting in {self._reconnect_delay}s...")
                time.sleep(self._reconnect_delay)

    def _connect(self):
        self._ws = websocket.WebSocketApp(
            PUMPPORTAL_WSS,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._ws.run_forever(ping_interval=30, ping_timeout=10)

    def _on_open(self, ws):
        log.info("Pump.fun WebSocket connected — subscribing to new token events")
        ws.send(json.dumps({"method": "subscribeNewToken"}))

    def _on_message(self, ws, raw: str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        # New token launch event
        if "mint" in data and "name" in data and "traderPublicKey" in data:
            self._handle_new_token(data)
            return

        # Trade event
        if "txType" in data and self.on_new_trade:
            self.on_new_trade(data)

    def _on_error(self, ws, error):
        log.warning(f"Pump.fun WebSocket error: {error}")

    def _on_close(self, ws, close_status_code, close_msg):
        log.info(f"Pump.fun WebSocket closed: {close_status_code} {close_msg}")

    def _handle_new_token(self, data: dict):
        """Parse a new token event and call on_new_token callback."""
        mint   = data.get("mint", "")
        name   = data.get("name", "Unknown")
        symbol = data.get("symbol", "???")

        # Dev initial buy amount in SOL
        initial_buy_sol = float(data.get("solAmount", 0)) / 1e9

        log.info(
            f"NEW TOKEN: {symbol} ({name}) | mint={mint[:12]}... "
            f"| dev bought {initial_buy_sol:.3f} SOL"
        )

        # Filter: skip tokens where dev bought almost nothing (low conviction)
        if self.min_sol_in_dev_buy > 0 and initial_buy_sol < self.min_sol_in_dev_buy:
            log.debug(f"Skipping {symbol} — dev only bought {initial_buy_sol:.4f} SOL")
            return

        # Build a PumpToken from the WebSocket payload
        # Note: market cap and price are approximate at launch (bonding curve start)
        sol_price_usd = self._get_sol_price_usd()
        initial_mcap_usd = initial_buy_sol * sol_price_usd * 10  # rough estimate

        token = PumpToken(
            mint=mint,
            name=name,
            symbol=symbol,
            market_cap_usd=float(data.get("marketCapSol", 0)) * sol_price_usd,
            # Default to 0 (not 1) on missing fields — 1 SOL/token is wildly
            # wrong and would poison every downstream calc.
            price_sol=(
                float(data.get("solAmount", 0)) / float(data.get("tokenAmount", 0))
                if float(data.get("tokenAmount", 0)) > 0
                else 0.0
            ),
            volume_24h=initial_buy_sol * sol_price_usd,
            created_timestamp=int(time.time()),
            bonding_curve=data.get("bondingCurveKey", ""),
            complete=False,
        )

        if self.on_new_token:
            try:
                self.on_new_token(token)
            except Exception as e:
                log.error(f"on_new_token callback error: {e}", exc_info=True)

    def _get_sol_price_usd(self) -> float:
        """Fetch current SOL price. Cached for 60s to avoid hammering the API."""
        now = time.time()
        if hasattr(self, "_sol_price_cache") and now - self._sol_price_ts < 60:
            return self._sol_price_cache

        try:
            resp = __import__("requests").get(
                "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd",
                timeout=5,
            )
            price = resp.json()["solana"]["usd"]
            self._sol_price_cache = price
            self._sol_price_ts    = now
            return price
        except Exception:
            return getattr(self, "_sol_price_cache", 150.0)   # fallback
