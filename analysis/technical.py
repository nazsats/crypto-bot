"""
analysis/technical.py — Technical Analysis engine for Top 20 market-cap coins.

Fetches 1h OHLCV from Binance (free, no API key) and computes:
  RSI(14), MACD(12,26,9), EMA(20/50), Bollinger Bands(20,2), Volume spikes,
  Support / Resistance (rolling pivot highs/lows).

Run standalone:
  python analysis/technical.py
"""
from __future__ import annotations
import time
from dataclasses import dataclass, field
from typing import Optional
import requests
import pandas as pd
import numpy as np
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from utils.logger import get_logger

log = get_logger("technical")
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
DEFAULT_TIMEFRAME  = "1h"
DEFAULT_LIMIT      = 100


@dataclass
class TechnicalSignal:
    symbol:       str
    timeframe:    str
    price:        float
    rsi:          float
    ema20:        float
    ema50:        float
    macd:         float
    macd_signal_val: float
    macd_hist:    float
    bb_upper:     float
    bb_middle:    float
    bb_lower:     float
    volume:       float
    avg_volume:   float
    support:      float
    resistance:   float
    rsi_label:    str
    macd_label:   str
    trend:        str
    bb_label:     str
    volume_spike: bool
    ta_score:     float
    call:         str
    call_emoji:   str
    summary:      str
    fetched_at:   float = field(default_factory=time.time)
    error:        str = ""

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol, "timeframe": self.timeframe,
            "price": self.price, "rsi": round(self.rsi, 2),
            "ema20": round(self.ema20, 4), "ema50": round(self.ema50, 4),
            "macd": round(self.macd, 6), "macd_signal": round(self.macd_signal_val, 6),
            "macd_hist": round(self.macd_hist, 6),
            "bb_upper": round(self.bb_upper, 4), "bb_middle": round(self.bb_middle, 4),
            "bb_lower": round(self.bb_lower, 4),
            "volume": round(self.volume, 2), "avg_volume": round(self.avg_volume, 2),
            "support": round(self.support, 4), "resistance": round(self.resistance, 4),
            "rsi_label": self.rsi_label, "macd_label": self.macd_label,
            "trend": self.trend, "bb_label": self.bb_label,
            "volume_spike": self.volume_spike,
            "ta_score": round(self.ta_score, 3),
            "call": self.call, "call_emoji": self.call_emoji,
            "summary": self.summary, "fetched_at": self.fetched_at,
            "error": self.error,
        }


def _ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False).mean()

def _rsi(s: pd.Series, period: int = 14) -> pd.Series:
    d = s.diff()
    gain = d.clip(lower=0).ewm(com=period - 1, min_periods=period).mean()
    loss = (-d).clip(lower=0).ewm(com=period - 1, min_periods=period).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    # On flat candles loss is all-zero → rs is NaN → rsi is NaN, which makes
    # all downstream comparisons silently False. Fill with the neutral value 50.
    return rsi.fillna(50.0)

def _macd(s: pd.Series, fast=12, slow=26, signal=9):
    m = _ema(s, fast) - _ema(s, slow)
    sig = _ema(m, signal)
    return m, sig, m - sig

def _bollinger(s: pd.Series, period=20, std_dev=2.0):
    mid = s.rolling(period).mean()
    std = s.rolling(period).std()
    return mid + std_dev * std, mid, mid - std_dev * std


class TechnicalAnalyzer:
    def __init__(self, timeframe: str = DEFAULT_TIMEFRAME, candle_limit: int = DEFAULT_LIMIT):
        self.timeframe    = timeframe
        self.candle_limit = candle_limit

    def analyze(self, symbol: str) -> TechnicalSignal:
        df = self._fetch_ohlcv(symbol)
        if df is None or len(df) < 52:
            return self._err(symbol, "Not enough candle data")
        try:
            return self._compute(symbol, df)
        except Exception as e:
            log.warning(f"[TA] {symbol} error: {e}")
            return self._err(symbol, str(e))

    def _fetch_ohlcv(self, symbol: str) -> Optional[pd.DataFrame]:
        try:
            r = requests.get(BINANCE_KLINES_URL, params={
                "symbol": f"{symbol.upper()}USDT",
                "interval": self.timeframe,
                "limit": self.candle_limit,
            }, timeout=10)
            r.raise_for_status()
            raw = r.json()
        except Exception as e:
            log.warning(f"[TA] Binance fetch failed {symbol}: {e}")
            return None
        if not raw or isinstance(raw, dict):
            return None
        df = pd.DataFrame(raw, columns=[
            "open_time","open","high","low","close","volume",
            "close_time","quote_volume","trades","taker_buy_base","taker_buy_quote","ignore"])
        for col in ("open","high","low","close","volume"):
            df[col] = df[col].astype(float)
        return df

    def _compute(self, symbol: str, df: pd.DataFrame) -> TechnicalSignal:
        close, volume = df["close"], df["volume"]
        rsi_s = _rsi(close)
        e20, e50 = _ema(close, 20), _ema(close, 50)
        macd_l, sig_l, hist_l = _macd(close)
        bb_u, bb_m, bb_l = _bollinger(close)

        price    = float(close.iloc[-1])
        rsi      = float(rsi_s.iloc[-1])
        ema20    = float(e20.iloc[-1]); ema50 = float(e50.iloc[-1])
        macd_v   = float(macd_l.iloc[-1]); sig_v = float(sig_l.iloc[-1])
        hist_v   = float(hist_l.iloc[-1])
        prev_h   = float(hist_l.iloc[-2]) if len(hist_l) >= 2 else 0
        bbu      = float(bb_u.iloc[-1]); bbm = float(bb_m.iloc[-1]); bbl = float(bb_l.iloc[-1])
        vol_now  = float(volume.iloc[-1])
        avg_vol  = float(volume.rolling(20).mean().iloc[-1])
        support  = float(df["low"].rolling(20).min().iloc[-1])
        resist   = float(df["high"].rolling(20).max().iloc[-1])

        rsi_label = "oversold" if rsi < 30 else ("overbought" if rsi > 70 else "neutral")

        if hist_v > 0 and prev_h <= 0:
            macd_label = "bullish_cross"
        elif hist_v < 0 and prev_h >= 0:
            macd_label = "bearish_cross"
        elif hist_v > 0:
            macd_label = "bullish"
        elif hist_v < 0:
            macd_label = "bearish"
        else:
            macd_label = "neutral"

        if price > ema50 and ema20 > ema50:
            trend = "uptrend"
        elif price < ema50 and ema20 < ema50:
            trend = "downtrend"
        else:
            trend = "sideways"

        bb_width     = bbu - bbl
        bb_avg_width = float((bb_u - bb_l).rolling(20).mean().iloc[-1])
        if bb_width < bb_avg_width * 0.6:
            bb_label = "squeeze"
        elif price > bbu:
            bb_label = "upper_break"
        elif price < bbl:
            bb_label = "lower_break"
        else:
            bb_label = "normal"

        volume_spike = avg_vol > 0 and vol_now > avg_vol * 2.0

        # Composite score — baseline 0.5
        score = 0.5
        if rsi_label == "oversold":    score += 0.25
        elif rsi_label == "overbought": score -= 0.25
        if macd_label == "bullish_cross":   score += 0.20
        elif macd_label == "bearish_cross": score -= 0.20
        elif macd_label == "bullish":   score += 0.10
        elif macd_label == "bearish":   score -= 0.10
        if trend == "uptrend":   score += 0.15
        elif trend == "downtrend": score -= 0.15
        if volume_spike:         score += 0.10
        if bb_label == "lower_break": score += 0.10
        elif bb_label == "upper_break": score += 0.15

        ta_score = max(0.0, min(1.0, score))

        if ta_score >= 0.75:    call, emoji = "STRONG BUY",  "🚀"
        elif ta_score >= 0.62:  call, emoji = "BUY",         "✅"
        elif ta_score <= 0.25:  call, emoji = "STRONG SELL", "🔴"
        elif ta_score <= 0.38:  call, emoji = "SELL",        "❌"
        else:                   call, emoji = "HOLD",        "⏸️"

        parts = []
        if rsi_label != "neutral": parts.append(f"RSI {rsi:.0f} ({rsi_label})")
        parts.append(trend)
        if macd_label not in ("neutral",): parts.append(f"MACD {macd_label.replace('_',' ')}")
        if volume_spike: parts.append("vol spike 📈")
        if bb_label != "normal": parts.append(f"BB {bb_label.replace('_',' ')}")
        summary = " | ".join(parts) or "No strong signals"

        return TechnicalSignal(
            symbol=symbol, timeframe=self.timeframe, price=price,
            rsi=rsi, ema20=ema20, ema50=ema50,
            macd=macd_v, macd_signal_val=sig_v, macd_hist=hist_v,
            bb_upper=bbu, bb_middle=bbm, bb_lower=bbl,
            volume=vol_now, avg_volume=avg_vol,
            support=support, resistance=resist,
            rsi_label=rsi_label, macd_label=macd_label,
            trend=trend, bb_label=bb_label, volume_spike=volume_spike,
            ta_score=ta_score, call=call, call_emoji=emoji, summary=summary,
        )

    @staticmethod
    def _err(symbol: str, msg: str) -> TechnicalSignal:
        return TechnicalSignal(
            symbol=symbol, timeframe=DEFAULT_TIMEFRAME, price=0,
            rsi=50, ema20=0, ema50=0, macd=0, macd_signal_val=0, macd_hist=0,
            bb_upper=0, bb_middle=0, bb_lower=0, volume=0, avg_volume=0,
            support=0, resistance=0,
            rsi_label="neutral", macd_label="neutral",
            trend="sideways", bb_label="normal", volume_spike=False,
            ta_score=0.5, call="HOLD", call_emoji="⏸️",
            summary=f"Error: {msg}", error=msg,
        )


if __name__ == "__main__":
    from analysis.top20_scanner import TOP20_COINS
    analyzer = TechnicalAnalyzer()
    test = TOP20_COINS[:5]
    print(f"\n{'─'*78}")
    print(f"  TA TEST — 1H — {pd.Timestamp.utcnow().strftime('%Y-%m-%d %H:%M')} UTC")
    print(f"{'─'*78}")
    print(f"{'COIN':<6} {'PRICE':>12} {'RSI':>6} {'TREND':<11} {'MACD':<18} {'SCORE':>6} {'CALL'}")
    print(f"{'─'*78}")
    for coin in test:
        s = analyzer.analyze(coin)
        print(f"{s.symbol:<6} ${s.price:>11,.2f} {s.rsi:>6.1f} {s.trend:<11} {s.macd_label:<18} {s.ta_score:>6.3f} {s.call_emoji} {s.call}")
    print(f"{'─'*78}\n")
