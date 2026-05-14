"""
analysis/sentiment.py — Two-layer sentiment engine:

Layer 1 (fast, offline): VADER for quick scoring of each post.
Layer 2 (deep, free):    Groq LLM (Llama 3.3 70B) for narrative-level analysis
                         on aggregated text — detects "buy rumors",
                         "partnership announcements", FUD, etc.

Final score per coin: float in [0.0 → 1.0]
  0.0 = extremely bearish  |  0.5 = neutral  |  1.0 = extremely bullish
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from groq import Groq
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config import GROQ_API_KEY, SENTIMENT_WINDOW_MINUTES, MIN_POSTS_REQUIRED
from data.data_fetcher import Post
from utils.logger import get_logger

log = get_logger("sentiment")

GROQ_MODEL = "llama-3.3-70b-versatile"   # Free on Groq, 70B quality


@dataclass
class CoinSentiment:
    coin: str
    score: float             # 0.0–1.0
    post_count: int
    narrative_summary: str   # LLM explanation
    signal: str              # "BUY" | "SELL" | "HOLD"
    timestamp: float = 0.0

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()


class SentimentAnalyzer:
    def __init__(self):
        self.vader = SentimentIntensityAnalyzer()
        if GROQ_API_KEY:
            self.groq = Groq(api_key=GROQ_API_KEY)
            log.info("Groq LLM initialized (Llama 3.3 70B)")
        else:
            self.groq = None
            log.warning("GROQ_API_KEY not set — using VADER only")

    # ──────────────────────────────────────────
    # PUBLIC: analyze a batch of posts → per-coin scores
    # ──────────────────────────────────────────
    def analyze(self, posts: list[Post]) -> dict[str, CoinSentiment]:
        now = time.time()
        cutoff = now - (SENTIMENT_WINDOW_MINUTES * 60)

        # Filter to the rolling window
        recent = [p for p in posts if p.created_utc >= cutoff]

        # Group by coin
        by_coin: dict[str, list[Post]] = defaultdict(list)
        for p in recent:
            by_coin[p.coin].append(p)

        results: dict[str, CoinSentiment] = {}
        for coin, coin_posts in by_coin.items():
            if len(coin_posts) < MIN_POSTS_REQUIRED:
                log.debug(f"{coin}: only {len(coin_posts)} posts, skipping")
                continue
            results[coin] = self._score_coin(coin, coin_posts)

        return results

    # ──────────────────────────────────────────
    # PRIVATE: score one coin
    # ──────────────────────────────────────────
    def _score_coin(self, coin: str, posts: list[Post]) -> CoinSentiment:
        # Step 1: VADER quick scores
        vader_scores = []
        for p in posts:
            text = f"{p.title} {p.body}"
            vs = self.vader.polarity_scores(text)
            compound = vs["compound"]   # -1.0 to +1.0
            normalized = (compound + 1) / 2   # → 0.0 to 1.0
            vader_scores.append(normalized)

            # CryptoPanic pre-label boosts
            if p.raw_sentiment == "positive":
                vader_scores.append(0.75)
            elif p.raw_sentiment == "negative":
                vader_scores.append(0.25)

        vader_avg = sum(vader_scores) / len(vader_scores) if vader_scores else 0.5

        # Step 2: Groq LLM narrative analysis (if available)
        narrative_summary = "VADER-only analysis."
        llm_score: Optional[float] = None

        if self.groq:
            llm_score, narrative_summary = self._groq_analyze(coin, posts)

        # Blend: 40% VADER + 60% LLM (if available), else 100% VADER
        if llm_score is not None:
            final_score = 0.4 * vader_avg + 0.6 * llm_score
        else:
            final_score = vader_avg

        final_score = max(0.0, min(1.0, final_score))

        from config import SENTIMENT_BUY_THRESHOLD, SENTIMENT_SELL_THRESHOLD
        if final_score >= SENTIMENT_BUY_THRESHOLD:
            signal = "BUY"
        elif final_score <= SENTIMENT_SELL_THRESHOLD:
            signal = "SELL"
        else:
            signal = "HOLD"

        log.info(
            f"{coin}: score={final_score:.2f} signal={signal} "
            f"posts={len(posts)} vader={vader_avg:.2f} llm={llm_score}"
        )
        return CoinSentiment(
            coin=coin,
            score=final_score,
            post_count=len(posts),
            narrative_summary=narrative_summary,
            signal=signal,
        )

    def _groq_analyze(self, coin: str, posts: list[Post]) -> tuple[float, str]:
        # Build a compact text block (max ~2000 chars to save tokens)
        snippets = "\n".join(
            f"- [{p.source}] {p.title[:120]}"
            for p in sorted(posts, key=lambda x: x.score, reverse=True)[:15]
        )

        prompt = f"""You are a crypto trading sentiment analyst.
Analyze the following recent social media posts about {coin} and return a JSON object.

Posts:
{snippets}

Return ONLY valid JSON with these fields:
{{
  "score": <float 0.0 to 1.0, where 0=very bearish, 0.5=neutral, 1.0=very bullish>,
  "narrative": "<one sentence: what is the dominant narrative driving sentiment?>",
  "key_themes": ["<theme1>", "<theme2>"]
}}"""

        try:
            response = self.groq.chat.completions.create(
                model=GROQ_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=200,
            )
            raw = response.choices[0].message.content.strip()
            # Strip ```/```json fences (and trailing fence) robustly.
            if raw.startswith("```"):
                raw = raw.strip("`").strip()
                if raw.lower().startswith("json"):
                    raw = raw[4:].lstrip()
            data = json.loads(raw)
            score = float(data["score"])
            summary = data.get("narrative", "No summary")
            log.debug(f"{coin} LLM score={score:.2f}: {summary}")
            return score, summary
        except Exception as e:
            log.warning(f"Groq analysis failed for {coin}: {e}")
            return None, "LLM analysis failed, using VADER only."
