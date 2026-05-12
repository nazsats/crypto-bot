# Crypto Narrative Trading Bot

Automated crypto trading bot using Reddit + CryptoPanic sentiment, Groq LLM analysis, Binance CEX trading, and optional DEX (PancakeSwap/Uniswap) interaction.

## Quick Start

### 1. Install dependencies
```bash
cd crypto-narrative-bot
pip install -r requirements.txt
```

### 2. Configure API keys
```bash
cp .env.example .env
# Edit .env with your keys
```

### 3. Get free API keys

| Service | URL | Cost |
|---|---|---|
| Groq (LLM) | https://console.groq.com | Free |
| CryptoPanic | https://cryptopanic.com/developers/api/ | Free |
| Reddit API | https://www.reddit.com/prefs/apps | Free |
| Binance Testnet | https://testnet.binance.vision/ | Free |
| Alchemy (ETH RPC) | https://www.alchemy.com | Free |

### 4. Run

```bash
# Dry run (no trades, just fetch + analyze)
python main.py --dry-run

# Single cycle test
python main.py --once

# Full bot loop (every 5 min)
python main.py
```

## Architecture

```
main.py                     ← Bot loop + orchestration
config.py                   ← All settings in one place
data/
  data_fetcher.py           ← Reddit + CryptoPanic data
analysis/
  sentiment.py              ← VADER + Groq LLM scoring
execution/
  trader.py                 ← Binance CEX trading (testnet/live)
dapp/
  dex_trader.py             ← PancakeSwap / Uniswap DEX trading
utils/
  logger.py                 ← Colored logs + file rotation
```

## Sentiment Signal Logic

| Score | Signal | Action |
|---|---|---|
| ≥ 0.65 | BUY | Open position |
| ≤ 0.35 | SELL | Close position |
| 0.35–0.65 | HOLD | No action |

## DApp Mode (Optional)

Set `DAPP_ENABLED=True` in `config.py` to enable DEX trading alongside CEX.

- Uses **BSC (PancakeSwap)** by default — cheapest gas (~$0.10/swap)
- Swaps BNB → token on BUY signal, token → BNB on SELL
- **Use a dedicated hot wallet with small funds only**

## Safety

- Default: Binance **Testnet** (paper trading, no real money)
- Start with `--dry-run` to verify everything works
- Small fixed position sizes (`TRADE_SIZE_USDT = 20`)
- Built-in stop-loss (5%) and take-profit (10%)
