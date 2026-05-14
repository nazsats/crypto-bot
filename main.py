"""
main.py — Decentralized Crypto Narrative Trading Bot.

Features:
  - Telegram commands   (/buy /sell /portfolio /pause /resume /status /scan /trending)
  - Rug pull scanner    (rugcheck.xyz + honeypot.is before every buy)
  - WebSocket watcher   (catches Pump.fun launches within 1-2 seconds)
  - Take-profit ladder  (sell 25% at 2x, 5x, 10x automatically)
  - Whale copy trading  (mirrors known profitable wallets)

Run:
  python main.py              # full bot loop
  python main.py --dry-run    # sentiment only, no trades
  python main.py --once       # single pass then exit
  python main.py --scan       # scan Pump.fun new launches only
"""

from __future__ import annotations

import argparse
import re
import sys
import time

import schedule
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer as VaderAnalyzer

from config import (
    FETCH_INTERVAL_SECONDS,
    PUMPFUN_ENABLED,
    ETH_SNIPER_ENABLED,
    BASE_SNIPER_ENABLED,
    PUMPFUN_MIN_SENTIMENT,
    PUMPFUN_BUY_SOL,
    PUMPFUN_WS_ENABLED,
    PUMPFUN_WS_MIN_DEV_BUY_SOL,
    PUMPFUN_WS_MAX_MCAP_BUY_USD,
    MEMECOIN_HYPE_KEYWORDS,
    WHALE_TRACKING_ENABLED,
    WHALE_WALLETS,
    WHALE_POLL_INTERVAL_SEC,
    WHALE_MIN_SOL_BUY,
    SOLSCAN_API_KEY,
    RUG_SCAN_ENABLED,
    PAPER_TRADING,
    PAPER_STARTING_SOL,
    TERMINAL_DASHBOARD,
    WEB_DASHBOARD,
    API_HOST,
    API_PORT,
    CEX_ENABLED,
    CEX_MODE,
    BYBIT_API_KEY,
    BYBIT_API_SECRET,
    CEX_STARTING_USDT,
    CEX_USDT_PER_TRADE,
    CEX_MAX_POSITIONS,
    CEX_TAKE_PROFIT_PCT,
    CEX_STOP_LOSS_PCT,
    CEX_MIN_SENTIMENT,
    CEX_MIN_GAIN_PCT,
    TOP_GAINERS_INTERVAL_SEC,
    TA_ENABLED,
    TA_TIMEFRAME,
    TA_CANDLE_LIMIT,
    TRENDING_ENABLED,
    SOCIAL_INSIGHTS_ENABLED,
)
from utils.terminal_ui import start_dashboard, log_activity
from data.data_fetcher import DataFetcher
from analysis.sentiment import SentimentAnalyzer
from dapp.solana.pumpfun_sniper import PumpFunSniper, PumpToken
from utils.logger import get_logger
from utils.bot_state import state
import utils.telegram_notifier as tg
from utils.telegram_bot import start_command_handler

log = get_logger("main")
_vader = VaderAnalyzer()


# ─────────────────────────────────────────────────────────────────────────────
# ARGS
# ─────────────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="Decentralized Crypto Narrative Bot")
    parser.add_argument("--dry-run", action="store_true", help="No trades executed")
    parser.add_argument("--once",    action="store_true", help="Run one cycle and exit")
    parser.add_argument("--scan",    action="store_true", help="Scan Pump.fun launches and exit")
    return parser.parse_args()


# ─────────────────────────────────────────────────────────────────────────────
# WEBSOCKET CALLBACK — fires on every new Pump.fun launch
# ─────────────────────────────────────────────────────────────────────────────

def on_new_pumpfun_token(token: PumpToken):
    """
    Called instantly when a new token launches on Pump.fun.
    This runs in the WebSocket background thread.
    """
    if state.paused or state.dry_run:
        log.info(f"[WS] New token {token.symbol} — skipped (paused or dry-run)")
        return

    log_activity("LAUNCH", f"{token.symbol} just launched on Pump.fun — mcap ${token.market_cap_usd:,.0f}")

    if token.market_cap_usd > PUMPFUN_WS_MAX_MCAP_BUY_USD:
        log.info(f"[WS] {token.symbol} mcap ${token.market_cap_usd:,.0f} too high, skip")
        return

    tg.alert_hype_detected(token.symbol, 0.9, source="Pump.fun WebSocket (live launch)")

    pumpfun = state.pumpfun
    if pumpfun and not state.dry_run:
        log.info(f"[WS] Auto-buying new launch: {token.symbol}")
        pumpfun.buy(token)
        log_activity("BUY", f"[WS] {token.symbol} auto-bought at launch")


# ─────────────────────────────────────────────────────────────────────────────
# WHALE COPY CALLBACK — fires when a whale buys a Pump.fun token
# ─────────────────────────────────────────────────────────────────────────────

def on_whale_buy(wallet: str, mint: str, sol_amount: float):
    """
    Called when a tracked whale wallet buys a Pump.fun token.
    Runs in the WhaleTracker background thread.
    """
    if state.paused:
        log.info(f"[WHALE] Copy trade skipped — bot is paused")
        return

    pumpfun = state.pumpfun
    if not pumpfun:
        return

    token = pumpfun.get_token_by_mint(mint)
    if not token:
        log.warning(f"[WHALE] Could not find token {mint[:12]}... — skipping copy")
        return

    if token.complete:
        log.info(f"[WHALE] {token.symbol} already graduated — skip copy")
        return

    tg._send(
        f"🐋 <b>Whale Copy Trade</b>\n"
        f"Wallet: <code>{wallet[:16]}...</code>\n"
        f"Token:  <b>{token.symbol}</b> ({token.name})\n"
        f"Amount: {sol_amount:.3f} SOL\n"
        f"MCap:   ${token.market_cap_usd:,.0f}\n"
        f"Copying trade..."
    )

    log_activity("WHALE", f"{wallet[:12]}... bought {token.symbol} for {sol_amount:.3f} SOL")
    if not state.dry_run:
        pumpfun.buy(token)
        log_activity("BUY", f"[WHALE COPY] {token.symbol} for {sol_amount:.3f} SOL")


# ─────────────────────────────────────────────────────────────────────────────
# SCAN MODE
# ─────────────────────────────────────────────────────────────────────────────

def scan_pumpfun():
    sniper = PumpFunSniper()
    tokens = sniper.get_new_launches(limit=20)
    log.info(f"\n{'─'*70}")
    log.info(f"{'SYMBOL':<10} {'NAME':<20} {'MCAP':>12} {'PRICE SOL':>14} {'GRADUATED'}")
    log.info(f"{'─'*70}")
    for t in tokens:
        status = "YES" if t.complete else "no"
        log.info(f"{t.symbol:<10} {t.name[:18]:<20} ${t.market_cap_usd:>10,.0f} "
                 f"{t.price_sol:>14.8f}  {status}")
    log.info(f"{'─'*70}\n")
    tg.alert_pumpfun_scan(tokens)   # properly escaped, split-safe


# ─────────────────────────────────────────────────────────────────────────────
# MAIN CYCLE
# ─────────────────────────────────────────────────────────────────────────────

def run_cycle(fetcher: DataFetcher,
              analyzer: SentimentAnalyzer,
              pumpfun: PumpFunSniper,
              eth_sniper,
              base_sniper,
              dry_run: bool):

    if state.paused:
        log.info("Bot is paused — skipping cycle")
        return

    state.cycle_count += 1
    state.last_cycle_time = time.time()
    log.info("─" * 60)
    log_activity("CYCLE", f"Cycle #{state.cycle_count} started")

    # ── Pillar 1: Technical Analysis (Top 20 by market cap) ─────────────────
    if TA_ENABLED:
        try:
            from analysis.top20_scanner import get_scanner
            scanner = get_scanner()
            ta_signals = scanner.scan_all()   # cached 5 min, fast
            # Log summary table every cycle
            log.info("\n" + scanner.summary_table())
            log_activity("TA", f"Top 20 TA scan complete — {len(ta_signals)} coins")
            # Alert on strong signals
            for sig in scanner.strong_signals():
                log_activity(
                    "TA",
                    f"{sig.call_emoji} {sig.symbol}: {sig.call} "
                    f"(RSI={sig.rsi:.0f}, {sig.trend}, score={sig.ta_score:.2f})"
                )
                tg._send(
                    f"📊 <b>TA Signal — {sig.symbol}</b>\n"
                    f"{sig.call_emoji} <b>{sig.call}</b>\n"
                    f"RSI: {sig.rsi:.1f} ({sig.rsi_label})\n"
                    f"Trend: {sig.trend}\n"
                    f"MACD: {sig.macd_label}\n"
                    f"Volume spike: {'Yes 📈' if sig.volume_spike else 'No'}\n"
                    f"Score: {sig.ta_score:.2f}\n"
                    f"<i>{sig.summary}</i>"
                )
        except Exception as e:
            log.error(f"TA scan error: {e}")

    # ── Pillar 2: Trending Token Sentiment ──────────────────────────────────
    if TRENDING_ENABLED:
        try:
            from analysis.trending_sentiment import get_trending_analyzer
            trending_tokens = get_trending_analyzer().scan()   # cached 10 min
            hype_tokens = [t for t in trending_tokens if t.signal == "HYPE"]
            log_activity(
                "TREND",
                f"{len(trending_tokens)} trending tokens — "
                f"{len(hype_tokens)} HYPE: {', '.join(t.symbol for t in hype_tokens[:4])}"
            )
            if hype_tokens:
                hype_lines = "\n".join(
                    f"  {t.signal_emoji} {t.symbol} ({t.name[:12]}): score={t.sentiment_score:.2f}"
                    for t in hype_tokens[:5]
                )
                tg._send(
                    f"🔥 <b>Trending Tokens — HYPE Alert</b>\n{hype_lines}"
                )
        except Exception as e:
            log.error(f"Trending sentiment error: {e}")

    # 1. Fetch from all data sources
    posts = fetcher.fetch_all()
    if not posts:
        log.warning("No posts fetched — check API keys")
        log_activity("ERROR", "No posts fetched — check API keys")
        tg.alert_error("No posts fetched this cycle — check API keys.")
        return

    state.posts_last_cycle = len(posts)
    log_activity("INFO", f"Fetched {len(posts)} posts from all sources")

    # 2. Sentiment analysis
    sentiments = analyzer.analyze(posts)
    state.latest_signals = {c: s.signal for c, s in sentiments.items()}

    # 3. Update trending list in state (for /status command)
    state.latest_trending = list(dict.fromkeys(
        p.coin for p in posts if p.source == "coingecko_trending"
    ))
    if state.latest_trending:
        tg.alert_trending_coins(state.latest_trending)
        log_activity("INFO", f"Trending: {', '.join(state.latest_trending[:5])}")

    # 4. Hype token detection → Pump.fun snipe (or paper trade)
    hype_tokens = _extract_hype_tokens(posts)
    for token_name, score in hype_tokens.items():
        log.info(f"Hype: '{token_name}' score={score:.2f}")
        if score < PUMPFUN_MIN_SENTIMENT:
            continue

        log_activity("HYPE", f"{token_name} score={score:.2f}")
        tg.alert_hype_detected(token_name, score)

        token = pumpfun.find_token(token_name)
        if not token or token.complete or token.market_cap_usd > 50_000:
            continue

        log.info(f"PUMP.FUN: {token.symbol} | mcap=${token.market_cap_usd:,.0f}")

        if state.paper_trading and state.paper_ledger:
            state.paper_ledger.buy(token)
            log_activity("PAPER", f"Paper BUY {token.symbol} @ {token.price_sol:.8f} SOL")
        elif not dry_run:
            tg.alert_pumpfun_buy(
                symbol=token.symbol, name=token.name,
                market_cap=token.market_cap_usd, price_sol=token.price_sol,
                amount_sol=PUMPFUN_BUY_SOL, dry_run=False,
            )
            pumpfun.buy(token)
            log_activity("BUY", f"{token.symbol} @ {token.price_sol:.8f} SOL | mcap ${token.market_cap_usd:,.0f}")

    # 5. Check exits (ladder + stop-loss)
    if state.paper_trading and state.paper_ledger:
        state.paper_ledger.check_exits(pumpfun.get_token_by_mint)
        state.paper_ledger.log_summary(pumpfun.get_token_by_mint)
    elif not dry_run:
        pumpfun.check_exits()

    # 6. ETH / Base sentiment signals
    signals: dict[str, str] = {}
    for coin, s in sentiments.items():
        log.info(f"[{coin}] score={s.score:.2f} signal={s.signal} | {s.narrative_summary}")
        signals[coin] = s.signal
        log_activity("SIGNAL", f"{coin}: {s.signal} (score={s.score:.2f}) — {s.narrative_summary[:50]}")

        if s.signal == "BUY":
            tg.alert_buy_signal(coin, s.score, s.narrative_summary)
        elif s.signal == "SELL":
            tg.alert_sell_signal(coin, s.score)

        if not dry_run:
            if eth_sniper:
                eth_sniper.check_exits()
            if base_sniper:
                base_sniper.check_exits()

    # 7. CEX trading — pass signals + gainers to CEX trader
    if state.cex_trader and not dry_run:
        try:
            state.cex_trader.receive_signals(signals, state.cex_gainers)
            state.cex_portfolio = state.cex_trader.get_portfolio()
            log.info(state.cex_trader.status_summary())
        except Exception as e:
            log.error(f"CEX cycle error: {e}")

    # 8. Cycle summary every 5 cycles
    if state.cycle_count % 5 == 0:
        tg.alert_cycle_summary(
            cycle_num=state.cycle_count,
            posts_fetched=len(posts),
            coins_analyzed=len(sentiments),
            signals=signals,
        )

    log_activity("CYCLE", f"Cycle #{state.cycle_count} complete — {len(sentiments)} coins analysed")
    log.info("Cycle complete.")


# ─────────────────────────────────────────────────────────────────────────────
# HYPE TOKEN EXTRACTOR
# ─────────────────────────────────────────────────────────────────────────────

def _extract_hype_tokens(posts) -> dict[str, float]:
    hype: dict[str, list[float]] = {}
    for post in posts:
        text = (post.title + " " + post.body).lower()
        if not any(kw in text for kw in MEMECOIN_HYPE_KEYWORDS):
            continue
        score = (_vader.polarity_scores(post.title)["compound"] + 1) / 2
        tickers    = re.findall(r'\$([A-Z]{2,8})', post.title.upper())
        caps_words = re.findall(r'\b([A-Z]{2,8})\b', post.title)
        for name in set(tickers + caps_words):
            if name in ("THE", "AND", "FOR", "NOT", "BUT", "NEW", "GET", "ARE"):
                continue
            hype.setdefault(name, []).append(score)
    return {k: sum(v) / len(v) for k, v in hype.items()}


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()

    log.info("=" * 60)
    log.info("  Decentralized Crypto Narrative Bot  ")
    log.info(f"  Mode:       {'DRY-RUN' if args.dry_run else 'LIVE'}")
    log.info(f"  Pump.fun:   {'ON' if PUMPFUN_ENABLED else 'OFF'}")
    log.info(f"  WebSocket:  {'ON' if PUMPFUN_WS_ENABLED else 'OFF'}")
    log.info(f"  Rug Scan:   {'ON' if RUG_SCAN_ENABLED else 'OFF'}")
    log.info(f"  Whale Copy: {'ON' if WHALE_TRACKING_ENABLED else 'OFF'}")
    log.info(f"  ETH:        {'ON' if ETH_SNIPER_ENABLED else 'OFF'}")
    log.info(f"  Base:       {'ON' if BASE_SNIPER_ENABLED else 'OFF'}")
    log.info(f"  CEX:        {'ON (' + CEX_MODE + ')' if CEX_ENABLED else 'OFF'}")
    log.info(f"  TA Engine:  {'ON (' + TA_TIMEFRAME + ' candles)' if TA_ENABLED else 'OFF'}")
    log.info(f"  Trending:   {'ON' if TRENDING_ENABLED else 'OFF'}")
    log.info(f"  Social:     {'ON' if SOCIAL_INSIGHTS_ENABLED else 'OFF'}")
    log.info("=" * 60)

    if args.scan:
        scan_pumpfun()
        return

    # ── Init core components ──────────────────────────────────────────
    fetcher  = DataFetcher()
    analyzer = SentimentAnalyzer()
    pumpfun  = PumpFunSniper()

    # ── Populate shared state ─────────────────────────────────────────
    state.pumpfun       = pumpfun
    state.dry_run       = args.dry_run
    state.paper_trading = PAPER_TRADING

    if PAPER_TRADING:
        from dapp.solana.paper_ledger import PaperLedger
        state.paper_ledger = PaperLedger(starting_sol=PAPER_STARTING_SOL)
        log.info(f"Paper trading ON — virtual balance: {PAPER_STARTING_SOL} SOL")

    eth_sniper  = None
    base_sniper = None

    if ETH_SNIPER_ENABLED or BASE_SNIPER_ENABLED:
        try:
            from dapp.ethereum.uniswap_sniper import UniswapSniper
            if ETH_SNIPER_ENABLED:
                eth_sniper  = UniswapSniper(chain="ETH")
                state.eth_sniper = eth_sniper
            if BASE_SNIPER_ENABLED:
                base_sniper = UniswapSniper(chain="BASE")
                state.base_sniper = base_sniper
        except ImportError:
            log.error("web3 not installed — ETH/Base sniping disabled")

    # ── Start terminal dashboard (background thread) ─────────────────
    if TERMINAL_DASHBOARD:
        start_dashboard(state)

    # ── Start web API server for Next.js dashboard ────────────────────
    if WEB_DASHBOARD:
        try:
            from api.server import start_api_server
            start_api_server(host=API_HOST, port=API_PORT)
            log.info(f"Web dashboard API: http://localhost:{API_PORT}")
        except ImportError:
            log.warning("FastAPI/uvicorn not installed — web dashboard disabled")

    # ── Warm-up Top 20 TA scan in background (so first API call is instant) ──
    if TA_ENABLED:
        import threading
        def _warmup_ta():
            try:
                from analysis.top20_scanner import get_scanner
                scanner = get_scanner()
                scanner.scan_all(force=True)
                log.info("[TA] Warm-up scan complete — Top 20 TA cache ready")
                log_activity("TA", "Top 20 TA warm-up scan complete")
            except Exception as e:
                log.warning(f"[TA] Warm-up scan failed: {e}")
        threading.Thread(target=_warmup_ta, daemon=True, name="TAWarmup").start()

    # ── Warm-up trending sentiment in background ───────────────────────────
    if TRENDING_ENABLED:
        import threading as _t
        def _warmup_trending():
            try:
                from analysis.trending_sentiment import get_trending_analyzer
                get_trending_analyzer().scan(force=True)
                log.info("[Trending] Warm-up scan complete")
            except Exception as e:
                log.warning(f"[Trending] Warm-up failed: {e}")
        _t.Thread(target=_warmup_trending, daemon=True, name="TrendingWarmup").start()

    # ── Start Telegram command handler (background thread) ────────────
    start_command_handler(state)

    # ── Start WebSocket watcher (background thread) ───────────────────
    if PUMPFUN_WS_ENABLED:
        try:
            from dapp.solana.pumpfun_websocket import PumpFunWebSocket
            ws_watcher = PumpFunWebSocket(
                on_new_token=on_new_pumpfun_token,
                min_sol_in_dev_buy=PUMPFUN_WS_MIN_DEV_BUY_SOL,
            )
            ws_watcher.start()
        except ImportError:
            log.error("websocket-client not installed — run: pip install websocket-client")

    # ── Start CEX trader + top gainers fetcher ───────────────────────
    if CEX_ENABLED:
        from cex.bybit_trader import CEXTrader
        from cex.top_gainers import TopGainersFetcher
        cex_trader = CEXTrader(
            mode            = CEX_MODE,
            starting_usdt   = CEX_STARTING_USDT,
            usdt_per_trade  = CEX_USDT_PER_TRADE,
            take_profit_pct = CEX_TAKE_PROFIT_PCT,
            stop_loss_pct   = CEX_STOP_LOSS_PCT,
            max_positions   = CEX_MAX_POSITIONS,
            min_gain_pct    = CEX_MIN_GAIN_PCT,
            min_sentiment   = CEX_MIN_SENTIMENT,
            api_key         = BYBIT_API_KEY,
            api_secret      = BYBIT_API_SECRET,
        )
        state.cex_trader = cex_trader
        cex_trader.start(state)

        gainers_fetcher = TopGainersFetcher(interval_sec=TOP_GAINERS_INTERVAL_SEC)
        gainers_fetcher.start(state)
        log.info(f"CEX trading started — mode={CEX_MODE}  budget=${CEX_USDT_PER_TRADE}/trade")
        log_activity("INFO", f"CEX trader started ({CEX_MODE}) — ${CEX_STARTING_USDT:.0f} virtual USDT")

    # ── Start Whale Tracker (background thread) ───────────────────────
    if WHALE_TRACKING_ENABLED and WHALE_WALLETS:
        from analysis.whale_tracker import WhaleTracker
        whale_tracker = WhaleTracker(
            whale_wallets=WHALE_WALLETS,
            on_whale_buy=on_whale_buy,
            poll_interval=WHALE_POLL_INTERVAL_SEC,
            min_sol_amount=WHALE_MIN_SOL_BUY,
            solscan_api_key=SOLSCAN_API_KEY,
        )
        whale_tracker.start()
    elif WHALE_TRACKING_ENABLED:
        log.warning("WHALE_TRACKING_ENABLED=True but WHALE_WALLETS is empty in config")

    # ── Notify Telegram ───────────────────────────────────────────────
    tg.alert_bot_started(
        dry_run=args.dry_run,
        paper_mode=PAPER_TRADING,
        pumpfun_on=PUMPFUN_ENABLED,
        eth_on=ETH_SNIPER_ENABLED,
        base_on=BASE_SNIPER_ENABLED,
    )

    # ── Single-run mode ───────────────────────────────────────────────
    if args.once:
        run_cycle(fetcher, analyzer, pumpfun, eth_sniper, base_sniper, args.dry_run)
        return

    # ── Main loop ─────────────────────────────────────────────────────
    interval_min = max(1, FETCH_INTERVAL_SECONDS // 60)
    log.info(f"Polling every {interval_min} min. WebSocket catches launches instantly. Ctrl+C to stop.")

    def job():
        try:
            run_cycle(fetcher, analyzer, pumpfun, eth_sniper, base_sniper, args.dry_run)
        except Exception as e:
            log.error(f"Cycle error: {e}", exc_info=True)
            tg.alert_error(str(e))

    job()
    schedule.every(interval_min).minutes.do(job)

    while True:
        schedule.run_pending()
        time.sleep(10)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Bot stopped.")
        tg.alert_bot_stopped()
        sys.exit(0)
