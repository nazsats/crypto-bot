"""
data/data_fetcher.py — Fetches crypto sentiment data from multiple free sources:

  1. Reddit          — social hype (free, needs API key)
  2. CryptoPanic     — crypto news with sentiment (paid, kept for backward compat)
  3. CoinGecko       — trending coins + market data (FREE, no key needed)
  4. CoinMarketCap   — trending + market data (FREE basic tier, needs key)
  5. CoinTelegraph   — news headlines via RSS (FREE, no key needed)
  6. CoinDesk        — news headlines via RSS (FREE, no key needed)
  7. Binance Blog    — exchange news via RSS (FREE, no key needed)
  8. Watcher Guru    — crypto alerts via RSS (FREE, no key needed)
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import feedparser
import praw
import requests

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config import (
    CRYPTOPANIC_API_KEY,
    REDDIT_CLIENT_ID,
    REDDIT_CLIENT_SECRET,
    REDDIT_USER_AGENT,
    REDDIT_SUBREDDITS,
    TRACKED_COINS,
    CMC_API_KEY,
)
from utils.logger import get_logger

log = get_logger("data_fetcher")


@dataclass
class Post:
    source: str           # "reddit" | "coingecko" | "cmc" | "rss_cointelegraph" | etc.
    coin: str             # e.g. "BTC"
    title: str
    body: str
    score: int            # upvotes / popularity weight
    created_utc: float
    url: str = ""
    raw_sentiment: Optional[str] = None   # "positive" | "negative" | None


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _detect_coin(text: str) -> Optional[str]:
    """Return the first tracked coin found in text, or None."""
    text_lower = text.lower()
    for coin, cfg in TRACKED_COINS.items():
        for kw in cfg["reddit_keywords"]:
            if kw.lower() in text_lower:
                return coin
    return None


def _parse_rss_date(entry) -> float:
    """Parse feedparser entry published date to UTC timestamp."""
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        try:
            return datetime(*entry.published_parsed[:6], tzinfo=timezone.utc).timestamp()
        except Exception:
            pass
    return time.time()


# ─────────────────────────────────────────────────────────────────────────────
# 1. REDDIT (free, needs API key)
# ─────────────────────────────────────────────────────────────────────────────

class RedditFetcher:
    def __init__(self):
        if not REDDIT_CLIENT_ID:
            log.warning("Reddit credentials not set — Reddit fetcher disabled")
            self.reddit = None
            return
        self.reddit = praw.Reddit(
            client_id=REDDIT_CLIENT_ID,
            client_secret=REDDIT_CLIENT_SECRET,
            user_agent=REDDIT_USER_AGENT,
        )
        log.info("Reddit fetcher initialized (read-only)")

    def fetch(self, limit_per_sub: int = 25) -> list[Post]:
        if not self.reddit:
            return []
        posts: list[Post] = []
        for sub_name in REDDIT_SUBREDDITS:
            try:
                sub = self.reddit.subreddit(sub_name)
                for submission in sub.new(limit=limit_per_sub):
                    coin = _detect_coin(submission.title + " " + (submission.selftext or ""))
                    if coin:
                        posts.append(Post(
                            source="reddit",
                            coin=coin,
                            title=submission.title,
                            body=submission.selftext[:500] if submission.selftext else "",
                            score=submission.score,
                            created_utc=submission.created_utc,
                            url=f"https://reddit.com{submission.permalink}",
                        ))
                time.sleep(0.5)
            except Exception as e:
                log.error(f"Reddit fetch error on r/{sub_name}: {e}")
        log.info(f"Reddit: fetched {len(posts)} relevant posts")
        return posts


# ─────────────────────────────────────────────────────────────────────────────
# 2. CRYPTOPANIC (paid — kept for backward compatibility)
# ─────────────────────────────────────────────────────────────────────────────

class CryptoPanicFetcher:
    BASE_URL = "https://cryptopanic.com/api/v1/posts/"

    def __init__(self):
        if not CRYPTOPANIC_API_KEY:
            log.warning("CryptoPanic key not set — skipping (use free sources instead)")
        else:
            log.info("CryptoPanic fetcher initialized")

    def fetch(self, currencies: Optional[list[str]] = None) -> list[Post]:
        if not CRYPTOPANIC_API_KEY:
            return []
        currencies = currencies or list(TRACKED_COINS.keys())
        posts: list[Post] = []
        for coin in currencies:
            try:
                params = {
                    "auth_token": CRYPTOPANIC_API_KEY,
                    "currencies": coin,
                    "public": "true",
                    "kind": "news",
                }
                resp = requests.get(self.BASE_URL, params=params, timeout=10)
                resp.raise_for_status()
                data = resp.json()
                for item in data.get("results", []):
                    votes = item.get("votes", {})
                    raw_sentiment = None
                    if votes.get("bullish", 0) > votes.get("bearish", 0):
                        raw_sentiment = "positive"
                    elif votes.get("bearish", 0) > votes.get("bullish", 0):
                        raw_sentiment = "negative"
                    try:
                        dt = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
                        ts = dt.timestamp()
                    except Exception:
                        ts = time.time()
                    posts.append(Post(
                        source="cryptopanic",
                        coin=coin,
                        title=item.get("title", ""),
                        body="",
                        score=votes.get("liked", 0),
                        created_utc=ts,
                        url=item.get("url", ""),
                        raw_sentiment=raw_sentiment,
                    ))
                time.sleep(0.3)
            except Exception as e:
                log.error(f"CryptoPanic fetch error for {coin}: {e}")
        log.info(f"CryptoPanic: fetched {len(posts)} news items")
        return posts


# ─────────────────────────────────────────────────────────────────────────────
# 3. COINGECKO — 100% FREE, no API key needed
#    - Trending coins (what the world is searching right now)
#    - Market data for tracked coins
# ─────────────────────────────────────────────────────────────────────────────

class CoinGeckoFetcher:
    BASE = "https://api.coingecko.com/api/v3"
    HEADERS = {"accept": "application/json"}

    # Maps CoinGecko coin IDs → our ticker symbols
    COINGECKO_ID_MAP = {
        "bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL",
        "dogecoin": "DOGE", "shiba-inu": "SHIB", "pepe": "PEPE",
        "dogwifcoin": "WIF", "bonk": "BONK", "ripple": "XRP",
        "binancecoin": "BNB",
    }

    def __init__(self):
        log.info("CoinGecko fetcher initialized (free, no key)")

    def fetch(self) -> list[Post]:
        posts: list[Post] = []
        posts.extend(self._fetch_trending())
        posts.extend(self._fetch_market_data())
        log.info(f"CoinGecko: fetched {len(posts)} data points")
        return posts

    def _fetch_trending(self) -> list[Post]:
        """Top 7 trending coins on CoinGecko right now."""
        posts: list[Post] = []
        try:
            resp = requests.get(f"{self.BASE}/search/trending",
                                headers=self.HEADERS, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            for entry in data.get("coins", []):
                item = entry.get("item", {})
                cg_id = item.get("id", "")
                coin = self.COINGECKO_ID_MAP.get(cg_id)
                if not coin:
                    # Try to match by symbol against our tracked coins
                    symbol = item.get("symbol", "").upper()
                    coin = symbol if symbol in TRACKED_COINS else None
                if not coin:
                    coin = item.get("symbol", "UNKNOWN").upper()

                rank = item.get("market_cap_rank", 999)
                name = item.get("name", coin)
                title = f"{name} ({coin}) is trending on CoinGecko — market cap rank #{rank}"

                posts.append(Post(
                    source="coingecko_trending",
                    coin=coin,
                    title=title,
                    body=f"CoinGecko trending coin. Score rank: {item.get('score', 0)}",
                    score=100,          # trending = high weight
                    created_utc=time.time(),
                    url=f"https://www.coingecko.com/en/coins/{cg_id}",
                    raw_sentiment="positive",   # being trending = bullish signal
                ))
        except Exception as e:
            log.error(f"CoinGecko trending fetch error: {e}")
        return posts

    def _fetch_market_data(self) -> list[Post]:
        """Price changes for tracked coins — big moves = sentiment signal."""
        posts: list[Post] = []
        # Map our tickers to CoinGecko IDs
        id_map_reversed = {v: k for k, v in self.COINGECKO_ID_MAP.items()}
        ids = ",".join(
            id_map_reversed[coin] for coin in TRACKED_COINS if coin in id_map_reversed
        )
        if not ids:
            return posts
        try:
            resp = requests.get(
                f"{self.BASE}/coins/markets",
                headers=self.HEADERS,
                params={
                    "vs_currency": "usd",
                    "ids": ids,
                    "price_change_percentage": "1h,24h",
                    "order": "market_cap_desc",
                },
                timeout=10,
            )
            resp.raise_for_status()
            for coin_data in resp.json():
                coin = self.COINGECKO_ID_MAP.get(coin_data.get("id", ""))
                if not coin:
                    continue
                change_1h  = coin_data.get("price_change_percentage_1h_in_currency", 0) or 0
                change_24h = coin_data.get("price_change_percentage_24h", 0) or 0
                price      = coin_data.get("current_price", 0)

                # Only create a post if there's a notable move (>3% in either window)
                if abs(change_1h) < 3 and abs(change_24h) < 3:
                    continue

                direction = "up" if change_24h > 0 else "down"
                title = (
                    f"{coin} is {direction} {abs(change_24h):.1f}% in 24h "
                    f"({change_1h:+.1f}% last hour) — price ${price:,.4f}"
                )
                sentiment = "positive" if change_24h > 0 else "negative"

                posts.append(Post(
                    source="coingecko_market",
                    coin=coin,
                    title=title,
                    body=f"Market cap: ${coin_data.get('market_cap', 0):,.0f}",
                    score=50,
                    created_utc=time.time(),
                    url=f"https://www.coingecko.com/en/coins/{coin_data.get('id')}",
                    raw_sentiment=sentiment,
                ))
        except Exception as e:
            log.error(f"CoinGecko market data fetch error: {e}")
        return posts


# ─────────────────────────────────────────────────────────────────────────────
# 4. COINMARKETCAP — Free basic tier (needs free API key)
#    Sign up: pro.coinmarketcap.com → Basic plan → copy API key
# ─────────────────────────────────────────────────────────────────────────────

class CoinMarketCapFetcher:
    BASE = "https://pro-api.coinmarketcap.com"
    HEADERS_TEMPLATE = {
        "Accepts": "application/json",
        "X-CMC_PRO_API_KEY": "",
    }

    CMC_SYMBOL_MAP = {
        "BTC": 1, "ETH": 1027, "SOL": 5426, "DOGE": 74,
        "SHIB": 5994, "PEPE": 24478, "XRP": 52, "BNB": 1839,
    }

    def __init__(self):
        if not CMC_API_KEY:
            log.warning("CMC_API_KEY not set — CoinMarketCap fetcher disabled")
        else:
            log.info("CoinMarketCap fetcher initialized (free tier)")
        self.headers = {**self.HEADERS_TEMPLATE, "X-CMC_PRO_API_KEY": CMC_API_KEY}

    def fetch(self) -> list[Post]:
        if not CMC_API_KEY:
            return []
        posts: list[Post] = []
        posts.extend(self._fetch_trending())
        posts.extend(self._fetch_gainers())
        log.info(f"CoinMarketCap: fetched {len(posts)} data points")
        return posts

    def _fetch_trending(self) -> list[Post]:
        """Most visited coins on CMC in the last 24h."""
        posts: list[Post] = []
        try:
            resp = requests.get(
                f"{self.BASE}/v1/cryptocurrency/trending/most-visited",
                headers=self.headers,
                params={"limit": 10, "time_period": "24h", "convert": "USD"},
                timeout=10,
            )
            resp.raise_for_status()
            for item in resp.json().get("data", []):
                symbol = item.get("symbol", "")
                coin   = symbol if symbol in TRACKED_COINS else symbol
                name   = item.get("name", symbol)
                quote  = item.get("quote", {}).get("USD", {})
                change = quote.get("percent_change_24h", 0) or 0
                price  = quote.get("price", 0) or 0

                title = (
                    f"{name} ({coin}) trending on CoinMarketCap — "
                    f"{change:+.1f}% 24h, price ${price:,.4f}"
                )
                posts.append(Post(
                    source="cmc_trending",
                    coin=coin,
                    title=title,
                    body="Most visited on CoinMarketCap in last 24h",
                    score=80,
                    created_utc=time.time(),
                    url=f"https://coinmarketcap.com/currencies/{name.lower().replace(' ', '-')}/",
                    raw_sentiment="positive" if change > 0 else "negative",
                ))
        except Exception as e:
            log.error(f"CMC trending fetch error: {e}")
        return posts

    def _fetch_gainers(self) -> list[Post]:
        """Top gaining coins in the last 24h."""
        posts: list[Post] = []
        try:
            resp = requests.get(
                f"{self.BASE}/v1/cryptocurrency/trending/gainers-losers",
                headers=self.headers,
                params={"limit": 10, "time_period": "24h", "convert": "USD"},
                timeout=10,
            )
            resp.raise_for_status()
            for item in resp.json().get("data", {}).get("gainers", []):
                symbol = item.get("symbol", "")
                coin   = symbol if symbol in TRACKED_COINS else symbol
                name   = item.get("name", symbol)
                quote  = item.get("quote", {}).get("USD", {})
                change = quote.get("percent_change_24h", 0) or 0
                price  = quote.get("price", 0) or 0

                title = f"{name} ({coin}) is a top gainer: +{change:.1f}% in 24h — ${price:,.4f}"
                posts.append(Post(
                    source="cmc_gainers",
                    coin=coin,
                    title=title,
                    body="Top 24h gainer on CoinMarketCap",
                    score=90,
                    created_utc=time.time(),
                    url=f"https://coinmarketcap.com/currencies/{name.lower().replace(' ', '-')}/",
                    raw_sentiment="positive",
                ))
        except Exception as e:
            log.error(f"CMC gainers fetch error: {e}")
        return posts


# ─────────────────────────────────────────────────────────────────────────────
# 5. RSS FETCHER — Handles all RSS-based news sources
#    Sources: CoinTelegraph | CoinDesk | Binance Blog | Watcher Guru
#    All 100% FREE — no API keys needed
# ─────────────────────────────────────────────────────────────────────────────

RSS_FEEDS = {
    "rss_cointelegraph": "https://cointelegraph.com/rss",
    "rss_coindesk":      "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "rss_binance":       "https://www.binance.com/en/feed/rss",
    "rss_watcherguru":   "https://watcher.guru/news/feed",
}


class RSSFetcher:
    def __init__(self):
        log.info(f"RSS fetcher initialized — {len(RSS_FEEDS)} feeds: {', '.join(RSS_FEEDS.keys())}")

    def fetch(self, max_per_feed: int = 20) -> list[Post]:
        posts: list[Post] = []
        for source_name, url in RSS_FEEDS.items():
            try:
                feed = feedparser.parse(url)
                if feed.bozo and not feed.entries:
                    log.warning(f"RSS [{source_name}] failed or empty: {url}")
                    continue

                count = 0
                for entry in feed.entries[:max_per_feed]:
                    title   = entry.get("title", "").strip()
                    summary = entry.get("summary", "").strip()
                    link    = entry.get("link", "")

                    if not title:
                        continue

                    # Try to match to a known coin
                    coin = _detect_coin(title + " " + summary)
                    if not coin:
                        continue     # skip articles not about tracked coins

                    posts.append(Post(
                        source=source_name,
                        coin=coin,
                        title=title,
                        body=summary[:400],
                        score=10,
                        created_utc=_parse_rss_date(entry),
                        url=link,
                    ))
                    count += 1

                log.info(f"RSS [{source_name}]: {count} relevant articles")
            except Exception as e:
                log.error(f"RSS fetch error [{source_name}]: {e}")

        log.info(f"RSS total: {len(posts)} posts from {len(RSS_FEEDS)} feeds")
        return posts


# ─────────────────────────────────────────────────────────────────────────────
# COMBINED FETCHER — runs all sources and merges results
# ─────────────────────────────────────────────────────────────────────────────

class DataFetcher:
    def __init__(self):
        self.reddit      = RedditFetcher()
        self.cryptopanic = CryptoPanicFetcher()
        self.coingecko   = CoinGeckoFetcher()
        self.cmc         = CoinMarketCapFetcher()
        self.rss         = RSSFetcher()

    def fetch_all(self) -> list[Post]:
        posts: list[Post] = []

        # Free sources — always run
        posts.extend(self.coingecko.fetch())
        posts.extend(self.rss.fetch())
        posts.extend(self.cmc.fetch())

        # Optional sources (need API keys)
        posts.extend(self.reddit.fetch())
        posts.extend(self.cryptopanic.fetch())

        log.info(f"Total posts fetched: {len(posts)} from all sources")
        self._log_source_summary(posts)
        return posts

    def _log_source_summary(self, posts: list[Post]):
        from collections import Counter
        counts = Counter(p.source for p in posts)
        for source, count in sorted(counts.items()):
            log.info(f"  {source:<30} {count} posts")
