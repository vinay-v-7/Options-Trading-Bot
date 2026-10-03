"""
Utilities for maintaining reviews/trade-journal.md entries and running stats.
"""

from datetime import datetime
from pathlib import Path
import re


JOURNAL_PATH = Path("reviews/trade-journal.md")
INDEX_UNDERLYINGS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"}
CREDIT_STRATEGIES = {"bull_put_spread", "bear_call_spread", "iron_condor"}


def _extract_net_pnls(text: str):
    # Matches patterns like ₹1,800 or ₹-1,200 or ₹1124.50
    pattern = re.compile(r"- \*\*Net P&L\*\*: ₹(-?[\d,]+(?:\.\d+)?)")
    matches = pattern.findall(text)
    return [float(x.replace(",", "")) for x in matches]


def _extract_trade_blocks(text: str) -> list[dict]:
    """Parse strategy / underlying / net P&L from journal entries."""
    blocks = []
    parts = re.split(r"\n## Trade Entry:", text)
    for part in parts[1:]:
        strategy_m = re.search(r"- \*\*Strategy\*\*: (.+)", part)
        underlying_m = re.search(r"- \*\*Underlying\*\*: (.+)", part)
        pnl_m = re.search(r"- \*\*Net P&L\*\*: ₹(-?[\d,]+(?:\.\d+)?)", part)
        if not pnl_m:
            continue
        blocks.append({
            "strategy": (strategy_m.group(1).strip() if strategy_m else ""),
            "underlying": (underlying_m.group(1).strip().upper() if underlying_m else ""),
            "net_pnl": float(pnl_m.group(1).replace(",", "")),
        })
    return blocks


def compute_running_stats(journal_text: str) -> dict:
    pnls = _extract_net_pnls(journal_text)
    if not pnls:
        return {
            "total": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "profit_factor": 0.0,
            "total_net": 0.0,
        }

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    total_wins = sum(wins)
    total_losses_abs = abs(sum(losses))

    return {
        "total": len(pnls),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round((len(wins) / len(pnls)) * 100, 2),
        "avg_win": round(total_wins / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "profit_factor": round((total_wins / total_losses_abs), 2) if total_losses_abs > 0 else 999.0,
        "total_net": round(sum(pnls), 2),
    }


def compound_phase_rule_breaches(journal_text: str, max_loss_abs: float = 800.0,
                                 credit_min_capital: float = 100000.0,
                                 capital: float = 30000.0) -> list[str]:
    """Flag journal patterns that violate compound-phase rules."""
    flags = []
    trades = _extract_trade_blocks(journal_text)
    for t in trades:
        if t["net_pnl"] < 0 and abs(t["net_pnl"]) > max_loss_abs:
            flags.append(
                f"Loss ₹{abs(t['net_pnl']):.0f} on {t['underlying']} exceeded MAX_LOSS_ABS ₹{max_loss_abs:.0f}"
            )
        strat = t["strategy"].lower().replace(" ", "_")
        if capital < credit_min_capital and any(c in strat for c in ("bull_put", "bear_call", "iron_condor", "credit")):
            flags.append(
                f"Credit/short-gamma structure logged under ₹{credit_min_capital:,.0f}: {t['strategy']} on {t['underlying']}"
            )
        if capital < credit_min_capital and t["underlying"] in INDEX_UNDERLYINGS and "spread" in strat:
            # Soft flag — index spreads often breach ₹800 at 1 lot
            flags.append(
                f"Index spread at compound-phase capital: {t['strategy']} on {t['underlying']}"
            )
    # Deduplicate while preserving order
    seen = set()
    unique = []
    for f in flags:
        if f not in seen:
            seen.add(f)
            unique.append(f)
    return unique


def append_trade_entry(
    strategy: str,
    underlying: str,
    exit_reason: str,
    net_pnl: float,
    what_worked: str,
    what_didnt: str,
    lesson: str,
    exit_date: str = None,
):
    """
    Append one journal entry in existing markdown style.
    """
    if exit_date is None:
        exit_date = datetime.now().strftime("%Y-%m-%d")

    if JOURNAL_PATH.exists():
        text = JOURNAL_PATH.read_text(encoding="utf-8")
    else:
        text = "# 📔 Options Trading Journal\n\n**Running Statistics**\n"

    entry = (
        f"\n---\n\n"
        f"## Trade Entry: {exit_date}\n"
        f"- **Strategy**: {strategy}\n"
        f"- **Underlying**: {underlying}\n"
        f"- **Exit Date**: {exit_date}\n"
        f"- **Exit Reason**: {exit_reason}\n"
        f"- **Net P&L**: ₹{net_pnl:.2f}\n"
        f"- **What worked**: {what_worked}\n"
        f"- **What didn't**: {what_didnt}\n"
        f"- **Lesson**: {lesson}\n"
    )

    updated = text + entry
    stats = compute_running_stats(updated)

    header = (
        "# 📔 Options Trading Journal\n\n"
        "**Running Statistics**\n"
        f"- **Win Rate**: {stats['win_rate']}%\n"
        f"- **Avg Win**: ₹{stats['avg_win']}\n"
        f"- **Avg Loss**: ₹{stats['avg_loss']}\n"
        f"- **Profit Factor**: {stats['profit_factor']}\n"
        f"- **Total Net P&L**: ₹{stats['total_net']}\n"
    )

    # Replace top section if it exists; otherwise prepend
    body_start = updated.find("\n---\n")
    body = updated[body_start:] if body_start != -1 else "\n"
    final_text = header + body

    JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    JOURNAL_PATH.write_text(final_text, encoding="utf-8")
