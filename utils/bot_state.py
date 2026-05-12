"""
utils/bot_state.py — Shared state singleton across all threads.

The main loop, Telegram command handler, WebSocket watcher, and
whale tracker all read/write this object to coordinate with each other.
"""

from __future__ import annotations

import time
import threading
from typing import Optional


class BotState:
    """
    Single shared state object. Pass this into every component.
    Thread-safe: uses a lock for pause/resume writes.
    """

    def __init__(self):
        self._lock = threading.Lock()

        # ── Trading control ──────────────────────────────
        self._paused = False           # True = auto-trading paused by /pause command

        # ── References set by main.py after init ────────
        self.pumpfun: Optional[object] = None     # PumpFunSniper instance
        self.eth_sniper: Optional[object] = None  # UniswapSniper ETH
        self.base_sniper: Optional[object] = None # UniswapSniper Base
        self.paper_ledger: Optional[object] = None # PaperLedger (paper trading)
        self.dry_run: bool = False
        self.paper_trading: bool = False

        # ── Stats ────────────────────────────────────────
        self.cycle_count: int = 0
        self.posts_last_cycle: int = 0
        self.last_cycle_time: float = 0.0
        self.start_time: float = time.time()

        # ── Latest signals (for /status output) ─────────
        self.latest_signals: dict[str, str] = {}   # {coin: "BUY"/"SELL"/"HOLD"}
        self.latest_trending: list[str] = []

        # ── CEX trading state ────────────────────────────
        self.cex_trader: Optional[object] = None   # CEXTrader instance
        self.cex_gainers: list            = []     # latest top gainers
        self.cex_portfolio: dict          = {}     # latest portfolio snapshot

    # ── Pause / Resume ───────────────────────────────────

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._paused

    def pause(self):
        with self._lock:
            self._paused = True

    def resume(self):
        with self._lock:
            self._paused = False

    # ── Uptime helper ────────────────────────────────────

    def uptime_str(self) -> str:
        secs = int(time.time() - self.start_time)
        h, rem = divmod(secs, 3600)
        m, s   = divmod(rem, 60)
        return f"{h}h {m}m {s}s"

    def last_cycle_ago(self) -> str:
        if not self.last_cycle_time:
            return "never"
        ago = int(time.time() - self.last_cycle_time)
        if ago < 60:
            return f"{ago}s ago"
        return f"{ago // 60}m ago"


# Global singleton — import and use everywhere
state = BotState()
