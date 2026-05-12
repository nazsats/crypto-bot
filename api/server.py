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

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from utils.bot_state import state as bot_state
from utils.logger import get_logger

log = get_logger("api")

app = FastAPI(title="Crypto Narrative Bot API", version="1.0.0")

# Allow Next.js dev server (localhost:3000) and production
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000", "*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
def pause_bot():
    bot_state.pause()
    _broadcast_update()
    return {"ok": True, "paused": True}


@app.post("/api/resume")
def resume_bot():
    bot_state.resume()
    _broadcast_update()
    return {"ok": True, "paused": False}


@app.post("/api/scan")
def trigger_scan():
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
def cex_manual_sell(symbol: str):
    """Manually close a CEX position by symbol."""
    trader = bot_state.cex_trader
    if not trader:
        return {"ok": False, "error": "CEX trader not initialized"}
    success = trader.manual_sell(symbol)
    return {"ok": success, "symbol": symbol.upper()}


@app.post("/api/cex/buy")
async def cex_manual_buy(request: Request):
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

    symbol = body.get("symbol", "").strip()
    if not symbol:
        return {"ok": False, "error": "symbol is required"}

    result = trader.manual_buy(
        symbol          = symbol,
        usdt_amount     = body.get("usdt_amount"),
        take_profit_pct = body.get("take_profit_pct"),
        stop_loss_pct   = body.get("stop_loss_pct"),
    )
    return result


@app.post("/api/cex/update/{symbol}")
async def cex_update_position(symbol: str, request: Request):
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


def _broadcast_update():
    """Push a snapshot to all connected WebSocket clients (sync helper)."""
    # This is called from sync context — just update the state;
    # the WS loop will pick it up on next 2s tick automatically
    pass


# ─────────────────────────────────────────────────────────────────────────────
# RUNNER
# ─────────────────────────────────────────────────────────────────────────────

def start_api_server(host: str = "0.0.0.0", port: int = 8000):
    """Start the FastAPI server in a background daemon thread."""
    def _run():
        uvicorn.run(app, host=host, port=port, log_level="warning")

    thread = threading.Thread(target=_run, daemon=True, name="APIServer")
    thread.start()
    log.info(f"API server started at http://{host}:{port}")
    return thread


if __name__ == "__main__":
    print("Starting API server on http://localhost:8000")
    print("Open your Next.js dashboard and it will connect automatically.")
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)
