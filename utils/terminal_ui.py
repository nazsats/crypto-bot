"""
utils/terminal_ui.py — Live rich terminal dashboard.

Replaces plain log lines with a beautiful live-updating terminal UI.
Shows: bot status, open positions, latest signals, trending coins,
       recent activity feed, and cycle stats — all updating in real time.

Install: pip install rich
Run:     The dashboard starts automatically when main.py runs.
         Or run standalone: python utils/terminal_ui.py
"""

from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime
from typing import Optional

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

console = Console()

# Activity feed — stores last 20 events
_activity: deque[tuple[str, str, str]] = deque(maxlen=20)  # (time, type, message)
_activity_lock = threading.Lock()


def log_activity(event_type: str, message: str):
    """Add an event to the activity feed. Called from main.py."""
    ts = datetime.now().strftime("%H:%M:%S")
    with _activity_lock:
        _activity.appendleft((ts, event_type, message))


# ─────────────────────────────────────────────────────────────────────────────
# PANEL BUILDERS
# ─────────────────────────────────────────────────────────────────────────────

def _build_header(state) -> Panel:
    mode = (
        "[yellow]📋 PAPER[/]" if state.paper_trading
        else "[red]🔴 LIVE[/]" if not state.dry_run
        else "[blue]🔍 DRY-RUN[/]"
    )
    status = "[red]⏸ PAUSED[/]" if state.paused else "[green]▶ RUNNING[/]"

    text = Text()
    text.append("  🤖 Crypto Narrative Bot  ", style="bold white on dark_blue")
    text.append(f"   {status}   ")
    text.append(f"Mode: {mode}   ")
    text.append(f"Uptime: [cyan]{state.uptime_str()}[/]   ")
    text.append(f"Cycle: [cyan]#{state.cycle_count}[/]   ")
    text.append(f"Last: [cyan]{state.last_cycle_ago()}[/]")

    return Panel(text, style="bold", padding=(0, 1))


def _build_signals(state) -> Panel:
    table = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan", expand=True)
    table.add_column("Coin",   style="bold white", width=8)
    table.add_column("Signal", width=8)
    table.add_column("Sentiment", justify="center", width=12)

    signals = state.latest_signals
    if not signals:
        table.add_row("[dim]—[/]", "[dim]waiting...[/]", "[dim]—[/]")
    else:
        for coin, signal in signals.items():
            if signal == "BUY":
                sig_str  = "[bold green]🟢 BUY[/]"
                bar_color = "green"
            elif signal == "SELL":
                sig_str  = "[bold red]🔴 SELL[/]"
                bar_color = "red"
            else:
                sig_str  = "[dim]⚪ HOLD[/]"
                bar_color = "yellow"
            table.add_row(f"[bold]{coin}[/]", sig_str, f"[{bar_color}]●●●●●[/]")

    return Panel(table, title="[bold cyan]📊 Sentiment Signals[/]", border_style="cyan")


def _build_positions(state) -> Panel:
    table = Table(box=box.SIMPLE, show_header=True, header_style="bold yellow", expand=True)
    table.add_column("Token",  style="bold white", width=8)
    table.add_column("Entry",  justify="right",    width=14)
    table.add_column("Now",    justify="right",    width=14)
    table.add_column("PnL",    justify="right",    width=9)
    table.add_column("Spent",  justify="right",    width=8)

    pumpfun = state.pumpfun

    # Paper positions
    if state.paper_trading and state.paper_ledger:
        positions = state.paper_ledger.positions
        get_token  = pumpfun.get_token_by_mint if pumpfun else lambda m: None
        if not positions:
            table.add_row("[dim]—[/]", "[dim]No paper positions[/]", "", "", "")
        else:
            for mint, pos in positions.items():
                token   = get_token(mint)
                current = token.price_sol if token else pos.entry_price_sol
                pnl_pct = (current - pos.entry_price_sol) / pos.entry_price_sol * 100
                pnl_col = f"[green]+{pnl_pct:.1f}%[/]" if pnl_pct >= 0 else f"[red]{pnl_pct:.1f}%[/]"
                table.add_row(
                    pos.symbol,
                    f"{pos.entry_price_sol:.8f}",
                    f"{current:.8f}",
                    pnl_col,
                    f"{pos.sol_spent:.3f}",
                )
    elif pumpfun and pumpfun.positions:
        for mint, pos in pumpfun.positions.items():
            token   = pumpfun.get_token_by_mint(mint)
            current = token.price_sol if token else pos.entry_price_sol
            pnl_pct = (current - pos.entry_price_sol) / pos.entry_price_sol * 100
            pnl_col = f"[green]+{pnl_pct:.1f}%[/]" if pnl_pct >= 0 else f"[red]{pnl_pct:.1f}%[/]"
            table.add_row(
                pos.symbol,
                f"{pos.entry_price_sol:.8f}",
                f"{current:.8f}",
                pnl_col,
                f"{pos.sol_spent:.3f}",
            )
    else:
        table.add_row("[dim]—[/]", "[dim]No open positions[/]", "", "", "")

    title = "📋 Paper Positions" if state.paper_trading else "💼 Live Positions"
    return Panel(table, title=f"[bold yellow]{title}[/]", border_style="yellow")


def _build_trending(state) -> Panel:
    trending = getattr(state, "latest_trending", [])
    if trending:
        items = "  ".join(f"[cyan]{c}[/]" for c in trending[:8])
    else:
        items = "[dim]Waiting for first cycle...[/]"

    return Panel(
        Text.from_markup(f"  {items}"),
        title="[bold magenta]📈 Trending Coins[/]",
        border_style="magenta",
    )


def _build_activity() -> Panel:
    table = Table(box=box.SIMPLE, show_header=False, expand=True, padding=(0, 1))
    table.add_column("Time",  style="dim",         width=10)
    table.add_column("Type",  width=12)
    table.add_column("Event", style="white")

    type_styles = {
        "BUY":     "[bold green]🟢 BUY[/]",
        "SELL":    "[bold red]🔴 SELL[/]",
        "HYPE":    "[bold yellow]🔥 HYPE[/]",
        "WHALE":   "[bold blue]🐋 WHALE[/]",
        "LAUNCH":  "[bold cyan]🚀 LAUNCH[/]",
        "SIGNAL":  "[bold magenta]📡 SIGNAL[/]",
        "ERROR":   "[bold red]⚠️  ERROR[/]",
        "INFO":    "[dim]ℹ  INFO[/]",
        "CYCLE":   "[cyan]🔄 CYCLE[/]",
        "PAPER":   "[yellow]📋 PAPER[/]",
    }

    with _activity_lock:
        events = list(_activity)

    if not events:
        table.add_row("", "[dim]—[/]", "[dim]No activity yet — waiting for first cycle[/]")
    else:
        for ts, etype, msg in events[:15]:
            styled = type_styles.get(etype, f"[dim]{etype}[/]")
            table.add_row(ts, styled, msg[:70])

    return Panel(table, title="[bold white]📜 Activity Feed[/]", border_style="white")


def _build_stats(state) -> Panel:
    # Paper stats
    if state.paper_trading and state.paper_ledger:
        ledger = state.paper_ledger
        pumpfun = state.pumpfun
        summary = ledger.portfolio_summary(
            pumpfun.get_token_by_mint if pumpfun else lambda m: None
        )
        pnl_col = "[green]" if summary["total_pnl"] >= 0 else "[red]"
        text = (
            f"[bold]Virtual Balance:[/]  {summary['virtual_sol']:.4f} SOL\n"
            f"[bold]Realised PnL:[/]    {pnl_col}{summary['realised_pnl']:+.4f} SOL[/]\n"
            f"[bold]Unrealised PnL:[/]  {pnl_col}{summary['unrealised_pnl']:+.4f} SOL[/]\n"
            f"[bold]Total Trades:[/]    {summary['total_trades']}\n"
            f"[bold]Win Rate:[/]        {summary['win_rate']:.0f}%\n"
            f"[bold]Posts/cycle:[/]     {state.posts_last_cycle}"
        )
    else:
        text = (
            f"[bold]Cycles run:[/]  {state.cycle_count}\n"
            f"[bold]Posts/cycle:[/] {state.posts_last_cycle}\n"
            f"[bold]Last cycle:[/]  {state.last_cycle_ago()}\n"
            f"[bold]Uptime:[/]      {state.uptime_str()}"
        )

    return Panel(text, title="[bold green]📊 Stats[/]", border_style="green")


# ─────────────────────────────────────────────────────────────────────────────
# LAYOUT BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def _build_layout(state) -> Layout:
    layout = Layout()

    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
        Layout(name="footer", size=3),
    )

    layout["body"].split_row(
        Layout(name="left",  ratio=2),
        Layout(name="right", ratio=3),
    )

    layout["left"].split_column(
        Layout(name="signals",  ratio=2),
        Layout(name="stats",    ratio=1),
    )

    layout["right"].split_column(
        Layout(name="positions", ratio=2),
        Layout(name="trending",  size=3),
        Layout(name="activity",  ratio=3),
    )

    layout["header"].update(_build_header(state))
    layout["signals"].update(_build_signals(state))
    layout["stats"].update(_build_stats(state))
    layout["positions"].update(_build_positions(state))
    layout["trending"].update(_build_trending(state))
    layout["activity"].update(_build_activity())
    layout["footer"].update(Panel(
        "[dim]  Controls: Ctrl+C to stop  |  Send /menu to your Telegram bot to control from phone[/]",
        style="dim"
    ))

    return layout


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC: start the live dashboard in a background thread
# ─────────────────────────────────────────────────────────────────────────────

def start_dashboard(state, refresh_rate: float = 2.0):
    """Start the live terminal dashboard in a background thread."""

    def _run():
        try:
            with Live(
                _build_layout(state),
                console=console,
                refresh_per_second=1 / refresh_rate,
                screen=True,
            ) as live:
                while True:
                    live.update(_build_layout(state))
                    time.sleep(refresh_rate)
        except Exception as e:
            # Dashboard crash shouldn't kill the bot
            console.print(f"[red]Dashboard error: {e}[/]")

    thread = threading.Thread(target=_run, daemon=True, name="TerminalUI")
    thread.start()
    return thread
