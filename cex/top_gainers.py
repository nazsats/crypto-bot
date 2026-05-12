"""
cex/top_gainers.py — Fetches top gaining & trending coins from CoinGecko + Binance.

No API key required — uses free public endpoints.

Runs in a background thread, updates every TOP_GAINERS_INTERVAL_SEC seconds.
Results are stored in bot_state.cex_gainers for the trader to consume.

Data returned per coin:
  symbol        — ticker, e.g. "BTC"
  name          — full name, e.g. "Bitcoin"
  price_usd     — current price in USD
  change_24h    — % price change in last 24 hours
  volume_24h    — 24h trading volume in USD
  market_cap    — total market cap in USD
  rank          — CoinGecko market cap rank
  source        — "coingecko" or "binance"
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from utils.logger import get_logger

log = get_logger("top_gainers")

COINGECKO_URL = "https://api.coingecko.com/api/v3"
BINANCE_URL   = "https://api.binance.com/api/v3"

# Coins to exclude from gainers (stablecoins, wrapped assets)
EXCLUDE_SYMBOLS = {
    "USDT","USDC","BUSD","DAI","TUSD","USDP","FDUSD","GUSD",
    "WBTC","WETH","STETH","WSTETH","RETH","CBETH",
    "EUR","GBP","BRL","ARS","NGN","TRY",
}


@dataclass
class GainerCoin:
    symbol:     str
    name:       str
    price_usd:  float
    change_24h: float          # e.g. 15.3 means +15.3%
    volume_24h: float
    market_cap: float
    rank:       int
    source:     str = "coingecko"
    image_url:  str = ""

    @property
    def is_strong_gainer(self) -> bool:
        """True if coin is up > 10% with decent volume."""
        return self.change_24h > 10.0 and self.volume_24h > 1_000_000

    @property
    def momentum_score(self) -> float:
        """
        0–100 score combining gain% and volume.
        Used to rank gainers by trading opportunity.
        """
        gain_score   = min(self.change_24h / 100 * 50, 50)   # max 50 pts from gain
        vol_score    = min((self.volume_24h / 1_000_000) * 5, 50)  # max 50 pts from volume
        return round(gain_score + vol_score, 1)


class TopGainersFetcher:
    """
    Fetches top gainers from CoinGecko and Binance.
    Call start() to run in background thread.
    Call stop() to shut down.
    """

    def __init__(self, interval_sec: int = 300):
        self.interval_sec = interval_sec
        self._stop_event  = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_updated: float = 0.0

    # ── Public ──────────────────────────────────────────────────────────────

    def fetch_now(self) -> list[GainerCoin]:
        """Fetch gainers immediately and return results."""
        gainers = self._fetch_coingecko_gainers()
        if not gainers:
            log.warning("CoinGecko failed, trying Binance fallback")
            gainers = self._fetch_binance_gainers()
        self.last_updated = time.time()
        log.info(f"Top gainers updated: {len(gainers)} coins fetched")
        return gainers

    def start(self, state) -> threading.Thread:
        """Start background refresh thread. Stores results in state.cex_gainers."""
        self._stop_event.clear()

        def _loop():
            while not self._stop_event.is_set():
                try:
                    gainers = self.fetch_now()
                    state.cex_gainers = gainers
                    log.info(
                        f"Top gainers: "
                        f"{', '.join(f'{g.symbol}({g.change_24h:+.1f}%)' for g in gainers[:5])}"
                    )
                except Exception as e:
                    log.error(f"Top gainers fetch error: {e}")
                self._stop_event.wait(self.interval_sec)

        self._thread = threading.Thread(target=_loop, daemon=True, name="TopGainersFetcher")
        self._thread.start()
        log.info(f"TopGainersFetcher started — updating every {self.interval_sec}s")
        return self._thread

    def stop(self):
        self._stop_event.set()

    # ── CoinGecko ────────────────────────────────────────────────────────────

    def _fetch_coingecko_gainers(self) -> list[GainerCoin]:
        """
        Fetch top 250 coins by market cap sorted by 24h gain.
        Returns top 30 gainers, filtered for non-stablecoins.
        """
        try:
            resp = requests.get(
                f"{COINGECKO_URL}/coins/markets",
                params={
                    "vs_currency":          "usd",
                    "order":                "percent_change_24h_desc",
                    "per_page":             250,
                    "page":                 1,
                    "sparkline":            False,
                    "price_change_percentage": "24h",
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.warning(f"CoinGecko markets failed: {e}")
            return []

        gainers: list[GainerCoin] = []
        for c in data:
            sym = (c.get("symbol") or "").upper()
            if sym in EXCLUDE_SYMBOLS:
                continue
            change = c.get("price_change_percentage_24h") or 0.0
            if change <= 0:
                continue
            gainers.append(GainerCoin(
                symbol     = sym,
                name       = c.get("name", sym),
                price_usd  = float(c.get("current_price") or 0),
                change_24h = round(float(change), 2),
                volume_24h = float(c.get("total_volume") or 0),
                market_cap = float(c.get("market_cap") or 0),
                rank       = int(c.get("market_cap_rank") or 9999),
                image_url  = c.get("image", ""),
                source     = "coingecko",
            ))

        # Sort by momentum score (gain + volume combined)
        gainers.sort(key=lambda g: g.momentum_score, reverse=True)
        return gainers[:30]

    # ── Binance fallback ─────────────────────────────────────────────────────

    def _fetch_binance_gainers(self) -> list[GainerCoin]:
        """
        Use Binance 24hr ticker as fallback.
        Returns USDT pairs sorted by 24h price change %.
        """
        try:
            resp = requests.get(f"{BINANCE_URL}/ticker/24hr", timeout=15)
            resp.raise_for_status()
            tickers = resp.json()
        except Exception as e:
            log.warning(f"Binance 24hr ticker failed: {e}")
            return []

        gainers: list[GainerCoin] = []
        for t in tickers:
            sym = t.get("symbol", "")
            if not sym.endswith("USDT"):
                continue
            base = sym[:-4]   # strip USDT → "BTC"
            if base in EXCLUDE_SYMBOLS:
                continue
            try:
                change  = float(t.get("priceChangePercent", 0))
                price   = float(t.get("lastPrice", 0))
                volume  = float(t.get("quoteVolume", 0))   # in USDT
            except (ValueError, TypeError):
                continue
            if change <= 0 or volume < 100_000:
                continue
            gainers.append(GainerCoin(
                symbol     = base,
                name       = base,
                price_usd  = price,
                change_24h = round(change, 2),
                volume_24h = volume,
                market_cap = 0,
                rank       = 9999,
                source     = "binance",
            ))

        gainers.sort(key=lambda g: g.change_24h, reverse=True)
        return gainers[:30]

    # ── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def get_current_price(symbol: str) -> Optional[float]:
        """Fetch live price for a single symbol in USD (CoinGecko)."""
        try:
            resp = requests.get(
                f"{COINGECKO_URL}/simple/price",
                params={"ids": symbol.lower(), "vs_currencies": "usd"},
                timeout=10,
            )
            data = resp.json()
            # CoinGecko uses coin IDs not symbols — try common mappings
            for key in data:
                return float(data[key]["usd"])
        except Exception:
            pass
        # Fallback: Binance USDT pair
        try:
            resp = requests.get(
                f"{BINANCE_URL}/ticker/price",
                params={"symbol": f"{symbol.upper()}USDT"},
                timeout=10,
            )
            return float(resp.json()["price"])
        except Exception:
            return None

    @staticmethod
    def get_prices_batch(symbols: list[str]) -> dict[str, float]:
        """
        Fetch current USD prices for multiple symbols at once via Binance.
        Returns {symbol: price_usd}.
        """
        prices: dict[str, float] = {}
        try:
            resp = requests.get(f"{BINANCE_URL}/ticker/price", timeout=15)
            all_tickers = {t["symbol"]: float(t["price"]) for t in resp.json()}
            for sym in symbols:
                key = f"{sym.upper()}USDT"
                if key in all_tickers:
                    prices[sym.upper()] = all_tickers[key]
        except Exception as e:
            log.warning(f"Batch price fetch failed: {e}")
        return prices
