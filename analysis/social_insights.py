"""
analysis/social_insights.py — Social Media Insights layer (reporting only).

NOT a trading signal generator — this is a dedicated INSIGHTS / ANALYTICS module.

Tracks per coin (tracked list + trending):
  - mention_count_24h       Total posts across Reddit + CryptoPanic + RSS
  - mention_velocity        Posts per hour (rising/stable/falling)
  - sentiment_breakdown     % positive / neutral / negative
  - top_posts               Top 3 Reddit posts (title, upvotes, link)
  - top_headlines           Top 3 news headlines (title, source, url)
  - fear_greed_proxy        0 (max fear) → 100 (max greed)
  - social_score            0–100 composite heat score
  - sentiment_trend         "rising" | "falling" | "stable"

Run standalone:
  python analysis/social_insights.py
"""
from __future__ import annotations

import re
import time
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

import requests

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from utils.logger import get_logger

log = get_logger("social_insights")

# Keywords
_FEAR_KW  = ["crash","rug","scam","dump","bear","selloff","dead","bust","hack","exploit","fud","panic","fear"]
_GREED_KW = ["moon","pump","bull","ath","gain","profit","lambo","buy","long","rally","breakout","bullish","green"]


@dataclass
class SocialInsight:
    symbol:            str
    mention_count_24h: int
    mention_velocity:  float      # posts/hour
    positive_pct:      float      # 0–100
    negative_pct:      float      # 0–100
    neutral_pct:       float      # 0–100
    top_posts:         list[dict] # {title, score, url, subreddit}
    top_headlines:     list[dict] # {title, source, url}
    fear_greed_proxy:  float      # 0=fear, 100=greed
    social_score:      float      # 0–100
    sentiment_trend:   str        # "rising" | "falling" | "stable"
    fetched_at:        float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "symbol":            self.symbol,
            "mention_count_24h": self.mention_count_24h,
            "mention_velocity":  round(self.mention_velocity, 2),
            "positive_pct":      round(self.positive_pct, 1),
            "negative_pct":      round(self.negative_pct, 1),
            "neutral_pct":       round(self.neutral_pct, 1),
            "top_posts":         self.top_posts,
            "top_headlines":     self.top_headlines,
            "fear_greed_proxy":  round(self.fear_greed_proxy, 1),
            "social_score":      round(self.social_score, 1),
            "sentiment_trend":   self.sentiment_trend,
            "fetched_at":        self.fetched_at,
        }


class SocialInsightsEngine:
    """
    Gathers and caches social media metrics for a list of coin symbols.
    Caches results per-coin for 15 minutes to avoid rate limits.
    """

    def __init__(self, cryptopanic_api_key: str = ""):
        self._cp_key = cryptopanic_api_key
        self._cache: dict[str, SocialInsight] = {}
        self._history: dict[str, list[float]] = defaultdict(list)   # rolling score history
        self._cache_time: dict[str, float] = {}
        self._lock = threading.Lock()

        try:
            from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
            self._vader = SentimentIntensityAnalyzer()
        except ImportError:
            self._vader = None

    # ── Public ───────────────────────────────────────────────────────────────

    def get_insight(self, symbol: str, force: bool = False) -> SocialInsight:
        """Return social insight for one symbol (cached 15 min)."""
        sym = symbol.upper()
        with self._lock:
            age = time.time() - self._cache_time.get(sym, 0)
            if not force and sym in self._cache and age < 900:
                return self._cache[sym]

        insight = self._build_insight(sym)

        with self._lock:
            # Track score history for trend detection
            self._history[sym].append(insight.social_score)
            if len(self._history[sym]) > 10:
                self._history[sym].pop(0)
            self._cache[sym] = insight
            self._cache_time[sym] = time.time()

        return insight

    def get_all_insights(self, symbols: list[str], force: bool = False) -> list[SocialInsight]:
        """Fetch insights for multiple coins. Results are cached individually."""
        return [self.get_insight(s, force=force) for s in symbols]

    def as_dicts(self, symbols: list[str]) -> list[dict]:
        return [self.get_insight(s).to_dict() for s in symbols]

    # ── Build ─────────────────────────────────────────────────────────────────

    def _build_insight(self, symbol: str) -> SocialInsight:
        log.info(f"[Social] Fetching insights for {symbol}...")

        reddit_posts   = self._reddit_posts(symbol)
        cp_headlines   = self._cryptopanic_headlines(symbol)

        all_texts = [p["title"] for p in reddit_posts] + [h["title"] for h in cp_headlines]

        mention_count = len(all_texts)
        mention_velocity = mention_count / 24.0   # rough: posts per hour if spread over 24h

        # Sentiment breakdown via VADER
        pos = neg = neu = 0
        fear_signals = greed_signals = 0

        for text in all_texts:
            if self._vader:
                scores = self._vader.polarity_scores(text)
                if scores["compound"] > 0.05:
                    pos += 1
                elif scores["compound"] < -0.05:
                    neg += 1
                else:
                    neu += 1

            tl = text.lower()
            fear_signals  += sum(1 for kw in _FEAR_KW  if kw in tl)
            greed_signals += sum(1 for kw in _GREED_KW if kw in tl)

        total = max(pos + neg + neu, 1)
        pos_pct = pos / total * 100
        neg_pct = neg / total * 100
        neu_pct = neu / total * 100

        total_fg = fear_signals + greed_signals
        fear_greed = (greed_signals / total_fg * 100) if total_fg > 0 else 50.0

        # Social score 0–100
        # Components: mention volume (max 30) + pos sentiment (max 40) + fear/greed (max 30)
        vol_score      = min(mention_count / 20 * 30, 30)
        sent_score     = pos_pct / 100 * 40
        fg_score       = fear_greed / 100 * 30
        social_score   = vol_score + sent_score + fg_score

        # Trend detection (compare current score to rolling history)
        history = self._history.get(symbol, [])
        if len(history) >= 2:
            delta = social_score - history[-1]
            sentiment_trend = "rising" if delta > 3 else ("falling" if delta < -3 else "stable")
        else:
            sentiment_trend = "stable"

        return SocialInsight(
            symbol            = symbol,
            mention_count_24h = mention_count,
            mention_velocity  = round(mention_velocity, 2),
            positive_pct      = pos_pct,
            negative_pct      = neg_pct,
            neutral_pct       = neu_pct,
            top_posts         = reddit_posts[:3],
            top_headlines     = cp_headlines[:3],
            fear_greed_proxy  = round(fear_greed, 1),
            social_score      = round(social_score, 1),
            sentiment_trend   = sentiment_trend,
        )

    # ── Data fetchers ─────────────────────────────────────────────────────────

    def _reddit_posts(self, symbol: str) -> list[dict]:
        """Search Reddit for recent posts mentioning this symbol."""
        subs = ["CryptoCurrency", "SatoshiStreetBets", "memecoins", "solana", "ethereum"]
        results = []
        pattern = re.compile(
            r'\b' + re.escape(symbol) + r'\b', re.IGNORECASE
        )

        for sub in subs:
            try:
                r = requests.get(
                    f"https://www.reddit.com/r/{sub}/hot.json",
                    params={"limit": 25},
                    headers={"User-Agent": "CryptoNarrativeBot/1.0"},
                    timeout=6,
                )
                r.raise_for_status()
                posts = r.json().get("data", {}).get("children", [])
                for p in posts:
                    data  = p.get("data", {})
                    title = data.get("title", "")
                    if pattern.search(title) or f"${symbol}" in title.upper():
                        results.append({
                            "title":     title,
                            "score":     data.get("score", 0),
                            "url":       f"https://reddit.com{data.get('permalink','')}",
                            "subreddit": sub,
                        })
            except Exception as e:
                log.debug(f"[Social] Reddit {sub} failed for {symbol}: {e}")

        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:10]

    def _cryptopanic_headlines(self, symbol: str) -> list[dict]:
        """Fetch CryptoPanic headlines for this specific coin."""
        params = {
            "public": "true",
            "currencies": symbol.lower(),
            "filter": "hot",
            "limit": "20",
        }
        if self._cp_key:
            params["auth_token"] = self._cp_key
        try:
            r = requests.get(
                "https://cryptopanic.com/api/v1/posts/",
                params=params, timeout=8,
            )
            r.raise_for_status()
            headlines = []
            for post in r.json().get("results", []):
                headlines.append({
                    "title":  post.get("title", ""),
                    "source": post.get("source", {}).get("title", "Unknown"),
                    "url":    post.get("url", ""),
                })
            return headlines[:10]
        except Exception as e:
            log.debug(f"[Social] CryptoPanic for {symbol} failed: {e}")
            return []


# ─────────────────────────────────────────────────────────────────────────────
# SINGLETON
# ─────────────────────────────────────────────────────────────────────────────

_instance: Optional[SocialInsightsEngine] = None

def get_social_engine() -> SocialInsightsEngine:
    global _instance
    if _instance is None:
        try:
            import config as cfg
            _instance = SocialInsightsEngine(
                cryptopanic_api_key=cfg.CRYPTOPANIC_API_KEY or ""
            )
        except Exception:
            _instance = SocialInsightsEngine()
    return _instance


if __name__ == "__main__":
    engine = get_social_engine()
    test_coins = ["BTC", "ETH", "SOL", "DOGE"]

    for sym in test_coins:
        print(f"\n{'═'*60}")
        print(f"  📊 SOCIAL INSIGHTS — {sym}")
        print(f"{'═'*60}")
        insight = engine.get_insight(sym)
        print(f"  Mentions (24h):    {insight.mention_count_24h}")
        print(f"  Velocity:          {insight.mention_velocity:.1f} posts/hr")
        print(f"  Sentiment:         ✅ {insight.positive_pct:.0f}% pos  ❌ {insight.negative_pct:.0f}% neg  ➡️ {insight.neutral_pct:.0f}% neu")
        print(f"  Fear/Greed:        {insight.fear_greed_proxy:.0f}/100")
        print(f"  Social Score:      {insight.social_score:.1f}/100  ({insight.sentiment_trend})")
        if insight.top_posts:
            print(f"  Top Reddit Post:   {insight.top_posts[0]['title'][:70]}")
        if insight.top_headlines:
            print(f"  Top Headline:      {insight.top_headlines[0]['title'][:70]}")
    print()
