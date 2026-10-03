"""
Risk control helpers for the options recommendation pipeline.

Additions (Phase 1 & 2):
- CircuitBreaker: daily/weekly/monthly/consecutive-loss tracking (Fix 20)
- EventBlackoutChecker: blocks short-gamma entries near binary events (Fix 22)
- validate_trade_risk / validate_portfolio_capacity: unchanged, enhanced docs
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_FILE = Path("data/circuit_breaker_state.json")
EVENT_CALENDAR_FILE = Path("resources/event-calendar.json")


@dataclass
class RiskConfig:
    total_capital: float = 30000.0
    max_concurrent_positions: int = 3
    max_capital_deployed_pct: float = 85.0  # Changed from 50.0; protected by CircuitBreaker


# ---------------------------------------------------------------------------
# Confidence-Tiered Risk Functions
# ---------------------------------------------------------------------------

def get_confidence_tier(score: float) -> dict:
    """
    Returns capital deployment fraction and stoploss cap fraction for the given score.

    Tiers:
      Tier 0 (< 8.0):  No trade.
      Tier 1 (8.0-8.4): Deploy 50%, stoploss cap 5% of deployed.
      Tier 2 (8.5-8.9): Deploy 70%, stoploss cap 6.5% of deployed.
      Tier 3 (>= 9.0):  Deploy 85%, stoploss cap 8% of deployed.
    """
    from src.config import settings as _s
    # User-requested rule: deploy 100% capital at score>=8, but keep the
    # existing stoploss-cap percentages by tier unchanged.
    if score >= 9.0:
        return {
            'deploy_pct': 1.0,
            'stoploss_cap_pct': _s.STOPLOSS_CAP_TIER_3,
            'tier': 3,
            'label': 'High Conviction',
        }
    elif score >= 8.5:
        return {
            'deploy_pct': 1.0,
            'stoploss_cap_pct': _s.STOPLOSS_CAP_TIER_2,
            'tier': 2,
            'label': 'Strong',
        }
    elif score >= 8.0:
        return {
            'deploy_pct': 1.0,
            'stoploss_cap_pct': _s.STOPLOSS_CAP_TIER_1,
            'tier': 1,
            'label': 'Qualified',
        }
    else:
        return {
            'deploy_pct': 0.0,
            'stoploss_cap_pct': 0.0,
            'tier': 0,
            'label': 'Rejected',
        }


def confidence_capital_limit(total_capital: float, score: float) -> float:
    """Max capital deployable for a single trade at this confidence score."""
    tier = get_confidence_tier(score)
    return total_capital * tier['deploy_pct']


def max_loss_for_trade(deployed_capital: float, score: float) -> float:
    """
    Max acceptable loss for a trade = min(stoploss_cap_pct × deployed, MAX_LOSS_ABS).
      Score 8.0-8.4: 5% of deployed.
      Score 8.5-8.9: 6.5% of deployed.
      Score >= 9.0:  8% of deployed.
    Always clamped by settings.MAX_LOSS_ABS (compound-phase hard ceiling).
    """
    from src.config import settings as _s
    tier = get_confidence_tier(score)
    tier_cap = deployed_capital * tier['stoploss_cap_pct']
    return min(tier_cap, _s.MAX_LOSS_ABS)


def check_hard_floor(equity: float, hard_floor: float = None) -> dict:
    """
    Returns whether equity is above the capital hard floor.

    When equity <= hard floor, all new trading must stop.
    """
    from src.config import settings as _s
    floor = _s.CAPITAL_HARD_FLOOR if hard_floor is None else hard_floor
    allowed = equity > floor
    return {
        'allowed': allowed,
        'equity': round(equity, 2),
        'hard_floor': round(floor, 2),
        'reason': None if allowed else (
            f"Hard floor breached: equity ₹{equity:,.0f} <= floor ₹{floor:,.0f}. "
            f"STOP TRADING until review / reset."
        ),
    }


# ---------------------------------------------------------------------------
# Per-trade and portfolio validators
# ---------------------------------------------------------------------------

def max_loss_limit(total_capital: float, score: float = 8.0,
                   deployed_capital: float = None) -> float:
    """Absolute max loss allowed per trade (tier cap clamped by MAX_LOSS_ABS)."""
    if deployed_capital is None:
        deployed_capital = confidence_capital_limit(total_capital, score)
    return max_loss_for_trade(deployed_capital, score)


def validate_trade_risk(gross_risk: float, total_capital: float,
                        score: float = 8.0,
                        deployed_capital: float = None,
                        # Legacy parameter kept for backward compatibility — ignored
                        max_risk_pct: float = None,
                        confidence_score: float = None) -> dict:
    """
    Validate if trade max loss is within the confidence-tiered risk limit
    AND the absolute MAX_LOSS_ABS ceiling.

    Args:
        gross_risk:       Actual max loss for the trade (e.g. premium × lot_size × 0.5).
        total_capital:    Total trading capital (e.g. ₹30,000).
        score:            4-Pillar weighted confidence score (8.0–10.0).
        deployed_capital: Capital being deployed in this trade. If None, derived from score tier.
    """
    from src.config import settings as _s

    # Support old call sites that passed confidence_score as a kwarg
    if confidence_score is not None and score == 8.0:
        score = confidence_score

    if deployed_capital is None:
        deployed_capital = confidence_capital_limit(total_capital, score)

    tier_uncapped = deployed_capital * get_confidence_tier(score)['stoploss_cap_pct']
    allowed = max_loss_for_trade(deployed_capital, score)
    passed = gross_risk <= allowed
    tier = get_confidence_tier(score)

    if passed:
        reason = None
    elif gross_risk > _s.MAX_LOSS_ABS and allowed <= _s.MAX_LOSS_ABS:
        reason = (
            f"Gross risk ₹{gross_risk:.2f} exceeds absolute max-loss ceiling "
            f"₹{_s.MAX_LOSS_ABS:.0f} (tier uncapped would allow ₹{tier_uncapped:.2f})"
        )
    else:
        reason = (
            f"Gross risk ₹{gross_risk:.2f} exceeds tier-{tier['tier']} "
            f"({tier['label']}) limit ₹{allowed:.2f} "
            f"({tier['stoploss_cap_pct'] * 100:.1f}% of ₹{deployed_capital:,.0f} deployed, "
            f"capped at ₹{_s.MAX_LOSS_ABS:.0f})"
        )

    return {
        'passed': passed,
        'gross_risk': round(gross_risk, 2),
        'allowed_risk': round(allowed, 2),
        'max_loss_abs': _s.MAX_LOSS_ABS,
        'tier': tier['label'],
        'tier_num': tier['tier'],
        'deploy_pct': f"{tier['deploy_pct'] * 100:.0f}%",
        'stoploss_cap_pct': f"{tier['stoploss_cap_pct'] * 100:.1f}%",
        'risk_pct_of_account': round((gross_risk / total_capital) * 100, 2) if total_capital > 0 else None,
        'reason': reason,
    }


def validate_portfolio_capacity(open_positions: int, deployed_capital: float, cfg: RiskConfig) -> dict:
    """Check portfolio-level constraints for opening new positions."""
    max_deployed = (cfg.total_capital * cfg.max_capital_deployed_pct) / 100.0
    concurrent_ok = open_positions < cfg.max_concurrent_positions
    deployed_ok = deployed_capital <= max_deployed
    passed = concurrent_ok and deployed_ok
    reasons = []
    if not concurrent_ok:
        reasons.append(f"Concurrent position limit reached ({open_positions}/{cfg.max_concurrent_positions})")
    if not deployed_ok:
        reasons.append(f"Capital deployed ₹{deployed_capital:.2f} exceeds ₹{max_deployed:.2f}")
    return {
        "passed": passed,
        "open_positions": open_positions,
        "max_concurrent_positions": cfg.max_concurrent_positions,
        "deployed_capital": round(deployed_capital, 2),
        "max_deployed_capital": round(max_deployed, 2),
        "reasons": reasons,
    }



# ---------------------------------------------------------------------------
# Circuit Breaker (Fix 20 — Phase 2)
# ---------------------------------------------------------------------------

class CircuitBreaker:
    """
    Tracks daily/weekly/monthly P&L, consecutive losses, and account equity.
    State is persisted to JSON so it survives between runs.

    Triggers (from risk-management-rules.md Section 5 + compound-phase rules):
    - equity <= CAPITAL_HARD_FLOOR → stop all new trades
    - -3% daily P&L of total capital → no new trades today
    - -5% weekly P&L → no new trades this week
    - monthly loss limit = min(10% of capital, equity - hard floor)
    - 3 consecutive losses → pause 1 trading day
    """

    def __init__(self, total_capital: float, state_file: Path = STATE_FILE):
        self.total_capital = total_capital
        self.state_file = state_file
        self._state = self._load_state()
        # Ensure equity is initialized to total_capital on first run
        if 'account_equity' not in self._state:
            self._state['account_equity'] = float(total_capital)
            self._save_state()

    def _load_state(self) -> dict:
        if self.state_file.exists():
            try:
                return json.loads(self.state_file.read_text(encoding='utf-8'))
            except Exception:
                pass
        return {
            'daily_pnl': 0.0,
            'weekly_pnl': 0.0,
            'monthly_pnl': 0.0,
            'consecutive_losses': 0,
            'last_trade_date': None,
            'pause_until': None,
            'account_equity': None,  # set in __init__
        }

    def _save_state(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(self._state, indent=2), encoding='utf-8')

    @property
    def account_equity(self) -> float:
        eq = self._state.get('account_equity')
        if eq is None:
            return float(self.total_capital)
        return float(eq)

    def _reset_periods_if_needed(self):
        """Reset daily/weekly/monthly counters at period boundaries."""
        today = date.today().isoformat()
        last = self._state.get('last_trade_date')
        if last is None:
            return

        last_date = date.fromisoformat(last)
        today_date = date.today()

        if today_date > last_date:
            self._state['daily_pnl'] = 0.0

        if today_date.isocalendar()[1] != last_date.isocalendar()[1]:
            self._state['weekly_pnl'] = 0.0

        if today_date.month != last_date.month or today_date.year != last_date.year:
            self._state['monthly_pnl'] = 0.0

    def record_trade_result(self, net_pnl: float):
        """Call after each trade closes to update circuit breaker state and equity."""
        self._reset_periods_if_needed()
        self._state['daily_pnl']   = self._state.get('daily_pnl', 0.0) + net_pnl
        self._state['weekly_pnl']  = self._state.get('weekly_pnl', 0.0) + net_pnl
        self._state['monthly_pnl'] = self._state.get('monthly_pnl', 0.0) + net_pnl
        self._state['account_equity'] = self.account_equity + net_pnl
        self._state['last_trade_date'] = date.today().isoformat()

        if net_pnl < 0:
            self._state['consecutive_losses'] = self._state.get('consecutive_losses', 0) + 1
        else:
            self._state['consecutive_losses'] = 0

        # Set pause_until for consecutive loss trigger
        if self._state['consecutive_losses'] >= 3:
            tomorrow = (date.today() + timedelta(days=1)).isoformat()
            self._state['pause_until'] = tomorrow
            logger.warning("3 consecutive losses — pausing until %s. Review trade journal.", tomorrow)

        self._save_state()

    def _monthly_loss_limit(self) -> float:
        """
        Monthly loss limit (negative number) aligned with hard floor:
        min(10% of capital, room above hard floor).
        """
        from src.config import settings as _s
        pct_limit = self.total_capital * 0.10
        floor_budget = max(0.0, self.account_equity - _s.CAPITAL_HARD_FLOOR)
        return -min(pct_limit, floor_budget)

    def can_trade(self) -> dict:
        """Returns {'allowed': bool, 'reasons': list[str]}"""
        from src.config import settings as _s
        self._reset_periods_if_needed()
        reasons = []
        today = date.today()

        # Hard floor check
        floor_check = check_hard_floor(self.account_equity, _s.CAPITAL_HARD_FLOOR)
        if not floor_check['allowed']:
            reasons.append(floor_check['reason'])

        # Pause check
        pause_until = self._state.get('pause_until')
        if pause_until:
            pause_date = date.fromisoformat(pause_until)
            if today < pause_date:
                reasons.append(f"Consecutive loss pause active until {pause_until}. Review journal first.")

        # Daily P&L check (-3%)
        daily_limit = -(self.total_capital * 0.03)
        if self._state.get('daily_pnl', 0) <= daily_limit:
            reasons.append(f"Daily P&L limit hit: ₹{self._state['daily_pnl']:.0f} (limit ₹{daily_limit:.0f})")

        # Weekly P&L check (-5%)
        weekly_limit = -(self.total_capital * 0.05)
        if self._state.get('weekly_pnl', 0) <= weekly_limit:
            reasons.append(f"Weekly P&L limit hit: ₹{self._state['weekly_pnl']:.0f} (limit ₹{weekly_limit:.0f})")

        # Monthly P&L check (aligned with hard floor)
        monthly_limit = self._monthly_loss_limit()
        if self._state.get('monthly_pnl', 0) <= monthly_limit:
            reasons.append(
                f"Monthly P&L limit hit: ₹{self._state['monthly_pnl']:.0f} "
                f"(limit ₹{monthly_limit:.0f}) — STOP TRADING. Full strategy review required."
            )

        return {'allowed': len(reasons) == 0, 'reasons': reasons}

    def status_summary(self) -> str:
        from src.config import settings as _s
        s = self._state
        cap = self.total_capital
        monthly_limit = self._monthly_loss_limit()
        return (
            f"Equity: ₹{self.account_equity:.0f} (floor ₹{_s.CAPITAL_HARD_FLOOR:.0f}) | "
            f"Daily P&L: ₹{s.get('daily_pnl', 0):.0f} / limit ₹{-(cap*0.03):.0f} | "
            f"Weekly: ₹{s.get('weekly_pnl', 0):.0f} / ₹{-(cap*0.05):.0f} | "
            f"Monthly: ₹{s.get('monthly_pnl', 0):.0f} / ₹{monthly_limit:.0f} | "
            f"Consec losses: {s.get('consecutive_losses', 0)}"
        )


# ---------------------------------------------------------------------------
# Event Blackout Checker (Fix 22 — Phase 2)
# ---------------------------------------------------------------------------

class EventBlackoutChecker:
    """
    Blocks new SHORT-GAMMA entries near binary events.
    (Long options are allowed during blackout — buying convexity.)

    Event calendar loaded from JSON file. Falls back to empty list gracefully.
    Format: [{"name": "RBI Policy", "date": "2026-06-04", "blackout_days": 2, "scope": "index"}, ...]
    """

    def __init__(self, calendar_file: Path = EVENT_CALENDAR_FILE):
        self.calendar_file = calendar_file
        self._events = self._load_events()

    def _load_events(self) -> list:
        if self.calendar_file.exists():
            try:
                return json.loads(self.calendar_file.read_text(encoding='utf-8'))
            except Exception as e:
                logger.warning("Event calendar load failed: %s", e)
        return []

    def check(self, strategy: str, underlying: str = 'NIFTY') -> dict:
        """
        Returns {'blocked': bool, 'reason': str | None}

        Short-gamma strategies: 'iron_condor', 'bull_put_spread', 'bear_call_spread',
        any strategy containing 'credit' or 'short'.
        Long options / debit spreads: allowed during blackout.
        """
        short_gamma = any(kw in strategy.lower() for kw in
                           [
                               'iron_condor',
                               'credit',
                               'short',
                               'strangle_short',
                               # Credit/short-gamma spreads we may generate:
                               'bull_put_spread',
                               'bear_call_spread',
                           ])

        if not short_gamma:
            return {'blocked': False, 'reason': None}

        today = date.today()
        underlying_upper = underlying.upper()
        is_index = underlying_upper in {'NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY'}

        for event in self._events:
            scope = event.get('scope', 'all')
            if scope == 'index' and not is_index:
                continue
            if scope == 'stock' and is_index:
                continue

            try:
                event_date = date.fromisoformat(event['date'])
            except (KeyError, ValueError):
                continue

            blackout_days = int(event.get('blackout_days', 2))
            blackout_start = event_date - timedelta(days=blackout_days)

            if blackout_start <= today <= event_date:
                name = event.get('name', 'Unknown event')
                reason = (
                    f"Event blackout: {name} on {event_date} "
                    f"({blackout_days}d window). Short-gamma blocked. "
                    f"Long options/debit spreads still permitted."
                )
                logger.warning(reason)
                return {'blocked': True, 'reason': reason}

        return {'blocked': False, 'reason': None}

    def upcoming_events(self, days_ahead: int = 10) -> list:
        """Return events within the next N days for display in output."""
        today = date.today()
        upcoming = []
        for event in self._events:
            try:
                event_date = date.fromisoformat(event['date'])
                delta = (event_date - today).days
                if 0 <= delta <= days_ahead:
                    upcoming.append({
                        'name': event.get('name'),
                        'date': str(event_date),
                        'days_away': delta,
                        'blackout_days': event.get('blackout_days', 2),
                    })
            except (KeyError, ValueError):
                continue
        return sorted(upcoming, key=lambda x: x['days_away'])

    def is_in_blackout_window(self, underlying: str = 'NIFTY') -> bool:
        """
        Returns True if today falls within any event's blackout window.

        This is used by the pipeline to apply the 30% capital reduction
        for pre-event risk management. Checks all events in event-calendar.json
        whose scope applies to the given underlying.
        """
        today = date.today()
        underlying_upper = underlying.upper()
        is_index = underlying_upper in {'NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY'}

        for event in self._events:
            scope = event.get('scope', 'all')
            if scope == 'index' and not is_index:
                continue
            if scope == 'stock' and is_index:
                continue

            try:
                event_date = date.fromisoformat(event['date'])
            except (KeyError, ValueError):
                continue

            blackout_days = int(event.get('blackout_days', 2))
            blackout_start = event_date - timedelta(days=blackout_days)

            if blackout_start <= today <= event_date:
                logger.info(
                    "Blackout window active: %s on %s (%dd window)",
                    event.get('name', 'Unknown'), event_date, blackout_days
                )
                return True

        return False
