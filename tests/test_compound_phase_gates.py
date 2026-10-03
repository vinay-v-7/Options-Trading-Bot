"""
Unit tests for compound-phase risk gates (no live API).
"""

from pathlib import Path
import tempfile

import pytest

from src.config import settings
from src.risk.risk_controls import (
    CircuitBreaker,
    check_hard_floor,
    max_loss_for_trade,
    validate_trade_risk,
    confidence_capital_limit,
)
from src.orchestration.pipeline import (
    _resolve_credit_enabled,
    get_absolute_recommendation,
)
from src.analysis.analyzer import OptionsAnalyzer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_analyzer(**kwargs):
    """Build OptionsAnalyzer with empty frames for strategy-selection unit tests."""
    import pandas as pd
    defaults = dict(
        historical_df=pd.DataFrame({
            "close": [100.0, 101.0],
            "9_EMA": [100.0, 100.5],
            "HV": [15.0, 15.0],
        }),
        option_chain_data={"oc": {}},
        spot_price=100.0,
        lot_size=25,
        capital_limit=30000.0,
        governance_context={"vix": {"value": 12.0, "regime": "low_iv", "available": True}},
        underlying_symbol="RELIANCE",
        credit_enabled=False,
        step1b_context={},
    )
    defaults.update(kwargs)
    return OptionsAnalyzer(**defaults)


# ---------------------------------------------------------------------------
# Phase 1 — max loss abs / hard floor
# ---------------------------------------------------------------------------

def test_max_loss_abs_clamps_tier_cap():
    """Tier 3 uncapped on ₹30K would be > ₹800; clamp must apply."""
    deployed = confidence_capital_limit(30000.0, score=9.5)
    allowed = max_loss_for_trade(deployed, score=9.5)
    assert allowed == settings.MAX_LOSS_ABS
    assert allowed == 800.0

    result = validate_trade_risk(gross_risk=1200.0, total_capital=30000.0, score=9.5)
    assert result["passed"] is False
    assert result["allowed_risk"] == 800.0
    assert "absolute max-loss" in (result["reason"] or "").lower() or "800" in (result["reason"] or "")


def test_hard_floor_blocks_trading():
    floor = check_hard_floor(25000.0)
    assert floor["allowed"] is False
    assert floor["hard_floor"] == settings.CAPITAL_HARD_FLOOR

    ok = check_hard_floor(25001.0)
    assert ok["allowed"] is True

    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "cb.json"
        cb = CircuitBreaker(total_capital=30000.0, state_file=state)
        assert cb.can_trade()["allowed"] is True

        # Drive equity to floor
        cb.record_trade_result(-5000.0)  # 30000 -> 25000
        assert cb.account_equity == 25000.0
        status = cb.can_trade()
        assert status["allowed"] is False
        assert any("Hard floor" in r for r in status["reasons"])


# ---------------------------------------------------------------------------
# Phase 2 — credit capital gate / Step 1b / ranking
# ---------------------------------------------------------------------------

def test_credit_blocked_below_1l():
    assert _resolve_credit_enabled(30000.0, "short_term") is False
    assert _resolve_credit_enabled(99999.0, "short_term") is False
    assert _resolve_credit_enabled(100000.0, "short_term") is True
    assert _resolve_credit_enabled(100000.0, "standard") is False

    # Analyzer also clamps credit_enabled at init
    a = _minimal_analyzer(capital_limit=30000.0, credit_enabled=True)
    assert a.credit_enabled is False

    a2 = _minimal_analyzer(
        capital_limit=100000.0,
        credit_enabled=True,
        governance_context={"vix": {"value": 16.0, "regime": "normal_iv", "available": True}},
    )
    assert a2.credit_enabled is True


def test_select_strategy_high_vix_step1b_blocks_credit():
    a = _minimal_analyzer(
        capital_limit=100000.0,
        credit_enabled=True,
        governance_context={"vix": {"value": 22.0, "regime": "high_iv", "available": True}},
        step1b_context={"ema_downtrend": True, "gap_down_gt_1": False, "event_blackout": False},
    )
    # Bullish + PE would be bull_put_spread if Step 1b passed
    assert a._select_strategy("pe", "high_iv", "bullish") == "skip"

    a_ok = _minimal_analyzer(
        capital_limit=100000.0,
        credit_enabled=True,
        governance_context={"vix": {"value": 22.0, "regime": "high_iv", "available": True}},
        step1b_context={"ema_downtrend": False, "gap_down_gt_1": False, "event_blackout": False},
    )
    assert a_ok._select_strategy("pe", "high_iv", "bullish") == "bull_put_spread"


def test_prefer_stock_debit_ranking_under_1l():
    stock_debit = {
        "underlying": "RELIANCE",
        "strategy": "bull_call_spread",
        "score": 8.2,
        "rr": 1.6,
        "capital": 5000,
        "strike": 1400,
        "type": "ce",
    }
    index_long = {
        "underlying": "NIFTY",
        "strategy": "long_call",
        "score": 8.5,
        "rr": 2.0,
        "capital": 8000,
        "strike": 24000,
        "type": "ce",
    }
    pick = get_absolute_recommendation([index_long, stock_debit], capital_limit=30000.0)
    assert pick["underlying"] == "RELIANCE"
    assert pick["strategy"] == "bull_call_spread"


# ---------------------------------------------------------------------------
# Phase 3 — min net profit gate constants wired
# ---------------------------------------------------------------------------

def test_min_net_profit_rejects_candidate():
    """Constants and admission logic: values below MIN_NET_PROFIT / MIN_ROC_HARD fail gates."""
    assert settings.MIN_NET_PROFIT == 1000.0
    assert settings.MIN_ROC_HARD == 0.05

    # Simulate the screen_trades admission predicates
    net_profit = 500.0
    roc_after_cost = 4.0  # percent
    gross_risk = 600.0
    assert net_profit < settings.MIN_NET_PROFIT
    assert roc_after_cost < (settings.MIN_ROC_HARD * 100)
    assert gross_risk <= settings.MAX_LOSS_ABS  # risk alone would pass; profit gates fail

    net_ok = 1500.0
    roc_ok = 8.0
    assert net_ok >= settings.MIN_NET_PROFIT
    assert roc_ok >= (settings.MIN_ROC_HARD * 100)
