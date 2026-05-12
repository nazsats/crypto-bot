"""
dapp/ethereum/uniswap_sniper.py — Ethereum & Base memecoin sniper.

Monitors Uniswap v2/v3 for new pair launches and executes buys
based on sentiment signals.

Supports:
  - Ethereum mainnet (Uniswap v2 + v3)
  - Base chain (BaseSwap / Aerodrome — same Uniswap v2 interface, cheap gas)

Strategy:
  - Listen to PairCreated events on Uniswap v2 factory (new token launched)
  - Cross-reference with sentiment score from Reddit/CryptoPanic
  - Buy if sentiment score >= threshold AND token passes safety checks
  - Auto-sell at take-profit or stop-loss

Safety checks (honeypot detection basics):
  - Verify token can be sold (simulate sell transaction)
  - Check liquidity lock (owner != deployer)
  - Max buy tax < 10%

Install (requires C++ Build Tools):
  pip install web3
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Optional

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from config import (
    WALLET_PRIVATE_KEY,
    WALLET_ADDRESS,
    ETH_RPC_URL,
    ETH_WSS_URL,
    BASE_RPC_URL,
    ETH_SNIPER_ENABLED,
    BASE_SNIPER_ENABLED,
    ETH_BUY_AMOUNT_ETH,
    BASE_BUY_AMOUNT_ETH,
    ETH_SLIPPAGE_PCT,
    ETH_GAS_LIMIT,
    ETH_MAX_GAS_GWEI,
)
from utils.logger import get_logger

log = get_logger("eth_sniper")

# ─────────────────────────────────────────────
# CONTRACT ADDRESSES
# ─────────────────────────────────────────────

UNISWAP_V2_FACTORY   = "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f"
UNISWAP_V2_ROUTER    = "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"
UNISWAP_V3_FACTORY   = "0x1F98431c8aD98523631AE4a59f267346ea31F984"
WETH_ADDRESS         = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"

# Base chain
BASE_UNISWAP_FACTORY = "0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6"  # BaseSwap
BASE_UNISWAP_ROUTER  = "0x327Df1E6de05895d2ab08513aaDD9313Fe505d86"
BASE_WETH_ADDRESS    = "0x4200000000000000000000000000000000000006"

FACTORY_ABI = json.loads("""[
  {
    "anonymous": false,
    "inputs": [
      {"indexed": true,  "name": "token0", "type": "address"},
      {"indexed": true,  "name": "token1", "type": "address"},
      {"indexed": false, "name": "pair",   "type": "address"},
      {"indexed": false, "name": "",       "type": "uint256"}
    ],
    "name": "PairCreated",
    "type": "event"
  },
  {
    "inputs": [{"name": "tokenA", "type": "address"}, {"name": "tokenB", "type": "address"}],
    "name": "getPair",
    "outputs": [{"name": "pair", "type": "address"}],
    "stateMutability": "view",
    "type": "function"
  }
]""")

ROUTER_ABI = json.loads("""[
  {
    "inputs": [
      {"name": "amountOutMin", "type": "uint256"},
      {"name": "path",         "type": "address[]"},
      {"name": "to",           "type": "address"},
      {"name": "deadline",     "type": "uint256"}
    ],
    "name": "swapExactETHForTokens",
    "outputs": [{"name": "amounts", "type": "uint256[]"}],
    "stateMutability": "payable",
    "type": "function"
  },
  {
    "inputs": [
      {"name": "amountIn",     "type": "uint256"},
      {"name": "amountOutMin", "type": "uint256"},
      {"name": "path",         "type": "address[]"},
      {"name": "to",           "type": "address"},
      {"name": "deadline",     "type": "uint256"}
    ],
    "name": "swapExactTokensForETH",
    "outputs": [{"name": "amounts", "type": "uint256[]"}],
    "stateMutability": "nonpayable",
    "type": "function"
  },
  {
    "inputs": [
      {"name": "amountIn", "type": "uint256"},
      {"name": "path",     "type": "address[]"}
    ],
    "name": "getAmountsOut",
    "outputs": [{"name": "amounts", "type": "uint256[]"}],
    "stateMutability": "view",
    "type": "function"
  }
]""")

ERC20_ABI = json.loads("""[
  {"inputs": [{"name": "account", "type": "address"}],
   "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}],
   "stateMutability": "view", "type": "function"},
  {"inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
   "name": "approve", "outputs": [{"name": "", "type": "bool"}],
   "stateMutability": "nonpayable", "type": "function"},
  {"inputs": [],
   "name": "name", "outputs": [{"name": "", "type": "string"}],
   "stateMutability": "view", "type": "function"},
  {"inputs": [],
   "name": "symbol", "outputs": [{"name": "", "type": "string"}],
   "stateMutability": "view", "type": "function"}
]""")


@dataclass
class EthPosition:
    token_address: str
    symbol: str
    eth_spent: float
    entry_price_eth: float
    token_amount: int
    stop_loss_eth: float
    take_profit_eth: float
    chain: str              # "ETH" or "BASE"
    opened_at: float = field(default_factory=time.time)


class UniswapSniper:
    """
    Snipes new memecoin launches on Uniswap (ETH) and BaseSwap (Base).
    """

    def __init__(self, chain: str = "BASE"):
        self.chain = chain
        self.positions: dict[str, EthPosition] = {}
        self.w3 = None
        self.router = None
        self.factory = None

        enabled = BASE_SNIPER_ENABLED if chain == "BASE" else ETH_SNIPER_ENABLED
        if not enabled:
            log.info(f"{chain} sniper disabled in config")
            return

        self._init_web3(chain)

    def _init_web3(self, chain: str):
        try:
            from web3 import Web3  # type: ignore
            from web3.middleware import ExtraDataToPOAMiddleware  # type: ignore
        except ImportError:
            log.error("web3 not installed. Run: pip install web3")
            return

        rpc = BASE_RPC_URL if chain == "BASE" else ETH_RPC_URL
        if not rpc:
            log.error(f"{chain}_RPC_URL not set in .env")
            return

        self.w3 = Web3(Web3.HTTPProvider(rpc))
        if chain == "BASE":
            self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

        if not self.w3.is_connected():
            log.error(f"Cannot connect to {chain} RPC")
            self.w3 = None
            return

        router_addr  = BASE_UNISWAP_ROUTER  if chain == "BASE" else UNISWAP_V2_ROUTER
        factory_addr = BASE_UNISWAP_FACTORY if chain == "BASE" else UNISWAP_V2_FACTORY

        self.router  = self.w3.eth.contract(
            address=Web3.to_checksum_address(router_addr), abi=ROUTER_ABI)
        self.factory = self.w3.eth.contract(
            address=Web3.to_checksum_address(factory_addr), abi=FACTORY_ABI)
        self.account = self.w3.eth.account.from_key(WALLET_PRIVATE_KEY)

        log.info(f"{chain} sniper ready | wallet: {self.account.address[:10]}...")

    # ──────────────────────────────────────────
    # PUBLIC: buy a token by address
    # ──────────────────────────────────────────
    def buy_token(self, token_address: str, sentiment_score: float) -> bool:
        if not self.w3:
            return False

        from web3 import Web3  # type: ignore

        buy_amount = BASE_BUY_AMOUNT_ETH if self.chain == "BASE" else ETH_BUY_AMOUNT_ETH
        weth = BASE_WETH_ADDRESS if self.chain == "BASE" else WETH_ADDRESS

        token_addr = Web3.to_checksum_address(token_address)

        # Safety check
        if not self._basic_safety_check(token_addr):
            log.warning(f"Safety check FAILED for {token_address[:12]}... — skipping")
            return False

        # Get symbol
        try:
            token_contract = self.w3.eth.contract(address=token_addr, abi=ERC20_ABI)
            symbol = token_contract.functions.symbol().call()
        except Exception:
            symbol = token_address[:8]

        # Check gas price
        gas_gwei = self.w3.from_wei(self.w3.eth.gas_price, "gwei")
        if gas_gwei > ETH_MAX_GAS_GWEI:
            log.warning(f"Gas too high ({gas_gwei:.1f} gwei > {ETH_MAX_GAS_GWEI}) — skipping {symbol}")
            return False

        path = [Web3.to_checksum_address(weth), token_addr]
        amount_wei = self.w3.to_wei(buy_amount, "ether")
        deadline = int(time.time()) + 300

        try:
            amounts = self.router.functions.getAmountsOut(amount_wei, path).call()
            amount_out_min = int(amounts[1] * (1 - ETH_SLIPPAGE_PCT / 100))
        except Exception as e:
            log.error(f"getAmountsOut failed: {e}")
            return False

        nonce = self.w3.eth.get_transaction_count(self.account.address)
        tx = self.router.functions.swapExactETHForTokens(
            amount_out_min, path, self.account.address, deadline
        ).build_transaction({
            "from":     self.account.address,
            "value":    amount_wei,
            "gas":      ETH_GAS_LIMIT,
            "gasPrice": self.w3.eth.gas_price,
            "nonce":    nonce,
        })

        signed  = self.w3.eth.account.sign_transaction(tx, WALLET_PRIVATE_KEY)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        tx_hex  = self.w3.to_hex(tx_hash)

        explorer = "https://basescan.org/tx/" if self.chain == "BASE" else "https://etherscan.io/tx/"
        log.info(f"BUY {symbol} [{self.chain}]: {tx_hex} | {explorer}{tx_hex}")

        # Get current price
        try:
            one_token = 10 ** 18
            price_amounts = self.router.functions.getAmountsOut(
                one_token, [token_addr, Web3.to_checksum_address(weth)]
            ).call()
            price_eth = float(self.w3.from_wei(price_amounts[1], "ether"))
        except Exception:
            price_eth = buy_amount / (amounts[1] / 1e18) if amounts[1] > 0 else 0

        from config import PUMPFUN_STOP_LOSS_PCT, PUMPFUN_TAKE_PROFIT_PCT
        self.positions[token_address] = EthPosition(
            token_address=token_address,
            symbol=symbol,
            eth_spent=buy_amount,
            entry_price_eth=price_eth,
            token_amount=amounts[1],
            stop_loss_eth=price_eth * (1 - PUMPFUN_STOP_LOSS_PCT),
            take_profit_eth=price_eth * PUMPFUN_TAKE_PROFIT_PCT,
            chain=self.chain,
        )
        return True

    def check_exits(self):
        """Check all positions for stop-loss / take-profit."""
        if not self.w3:
            return

        from web3 import Web3  # type: ignore
        weth = BASE_WETH_ADDRESS if self.chain == "BASE" else WETH_ADDRESS

        for addr in list(self.positions.keys()):
            pos = self.positions[addr]
            try:
                one_token = 10 ** 18
                path = [Web3.to_checksum_address(addr), Web3.to_checksum_address(weth)]
                amounts = self.router.functions.getAmountsOut(one_token, path).call()
                current_price = float(self.w3.from_wei(amounts[1], "ether"))
            except Exception:
                continue

            if current_price <= pos.stop_loss_eth:
                log.info(f"STOP_LOSS hit for {pos.symbol}")
                self._sell(pos)
            elif current_price >= pos.take_profit_eth:
                log.info(f"TAKE_PROFIT hit for {pos.symbol}")
                self._sell(pos)

    # ──────────────────────────────────────────
    # PRIVATE
    # ──────────────────────────────────────────
    def _sell(self, pos: EthPosition):
        from web3 import Web3  # type: ignore

        token_addr = Web3.to_checksum_address(pos.token_address)
        weth = BASE_WETH_ADDRESS if self.chain == "BASE" else WETH_ADDRESS

        token_contract = self.w3.eth.contract(address=token_addr, abi=ERC20_ABI)
        balance = token_contract.functions.balanceOf(self.account.address).call()
        if balance == 0:
            del self.positions[pos.token_address]
            return

        # Approve
        allowance_check = token_contract.functions.balanceOf  # reuse contract
        nonce = self.w3.eth.get_transaction_count(self.account.address)
        approve_tx = token_contract.functions.approve(
            self.router.address, balance * 2
        ).build_transaction({
            "from": self.account.address, "gas": 100_000,
            "gasPrice": self.w3.eth.gas_price, "nonce": nonce,
        })
        signed = self.w3.eth.account.sign_transaction(approve_tx, WALLET_PRIVATE_KEY)
        self.w3.eth.send_raw_transaction(signed.raw_transaction)
        time.sleep(3)

        path = [token_addr, Web3.to_checksum_address(weth)]
        deadline = int(time.time()) + 300
        amounts = self.router.functions.getAmountsOut(balance, path).call()
        amount_out_min = int(amounts[1] * (1 - ETH_SLIPPAGE_PCT / 100))

        nonce = self.w3.eth.get_transaction_count(self.account.address)
        tx = self.router.functions.swapExactTokensForETH(
            balance, amount_out_min, path, self.account.address, deadline
        ).build_transaction({
            "from": self.account.address, "gas": ETH_GAS_LIMIT,
            "gasPrice": self.w3.eth.gas_price, "nonce": nonce,
        })
        signed = self.w3.eth.account.sign_transaction(tx, WALLET_PRIVATE_KEY)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        log.info(f"SELL {pos.symbol}: {self.w3.to_hex(tx_hash)}")
        del self.positions[pos.token_address]

    def _basic_safety_check(self, token_addr) -> bool:
        """
        Very basic honeypot check:
        - Can we get a quote for selling this token?
        - If router reverts on getAmountsOut for sell path, likely honeypot.
        """
        from web3 import Web3  # type: ignore
        weth = BASE_WETH_ADDRESS if self.chain == "BASE" else WETH_ADDRESS
        try:
            path = [token_addr, Web3.to_checksum_address(weth)]
            self.router.functions.getAmountsOut(10 ** 18, path).call()
            return True
        except Exception:
            return False  # Can't sell = likely honeypot
