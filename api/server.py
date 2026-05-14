"""
api/server.py — FastAPI backend for the Next.js dashboard.

Serves live bot data over HTTP + WebSocket so the web dashboard
can show real-time updates without polling.

Endpoints:
  GET  /api/status       — bot state, uptime, cycle count
  GET  /api/portfolio    — open positions + PnL
  GET  /api/signals      — latest sentiment signals
  GET  /api/trending     — trending coins
  GET  /api/activity     — recent activity feed
  POST /api/pause        — pause the bot
  POST /api/resume       — resume the bot
  POST /api/scan         — trigger a Pump.fun scan
  WS   /ws              — WebSocket, pushes updates every 2s

Run:
  python api/server.py
  (or it starts automatically from main.py when DASHBOARD_ENABLED=True)
"""

from __future__ import annotations

import asyncio
import json
import threading
import time

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, Header, HTTPException, Depends  # noqa: F401
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

import config as cfg
from utils.bot_state import state as bot_state
from utils.logger import get_logger

log = get_logger("api")

app = FastAPI(title="Crypto Narrative Bot API", version="1.0.0")

# CORS: explicit allow-list only. Never use "*" with credentials.
app.add_middleware(
    CORSMiddleware,
    allow_origins=cfg.API_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)


# ─────────────────────────────────────────────────────────────────────────────
# AUTH — required for all state-changing endpoints
# ─────────────────────────────────────────────────────────────────────────────
def require_auth(authorization: str | None = Header(default=None)):
    """Reject if API_AUTH_TOKEN is unset (server misconfigured) or token mismatch."""
    if not cfg.API_AUTH_TOKEN:
        # Server has no token configured -> trading endpoints must be disabled.
        raise HTTPException(status_code=503, detail="API_AUTH_TOKEN not configured; mutating endpoints disabled.")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    presented = authorization[len("Bearer "):].strip()
    # constant-time compare
    import hmac
    if not hmac.compare_digest(presented, cfg.API_AUTH_TOKEN):
        raise HTTPException(status_code=403, detail="Invalid token")
    return True

# Track connected WebSocket clients
_ws_clients: list[WebSocket] = []
_ws_lock = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# DATA SERIALIZERS
# ─────────────────────────────────────────────────────────────────────────────

def _serialize_state() -> dict:
    s = bot_state
    return {
        "running":       True,
        "paused":        s.paused,
        "paper_trading": s.paper_trading,
        "dry_run":       s.dry_run,
        "uptime":        s.uptime_str(),
        "cycle_count":   s.cycle_count,
        "last_cycle":    s.last_cycle_ago(),
        "posts_per_cycle": s.posts_last_cycle,
        "start_time":    s.start_time,
    }


def _serialize_portfolio() -> dict:
    s = bot_state

    if s.paper_trading and s.paper_ledger:
        pumpfun = s.pumpfun
        summary = s.paper_ledger.portfolio_summary(
            pumpfun.get_token_by_mint if pumpfun else lambda m: None
        )
        return {
            "mode":          "paper",
            "positions":     summary["positions"],
            "virtual_sol":   summary["virtual_sol"],
            "starting_sol":  summary["starting_sol"],
            "realised_pnl":  summary["realised_pnl"],
            "unrealised_pnl": summary["unrealised_pnl"],
            "total_pnl":     summary["total_pnl"],
            "total_trades":  summary["total_trades"],
            "win_rate":      summary["win_rate"],
        }

    # Live positions
    pumpfun = s.pumpfun
    if not pumpfun:
        return {"mode": "live", "positions": []}

    positions = []
    for mint, pos in pumpfun.positions.items():
        token   = pumpfun.get_token_by_mint(mint)
        current = token.price_sol if token else pos.entry_price_sol
        pnl_pct = (current - pos.entry_price_sol) / pos.entry_price_sol * 100
        positions.append({
            "mint":          mint,
            "symbol":        pos.symbol,
            "entry_price":   pos.entry_price_sol,
            "current_price": current,
            "pnl_pct":       round(pnl_pct, 2),
            "sol_spent":     pos.sol_spent,
            "opened_at":     pos.opened_at,
        })

    return {"mode": "live", "positions": positions}


def _serialize_activity() -> list[dict]:
    from utils.terminal_ui import _activity, _activity_lock
    with _activity_lock:
        return [
            {"time": t, "type": etype, "message": msg}
            for t, etype, msg in list(_activity)
        ]


def _serialize_cex() -> dict:
    """CEX section for the WebSocket snapshot."""
    trader = bot_state.cex_trader
    if not trader:
        return {"enabled": False}
    portfolio = bot_state.cex_portfolio or trader.get_portfolio()
    gainers = bot_state.cex_gainers or []
    return {
        "enabled":   True,
        "mode":      trader.mode,
        "portfolio": portfolio,
        "gainers": [
            {
                "symbol":     g.symbol,
                "name":       g.name,
                "price_usd":  g.price_usd,
                "change_24h": g.change_24h,
                "volume_24h": g.volume_24h,
                "market_cap": g.market_cap,
                "rank":       g.rank,
                "score":      g.momentum_score,
            }
            for g in gainers[:20]
        ],
    }


def _full_snapshot() -> dict:
    return {
        "status":    _serialize_state(),
        "portfolio": _serialize_portfolio(),
        "signals":   bot_state.latest_signals,
        "trending":  bot_state.latest_trending,
        "activity":  _serialize_activity(),
        "cex":       _serialize_cex(),
        "timestamp": time.time(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# REST ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/status")
def get_status():
    return _serialize_state()


@app.get("/api/portfolio")
def get_portfolio():
    return _serialize_portfolio()


@app.get("/api/signals")
def get_signals():
    return {"signals": bot_state.latest_signals}


@app.get("/api/trending")
def get_trending():
    return {"trending": bot_state.latest_trending}


@app.get("/api/activity")
def get_activity():
    return {"activity": _serialize_activity()}


@app.get("/api/snapshot")
def get_snapshot():
    """Full data snapshot — called on page load."""
    return _full_snapshot()


@app.post("/api/pause")
def pause_bot(_: bool = Depends(require_auth)):
    bot_state.pause()
    return {"ok": True, "paused": True}


@app.post("/api/resume")
def resume_bot(_: bool = Depends(require_auth)):
    bot_state.resume()
    return {"ok": True, "paused": False}


@app.post("/api/scan")
def trigger_scan(_: bool = Depends(require_auth)):
    """Trigger an immediate Pump.fun scan and return results."""
    pumpfun = bot_state.pumpfun
    if not pumpfun:
        return {"ok": False, "error": "Pump.fun not initialized"}

    tokens = pumpfun.get_new_launches(limit=20)
    return {
        "ok": True,
        "tokens": [
            {
                "symbol":     t.symbol,
                "name":       t.name,
                "market_cap": t.market_cap_usd,
                "price_sol":  t.price_sol,
                "graduated":  t.complete,
                "mint":       t.mint,
            }
            for t in tokens
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# PILLAR 1 — TECHNICAL ANALYSIS ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/ta/all")
def get_ta_all(force: bool = False):
    """
    TA calls for all Top 20 coins by market cap.
    Cached for 5 minutes. Use ?force=true to bypass cache.
    """
    try:
        from analysis.top20_scanner import get_scanner
        scanner = get_scanner()
        signals = scanner.scan_all(force=force)
        return {
            "ok":        True,
            "count":     len(signals),
            "timeframe": signals[0].timeframe if signals else "1h",
            "signals":   [s.to_dict() for s in signals],
        }
    except Exception as e:
        log.error(f"[API] /api/ta/all error: {e}")
        return {"ok": False, "error": str(e), "signals": []}


@app.get("/api/ta/{symbol}")
def get_ta_symbol(symbol: str, force: bool = False):
    """
    Full TA detail for a single coin.
    Returns RSI, MACD, EMA, Bollinger Bands, Volume, S/R, score, and call.
    """
    try:
        from analysis.top20_scanner import get_scanner
        scanner = get_scanner()
        sym = symbol.upper()
        # Use cached value if available, else compute fresh
        sig = scanner.get_signal(sym)
        if not sig or force:
            from analysis.technical import TechnicalAnalyzer
            sig = TechnicalAnalyzer().analyze(sym)
        return {"ok": True, "signal": sig.to_dict()}
    except Exception as e:
        log.error(f"[API] /api/ta/{symbol} error: {e}")
        return {"ok": False, "error": str(e)}


@app.get("/api/ta/strong")
def get_ta_strong():
    """Return only STRONG BUY and STRONG SELL signals from the Top 20 cache."""
    try:
        from analysis.top20_scanner import get_scanner
        strong = get_scanner().strong_signals()
        return {
            "ok":     True,
            "count":  len(strong),
            "signals": [s.to_dict() for s in strong],
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "signals": []}


# ─────────────────────────────────────────────────────────────────────────────
# PILLAR 2 — TRENDING SENTIMENT ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/trending/sentiment")
def get_trending_sentiment(force: bool = False):
    """
    Trending tokens discovered from CoinGecko, Reddit, CryptoPanic
    with VADER + LLM sentiment score.
    Returns HYPE / NEUTRAL / FADING signal per token.
    Cached 10 minutes. Use ?force=true to refresh.
    """
    try:
        from analysis.trending_sentiment import get_trending_analyzer
        tokens = get_trending_analyzer().scan(force=force)
        return {
            "ok":    True,
            "count": len(tokens),
            "tokens": [t.to_dict() for t in tokens],
        }
    except Exception as e:
        log.error(f"[API] /api/trending/sentiment error: {e}")
        return {"ok": False, "error": str(e), "tokens": []}


@app.get("/api/trending/hype")
def get_trending_hype():
    """Return only HYPE 🔥 tokens from the trending cache."""
    try:
        from analysis.trending_sentiment import get_trending_analyzer
        tokens = [t for t in get_trending_analyzer().scan() if t.signal == "HYPE"]
        return {"ok": True, "count": len(tokens), "tokens": [t.to_dict() for t in tokens]}
    except Exception as e:
        return {"ok": False, "error": str(e), "tokens": []}


# ─────────────────────────────────────────────────────────────────────────────
# PILLAR 3 — SOCIAL INSIGHTS ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/social/insights")
def get_social_insights_all():
    """
    Social media metrics for all tracked Top 20 coins.
    Includes mention count, velocity, sentiment breakdown,
    top Reddit posts, top headlines, fear/greed proxy, social score.
    Each coin is cached for 15 minutes independently.
    """
    try:
        from analysis.social_insights import get_social_engine
        from analysis.top20_scanner import TOP20_COINS
        engine   = get_social_engine()
        insights = engine.as_dicts(TOP20_COINS[:10])   # first 10 to avoid rate limits
        return {"ok": True, "count": len(insights), "insights": insights}
    except Exception as e:
        log.error(f"[API] /api/social/insights error: {e}")
        return {"ok": False, "error": str(e), "insights": []}


@app.get("/api/social/insights/{symbol}")
def get_social_insight_symbol(symbol: str, force: bool = False):
    """
    Deep social analytics for a single coin.
    Returns mention count, velocity, sentiment %, top posts,
    top headlines, fear/greed proxy, and 0-100 social score.
    """
    try:
        from analysis.social_insights import get_social_engine
        engine  = get_social_engine()
        insight = engine.get_insight(symbol.upper(), force=force)
        return {"ok": True, "insight": insight.to_dict()}
    except Exception as e:
        log.error(f"[API] /api/social/insights/{symbol} error: {e}")
        return {"ok": False, "error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# CEX ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/cex/portfolio")
def get_cex_portfolio():
    """CEX paper/demo/live portfolio — balance, positions, PnL, win rate."""
    trader = bot_state.cex_trader
    if not trader:
        return {"ok": False, "error": "CEX trader not initialized", "mode": "disabled"}
    return {"ok": True, **trader.get_portfolio()}


@app.get("/api/cex/gainers")
def get_cex_gainers():
    """Top gaining coins fetched from CoinGecko (updates every 5 min)."""
    gainers = bot_state.cex_gainers or []
    return {
        "ok":      True,
        "count":   len(gainers),
        "gainers": [
            {
                "symbol":     g.symbol,
                "name":       g.name,
                "price_usd":  g.price_usd,
                "change_24h": g.change_24h,
                "volume_24h": g.volume_24h,
                "market_cap": g.market_cap,
                "rank":       g.rank,
                "score":      g.momentum_score,
                "source":     g.source,
            }
            for g in gainers
        ],
    }


@app.post("/api/cex/sell/{symbol}")
def cex_manual_sell(symbol: str, _: bool = Depends(require_auth)):
    """Manually close a CEX position by symbol."""
    trader = bot_state.cex_trader
    if not trader:
        return {"ok": False, "error": "CEX trader not initialized"}
    success = trader.manual_sell(symbol)
    return {"ok": success, "symbol": symbol.upper()}


@app.post("/api/cex/buy")
async def cex_manual_buy(request: Request, _: bool = Depends(require_auth)):
    """
    Manually open a CEX position — bypasses sentiment/gainer requirements.

    Body (JSON):
      symbol          string  required  e.g. "BTC"
      usdt_amount     float   optional  default = CEX_USDT_PER_TRADE
      take_profit_pct float   optional  default = CEX_TAKE_PROFIT_PCT
      stop_loss_pct   float   optional  default = CEX_STOP_LOSS_PCT
    """
    trader = bot_state.cex_trader
    if not trader:
        return {"ok": False, "error": "CEX trader not initialized"}
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "error": "Invalid JSON body"}

    symbol = str(body.get("symbol", "")).strip()
    if not symbol:
        return {"ok": False, "error": "symbol is required"}

    # Validate numeric inputs — JSON strings/None coerced to None for defaults.
    def _opt_float(v):
        if v is None or v == "":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    result = trader.manual_buy(
        symbol          = symbol.upper(),
        usdt_amount     = _opt_float(body.get("usdt_amount")),
        take_profit_pct = _opt_float(body.get("take_profit_pct")),
        stop_loss_pct   = _opt_float(body.get("stop_loss_pct")),
    )
    return result


@app.post("/api/cex/update/{symbol}")
async def cex_update_position(symbol: str, request: Request, _: bool = Depends(require_auth)):
    """
    Update TP/SL thresholds for an open position.

    Body (JSON):
      take_profit_pct float  optional
      stop_loss_pct   float  optional
    """
    trader = bot_state.cex_trader
    if not trader:
        return {"ok": False, "error": "CEX trader not initialized"}
    try:
        body = await request.json()
    except Exception:
        body = {}

    result = trader.update_position(
        symbol          = symbol,
        take_profit_pct = body.get("take_profit_pct"),
        stop_loss_pct   = body.get("stop_loss_pct"),
    )
    return result


# ─────────────────────────────────────────────────────────────────────────────
# WEBSOCKET
# ─────────────────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    with _ws_lock:
        _ws_clients.append(websocket)
    log.info(f"WebSocket client connected ({len(_ws_clients)} total)")

    try:
        # Send initial snapshot immediately
        await websocket.send_text(json.dumps(_full_snapshot()))

        # Keep connection alive and push updates
        while True:
            await asyncio.sleep(2)
            data = json.dumps(_full_snapshot())
            await websocket.send_text(data)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.debug(f"WebSocket error: {e}")
    finally:
        with _ws_lock:
            if websocket in _ws_clients:
                _ws_clients.remove(websocket)
        log.info(f"WebSocket client disconnected ({len(_ws_clients)} remaining)")


# ─────────────────────────────────────────────────────────────────────────────
# RUNNER
# ─────────────────────────────────────────────────────────────────────────────

def start_api_server(host: str | None = None, port: int | None = None):
    """Start the FastAPI server in a background daemon thread."""
    h = host or cfg.API_HOST
    p = port or cfg.API_PORT

    def _run():
        uvicorn.run(app, host=h, port=p, log_level="warning")

    thread = threading.Thread(target=_run, daemon=True, name="APIServer")
    thread.start()
    log.info(f"API server started at http://{h}:{p}")
    if not cfg.API_AUTH_TOKEN:
        log.warning("API_AUTH_TOKEN is empty — mutating endpoints will reject all requests.")
    return thread


if __name__ == "__main__":
    print(f"Starting API server on http://{cfg.API_HOST}:{cfg.API_PORT}")
    print("Open your Next.js dashboard and it will connect automatically.")
    uvicorn.run(app, host=cfg.API_HOST, port=cfg.API_PORT, reload=False)
