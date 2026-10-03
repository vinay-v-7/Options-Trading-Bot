import math
import logging
import pandas as pd
import numpy as np
from src.analysis.transaction import estimate_round_trip_cost, get_min_cost_buffer, estimate_stt_trap_cost
from src.adapters.data_validator import MetricValidator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# GATE: Per-strategy minimum capital threshold
# ---------------------------------------------------------------------------
def _strategy_config() -> dict:
    """Build STRATEGY_CONFIG with credit min_capital from settings."""
    from src.config import settings as _s
    return {
        'long_call':               {'min_capital': 3000,  'legs': 1, 'direction': 'bullish', 'opt_type': 'ce'},
        'long_put':                {'min_capital': 3000,  'legs': 1, 'direction': 'bearish', 'opt_type': 'pe'},
        'bull_call_spread':        {'min_capital': 2000,  'legs': 2, 'direction': 'bullish', 'opt_type': 'ce'},
        'bear_put_spread':         {'min_capital': 2000,  'legs': 2, 'direction': 'bearish', 'opt_type': 'pe'},
        # Credit / short-gamma — blocked below CREDIT_MIN_CAPITAL (₹1L+)
        'bull_put_spread':        {'min_capital': int(_s.CREDIT_MIN_CAPITAL), 'legs': 2, 'direction': 'bullish', 'opt_type': 'pe'},
        'bear_call_spread':       {'min_capital': int(_s.CREDIT_MIN_CAPITAL), 'legs': 2, 'direction': 'bearish', 'opt_type': 'ce'},
        'long_strangle_candidate': {'min_capital': 8000,  'legs': 2, 'direction': 'neutral', 'opt_type': 'both'},
    }


# Lazy-compatible alias used throughout the module
STRATEGY_CONFIG = None  # populated on first OptionsAnalyzer init / access


def get_strategy_config() -> dict:
    global STRATEGY_CONFIG
    if STRATEGY_CONFIG is None:
        STRATEGY_CONFIG = _strategy_config()
    return STRATEGY_CONFIG


# Module-level OI context cache (per instrument per day)
# Prevents redundant OI computation during multi-instrument scans
_OI_CACHE = {}

def _get_oi_cache_key(underlying: str, expiry: str) -> str:
    from datetime import date
    return f"{underlying}:{expiry}:{date.today().isoformat()}"


class DataHealth:
    """Tracks which signals are live vs placeholder for transparency reporting."""
    def __init__(self):
        self.signals = {}

    def record(self, name: str, available: bool, note: str = ''):
        self.signals[name] = {'ok': available, 'note': note}

    @property
    def all_ok(self):
        return all(v['ok'] for v in self.signals.values())

    def missing(self):
        return [k for k, v in self.signals.items() if not v['ok']]

    def report(self) -> str:
        lines = []
        for name, info in self.signals.items():
            icon = '✅' if info['ok'] else '⚠️ '
            note = f" ({info['note']})" if info['note'] else ''
            lines.append(f"   {icon} {name}{note}")
        return '\n'.join(lines)


class OptionsAnalyzer:
    def __init__(
        self,
        historical_df,
        option_chain_data,
        spot_price,
        lot_size=25,
        capital_limit=30000,
        governance_context=None,
        underlying_symbol='NIFTY',
        dte_preference=(21, 45),  # RISK: Configurable DTE range (21-45 default)
        credit_enabled: bool = False,
        step1b_context: dict | None = None,
    ):
        self.hist = historical_df
        self.chain = option_chain_data
        self.spot = spot_price
        self.lot_size = lot_size
        self.capital_limit = capital_limit
        self.governance_context = governance_context or {}
        self.underlying_symbol = (underlying_symbol or 'NIFTY').upper()
        self.dte_preference = dte_preference
        # Credit only when caller enables AND capital >= CREDIT_MIN_CAPITAL
        from src.config import settings as _s
        self.credit_enabled = bool(credit_enabled) and (
            capital_limit >= _s.CREDIT_MIN_CAPITAL
        )
        self.step1b_context = step1b_context or {}
        self.data_health = DataHealth()
        self.sanity_check_results = {} # Store external validation results
        get_strategy_config()  # ensure STRATEGY_CONFIG is loaded

        # DATA: Parse India VIX from governance feed
        vix_data = self.governance_context.get('vix', {})
        self.vix = vix_data.get('value') if isinstance(vix_data, dict) else None
        self.vix_regime = vix_data.get('regime', 'data_unavailable') if isinstance(vix_data, dict) else 'data_unavailable'
        self.data_health.record('India VIX', self.vix is not None,
                                f"={self.vix}" if self.vix else "not fetched — IV/HV fallback active")

        # 4-Pillar weights
        self.weights = {
            'Pillar 1': 0.40,
            'Pillar 2': 0.30,
            'Pillar 3': 0.20,
            'Pillar 4': 0.10,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _is_index_underlying(self):
        return self.underlying_symbol in {'NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY'}

    def _governance_gates(self):
        if self._is_index_underlying():
            return {'passed': True, 'reason': None}
        ban_mwpl = self.governance_context.get('ban_mwpl', {})
        is_in_ban = ban_mwpl.get('is_in_ban')
        mwpl_pct = ban_mwpl.get('mwpl_pct')
        if is_in_ban is True:
            return {'passed': False, 'reason': f'{self.underlying_symbol} is in F&O ban period'}
        try:
            mwpl_num = float(mwpl_pct) if mwpl_pct is not None else None
        except Exception:
            mwpl_num = None
        if mwpl_num is not None and mwpl_num >= 93.0:
            return {'passed': False, 'reason': f'MWPL elevated at {mwpl_num:.2f}% (avoid new entries)'}
        return {'passed': True, 'reason': None}

    def _validate_iv(self, opt_data) -> float | None:
        """Return validated IV float or None; rejects zero/negative/non-numeric."""
        raw = opt_data.get('implied_volatility')
        if raw is None:
            return None
        try:
            iv = float(raw)
            return iv if iv > 0 else None
        except (TypeError, ValueError):
            return None

    def _get_entry_price(self, opt_data):
        """Prefer ask for conservative buy-side entry; fallback to last traded."""
        ask = float(opt_data.get('top_ask_price') or 0)
        last = float(opt_data.get('last_price') or 0)
        return ask if ask > 0 else last

    def _get_sell_price(self, opt_data):
        """Prefer bid for conservative sell-side entry; fallback to last traded."""
        bid = float(opt_data.get('top_bid_price') or 0)
        last = float(opt_data.get('last_price') or 0)
        return bid if bid > 0 else last

    def _get_exit_bid(self, opt_data, target_price: float) -> float:
        """
        Approximate realistic exit at bid (not mid).
        Subtracts half-spread to model slippage.
        """
        bid = float(opt_data.get('top_bid_price') or 0)
        ask = float(opt_data.get('top_ask_price') or 0)
        half_spread = max((ask - bid) / 2, 0) if (ask > 0 and bid > 0) else 0
        return max(target_price - half_spread, bid) if bid > 0 else target_price

    # ------------------------------------------------------------------
    # CALC: IV regime classifier with data_unavailable sentinel
    # ------------------------------------------------------------------
    def _iv_regime(self, iv, hv) -> str:
        """
        Classify IV regime. Primary: India VIX. Secondary: IV/HV ratio.
        Returns 'data_unavailable' when both inputs are invalid.
        """
        if self.vix_regime != 'data_unavailable':
            return self.vix_regime
        if iv is None or iv <= 0 or hv is None or hv <= 0:
            return 'data_unavailable'
        ratio = iv / hv
        if ratio <= 0.9:
            return 'low_iv'
        if ratio >= 1.3:
            return 'high_iv'
        return 'normal_iv'

    # ------------------------------------------------------------------
    # CALC: Weighted directional bias — RSI, VWAP, EMA slope, FII/DII
    # ------------------------------------------------------------------
    def _market_bias(self, latest_hist) -> str:
        """
        Weighted directional bias with RSI, VWAP, EMA slope, and FII/DII modifier.
        Returns 'bullish', 'bearish', or 'neutral'.
        """
        score = 0  # positive = bullish, negative = bearish

        # EMA/SMA signals
        ema9 = latest_hist.get('9_EMA', self.spot)
        sma20 = latest_hist.get('20_SMA', self.spot)
        if self.spot > ema9:
            score += 2
        else:
            score -= 2
        if self.spot > sma20:
            score += 1
        else:
            score -= 1

        # CALC: VWAP signal — price above/below typical price
        vwap = latest_hist.get('VWAP', self.spot)
        if self.spot > vwap:
            score += 2
        else:
            score -= 2

        # CALC: RSI directional tilt (not extremes)
        rsi = latest_hist.get('RSI', 50)
        if rsi > 55:
            score += 1
        elif rsi < 45:
            score -= 1

        # DATA: FII/DII flow regime modifier (enhanced with hard gate for extreme flows)
        # Journal lesson (System Audit 2026-05-15): Never trade against FII buying in low-VIX env.
        # Senior Review §19.3.3: Use 3-day rolling average, not single-day binary.
        fii_data = self.governance_context.get('fii_dii', {})
        fii_hint = fii_data.get('regime_hint', 'neutral')
        fii_net_cr = fii_data.get('fii_net_cr')

        if fii_hint == 'risk_on':
            score += 1
        elif fii_hint == 'risk_off':
            score -= 1

        # HARD GATE: Extreme FII selling blocks bullish index trades
        # (fii_net_cr is in crores; negative = selling)
        if fii_net_cr is not None:
            try:
                fii_val = float(fii_net_cr)
                if fii_val < -2000 and self._is_index_underlying():
                    # Extreme FII selling (> ₹2,000 Cr) — strong bearish undercurrent
                    score -= 3  # Heavy penalty, likely forces bearish or neutral bias
                    logger.warning(
                        "FII extreme selling: ₹%.0f Cr — heavy bearish penalty applied to index bias",
                        fii_val
                    )
                elif fii_val > 1500 and self._is_index_underlying():
                    # Strong FII buying — supportive of bullish thesis
                    score += 1
            except (ValueError, TypeError):
                pass

        if score >= 3:
            return 'bullish'
        if score <= -3:
            return 'bearish'
        return 'neutral'


    # ------------------------------------------------------------------
    # GATE: Strategy selector — 9 bias×type×regime combinations
    # ------------------------------------------------------------------
    def _step1b_allows_credit(self) -> bool:
        """
        High-VIX Step 1b filter from strategy-selection-matrix.md.
        Credit/short-gamma only when ALL pass:
          - not in multi-day EMA downtrend
          - no gap-down > 1% recently
          - no event blackout
        Non-high-IV regimes: Step 1b does not apply (returns True).
        """
        is_high_vix = (
            self.vix_regime == 'high_iv'
            or (self.vix is not None and float(self.vix) > 20)
        )
        if not is_high_vix:
            return True

        ctx = self.step1b_context
        if ctx.get('ema_downtrend', False):
            logger.info("Step 1b block: EMA downtrend — no short-gamma in high VIX")
            return False
        if ctx.get('gap_down_gt_1', False):
            logger.info("Step 1b block: gap-down > 1% — no short-gamma in high VIX")
            return False
        if ctx.get('event_blackout', False):
            logger.info("Step 1b block: event blackout — no short-gamma in high VIX")
            return False
        return True

    def _select_strategy(self, opt_type: str, iv_regime: str, bias: str) -> str:
        """
        Complete strategy selector for all 9 bias×type×regime combos.
        Capital gating applied — strategies requiring more than capital_limit are skipped.
        Credit requires credit_enabled (capital ≥ CREDIT_MIN_CAPITAL) and Step 1b when high VIX.
        """
        from src.config import settings as _s

        if iv_regime == 'data_unavailable':
            return 'skip'

        strategy = 'skip'
        credit_ok = self.credit_enabled and self._step1b_allows_credit()

        if bias == 'bullish':
            if opt_type == 'ce':
                if iv_regime == 'low_iv':
                    strategy = 'long_call'
                else:  # normal_iv or high_iv — debit spread preferred
                    strategy = 'bull_call_spread'
            elif opt_type == 'pe':
                # short-gamma credit: selling puts when we expect upside
                if credit_ok:
                    strategy = 'bull_put_spread'
                else:
                    strategy = 'skip'

        elif bias == 'bearish':
            if opt_type == 'pe':
                if iv_regime == 'low_iv':
                    # Prefer debit put spread when capital is compound-phase (< ₹1L)
                    # to avoid naked long-put drag in low IV (journal: BANKNIFTY)
                    if self.capital_limit < _s.CREDIT_MIN_CAPITAL:
                        strategy = 'bear_put_spread'
                    else:
                        strategy = 'long_put'
                else:
                    strategy = 'bear_put_spread'
            elif opt_type == 'ce':
                # short-gamma credit: selling calls when we expect downside
                if credit_ok:
                    strategy = 'bear_call_spread'
                else:
                    strategy = 'skip'

        elif bias == 'neutral':
            if iv_regime == 'low_iv':
                strategy = 'long_strangle_candidate'
            # normal_iv neutral → no trade (matrix: "edge insufficient")
            # high_iv neutral → iron condor needs ₹1L+, skip below that

        if strategy == 'skip':
            return 'skip'

        # Continuous capital gating — strategy declares min_capital
        cfg = get_strategy_config().get(strategy, {})
        if self.capital_limit < cfg.get('min_capital', 0):
            logger.debug("Strategy %s skipped — capital ₹%.0f < min ₹%.0f",
                         strategy, self.capital_limit, cfg.get('min_capital', 0))
            return 'skip'

        return strategy

    # ------------------------------------------------------------------
    # CALC: Black-Scholes delta approximation (r=6.5% India T-bill)
    # ------------------------------------------------------------------
    def _compute_bs_delta(self, strike: float, dte_days: int, iv: float, opt_type: str) -> float:
        """
        Approximate Black-Scholes delta. Uses risk-free rate of 6.5% (India T-bill).
        Returns float in [0, 1] for calls and [-1, 0] for puts.
        Returns 0.5 (ATM) as fallback on bad inputs.
        """
        if iv <= 0 or dte_days <= 0 or self.spot <= 0 or strike <= 0:
            return 0.5
        try:
            S, K, r, T = self.spot, strike, 0.065, dte_days / 365.0
            sigma = iv / 100.0
            d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
            # Standard normal CDF via math.erfc
            nd1 = 0.5 * math.erfc(-d1 / math.sqrt(2))
            if opt_type == 'ce':
                return round(nd1, 3)
            else:
                return round(nd1 - 1.0, 3)
        except (ValueError, ZeroDivisionError):
            return 0.5

    # ------------------------------------------------------------------
    # DATA: OI context — PCR, max pain, OI walls, pre-qualification gate
    # ------------------------------------------------------------------
    def _oi_context(self):
        """Compute PCR, true max pain, OI walls from chain snapshot. Cached per day."""
        cache_key = _get_oi_cache_key(
            self.underlying_symbol,
            str(self.chain.get('expiry', 'unknown'))
        )
        if cache_key in _OI_CACHE:
            return _OI_CACHE[cache_key]

        oc = self.chain.get('oc', {}) if isinstance(self.chain, dict) else {}  # DATA: safe chain access
        if not oc:
            return {'pcr': None, 'max_pain': None, 'put_wall': None, 'call_wall': None,
                    'oi_bias': 'unknown', 'pcr_warning': None}

        total_put_oi = 0.0
        total_call_oi = 0.0
        put_wall = (None, -1)
        call_wall = (None, -1)
        pain_map = {}  # strike → total OI payout if expires at that strike

        for strike_str, node in oc.items():
            try:
                strike = float(strike_str)
            except ValueError:
                continue
            ce_oi = float(node.get('ce', {}).get('oi', 0) or 0)
            pe_oi = float(node.get('pe', {}).get('oi', 0) or 0)
            total_call_oi += ce_oi
            total_put_oi += pe_oi
            if pe_oi > put_wall[1]:
                put_wall = (strike, pe_oi)
            if ce_oi > call_wall[1]:
                call_wall = (strike, ce_oi)
            pain_map[strike] = {'ce_oi': ce_oi, 'pe_oi': pe_oi}

        pcr = (total_put_oi / total_call_oi) if total_call_oi > 0 else None

        # True max pain: strike where total OI payout (writers) is minimised
        max_pain = None
        if pain_map:
            all_strikes = sorted(pain_map.keys())
            min_pain_val = float('inf')
            for test_strike in all_strikes:
                # Call writer losses: all calls with strike < test_strike expire ITM
                call_loss = sum(
                    (test_strike - s) * pain_map[s]['ce_oi']
                    for s in all_strikes if s < test_strike
                )
                # Put writer losses: all puts with strike > test_strike expire ITM
                put_loss = sum(
                    (s - test_strike) * pain_map[s]['pe_oi']
                    for s in all_strikes if s > test_strike
                )
                total_pain = call_loss + put_loss
                if total_pain < min_pain_val:
                    min_pain_val = total_pain
                    max_pain = test_strike

        # GATE: PCR-based directional filter (>1.5 put-heavy, <0.6 call-heavy)
        oi_bias = 'neutral'
        pcr_warning = None
        if pcr is not None:
            if pcr > 1.5:
                oi_bias = 'put_dominant'
                pcr_warning = f"PCR={pcr:.2f} (>1.5): heavy put build-up — market likely supported; avoid aggressive puts"
            elif pcr < 0.6:
                oi_bias = 'call_dominant'
                pcr_warning = f"PCR={pcr:.2f} (<0.6): heavy call build-up — resistance overhead; avoid aggressive calls"

        result = {
            'pcr': round(pcr, 3) if pcr is not None else None,
            'max_pain': max_pain,
            'put_wall': put_wall[0],
            'call_wall': call_wall[0],
            'oi_bias': oi_bias,
            'pcr_warning': pcr_warning,
        }
        _OI_CACHE[cache_key] = result
        return result

    # ------------------------------------------------------------------
    # CALC: Risk/Reward plans — long option and debit spread
    # ------------------------------------------------------------------
    def _rr_plan_long_option(self, entry_price: float) -> dict:
        """Stoploss = 50% of premium. Min R:R = 1:1.5"""
        stoploss = entry_price * 0.50
        risk_per_share = max(entry_price - stoploss, 0)
        reward_per_share = risk_per_share * 1.5
        target = entry_price + reward_per_share
        rr = (reward_per_share / risk_per_share) if risk_per_share > 0 else 0
        return {'target': target, 'stoploss': stoploss,
                'risk_per_share': risk_per_share, 'reward_per_share': reward_per_share, 'rr': rr}

    def _rr_plan_debit_spread(self, net_debit: float, spread_width: float) -> dict:
        """Stoploss = 75% of net debit (exit when spread value drops to 25%)."""
        stoploss_val = net_debit * 0.25  # exit when spread worth 25% of entry
        risk_per_share = net_debit * 0.75
        max_profit_per_share = max(spread_width - net_debit, 0)
        target = net_debit + max_profit_per_share * 0.65  # target 65% of max profit
        rr = (max_profit_per_share * 0.65 / risk_per_share) if risk_per_share > 0 else 0
        return {'target': target, 'stoploss': stoploss_val,
                'risk_per_share': risk_per_share, 'reward_per_share': max_profit_per_share * 0.65, 'rr': rr}

    def _rr_plan_credit_spread(self, net_credit: float, spread_width: float) -> dict:
        """
        Credit spread RR plan (short-gamma).

        Stoploss methodology (from risk rules):
          - received net credit = C
          - close when spread value moves to 2C (exit debit = 2C)
          - that implies loss per share = (2C - C) = C

        Profit target methodology:
          - close at 50% profit
          - i.e., exit debit D = C/2, profit per share = C - D = C/2
        """
        stoploss_exit_debit = net_credit * 2.0  # exit when spread doubles
        risk_per_share = net_credit  # loss equals original credit

        target_exit_debit = net_credit * 0.5  # close at 50% profit
        reward_per_share = net_credit - target_exit_debit  # = C/2

        rr = (reward_per_share / risk_per_share) if risk_per_share > 0 else 0
        return {
            'target': target_exit_debit,
            'stoploss': stoploss_exit_debit,
            'risk_per_share': risk_per_share,
            'reward_per_share': reward_per_share,
            'rr': rr,
        }


    # ------------------------------------------------------------------
    # Scoring Pillars
    # ------------------------------------------------------------------
    def _score_pillar_1_rr_edge(self, rr: float, delta: float) -> int:
        """
        Pillar 1: Statistical Edge — R:R ratio + delta validation with OTM/ITM penalties.
        """
        if rr >= 2.0:
            base = 10
        elif rr >= 1.5:
            base = 7
        elif rr >= 1.0:
            base = 4
        else:
            base = 0

        # Delta modifier
        abs_delta = abs(delta)
        if abs_delta < 0.25:
            base = max(base - 3, 0)   # too far OTM
        elif abs_delta > 0.75:
            base = max(base - 1, 0)   # deep ITM — capital-heavy, illiquid
        return base

    def _score_pillar_2_volatility(self, iv, hv, is_credit: bool = False) -> int:
        """
        Pillar 2: Volatility Context.
        Primary: India VIX regime. Secondary: IV/HV ratio.
        High VIX + long option strategy → capped (buying in fear).
        For credit/short-gamma strategies, rich IV is favorable (selling premium).
        """
        # VIX-based cap for long options in high-IV regime
        if self.vix_regime == 'high_iv':
            return 10 if is_credit else 5  # credit benefits from high-IV, longs are penalized

        if hv is None or hv <= 0:
            return 5  # neutral fallback

        iv_hv_ratio = iv / hv
        if is_credit:
            if iv_hv_ratio >= 1.30:
                return 10
            if iv_hv_ratio >= 1.15:
                return 9
            if iv_hv_ratio >= 1.00:
                return 7
            if iv_hv_ratio >= 0.80:
                return 5
            return 3

        # Long option / debit-spread mapping (low IV relative to HV is favorable)
        if iv_hv_ratio <= 0.80:
            return 10
        if iv_hv_ratio <= 1.00:
            return 9
        if iv_hv_ratio <= 1.15:
            return 7
        if iv_hv_ratio <= 1.30:
            return 5
        return 3

    def _score_pillar_3_technical(self, latest_hist, bias: str = 'bullish') -> int:
        """
        Pillar 3: Technical Confluence — direction-aware scoring. (Fix 3F)

        Bullish trades score high on bullish signals (spot > EMA/VWAP).
        Bearish trades score high on bearish signals (spot < EMA/VWAP).
        Using direction-blind scoring was the most dangerous bug in the original system.
        """
        score = 0
        is_bearish = (bias == 'bearish')

        ema9 = latest_hist.get('9_EMA')
        sma20 = latest_hist.get('20_SMA')
        rsi = latest_hist.get('RSI')
        vwap = latest_hist.get('VWAP')

        available = 0

        if ema9 is not None:
            available += 1
            above_ema = self.spot > ema9
            # Score high when signal MATCHES trade direction
            if (above_ema and not is_bearish) or (not above_ema and is_bearish):
                score += 3

        if sma20 is not None:
            available += 1
            above_sma = self.spot > sma20
            if (above_sma and not is_bearish) or (not above_sma and is_bearish):
                score += 2

        if vwap is not None:
            available += 1
            above_vwap = self.spot > vwap
            if (above_vwap and not is_bearish) or (not above_vwap and is_bearish):
                score += 3

        if rsi is not None:
            available += 1
            if is_bearish:
                if 35 <= rsi <= 55:    # room to fall — neither oversold nor in free fall
                    score += 2
                elif rsi > 75:         # overbought — supports bearish thesis
                    score += 1
                elif rsi < 25:         # already oversold — bearish exhaustion risk
                    score -= 2
            else:
                if 40 <= rsi <= 65:    # room to run — neither overbought nor oversold
                    score += 2
                elif rsi > 75 or rsi < 25:  # extreme — fade signal
                    score -= 2

        self.data_health.record('RSI', rsi is not None, f"={rsi:.1f}" if rsi else "missing")
        self.data_health.record('VWAP', vwap is not None, f"={vwap:.0f}" if vwap else "missing")

        if available == 0:
            return 0

        return max(min(score, 10), 0)

    def _score_pillar_4_execution(self, spread_pct: float, roc_after_cost: float,
                                  costs_pct_of_gross) -> int:
        """Pillar 4: Execution quality — liquidity + cost efficiency."""
        liquidity_pass = spread_pct < 2.0
        cost_efficiency_pass = (costs_pct_of_gross <= 30.0) if costs_pct_of_gross is not None else False
        if not liquidity_pass or not cost_efficiency_pass:
            return 3
        if roc_after_cost >= 8.0:
            return 10
        if roc_after_cost >= 5.0:
            return 7
        return 4

    # ------------------------------------------------------------------
    # EXEC: Position sizing via confidence-tiered risk limits (Wave 1)
    # ------------------------------------------------------------------
    def _position_sizing(self, max_risk_per_lot: float, total_capital: float,
                         capital_per_lot: float, score: float = 8.0) -> int:
        """
        Confidence-tiered position sizing. (Replaces 2% rule — Wave 1)

        Hybrid approach:
          - Tier determines max deployable capital and max loss cap.
          - risk_lots  = tier_max_loss // risk_per_lot  (bounded by tier stoploss cap)
          - capital_lots = tier_capital // capital_per_lot (bounded by tier deploy %)
          - Returns min(risk_lots, capital_lots, 10 retail cap).

        max_risk_per_lot: max loss for 1 lot (premium-based stoploss × lot_size).
        capital_per_lot:  capital required for 1 lot entry.
        score:            4-Pillar weighted score for tier selection.
        """
        from src.risk.risk_controls import confidence_capital_limit, max_loss_for_trade

        tier_capital = confidence_capital_limit(total_capital, score)
        tier_max_loss = max_loss_for_trade(tier_capital, score)

        if max_risk_per_lot <= 0:
            return 1

        risk_lots = int(tier_max_loss // max_risk_per_lot) if tier_max_loss > 0 else 0
        capital_lots = int(tier_capital // capital_per_lot) if capital_per_lot > 0 else 1
        # RISK: Hard retail cap at 10 lots
        return max(1, min(risk_lots, capital_lots, 10))


    # ------------------------------------------------------------------
    # GATE: Math invariant validation — fail-closed on inconsistent output
    # ------------------------------------------------------------------
    def _validate_score_math_invariants(
        self,
        strategy: str,
        entry_price: float,
        net_debit: float,
        rr_plan: dict,
        gross_risk: float,
        gross_profit: float,
        net_profit: float,
        capital_required: float,
    ) -> tuple[bool, str]:
        """Return (ok, reason). Rejects candidates with internally inconsistent math."""
        tol = 0.02
        risk_per_share = rr_plan.get('risk_per_share', 0)
        reward_per_share = rr_plan.get('reward_per_share', 0)
        rr = rr_plan.get('rr', 0)
        stoploss = rr_plan.get('stoploss', 0)

        if gross_risk <= 0 or capital_required <= 0:
            return False, f"gross_risk={gross_risk}, capital_required={capital_required}"

        if risk_per_share > 0:
            expected_rr = reward_per_share / risk_per_share
            if abs(rr - expected_rr) > tol:
                return False, f"rr mismatch: {rr} vs {expected_rr:.4f}"

        is_spread = strategy in ('bull_call_spread', 'bear_put_spread', 'bull_put_spread', 'bear_call_spread')
        if is_spread:
            expected_capital = net_debit * self.lot_size
            if abs(capital_required - expected_capital) > tol:
                return False, f"spread capital mismatch: {capital_required} vs {expected_capital}"
        else:
            expected_stoploss = entry_price * 0.50
            if abs(stoploss - expected_stoploss) > tol:
                return False, f"long option stoploss mismatch: {stoploss} vs {expected_stoploss}"

        if net_profit > gross_profit + tol:
            return False, f"net_profit {net_profit} exceeds gross_profit {gross_profit}"

        return True, ""

    # ------------------------------------------------------------------
    # EXEC: calculate_score — routes R:R plan by strategy type
    # ------------------------------------------------------------------
    def calculate_score(self, leg1, strategy='long_option', dte_days=35):
        """
        Calculate 4-Pillar weighted score for a trade candidate.
        Routes RR plan by strategy type. (Fix 19)
        """
        # Fix 3D: Defensive data guard — cannot score without historical data
        if self.hist is None or self.hist.empty:
            logger.warning("Historical data unavailable — cannot calculate score.")
            return 0.0, {}

        scores = {}
        latest_hist = self.hist.iloc[-1]

        entry_price = self._get_entry_price(leg1['data'])
        iv = self._validate_iv(leg1['data'])
        hv = float(latest_hist.get('HV') or 0)
        iv_val = iv if iv is not None else 0.0

        # CALC: BS delta for strike/DTE/IV
        delta = self._compute_bs_delta(
            strike=leg1['strike'],
            dte_days=dte_days,
            iv=iv_val,
            opt_type=leg1['type'],
        )

        is_credit = strategy in ('bull_put_spread', 'bear_call_spread')

        # Route RR plan by strategy
        spread_width = leg1.get('spread_width', 0)
        if strategy in ('bull_call_spread', 'bear_put_spread'):
            net_debit = leg1.get('net_debit', entry_price)
            rr_plan = self._rr_plan_debit_spread(net_debit, spread_width)
            num_legs = 2
        elif is_credit:
            # For credit spreads, we treat `net_debit` as "net_credit" for downstream math consistency.
            net_debit = leg1.get('net_credit', entry_price)
            entry_price = net_debit
            rr_plan = self._rr_plan_credit_spread(net_debit, spread_width)
            num_legs = 2
        else:
            net_debit = entry_price
            rr_plan = self._rr_plan_long_option(entry_price)
            num_legs = 1

        # EXEC: Realistic exit price approximation
        realistic_exit = rr_plan['target'] if is_credit else self._get_exit_bid(leg1['data'], rr_plan['target'])

        # Pillar 1: for credit spreads, interpret R:R in the inverse direction (risk/reward),
        # because credit setups have reward<risk by construction.
        rr_metric = rr_plan['rr']
        if is_credit and rr_plan.get('reward_per_share', 0) > 0:
            rr_metric = rr_plan['risk_per_share'] / rr_plan['reward_per_share']

        scores['Pillar 1'] = self._score_pillar_1_rr_edge(rr_metric, delta)
        scores['Pillar 2'] = self._score_pillar_2_volatility(iv_val, hv, is_credit=is_credit)
        # Fix 3F: Pass bias for direction-aware technical scoring
        bias_for_scoring = leg1.get('bias', 'bullish')
        scores['Pillar 3'] = self._score_pillar_3_technical(latest_hist, bias=bias_for_scoring)

        bid = float(leg1['data'].get('top_bid_price', 0) or 0)
        ask = float(leg1['data'].get('top_ask_price', 0) or 0)
        mid = (ask + bid) / 2 if (ask + bid) > 0 else 0
        spread_pct = ((ask - bid) / mid) * 100 if mid > 0 else 999

        gross_profit = rr_plan['reward_per_share'] * self.lot_size
        gross_risk   = rr_plan['risk_per_share']   * self.lot_size

        min_cost_buffer = get_min_cost_buffer(num_legs)
        if is_credit:
            # Credit spreads: entry is "selling premium" and exit is "buy-to-close".
            # The existing transaction model is buy/sell oriented; for screening we use
            # a conservative floor buffer so credit math remains fail-closed.
            estimated_cost = 0.0
            effective_cost = min_cost_buffer
        else:
            estimated_cost = estimate_round_trip_cost(
                entry_price=entry_price,
                exit_price=realistic_exit,
                lot_size=self.lot_size,
                num_legs=num_legs,
            )
            effective_cost = max(estimated_cost, min_cost_buffer)

        net_profit = gross_profit - effective_cost
        capital_required = net_debit * self.lot_size
        roc_after_cost = (net_profit / capital_required) * 100 if capital_required > 0 else -999
        costs_pct_of_gross = (effective_cost / gross_profit) * 100 if gross_profit > 0 else None

        # COST: STT trap detection — near-expiry ITM physical settlement risk
        stt_trap_warning = None
        if dte_days <= 5 and abs(delta) > 0.5:
            stt_cost = estimate_stt_trap_cost(self.spot, self.lot_size)
            stt_trap_warning = f"⚠️  ITM + DTE={dte_days}: STT trap risk = ₹{stt_cost:.0f} if held to expiry"

        # Apply Sanity Check penalty if available (Fix: Multi-source fallback strategy)
        sanity_penalty = 0
        if self.sanity_check_results:
            # Check for Max Pain divergence
            mp_check = self.sanity_check_results.get('max_pain')
            if mp_check and not mp_check.get('verified'):
                penalty = mp_check.get('penalty', 0)
                # RISK: De-weight max pain penalty for DTE > 14
                if dte_days > 14:
                    penalty *= 0.5
                sanity_penalty += penalty
            
            # Check for RSI divergence
            rsi_check = self.sanity_check_results.get('rsi')
            if rsi_check and not rsi_check.get('verified'):
                sanity_penalty += rsi_check.get('penalty', 0)

        scores['Pillar 4'] = max(0, self._score_pillar_4_execution(
            spread_pct=spread_pct,
            roc_after_cost=roc_after_cost,
            costs_pct_of_gross=costs_pct_of_gross,
        ) + sanity_penalty)

        weighted_total = sum(scores[p] * self.weights[p] for p in self.weights)

        math_ok, math_reason = self._validate_score_math_invariants(
            strategy=strategy,
            entry_price=entry_price,
            net_debit=net_debit,
            rr_plan=rr_plan,
            gross_risk=gross_risk,
            gross_profit=gross_profit,
            net_profit=net_profit,
            capital_required=capital_required,
        )
        if not math_ok:
            logger.error("Score math invariant failed (%s) — rejecting candidate", math_reason)
            return 0.0, {}

        # EXEC: Confidence-tiered position sizing (Wave 1 — replaces 2% rule)
        optimal_lots = self._position_sizing(
            max_risk_per_lot=gross_risk,
            total_capital=self.capital_limit,
            capital_per_lot=capital_required,
            score=weighted_total,
        )

        details = {
            **scores,
            'weights': self.weights,
            'weighted_math': {
                'P1': round(scores['Pillar 1'] * self.weights['Pillar 1'], 2),
                'P2': round(scores['Pillar 2'] * self.weights['Pillar 2'], 2),
                'P3': round(scores['Pillar 3'] * self.weights['Pillar 3'], 2),
                'P4': round(scores['Pillar 4'] * self.weights['Pillar 4'], 2),
            },
            'target':           rr_plan['target'],
            'stoploss':         rr_plan['stoploss'],
            'risk_per_share':   rr_plan['risk_per_share'],
            'reward_per_share': rr_plan['reward_per_share'],
            'rr':               rr_plan['rr'],
            'delta':            delta,
            'gross_profit':     gross_profit,
            'gross_risk':       gross_risk,
            'estimated_cost':   estimated_cost,
            'effective_cost':   effective_cost,
            'net_profit':       net_profit,
            'capital_required': capital_required,
            'roc_after_cost':   roc_after_cost,
            'spread_pct':       spread_pct,
            'costs_pct_of_gross': costs_pct_of_gross,
            'optimal_lots':     optimal_lots,
            'stt_trap_warning': stt_trap_warning,
        }
        return round(weighted_total, 2), details

    def _find_spread_leg2(self, oc, leg1_strike, opt_type, bias):
        """
        Find the sell leg for a debit spread. Uses strike mapping to preserve original chain keys.
        """
        # DATA: Map float→string keys to avoid chain format mismatch
        strike_map = {float(s): s for s in oc.keys()}
        strikes = sorted(strike_map.keys())
        
        try:
            idx = strikes.index(float(leg1_strike))
        except ValueError:
            return None

        # Standard offset (1-2 strikes away)
        offset = 1 
        
        if opt_type == 'ce' and bias == 'bullish':
            if idx + offset < len(strikes):
                strike2 = strikes[idx + offset]
                return strike_map[strike2]
        elif opt_type == 'pe' and bias == 'bearish':
            if idx - offset >= 0:
                strike2 = strikes[idx - offset]
                return strike_map[strike2]
        return None

    def _find_credit_spread_leg2(self, oc, leg1_strike, opt_type, bias):
        """
        Find the buy hedge leg for a credit (short-gamma) spread.

        bull_put_spread: sell put (leg1) at strike, buy put (leg2) at higher strike
        bear_call_spread: sell call (leg1) at strike, buy call (leg2) at lower strike
        """
        strike_map = {float(s): s for s in oc.keys()}
        strikes = sorted(strike_map.keys())

        try:
            idx = strikes.index(float(leg1_strike))
        except ValueError:
            return None

        offset = 1
        if opt_type == 'pe' and bias == 'bullish':
            if idx + offset < len(strikes):
                strike2 = strikes[idx + offset]
                return strike_map[strike2]
        elif opt_type == 'ce' and bias == 'bearish':
            if idx - offset >= 0:
                strike2 = strikes[idx - offset]
                return strike_map[strike2]

        return None

    # ------------------------------------------------------------------
    # screen_trades — main screening loop (all Phase 0 fixes applied)
    # ------------------------------------------------------------------
    def screen_trades(self, expiry_date: str = None, open_positions: list = None):
        """
        Iterate through strikes and find best candidates.
        """
        # Fix 3D: Defensive data guard — abort if historical data is unavailable
        if self.hist is None or self.hist.empty:
            logger.warning("Historical data unavailable — cannot screen trades.")
            return []

        candidates = []

        # RISK: Correlated position count — limit index exposure
        correlated_count = 0
        if open_positions:
            for pos in open_positions:
                pos_sym = pos.get('symbol', '').upper()
                if any(idx in pos_sym for idx in ["NIFTY", "BANKNIFTY", "FINNIFTY"]):
                    correlated_count += 1
        
        # RISK: VIX > 20 halves effective capital (only when credit/short strategies are disabled)
        vix_val = self.governance_context.get('vix', {}).get('value', 0)
        actual_capital_limit = self.capital_limit
        self._regime_capital_warning = None  # Surface to pipeline output
        if vix_val and vix_val > 20 and not self.credit_enabled:
            actual_capital_limit = self.capital_limit * 0.5
            self._regime_capital_warning = (
                f"⚠️  REGIME-CAPITAL MISMATCH: India VIX={vix_val:.1f} (>20) suggests credit/sell strategies, "
                f"but Phase 0 capital (₹{self.capital_limit:,.0f}) restricts to debit strategies only. "
                f"Capital halved to ₹{actual_capital_limit:,.0f} as risk reduction. "
                f"Consider: (a) Reducing position size further, (b) Waiting for VIX to normalize, "
                f"or (c) Increasing capital to ₹1,0,000+ for credit strategy access."
            )
            logger.warning(self._regime_capital_warning)

        # DATA: Safe option chain access with empty-dict fallback
        oc = self.chain.get('oc', {}) if isinstance(self.chain, dict) else {}
        if not oc:
            logger.warning("Option chain has no 'oc' data — cannot screen.")
            return []

        latest_hist = self.hist.iloc[-1]
        oi_ctx = self._oi_context()

        # Fix 3A: Activate MetricValidator — populate sanity_check_results with max pain
        # (In automated mode, web consensus is unavailable; mark as not_verified with zero penalty.)
        if oi_ctx.get('max_pain') is not None:
            # Compute divergence between true max pain and OI wall midpoint
            put_wall = oi_ctx.get('put_wall')
            call_wall = oi_ctx.get('call_wall')
            mp_divergence_penalty = 0
            mp_reason = 'Max pain confidence: computed from OI data'

            if put_wall is not None and call_wall is not None:
                oi_wall_midpoint = (put_wall + call_wall) / 2
                mp_divergence_pct = abs(oi_ctx['max_pain'] - oi_wall_midpoint) / self.spot * 100
                if mp_divergence_pct > 2.0:
                    mp_divergence_penalty = -1  # Small penalty for low confidence
                    mp_reason = f'Max pain diverges {mp_divergence_pct:.1f}% from OI wall midpoint — low confidence'

            self.sanity_check_results['max_pain'] = {
                'verified': mp_divergence_penalty == 0,
                'penalty': mp_divergence_penalty,
                'reason': mp_reason,
                'local_value': oi_ctx['max_pain'],
            }
        gov_gate = self._governance_gates()

        if not gov_gate['passed']:
            logger.warning("Governance gate failed: %s", gov_gate['reason'])
            return []

        # CALC: DTE from expiry string
        dte_days = 35  # default
        if expiry_date:
            try:
                from datetime import datetime as _dt
                exp = _dt.strptime(expiry_date, '%Y-%m-%d').date()
                dte_days = (exp - _dt.now().date()).days
            except ValueError:
                pass

        # GATE: Block trades outside configured DTE preference range
        if dte_days < self.dte_preference[0] or dte_days > self.dte_preference[1]:
            logger.warning(f"DTE={dte_days} outside preference {self.dte_preference}. No new trades. (P2-2)")
            return []

        # GATE: Block stock options within 2 DTE — physical settlement risk
        if not self._is_index_underlying() and dte_days <= 2:
            logger.warning("Stock option DTE=%d <= 2 — physical settlement risk. Blocked.", dte_days)
            return []

        # CALC: Compute market bias once outside strike loop
        bias = self._market_bias(latest_hist)
        logger.info("Market bias: %s | VIX regime: %s | OI bias: %s",
                    bias, self.vix_regime, oi_ctx.get('oi_bias', 'unknown'))

        # PCR warning log
        if oi_ctx.get('pcr_warning'):
            logger.warning(oi_ctx['pcr_warning'])

        # Correlated limit gate (P1-2)
        is_index = self._is_index_underlying()
        if is_index and correlated_count >= 2:
            logger.warning("Correlated position limit (2 index trades) reached. Skipping new index entries. (P1-2)")
            return []

        pct_filter = 0.05  # GATE: Strike filter — ±5% from spot
        # Wave 1: Use widest tier stoploss cap (Tier 3 = 8%) as the pre-screening gate.
        # Precise tier limits are applied in _position_sizing() after scoring.
        from src.config import settings as _cfg
        # Pre-screen risk budget: tier-3 % of capital, clamped by MAX_LOSS_ABS
        max_risk_budget = min(
            actual_capital_limit * _cfg.STOPLOSS_CAP_TIER_3,
            _cfg.MAX_LOSS_ABS,
        )

        for strike_price_str, data in oc.items():
            try:
                strike_price = float(strike_price_str)
            except ValueError:
                continue

            # GATE: Skip strikes beyond ±5% filter
            if abs(strike_price - self.spot) / self.spot > pct_filter:
                continue

            for opt_type in ['ce', 'pe']:
                opt_data = data.get(opt_type, {})
                if not opt_data:
                    continue

                # Min OI liquidity gate
                if float(opt_data.get('oi', 0) or 0) <= 500:
                    continue

                entry_price = self._get_entry_price(opt_data)
                if entry_price <= 0:
                    continue

                # GATE: Skip strikes with invalid/missing IV
                iv = self._validate_iv(opt_data)
                if iv is None:
                    continue

                # Bid/ask liquidity gate
                bid = float(opt_data.get('top_bid_price', 0) or 0)
                ask = float(opt_data.get('top_ask_price', 0) or 0)
                mid = (ask + bid) / 2 if (ask + bid) > 0 else 0
                if mid <= 0:
                    continue
                spread_pct = ((ask - bid) / mid) * 100
                if spread_pct >= 2.0:
                    continue

                hv = float(latest_hist.get('HV', 0) or 0)
                iv_regime = self._iv_regime(iv, hv)

                # GATE: Skip strikes with unavailable IV regime
                if iv_regime == 'data_unavailable':
                    logger.debug("Strike %s %s skipped — IV regime unavailable", strike_price_str, opt_type.upper())
                    continue

                strategy = self._select_strategy(opt_type=opt_type, iv_regime=iv_regime, bias=bias)
                if strategy == 'skip':
                    continue

                is_debit_spread = strategy in ('bull_call_spread', 'bear_put_spread')
                is_credit_spread = strategy in ('bull_put_spread', 'bear_call_spread')

                # EXEC: Construct spread from leg1 + leg2
                leg1 = {'strike': strike_price, 'type': opt_type, 'data': opt_data}
                if is_debit_spread:
                    strike2_str = self._find_spread_leg2(oc, strike_price_str, opt_type, bias)
                    if strike2_str:
                        leg2_data = oc[strike2_str].get(opt_type, {})
                        if leg2_data:
                            entry_price2 = self._get_entry_price(leg2_data)
                            net_debit = entry_price - entry_price2
                            if net_debit > 0:
                                leg1['net_debit'] = net_debit
                                leg1['spread_width'] = abs(float(strike_price) - float(strike2_str))
                                leg1['leg2'] = {'strike': float(strike2_str), 'data': leg2_data}
                            else:
                                strategy = 'long_option'
                        else:
                            strategy = 'long_option'
                    else:
                        strategy = 'long_option'
                elif is_credit_spread:
                    strike2_str = self._find_credit_spread_leg2(oc, strike_price_str, opt_type, bias)
                    if strike2_str:
                        leg2_data = oc[strike2_str].get(opt_type, {})
                        if leg2_data:
                            # Credit spread: sell leg receives bid, buy hedge pays ask
                            entry_sell = self._get_sell_price(opt_data)
                            entry_buy = self._get_entry_price(leg2_data)
                            net_credit = entry_sell - entry_buy
                            if net_credit > 0:
                                leg1['net_credit'] = net_credit
                                leg1['spread_width'] = abs(float(strike_price) - float(strike2_str))
                                leg1['leg2'] = {'strike': float(strike2_str), 'data': leg2_data}
                            else:
                                strategy = 'long_option'
                        else:
                            strategy = 'long_option'
                    else:
                        strategy = 'long_option'

                # GATE: Buy-leg premium decay — skip overpriced buy legs (Journal Trades 2, 5)
                buy_leg_data = opt_data
                if strategy in ('bull_put_spread', 'bear_call_spread'):
                    buy_leg_data = leg1.get('leg2', {}).get('data', {})
                buy_leg_iv = self._validate_iv(buy_leg_data)
                buy_leg_hv = float(latest_hist.get('HV', 0) or 0)
                if buy_leg_iv and buy_leg_hv > 0:
                    iv_hv_ratio = buy_leg_iv / buy_leg_hv
                    if iv_hv_ratio > 1.2:
                        logger.info(
                            "BUY-LEG GATE: IV/HV=%.2f > 1.2 — skipping %s %s (premium overpriced)",
                            iv_hv_ratio, strike_price_str, opt_type.upper(),
                        )
                        continue

                # Use net_debit/net_credit for spreads, entry_price for long options
                if strategy in ('bull_put_spread', 'bear_call_spread'):
                    current_entry = leg1.get('net_credit', entry_price)
                else:
                    current_entry = leg1.get('net_debit', entry_price)
                capital_req = current_entry * self.lot_size
                
                # RISK: Gross risk calculation — debit=net×lot, long=50%×premium×lot
                if strategy in ('bull_call_spread', 'bear_put_spread', 'bull_put_spread', 'bear_call_spread'):
                    gross_risk = current_entry * self.lot_size  # Max loss = net_debit × lot (correct for debit spreads)
                else:
                    gross_risk = current_entry * 0.50 * self.lot_size  # Stoploss at 50% premium drawdown

                if capital_req > self.capital_limit:
                    continue
                if gross_risk > max_risk_budget:
                    continue

                # GATE: OI hard gate — block trades that fight dominant writer bias
                # Journal lesson (Trade 2 BANKNIFTY): buying puts in put-dominant regime = fighting the tape
                is_credit_strategy = strategy in ('bull_put_spread', 'bear_call_spread')

                oi_bias = oi_ctx.get('oi_bias', 'neutral')
                oi_contradiction = False
                oi_p3_penalty = 0
                pcr_val = oi_ctx.get('pcr')

                if oi_bias == 'call_dominant' and opt_type == 'ce':
                    oi_contradiction = not is_credit_strategy
                    oi_p3_penalty = -1 if oi_contradiction else 0
                elif oi_bias == 'put_dominant' and opt_type == 'pe':
                    oi_contradiction = not is_credit_strategy
                    oi_p3_penalty = -1 if oi_contradiction else 0

                # HARD GATE: Extreme PCR blocks the trade entirely (no breakout override)
                # PCR > 1.5 = heavy put writing = market supported → block PE buys
                # PCR < 0.6 = heavy call writing = resistance overhead → block CE buys
                if pcr_val is not None and not is_credit_strategy:
                    if pcr_val > 1.5 and opt_type == 'pe':
                        logger.info("OI HARD GATE: PCR=%.2f > 1.5 — blocking PE buys (put-writing dominant)", pcr_val)
                        continue  # Skip this strike entirely
                    elif pcr_val < 0.6 and opt_type == 'ce':
                        logger.info("OI HARD GATE: PCR=%.2f < 0.6 — blocking CE buys (call-writing dominant)", pcr_val)
                        continue  # Skip this strike entirely

                # Breakout override: if spot has moved past max pain in the trade direction,
                # the OI build-up will catch up — cancel the penalty.
                max_pain = oi_ctx.get('max_pain')
                if oi_contradiction and max_pain is not None:
                    if (opt_type == 'ce' and self.spot > max_pain * 1.01) or \
                       (opt_type == 'pe' and self.spot < max_pain * 0.99):
                        oi_p3_penalty = 0  # confirmed breakout

                # Thread bias into leg1 so calculate_score passes it to Pillar 3
                leg1['bias'] = bias

                score, details = self.calculate_score(
                    leg1=leg1,
                    strategy=strategy,
                    dte_days=dte_days,
                )

                # Fix 3B: Apply OI contradiction penalty to Pillar 3 in details
                if oi_p3_penalty != 0:
                    adjusted_p3 = max(0, details.get('Pillar 3', 0) + oi_p3_penalty)
                    details['Pillar 3'] = adjusted_p3
                    # Recompute weighted total with adjusted Pillar 3
                    score = round(sum(details[p] * self.weights[p] for p in self.weights), 2)

                # SCORING: Max pain proximity penalty (de-weighted — half effect)
                # Senior Review §19.3.5: max pain computed but not used in scoring.
                # Trades far from max pain have lower POP near expiry.
                max_pain = oi_ctx.get('max_pain')
                if max_pain is not None and dte_days <= 21:
                    max_pain_dist_pct = abs(strike_price - max_pain) / self.spot * 100
                    if max_pain_dist_pct > 3.0:  # > 3% away from max pain
                        # Half-weight penalty: -0.5 per 1% distance beyond 3%
                        mp_penalty = -int((max_pain_dist_pct - 3.0) * 0.5)
                        mp_penalty = max(mp_penalty, -2)  # Cap at -2 to prevent over-penalizing
                        adjusted_p3 = max(0, details.get('Pillar 3', 0) + mp_penalty)
                        details['Pillar 3'] = adjusted_p3
                        score = round(sum(details[p] * self.weights[p] for p in self.weights), 2)
                        logger.debug(
                            "Max pain penalty: strike=%s, max_pain=%s, dist=%.1f%%, penalty=%d",
                            strike_price, max_pain, max_pain_dist_pct, mp_penalty
                        )

                from src.config import settings as _settings
                if score < _settings.MIN_SCORE_THRESHOLD:
                    continue

                net_profit = details.get('net_profit', 0) or 0
                roc_after_cost = details.get('roc_after_cost', 0) or 0
                gross_risk = details.get('gross_risk', 0) or 0

                # Compound-phase hard gates (reject, not soft score)
                if net_profit < _settings.MIN_NET_PROFIT:
                    logger.debug(
                        "Reject strike %s — net_profit ₹%.0f < MIN_NET_PROFIT ₹%.0f",
                        strike_price, net_profit, _settings.MIN_NET_PROFIT,
                    )
                    continue
                if roc_after_cost < (_settings.MIN_ROC_HARD * 100):
                    logger.debug(
                        "Reject strike %s — ROC %.1f%% < MIN_ROC_HARD %.0f%%",
                        strike_price, roc_after_cost, _settings.MIN_ROC_HARD * 100,
                    )
                    continue
                if gross_risk > _settings.MAX_LOSS_ABS:
                    logger.debug(
                        "Reject strike %s — gross_risk ₹%.0f > MAX_LOSS_ABS ₹%.0f",
                        strike_price, gross_risk, _settings.MAX_LOSS_ABS,
                    )
                    continue

                candidates.append({
                    'strike':      strike_price,
                    'strike2':     leg1.get('leg2', {}).get('strike'),
                    'type':        opt_type,
                    'entry':       current_entry,
                    'score':       score,
                    'details':     details,
                    # Capital deployed is per-lot requirement × computed lots
                    'capital':     capital_req * details.get('optimal_lots', 1),
                    'target':      details['target'],
                    'stoploss':    details['stoploss'],
                    'rr':          details['rr'],
                    'delta':       details['delta'],
                    'roc_after_cost': details['roc_after_cost'],
                    'estimated_cost': details['effective_cost'],
                    'net_profit':  details['net_profit'],
                    'gross_risk':  gross_risk,
                    'strategy':    strategy,
                    'bias':        bias,
                    'iv_regime':   iv_regime,
                    'oi_context':  oi_ctx,
                    'oi_contradiction': oi_contradiction,
                    'optimal_lots': details['optimal_lots'],
                    'governance_context': self.governance_context,
                    # Anti-hallucination: provide real contract identifiers from API data
                    'expiry_date': expiry_date,
                    'contract_id': (
                        f"{self.underlying_symbol}"
                        f"{expiry_date.replace('-', '')[-4:]}"
                        f"{int(strike_price)}"
                        f"{opt_type.upper()}"
                        if expiry_date else None
                    ),
                })

        return sorted(candidates, key=lambda x: x['score'], reverse=True)


if __name__ == "__main__":
    print("OptionsAnalyzer ready — all Phase 0/1 fixes applied.")
