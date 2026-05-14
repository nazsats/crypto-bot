# Copilot Instructions for `crypto-bot`

- This repo is a single Python bot with a central orchestrator in `main.py`. The bot is configured by `config.py`, which uses `python-dotenv` and environment variables for feature toggles and API keys.
- `main.py` is the entrypoint. It builds a `DataFetcher`, `SentimentAnalyzer`, and optional trading clients, then starts background helpers and the main scheduled loop.
- Shared runtime state is stored in `utils/bot_state.py` as a singleton `state`. Background threads, the Telegram command handler, and the web API all read/write this object.
- Data ingestion lives in `data/data_fetcher.py`. It pulls from Reddit, CryptoPanic, CoinGecko, CoinMarketCap, and RSS feeds. Coin detection is based on `TRACKED_COINS` keywords in `config.py`.
- Sentiment scoring is in `analysis/sentiment.py`: VADER is the fast fallback, and Groq LLM is optional when `GROQ_API_KEY` is set. Final signals are derived from `SENTIMENT_BUY_THRESHOLD` / `SENTIMENT_SELL_THRESHOLD`.
- DEX trading is mainly implemented in `dapp/solana/pumpfun_sniper.py` for Pump.fun tokens. There are optional Ethereum/Base snipers under `dapp/ethereum/` controlled by `ETH_SNIPER_ENABLED` and `BASE_SNIPER_ENABLED`.
- The web dashboard API is in `api/server.py`. It exposes status, portfolio, signals, trending, and activity endpoints, plus pause/resume/scan actions.
- Telegram controls are implemented in `utils/telegram_bot.py` using long polling and inline buttons. Commands and button callbacks are wired through `start_command_handler(state)` in `main.py`.
- The project uses thread-based concurrency and a scheduler (`schedule`). Avoid introducing broad async refactors unless you fully understand the current thread + shared-state model.
- Run commands:
  - `pip install -r requirements.txt`
  - `python main.py` for full bot loop
  - `python main.py --dry-run` for sentiment-only mode
  - `python main.py --once` for a single cycle
  - `python main.py --scan` for Pump.fun scan mode
  - `python api/server.py` to run the FastAPI dashboard independently
- Optional packages are not installed by default: `web3`, `solders`, `solana` are needed only for Ethereum/Base and Solana DEX features. The repo's `requirements.txt` comments this.
- Preserve feature flag behavior: many components can be disabled via config toggles such as `PUMPFUN_ENABLED`, `RUG_SCAN_ENABLED`, `CEX_ENABLED`, `PAPER_TRADING`, `TERMINAL_DASHBOARD`, and `WEB_DASHBOARD`.
- Avoid generic changes to status and signal flows; the bot relies on consistent serialized state for the web API and Telegram views.

If any section is unclear or missing, please tell me which part of the bot you'd like clarified next.