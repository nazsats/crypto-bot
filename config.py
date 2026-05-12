"""
config.py — Decentralized-first configuration.
Focus: Pump.fun (Solana) + Ethereum/Base memecoins via Uniswap.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
# API KEYS
# ─────────────────────────────────────────────
GROQ_API_KEY          = os.getenv("GROQ_API_KEY", "")
CRYPTOPANIC_API_KEY   = os.getenv("CRYPTOPANIC_API_KEY", "")
REDDIT_CLIENT_ID      = os.getenv("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET  = os.getenv("REDDIT_CLIENT_SECRET", "")
REDDIT_USER_AGENT     = os.getenv("REDDIT_USER_AGENT", "CryptoNarrativeBot/1.0")
CMC_API_KEY           = os.getenv("CMC_API_KEY", "")   # coinmarketcap.com — free basic tier

# ─────────────────────────────────────────────
# TELEGRAM BOT
# ─────────────────────────────────────────────
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")   # from @BotFather
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID", "")     # your personal chat ID
TELEGRAM_ENABLED    = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

# ─────────────────────────────────────────────
# WALLET (shared across chains via private key)
# ─────────────────────────────────────────────
WALLET_PRIVATE_KEY  = os.getenv("WALLET_PRIVATE_KEY", "")
WALLET_ADDRESS      = os.getenv("WALLET_ADDRESS", "")
SOLANA_PRIVATE_KEY  = os.getenv("SOLANA_PRIVATE_KEY", "")  # base58

# ─────────────────────────────────────────────
# PAPER TRADING MODE
# ─────────────────────────────────────────────
# Set PAPER_TRADING=True to simulate trades with real Pump.fun data.
# No real transactions are sent. Perfect for learning the bot safely.
PAPER_TRADING           = os.getenv("PAPER_TRADING", "false").lower() == "true"
PAPER_STARTING_SOL      = float(os.getenv("PAPER_STARTING_SOL", "1.0"))  # virtual SOL balance

# ─────────────────────────────────────────────
# RPC ENDPOINTS
# ─────────────────────────────────────────────
SOL_RPC_URL        = os.getenv("SOL_RPC_URL",        "https://api.mainnet-beta.solana.com")
SOL_WSS_URL        = os.getenv("SOL_WSS_URL",        "wss://api.mainnet-beta.solana.com")
SOL_DEVNET_RPC_URL = os.getenv("SOL_DEVNET_RPC_URL", "https://api.devnet.solana.com")  # free devnet
ETH_RPC_URL   = os.getenv("ETH_RPC_URL",  "")
ETH_WSS_URL   = os.getenv("ETH_WSS_URL",  "")
BASE_RPC_URL  = os.getenv("BASE_RPC_URL", "")
BASE_WSS_URL  = os.getenv("BASE_WSS_URL", "")

# ─────────────────────────────────────────────
# PUMP.FUN SETTINGS (Solana)
# ─────────────────────────────────────────────
PUMPFUN_ENABLED         = False         # Set True when Solana wallet ready
PUMPFUN_BUY_SOL         = 0.05          # SOL per snipe (~$7-10)
PUMPFUN_MIN_SENTIMENT   = 0.70          # Only snipe if LLM score >= this
PUMPFUN_SELL_AT_MARKET_CAP = 100_000    # Auto-sell at $100k market cap
PUMPFUN_STOP_LOSS_PCT   = 0.30          # 30% stop-loss (memecoins are volatile)
PUMPFUN_TAKE_PROFIT_PCT = 3.0           # 3x take-profit (legacy, used if ladder disabled)

# ── Take-Profit Ladder ────────────────────────
# Each tuple: (price_multiplier, fraction_of_position_to_sell)
# Example below: sell 25% at 2x, 25% at 5x, 25% at 10x, hold 25% forever
PUMPFUN_LADDER_ENABLED  = True
PUMPFUN_TAKE_PROFIT_LADDER = [
    (2.0,  0.25),   # at 2x  → sell 25% (recover initial investment)
    (5.0,  0.25),   # at 5x  → sell 25% (guaranteed profit)
    (10.0, 0.25),   # at 10x → sell 25% (moon bag partial exit)
    # remaining 25% held until stop-loss or manual sell
]

# ── WebSocket Watcher ─────────────────────────
PUMPFUN_WS_ENABLED          = True      # Real-time launch detection
PUMPFUN_WS_MIN_DEV_BUY_SOL  = 0.1      # Skip launches where dev bought < 0.1 SOL
PUMPFUN_WS_MAX_MCAP_BUY_USD = 10_000   # Only buy WS-detected tokens under $10k mcap

# ── Rug Scanner ───────────────────────────────
RUG_SCAN_ENABLED        = True          # Check rugcheck.xyz before every buy
RUG_SCAN_MIN_SCORE      = 30            # Reject tokens with rugcheck score < 30

# ─────────────────────────────────────────────
# ETHEREUM MEMECOIN SETTINGS (Uniswap v2/v3)
# ─────────────────────────────────────────────
ETH_SNIPER_ENABLED      = False         # Set True when ETH wallet ready
ETH_BUY_AMOUNT_ETH      = 0.01          # ETH per trade (~$25)
ETH_SLIPPAGE_PCT        = 5.0           # 5% slippage (memecoins need higher)
ETH_GAS_LIMIT           = 300_000
ETH_MAX_GAS_GWEI        = 30            # Skip trade if gas > 30 gwei

# BASE CHAIN (cheaper gas, same Uniswap-style DEXes)
BASE_SNIPER_ENABLED     = False         # Set True when Base wallet ready
BASE_BUY_AMOUNT_ETH     = 0.005         # ETH on Base (~$12)

# ─────────────────────────────────────────────
# SENTIMENT THRESHOLDS
# ─────────────────────────────────────────────
SENTIMENT_WINDOW_MINUTES  = 30          # Shorter window for memecoins (faster)
SENTIMENT_BUY_THRESHOLD   = 0.68
SENTIMENT_SELL_THRESHOLD  = 0.35
MIN_POSTS_REQUIRED        = 3           # Lower for new/niche tokens

# ─────────────────────────────────────────────
# TRACKED COINS — keywords used to match posts/headlines to a coin
# ─────────────────────────────────────────────
TRACKED_COINS = {
    "BTC":  {"reddit_keywords": ["bitcoin", "btc", "$btc"]},
    "ETH":  {"reddit_keywords": ["ethereum", "eth", "$eth"]},
    "SOL":  {"reddit_keywords": ["solana", "sol", "$sol"]},
    "DOGE": {"reddit_keywords": ["dogecoin", "doge", "$doge"]},
    "SHIB": {"reddit_keywords": ["shiba inu", "shib", "$shib"]},
    "PEPE": {"reddit_keywords": ["pepe", "$pepe"]},
    "WIF":  {"reddit_keywords": ["dogwifhat", "wif", "$wif"]},
    "BONK": {"reddit_keywords": ["bonk", "$bonk"]},
    "XRP":  {"reddit_keywords": ["ripple", "xrp", "$xrp"]},
    "BNB":  {"reddit_keywords": ["binance coin", "bnb", "$bnb"]},
}

# ─────────────────────────────────────────────
# REDDIT SOURCES FOR MEMECOIN SENTIMENT
# ─────────────────────────────────────────────
REDDIT_SUBREDDITS = [
    "CryptoCurrency",
    "SatoshiStreetBets",
    "memecoins",
    "solana",
    "pumpfun",
    "ethereum",
    "UniSwap",
    "defi",
]

# Keywords to detect new memecoin hype (beyond coin names)
MEMECOIN_HYPE_KEYWORDS = [
    "just launched", "new launch", "gem", "100x", "moon",
    "pump.fun", "pumpfun", "just deployed", "fair launch",
    "stealth launch", "presale", "low cap", "early",
]

# ─────────────────────────────────────────────
# WHALE COPY TRADING
# ─────────────────────────────────────────────
WHALE_TRACKING_ENABLED  = False     # Set True to enable copy trading
WHALE_POLL_INTERVAL_SEC = 30        # How often to check whale wallets (seconds)
WHALE_MIN_SOL_BUY       = 0.5       # Only copy buys >= 0.5 SOL
SOLSCAN_API_KEY         = os.getenv("SOLSCAN_API_KEY", "")  # optional, free at solscan.io

# Add known profitable wallets here (Solana public addresses)
# Find them on: solscan.io → search for wallets with consistent Pump.fun profits
WHALE_WALLETS: list[str] = os.getenv("WHALE_WALLETS", "").split(",") if os.getenv("WHALE_WALLETS") else []

# ─────────────────────────────────────────────
# DASHBOARD / UI
# ─────────────────────────────────────────────
TERMINAL_DASHBOARD  = os.getenv("TERMINAL_DASHBOARD", "true").lower() == "true"
WEB_DASHBOARD       = os.getenv("WEB_DASHBOARD", "true").lower() == "true"
API_HOST            = os.getenv("API_HOST", "0.0.0.0")
API_PORT            = int(os.getenv("API_PORT", "8000"))

# ─────────────────────────────────────────────
# CEX TRADING (Bybit / internal paper)
# ─────────────────────────────────────────────
# CEX_MODE options:
#   "internal_paper"  → no API key needed, simulates with real live prices
#   "bybit_demo"      → Bybit Demo account (fake USDT, real orderbook)
#   "bybit_live"      → real Bybit account (REAL MONEY — be careful)
CEX_ENABLED          = os.getenv("CEX_ENABLED", "true").lower() == "true"
CEX_MODE             = os.getenv("CEX_MODE", "internal_paper")

# Bybit API keys (only needed for bybit_demo or bybit_live)
BYBIT_API_KEY        = os.getenv("BYBIT_API_KEY", "")
BYBIT_API_SECRET     = os.getenv("BYBIT_API_SECRET", "")

# Trade sizing
CEX_STARTING_USDT    = float(os.getenv("CEX_STARTING_USDT",  "1000.0"))
CEX_USDT_PER_TRADE   = float(os.getenv("CEX_USDT_PER_TRADE", "50.0"))
CEX_MAX_POSITIONS    = int(os.getenv("CEX_MAX_POSITIONS",     "5"))

# Exit strategy
CEX_TAKE_PROFIT_PCT  = float(os.getenv("CEX_TAKE_PROFIT_PCT", "20.0"))
CEX_STOP_LOSS_PCT    = float(os.getenv("CEX_STOP_LOSS_PCT",    "10.0"))

# Entry thresholds
CEX_MIN_SENTIMENT    = float(os.getenv("CEX_MIN_SENTIMENT",   "0.65"))
CEX_MIN_GAIN_PCT     = float(os.getenv("CEX_MIN_GAIN_PCT",    "5.0"))

# Top gainers refresh
TOP_GAINERS_INTERVAL_SEC = int(os.getenv("TOP_GAINERS_INTERVAL_SEC", "300"))

# ─────────────────────────────────────────────
# BOT LOOP
# ─────────────────────────────────────────────
FETCH_INTERVAL_SECONDS = 180    # 3 minutes (faster for memecoins)
LOG_LEVEL = "INFO"
LOG_DIR   = "logs"
