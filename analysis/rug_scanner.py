"""
analysis/rug_scanner.py — Rug pull + honeypot detection before buying.

Checks every token BEFORE the bot spends real money.

Solana (Pump.fun tokens):
  → rugcheck.xyz API (free, no key needed)
  → Checks: mint authority, freeze authority, dev wallet %, liquidity

Ethereum / Base (Uniswap tokens):
  → honeypot.is API (free, no key needed)
  → Checks: buy/sell tax, can tokens be sold, liquidity

A token that FAILS these checks is silently skipped.
"""

from __future__ import annotations

import requests
from dataclasses import dataclass
from typing import Optional

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from utils.logger import get_logger

log = get_logger("rug_scanner")


@dataclass
class RugReport:
    token: str              # symbol or address
    is_safe: bool           # True = OK to buy
    risk_level: str         # "low" | "medium" | "high" | "rugged"
    warnings: list[str]     # list of specific issues found
    score: int              # rugcheck risk score (lower = safer)

    def summary(self) -> str:
        emoji = "✅" if self.is_safe else "🚨"
        warns = ", ".join(self.warnings) if self.warnings else "none"
        return f"{emoji} [{self.risk_level.upper()}] {self.token} — warnings: {warns}"


# ─────────────────────────────────────────────────────────────────────────────
# SOLANA RUG CHECKER (rugcheck.xyz)
# ─────────────────────────────────────────────────────────────────────────────

class SolanaRugChecker:
    """
    Uses rugcheck.xyz free API to assess Pump.fun tokens.
    Endpoint: https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary
    """

    SAFE_LEVELS    = {"good"}
    CAUTION_LEVELS = {"warning"}
    DANGER_LEVELS  = {"danger", "rugged"}

    BLOCKED_RISKS = {
        "Freeze Authority still enabled",
        "Mint Authority still enabled",
        "Copycat token",
        "High dev wallet concentration",
    }

    def check(self, mint: str) -> RugReport:
        url = f"https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary"
        try:
            resp = requests.get(url, timeout=10)
            if resp.status_code == 404:
                # Token too new — not indexed yet, allow with caution
                return RugReport(
                    token=mint[:12], is_safe=True,
                    risk_level="unknown",
                    warnings=["Token not yet indexed by rugcheck — proceed with caution"],
                    score=50,
                )
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.Timeout:
            log.warning(f"Rugcheck timeout for {mint[:12]}")
            return RugReport(token=mint[:12], is_safe=True, risk_level="unknown",
                             warnings=["Rugcheck timeout — unverified"], score=50)
        except Exception as e:
            log.warning(f"Rugcheck API error for {mint[:12]}: {e}")
            return RugReport(token=mint[:12], is_safe=True, risk_level="unknown",
                             warnings=[f"Rugcheck unavailable: {e}"], score=50)

        score      = data.get("score", 0)          # lower = riskier
        risks      = data.get("risks", [])          # list of risk dicts
        risk_level = data.get("score_normalised", "unknown").lower()

        warnings = []
        blocked  = False

        for risk in risks:
            name  = risk.get("name", "")
            level = risk.get("level", "")
            if name in self.BLOCKED_RISKS or level in ("danger", "critical"):
                warnings.append(name)
                blocked = True
            elif level == "warn":
                warnings.append(f"[warn] {name}")

        # Hard rules regardless of rugcheck rating
        if score < 20:
            blocked = True
            warnings.append(f"Score critically low: {score}/100")

        is_safe = not blocked and risk_level not in self.DANGER_LEVELS

        report = RugReport(
            token=mint[:12],
            is_safe=is_safe,
            risk_level=risk_level,
            warnings=warnings,
            score=score,
        )
        log.info(f"Rugcheck {mint[:12]}: {report.summary()}")
        return report


# ─────────────────────────────────────────────────────────────────────────────
# ETHEREUM / BASE HONEYPOT CHECKER (honeypot.is)
# ─────────────────────────────────────────────────────────────────────────────

CHAIN_IDS = {"ETH": 1, "BASE": 8453, "BSC": 56}


class EthHoneypotChecker:
    """
    Uses honeypot.is free API to check EVM tokens before buying.
    Endpoint: https://api.honeypot.is/v2/IsHoneypot?address={addr}&chainID={id}
    """

    MAX_BUY_TAX  = 10.0   # reject if buy tax > 10%
    MAX_SELL_TAX = 10.0   # reject if sell tax > 10%
    MIN_LIQUIDITY = 5_000  # reject if liquidity < $5000

    def check(self, address: str, chain: str = "ETH") -> RugReport:
        chain_id = CHAIN_IDS.get(chain, 1)
        url = f"https://api.honeypot.is/v2/IsHoneypot?address={address}&chainID={chain_id}"
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.warning(f"Honeypot.is error for {address[:12]}: {e}")
            return RugReport(token=address[:12], is_safe=True, risk_level="unknown",
                             warnings=[f"Honeypot check unavailable: {e}"], score=50)

        warnings = []
        blocked  = False

        is_honeypot = data.get("IsHoneypot", False)
        if is_honeypot:
            warnings.append("HONEYPOT DETECTED — cannot sell after buying")
            blocked = True

        # Tax checks
        buy_tax  = data.get("BuyTax",  0) or 0
        sell_tax = data.get("SellTax", 0) or 0
        if buy_tax > self.MAX_BUY_TAX:
            warnings.append(f"Buy tax too high: {buy_tax:.1f}%")
            blocked = True
        if sell_tax > self.MAX_SELL_TAX:
            warnings.append(f"Sell tax too high: {sell_tax:.1f}%")
            blocked = True

        # Liquidity check
        # liquidity may be missing, None, a dict, or even a list — guard each step.
        liq = data.get("liquidity")
        if isinstance(liq, dict):
            liquidity = liq.get("usd", 0) or 0
        else:
            liquidity = 0
        try:
            liquidity = float(liquidity)
        except (TypeError, ValueError):
            liquidity = 0.0
        if liquidity < self.MIN_LIQUIDITY:
            warnings.append(f"Low liquidity: ${liquidity:,.0f}")
            blocked = True

        # Ownership
        if data.get("ownerAddress") == address:
            warnings.append("Contract owns itself — suspicious")

        risk_level = "high" if blocked else ("medium" if warnings else "low")
        score = 0 if is_honeypot else (100 - int(buy_tax + sell_tax) * 5)

        report = RugReport(
            token=address[:12],
            is_safe=not blocked,
            risk_level=risk_level,
            warnings=warnings,
            score=max(0, score),
        )
        log.info(f"Honeypot [{chain}] {address[:12]}: {report.summary()}")
        return report


# ─────────────────────────────────────────────────────────────────────────────
# UNIFIED INTERFACE — use this from the snipers
# ─────────────────────────────────────────────────────────────────────────────

_sol_checker = SolanaRugChecker()
_eth_checker = EthHoneypotChecker()


def check_solana(mint: str) -> RugReport:
    """Check a Pump.fun token before buying. Returns RugReport."""
    return _sol_checker.check(mint)


def check_evm(address: str, chain: str = "ETH") -> RugReport:
    """Check an EVM token (ETH/Base) before buying. Returns RugReport."""
    return _eth_checker.check(address, chain)


def is_safe_to_buy_solana(mint: str) -> bool:
    """Quick boolean — True means buy is allowed."""
    report = check_solana(mint)
    if not report.is_safe:
        log.warning(f"RUG BLOCKED [{mint[:12]}]: {', '.join(report.warnings)}")
    return report.is_safe


def is_safe_to_buy_evm(address: str, chain: str = "ETH") -> bool:
    """Quick boolean — True means buy is allowed."""
    report = check_evm(address, chain)
    if not report.is_safe:
        log.warning(f"HONEYPOT BLOCKED [{address[:12]}]: {', '.join(report.warnings)}")
    return report.is_safe
