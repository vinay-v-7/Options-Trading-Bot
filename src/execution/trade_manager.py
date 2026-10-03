"""
Trade management helpers for post-trade journaling and periodic strategy review notes.
"""

from datetime import datetime
from pathlib import Path
from src.execution.journal import (
    JOURNAL_PATH,
    append_trade_entry,
    compute_running_stats,
    compound_phase_rule_breaches,
)
from src.risk.risk_controls import CircuitBreaker
from src.config import settings


def _generate_strategy_review_if_needed(journal_path: Path, equity: float) -> Path | None:
    """
    Generate a lightweight strategy review note after every 5 journaled trades.
    Returns generated path, or None when not generated.
    """
    text = journal_path.read_text(encoding="utf-8") if journal_path.exists() else ""
    stats = compute_running_stats(text)
    total_trades = stats.get("total", 0)

    if total_trades == 0 or total_trades % 5 != 0:
        return None

    today = datetime.now().strftime("%Y-%m-%d")
    out_path = Path("reviews") / f"strategy-review-{today}.md"

    flags = []
    if stats.get("win_rate", 0) < 50:
        flags.append("Win rate below 50%")
    if stats.get("profit_factor", 0) < 1.5:
        flags.append("Profit factor below 1.5")

    rule_flags = compound_phase_rule_breaches(
        text,
        max_loss_abs=settings.MAX_LOSS_ABS,
        credit_min_capital=settings.CREDIT_MIN_CAPITAL,
        capital=settings.TOTAL_CAPITAL,
    )
    flags.extend(rule_flags)

    if not flags:
        flags.append("No critical metric breaches. Continue executing with discipline.")

    distance_1l = max(0.0, settings.CREDIT_MIN_CAPITAL - equity)
    distance_25l = max(0.0, 250000.0 - equity)

    content = (
        f"# Strategy Review Note — {today}\n\n"
        f"## Running Snapshot\n"
        f"- Total Trades: {stats.get('total', 0)}\n"
        f"- Win Rate: {stats.get('win_rate', 0)}%\n"
        f"- Profit Factor: {stats.get('profit_factor', 0)}\n"
        f"- Total Net P&L: ₹{stats.get('total_net', 0)}\n\n"
        f"## Compound Phase\n"
        f"- Equity: ₹{equity:,.0f}\n"
        f"- Hard floor: ₹{settings.CAPITAL_HARD_FLOOR:,.0f}\n"
        f"- Distance to credit unlock (₹1L): ₹{distance_1l:,.0f}\n"
        f"- Distance to ₹2.5L harvest target: ₹{distance_25l:,.0f}\n"
        f"- Max loss abs: ₹{settings.MAX_LOSS_ABS:,.0f}\n"
        f"- Min net profit: ₹{settings.MIN_NET_PROFIT:,.0f}\n\n"
        "## Flags\n"
        + "\n".join([f"- {x}" for x in flags])
        + "\n\n## Suggested Adjustments\n"
        "- Re-check liquidity gate (OI and spread %) before every entry.\n"
        "- Prefer stock debit spreads while capital < ₹1L.\n"
        "- Enforce max loss ≤ ₹800; do not average down.\n"
        "- Prefer entries only in the 11:00 AM – 1:00 PM IST window.\n"
        "- Multi-leg: buy hedge first; abort naked legs within 30s.\n"
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content, encoding="utf-8")
    return out_path


def record_trade_exit(
    strategy: str,
    underlying: str,
    exit_reason: str,
    net_pnl: float,
    what_worked: str,
    what_didnt: str,
    lesson: str,
    exit_date: str | None = None,
    total_capital: float = settings.TOTAL_CAPITAL,
) -> dict:
    """
    Append a post-trade journal entry and update CircuitBreaker state.
    """
    # 1. Journal Entry
    append_trade_entry(
        strategy=strategy,
        underlying=underlying,
        exit_reason=exit_reason,
        net_pnl=net_pnl,
        what_worked=what_worked,
        what_didnt=what_didnt,
        lesson=lesson,
        exit_date=exit_date,
    )

    # 2. Update Circuit Breaker (Fix P0-1) — also updates account_equity
    cb = CircuitBreaker(total_capital=total_capital)
    cb.record_trade_result(net_pnl=net_pnl)

    review_path = _generate_strategy_review_if_needed(JOURNAL_PATH, equity=cb.account_equity)
    return {
        "journal_path": str(JOURNAL_PATH),
        "review_generated": bool(review_path),
        "review_path": str(review_path) if review_path else None,
        "cb_status": cb.status_summary(),
        "account_equity": cb.account_equity,
    }
