"""
analysis/trending_sentiment.py — Trending token discovery + sentiment scoring.

SEPARATE from TA. Tracks tokens that are trending RIGHT NOW on:
  - CoinGecko /trending (top 7 coins searched in last 24h)
  - CryptoPanic trending news
  - Reddit r/CryptoCurrency + r/SatoshiStreetBets hot posts (ticker extraction)

For each discovered trending token, fetches recent posts and scores with
VADER + Groq LLM (same engine as existing SentimentAnalyzer).

Output signal per coin:  HYPE 🔥 | NEUTRAL ➡️ | FADING 📉

Run standalone:
  python analysis/trending_sentiment.py
"""
from __future__ import annotations

import re
import time
import threading
from dataclasses import dataclass, field
from typing import Optional
from collections import defaultdict

import requests

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from utils.logger import get_logger

log = get_logger("trending_sentiment")

COINGECKO_URL  = "https://api.coingecko.com/api/v3"
CRYPTOPANIC_URL = "https://cryptopanic.com/api/v1/posts/"

# Common stop-words to exclude from ticker extraction
_STOP = {
    "THE","AND","FOR","NOT","BUT","NEW","GET","ARE","THIS","THAT",
    "HAVE","WITH","FROM","YOUR","THEY","WILL","BEEN","WHAT","WHEN",
    "MORE","JUST","WAS","HAS","ALL","ALSO","INTO",
    "OVER","THAN","THEN","THEM","SOME","WOULD","LIKE","MAKE",
    "NOW","ONE","HOW","OUR","OUT","ITS","WHY","BIG","TOP",
}


# ─────────────────────────────────────────────────────────────────────────────
# DATA CLASSES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TrendingToken:
    symbol:          str
    name:            str
    trending_rank:   int        # 1 = hottest; 99 = low-rank
    mention_count:   int        # total mentions across sources
    sentiment_score: float      # 0.0–1.0
    signal:          str        # "HYPE" | "NEUTRAL" | "FADING"
    signal_emoji:    str        # 🔥 | ➡️ | 📉
    sources:         list[str]  # ["coingecko", "reddit", "cryptopanic"]
    top_headline:    str        # most-upvoted headline
    narrative:       str        # LLM one-liner (or VADER summary)
    market_cap_rank: int        # CoinGecko rank (0 = unknown)
    fetched_at:      float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "symbol":          self.symbol,
            "name":            self.name,
            "trending_rank":   self.trending_rank,
            "mention_count":   self.mention_count,
            "sentiment_score": round(self.sentiment_score, 3),
            "signal":          self.signal,
            "signal_emoji":    self.signal_emoji,
            "sources":         self.sources,
            "top_headline":    self.top_headline,
            "narrative":       self.narrative,
            "market_cap_rank": self.market_cap_rank,
            "fetched_at":      self.fetched_at,
        }


# ─────────────────────────────────────────────────────────────────────────────
# TRENDING SENTIMENT ANALYZER
# ─────────────────────────────────────────────────────────────────────────────

class TrendingSentimentAnalyzer:
    """
    Discovers trending tokens and scores their social sentiment.
    Completely separate from the TA engine and main bot sentiment flow.
    """

    def __init__(self, cryptopanic_api_key: str = ""):
        self._cp_key = cryptopanic_api_key
        self._cache: list[TrendingToken] = []
        self._cache_time: float = 0.0
        self._lock = threading.Lock()

        # Try to import VADER + Groq for scoring
        try:
            from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
            self._vader = SentimentIntensityAnalyzer()
        except ImportError:
            self._vader = None
            log.warning("vaderSentiment not installed — sentiment scoring disabled")

        try:
            from groq import Groq
            import config as cfg
            if cfg.GROQ_API_KEY:
                self._groq = Groq(api_key=cfg.GROQ_API_KEY)
            else:
                self._groq = None
        except Exception:
            self._groq = None

    # ── Public ───────────────────────────────────────────────────────────────

    def scan(self, force: bool = False) -> list[TrendingToken]:
        """
        Discover trending tokens + score sentiment.
        Caches results for 10 minutes.
        """
        with self._lock:
            if not force and self._cache and (time.time() - self._cache_time) < 600:
                return list(self._cache)

        log.info("[Trending] Starting trending token discovery...")

        # Step 1 — Discover trending symbols from multiple sources
        coingecko_trending = self._fetch_coingecko_trending()
        reddit_mentions    = self._fetch_reddit_mentions()
        cryptopanic_tickers = self._fetch_cryptopanic_trending()

        # Merge: symbol → {sources, mentions, rank, name}
        merged: dict[str, dict] = {}

        for rank, (sym, name, cg_rank) in enumerate(coingecko_trending, 1):
            merged.setdefault(sym, {
                "name": name, "mentions": 0, "sources": [],
                "trending_rank": rank, "market_cap_rank": cg_rank, "headlines": [],
            })
            merged[sym]["sources"].append("coingecko")
            merged[sym]["trending_rank"] = min(merged[sym]["trending_rank"], rank)

        for sym, count, headlines in reddit_mentions:
            merged.setdefault(sym, {
                "name": sym, "mentions": 0, "sources": [],
                "trending_rank": 99, "market_cap_rank": 0, "headlines": [],
            })
            merged[sym]["mentions"]  += count
            merged[sym]["headlines"] += headlines
            if "reddit" not in merged[sym]["sources"]:
                merged[sym]["sources"].append("reddit")

        for sym, headline in cryptopanic_tickers:
            merged.setdefault(sym, {
                "name": sym, "mentions": 0, "sources": [],
                "trending_rank": 99, "market_cap_rank": 0, "headlines": [],
            })
            merged[sym]["mentions"]  += 1
            merged[sym]["headlines"].append(headline)
            if "cryptopanic" not in merged[sym]["sources"]:
                merged[sym]["sources"].append("cryptopanic")

        if not merged:
            log.warning("[Trending] No trending tokens found")
            return []

        # Step 2 — Score each token
        results: list[TrendingToken] = []
        for sym, data in merged.items():
            score, narrative = self._score(sym, data["headlines"])
            signal, emoji = self._to_signal(score, data["trending_rank"])

            top_headline = data["headlines"][0] if data["headlines"] else "No headlines"
            results.append(TrendingToken(
                symbol          = sym,
                name            = data["name"],
                trending_rank   = data["trending_rank"],
                mention_count   = data["mentions"] + len(data["headlines"]),
                sentiment_score = score,
                signal          = signal,
                signal_emoji    = emoji,
                sources         = data["sources"],
                top_headline    = top_headline[:200],
                narrative       = narrative,
                market_cap_rank = data["market_cap_rank"],
            ))

        # Sort: trending_rank ASC then mention_count DESC
        results.sort(key=lambda t: (t.trending_rank, -t.mention_count))

        with self._lock:
            self._cache      = results
            self._cache_time = time.time()

        log.info(f"[Trending] Found {len(results)} trending tokens: "
                 f"{', '.join(t.symbol for t in results[:5])}")
        return results

    def as_dicts(self, force: bool = False) -> list[dict]:
        return [t.to_dict() for t in self.scan(force=force)]

    # ── Source fetchers ──────────────────────────────────────────────────────

    def _fetch_coingecko_trending(self) -> list[tuple[str, str, int]]:
        """Returns [(symbol, name, market_cap_rank), ...]"""
        try:
            r = requests.get(f"{COINGECKO_URL}/search/trending", timeout=10)
            r.raise_for_status()
            data = r.json()
            results = []
            for item in data.get("coins", []):
                c = item.get("item", {})
                sym  = (c.get("symbol") or "").upper()
                name = c.get("name", sym)
                rank = int(c.get("market_cap_rank") or 0)
                if sym:
                    results.append((sym, name, rank))
            return results[:7]
        except Exception as e:
            log.warning(f"[Trending] CoinGecko trending failed: {e}")
            return []

    def _fetch_reddit_mentions(self) -> list[tuple[str, int, list[str]]]:
        """Returns [(symbol, mention_count, [headlines]), ...]"""
        subreddits = ["CryptoCurrency", "SatoshiStreetBets", "memecoins"]
        mentions: dict[str, list[str]] = defaultdict(list)

        for sub in subreddits:
            try:
                r = requests.get(
                    f"https://www.reddit.com/r/{sub}/hot.json",
                    params={"limit": 25},
                    headers={"User-Agent": "CryptoNarrativeBot/1.0"},
                    timeout=8,
                )
                r.raise_for_status()
                posts = r.json().get("data", {}).get("children", [])
                for post in posts:
                    title = post.get("data", {}).get("title", "")
                    # Extract $TICKER and ALL-CAPS words
                    tickers    = re.findall(r'\$([A-Z]{2,8})', title.upper())
                    caps_words = re.findall(r'\b([A-Z]{2,8})\b', title.upper())
                    for sym in set(tickers + caps_words):
                        if sym not in _STOP and len(sym) >= 2:
                            mentions[sym].append(title)
            except Exception as e:
                log.debug(f"[Trending] Reddit {sub} failed: {e}")

        # Return top 10 most-mentioned
        ranked = sorted(mentions.items(), key=lambda x: len(x[1]), reverse=True)
        return [(sym, len(headlines), headlines[:3]) for sym, headlines in ranked[:10]]

    def _fetch_cryptopanic_trending(self) -> list[tuple[str, str]]:
        """Returns [(symbol, headline), ...]"""
        params = {"public": "true", "filter": "hot", "limit": "20"}
        if self._cp_key:
            params["auth_token"] = self._cp_key
        try:
            r = requests.get(CRYPTOPANIC_URL, params=params, timeout=10)
            r.raise_for_status()
            results = []
            for post in r.json().get("results", []):
                title      = post.get("title", "")
                currencies = post.get("currencies") or []
                for c in currencies:
                    sym = (c.get("code") or "").upper()
                    if sym and sym not in _STOP:
                        results.append((sym, title))
            return results[:20]
        except Exception as e:
            log.debug(f"[Trending] CryptoPanic failed: {e}")
            return []

    # ── Scoring ──────────────────────────────────────────────────────────────

    def _score(self, symbol: str, headlines: list[str]) -> tuple[float, str]:
        """Score headlines using VADER (and optionally Groq)."""
        if not headlines or not self._vader:
            return 0.5, "No data for scoring"

        scores = []
        for h in headlines:
            compound = self._vader.polarity_scores(h)["compound"]
            scores.append((compound + 1) / 2)
        vader_avg = sum(scores) / len(scores)

        # Try Groq LLM for deeper narrative
        if self._groq and headlines:
            llm_score, narrative = self._groq_score(symbol, headlines)
            if llm_score is not None:
                final = 0.4 * vader_avg + 0.6 * llm_score
                return max(0.0, min(1.0, final)), narrative

        label = "bullish" if vader_avg > 0.6 else ("bearish" if vader_avg < 0.4 else "neutral")
        return vader_avg, f"VADER-only: {label} sentiment across {len(headlines)} headlines"

    def _groq_score(self, symbol: str, headlines: list[str]) -> tuple[Optional[float], str]:
        import json
        snippets = "\n".join(f"- {h[:120]}" for h in headlines[:10])
        prompt = (
            f"Analyze social/news sentiment for {symbol} crypto token.\n"
            f"Headlines:\n{snippets}\n\n"
            "Return ONLY valid JSON: "
            "{\"score\": <0.0-1.0>, \"narrative\": \"<one sentence>\"}"
        )
        try:
            resp = self._groq.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1, max_tokens=120,
            )
            raw = resp.choices[0].message.content.strip()
            # Strip ```...``` fences correctly. `.lstrip("json")` strips any of
            # {j,s,o,n} chars which mangles real payloads — use a prefix check.
            if raw.startswith("```"):
                # Drop opening fence (optionally tagged ```json) and closing ```.
                raw = raw.strip("`").strip()
                if raw.lower().startswith("json"):
                    raw = raw[4:].lstrip()
            data = json.loads(raw)
            return float(data["score"]), data.get("narrative", "")
        except Exception as e:
            log.debug(f"[Trending] Groq failed for {symbol}: {e}")
            return None, ""

    @staticmethod
    def _to_signal(score: float, rank: int) -> tuple[str, str]:
        if score >= 0.68 and rank <= 5:
            return "HYPE",    "🔥"
        elif score >= 0.55:
            return "NEUTRAL", "➡️"
        else:
            return "FADING",  "📉"


# ─────────────────────────────────────────────────────────────────────────────
# SINGLETON
# ─────────────────────────────────────────────────────────────────────────────

_instance: Optional[TrendingSentimentAnalyzer] = None

def get_trending_analyzer() -> TrendingSentimentAnalyzer:
    global _instance
    if _instance is None:
        try:
            import config as cfg
            _instance = TrendingSentimentAnalyzer(
                cryptopanic_api_key=cfg.CRYPTOPANIC_API_KEY or ""
            )
        except Exception:
            _instance = TrendingSentimentAnalyzer()
    return _instance


if __name__ == "__main__":
    print("\n🔍 Scanning trending tokens...\n")
    analyzer = get_trending_analyzer()
    tokens = analyzer.scan()

    print(f"{'─'*70}")
    print(f"{'SYM':<8} {'NAME':<18} {'RANK':>5} {'MENTIONS':>9} {'SCORE':>7} {'SIGNAL':<12} {'SOURCES'}")
    print(f"{'─'*70}")
    for t in tokens[:15]:
        src = "+".join(t.sources)
        print(
            f"{t.symbol:<8} {t.name[:16]:<18} {t.trending_rank:>5} "
            f"{t.mention_count:>9} {t.sentiment_score:>7.3f} "
            f"{t.signal_emoji} {t.signal:<10} {src}"
        )
    print(f"{'─'*70}")
    print()
    for t in tokens[:5]:
        print(f"  {t.signal_emoji} {t.symbol}: {t.narrative}")
        print(f"     Top headline: {t.top_headline[:80]}")
    print()
