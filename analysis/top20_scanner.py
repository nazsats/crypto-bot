"""
analysis/top20_scanner.py — Parallel TA scanner for Top 20 coins by market cap.

Runs TechnicalAnalyzer on all 20 coins concurrently (ThreadPoolExecutor),
caches results for 5 minutes, and exposes results for the API and main loop.

Run standalone:
  python analysis/top20_scanner.py
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from analysis.technical import TechnicalAnalyzer, TechnicalSignal
from utils.logger import get_logger

log = get_logger("top20_scanner")

# ── Top 20 coins by market cap (excluding stablecoins USDT/USDC) ────────────
TOP20_COINS = [
    "BTC", "ETH", "BNB", "SOL", "XRP",
    "ADA", "AVAX", "DOGE", "TRX", "TON",
    "LINK", "SHIB", "DOT", "BCH", "NEAR",
    "LTC", "UNI", "APT", "OP", "ARB",
]

CACHE_TTL_SECONDS = 300   # 5-minute cache


class Top20Scanner:
    """
    Fetches and caches TA signals for all Top 20 coins.
    Thread-safe — safe to call from multiple endpoints simultaneously.
    """

    def __init__(self, timeframe: str = "1h", candle_limit: int = 100,
                 max_workers: int = 8, cache_ttl: int = CACHE_TTL_SECONDS):
        self._analyzer   = TechnicalAnalyzer(timeframe=timeframe, candle_limit=candle_limit)
        self._max_workers = max_workers
        self._cache_ttl  = cache_ttl
        self._cache:     dict[str, TechnicalSignal] = {}
        self._cache_time: float = 0.0
        self._lock       = threading.Lock()

    # ── Public ───────────────────────────────────────────────────────────────

    def scan_all(self, force: bool = False) -> list[TechnicalSignal]:
        """
        Return TA signals for all Top 20 coins.
        Uses cached results if < cache_ttl seconds old.
        Set force=True to bypass cache.
        """
        with self._lock:
            age = time.time() - self._cache_time
            if not force and self._cache and age < self._cache_ttl:
                log.debug(f"[Top20] Returning cached results (age={age:.0f}s)")
                return list(self._cache.values())

        log.info("[Top20] Running full TA scan on Top 20 coins...")
        results: dict[str, TechnicalSignal] = {}

        with ThreadPoolExecutor(max_workers=self._max_workers, thread_name_prefix="TA") as pool:
            futures = {pool.submit(self._analyzer.analyze, coin): coin for coin in TOP20_COINS}
            for fut in as_completed(futures):
                coin = futures[fut]
                try:
                    sig = fut.result()
                    results[sig.symbol] = sig
                    log.debug(f"[Top20] {sig.symbol}: {sig.call} (score={sig.ta_score:.2f})")
                except Exception as e:
                    log.warning(f"[Top20] {coin} failed: {e}")

        # Preserve TOP20_COINS order
        ordered = [results[c] for c in TOP20_COINS if c in results]

        with self._lock:
            self._cache      = {s.symbol: s for s in ordered}
            self._cache_time = time.time()

        log.info(f"[Top20] Scan complete — {len(ordered)}/{len(TOP20_COINS)} coins processed")
        return ordered

    def get_signal(self, symbol: str) -> Optional[TechnicalSignal]:
        """Return cached TA signal for a single coin (or None if not cached)."""
        with self._lock:
            return self._cache.get(symbol.upper())

    def get_all_cached(self) -> list[TechnicalSignal]:
        """Return current cache without triggering a refresh."""
        with self._lock:
            return [self._cache[c] for c in TOP20_COINS if c in self._cache]

    def as_dicts(self, force: bool = False) -> list[dict]:
        return [s.to_dict() for s in self.scan_all(force=force)]

    def summary_table(self) -> str:
        """Return a formatted terminal table of all TA calls."""
        signals = self.get_all_cached() or self.scan_all()
        lines = [
            "┌" + "─"*6 + "┬" + "─"*13 + "┬" + "─"*6 + "┬" + "─"*11 + "┬" + "─"*18 + "┬" + "─"*7 + "┬" + "─"*14 + "┐",
            f"│{'COIN':<6}│{'PRICE':>13}│{'RSI':>6}│{'TREND':<11}│{'MACD':<18}│{'SCORE':>7}│{'CALL':<14}│",
            "├" + "─"*6 + "┼" + "─"*13 + "┼" + "─"*6 + "┼" + "─"*11 + "┼" + "─"*18 + "┼" + "─"*7 + "┼" + "─"*14 + "┤",
        ]
        for s in signals:
            if s.error:
                lines.append(f"│{s.symbol:<6}│{'ERROR':>13}│{'─':>6}│{'─':<11}│{s.error[:18]:<18}│{'─':>7}│{'N/A':<14}│")
            else:
                price_str = f"${s.price:,.2f}" if s.price >= 1 else f"${s.price:.6f}"
                lines.append(
                    f"│{s.symbol:<6}│{price_str:>13}│{s.rsi:>6.1f}│{s.trend:<11}│{s.macd_label:<18}│{s.ta_score:>7.3f}│{s.call_emoji} {s.call:<12}│"
                )
        lines.append("└" + "─"*6 + "┴" + "─"*13 + "┴" + "─"*6 + "┴" + "─"*11 + "┴" + "─"*18 + "┴" + "─"*7 + "┴" + "─"*14 + "┘")
        return "\n".join(lines)

    def strong_signals(self) -> list[TechnicalSignal]:
        """Return only STRONG BUY / STRONG SELL signals from cache."""
        return [
            s for s in self.get_all_cached()
            if s.call in ("STRONG BUY", "STRONG SELL") and not s.error
        ]


# Singleton instance — shared across main.py and api/server.py
_scanner_instance: Optional[Top20Scanner] = None

def get_scanner() -> Top20Scanner:
    global _scanner_instance
    if _scanner_instance is None:
        _scanner_instance = Top20Scanner()
    return _scanner_instance


if __name__ == "__main__":
    import time as _time
    scanner = Top20Scanner(timeframe="1h")
    print("\n🔍 Running Top 20 TA scan (parallel, ~10s)...\n")
    t0 = _time.time()
    scanner.scan_all()
    elapsed = _time.time() - t0
    print(scanner.summary_table())
    print(f"\n⏱  Scan completed in {elapsed:.1f}s\n")

    strong = scanner.strong_signals()
    if strong:
        print("⚡ STRONG SIGNALS:")
        for s in strong:
            print(f"  {s.call_emoji} {s.symbol}: {s.call} — {s.summary}")
    else:
        print("No strong signals at this time.")
    print()
