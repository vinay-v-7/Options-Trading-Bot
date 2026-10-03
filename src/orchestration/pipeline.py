"""
Options Recommendation Generator — Production Workflow

Integrates all Phase 0/1/2 fixes:
- Fix 7:  Parameterised underlying/capital (no hardcoded NIFTY/₹30K)
- Fix 18: Pre-market gap assessment
- Fix 20: Circuit breaker check
- Fix 21: Structured logging
- Fix 22: Event blackout check
- Fix 23: PositionStore for real deployed capital
- Fix 24: DataHealth Signal Health Report in output
- Fix 26: Recommendation timestamp
"""

import logging
import logging.handlers
from datetime import datetime, timedelta
from pathlib import Path
import sys

# Ensure stdout uses UTF-8 to prevent UnicodeEncodeError when printing the Rupee (₹) symbol on Windows
if sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        pass

from src.adapters.dhan_feed import DhanDataFeed
from src.adapters.governance import MarketGovernanceFeed
from src.analysis.analyzer import OptionsAnalyzer
from src.risk.risk_controls import RiskConfig, validate_trade_risk, validate_portfolio_capacity, CircuitBreaker, EventBlackoutChecker
from src.execution.position_store import PositionStore
from src.execution.trade_manager import record_trade_exit
from src.adapters.universe import DynamicUniverseBuilder
from src.config import settings

# Universe is now handled dynamically via DynamicUniverseBuilder in scan_market()


# ---------------------------------------------------------------------------
# Logging setup (Fix 21)
# ---------------------------------------------------------------------------
def _setup_logging():
    """Configure structured logging with daily rotation and run_id."""
    log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    fmt = f"%(asctime)s | run={run_id} | %(levelname)-8s | %(name)s: %(message)s"
    formatter = logging.Formatter(fmt, datefmt="%Y-%m-%dT%H:%M:%S")

    # Console handler
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)

    # File handler — daily rotation, keep 30 days
    fh = logging.handlers.TimedRotatingFileHandler(
        log_dir / "trading.log", when='midnight', backupCount=30, encoding='utf-8'
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    if not root.handlers:
        root.addHandler(console)
        root.addHandler(fh)

    return run_id


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
def _sep(char='=', n=56):
    return char * n


def _print_weighted_math(details):
    wm = details.get('weighted_math', {})
    print("\n--- Weighted Score Math ---")
    print(f"   P1 (R:R+Delta) ×0.40: {wm.get('P1', 0):.2f}")
    print(f"   P2 (Volatility)×0.30: {wm.get('P2', 0):.2f}")
    print(f"   P3 (Technical) ×0.20: {wm.get('P3', 0):.2f}")
    print(f"   P4 (Execution) ×0.10: {wm.get('P4', 0):.2f}")


def _build_execution_advisory(strategy: str) -> dict:
    """Output-only execution guidance — does not affect scoring or selection."""
    advisory = {
        'preferred_window_ist': '11:00-13:00',
        'avoid_window_ist': '09:15-10:15',
        'note': (
            'If limit price is not available in the preferred window, walk away — '
            'do not chase premium.'
        ),
    }
    multi_leg = strategy in (
        'bull_call_spread', 'bear_put_spread',
        'bull_put_spread', 'bear_call_spread',
        'long_strangle_candidate',
    )
    if multi_leg:
        advisory['leg_order'] = 'buy_hedge_first'
        advisory['note'] = (
            'MANDATORY: NSE fills spread legs sequentially. Execute BUY (hedge) leg first '
            'or use strategy basket with a tight net limit. '
            'If one leg fails within 30 seconds, square off the filled leg immediately — '
            'naked exposure is prohibited.'
        )
    return advisory


def _resolve_credit_enabled(capital: float, mode: str) -> bool:
    """Credit/short-gamma only when mode allows AND capital >= CREDIT_MIN_CAPITAL."""
    mode_allows = (mode or "").lower() == "short_term"
    return mode_allows and capital >= settings.CREDIT_MIN_CAPITAL


def _build_step1b_context(hist_df, underlying: str, gap: dict) -> dict:
    """
    High-VIX Step 1b flags for credit/short-gamma gating.
    """
    ema_downtrend = False
    if hist_df is not None and len(hist_df) >= 3:
        try:
            recent = hist_df.tail(3)
            if '9_EMA' in recent.columns:
                emas = [float(x) for x in recent['9_EMA'].tolist()]
                closes = [float(x) for x in recent['close'].tolist()]
                # Multi-day downtrend: EMA declining and price below EMA
                ema_downtrend = (
                    emas[-1] < emas[-2] <= emas[0]
                    and closes[-1] < emas[-1]
                )
        except Exception:
            ema_downtrend = False

    gap_pct = gap.get('gap_pct') if gap else None
    gap_down_gt_1 = gap_pct is not None and gap_pct < -1.0

    blackout = EventBlackoutChecker().is_in_blackout_window(underlying=underlying)

    return {
        'ema_downtrend': ema_downtrend,
        'gap_down_gt_1': gap_down_gt_1,
        'event_blackout': blackout,
    }


def _print_regime_dial(
    vix_data: dict,
    capital: float,
    credit_enabled: bool,
    step1b: dict,
    equity: float | None = None,
):
    """Print compound-phase Regime Dial at start of recommendation output."""
    regime = vix_data.get('regime', 'data_unavailable') if vix_data else 'data_unavailable'
    vix_val = vix_data.get('value') if vix_data else None
    vix_str = f"{vix_val:.1f}" if vix_val is not None else "n/a"
    credit_status = "UNLOCKED" if credit_enabled else f"LOCKED (need ₹{settings.CREDIT_MIN_CAPITAL:,.0f}+)"
    if capital < settings.CREDIT_MIN_CAPITAL:
        families = "long options, debit spreads (stock preferred)"
    else:
        families = "long/debit + credit (if Step 1b passes)"
    step1b_ok = not (
        step1b.get('ema_downtrend')
        or step1b.get('gap_down_gt_1')
        or step1b.get('event_blackout')
    )
    eq = equity if equity is not None else capital
    print("\n--- Regime Dial (Compound Phase) ---")
    print(f"   India VIX:        {vix_str} → {regime}")
    print(f"   Allowed families: {families}")
    print(f"   Credit strategies:{credit_status}")
    print(f"   Step 1b (high VIX credit): {'PASS' if step1b_ok else 'FAIL — short-gamma blocked'}")
    print(f"   Max loss/trade:   ₹{settings.MAX_LOSS_ABS:.0f}")
    print(f"   Min net profit:   ₹{settings.MIN_NET_PROFIT:.0f}")
    print(f"   Equity / floor:   ₹{eq:,.0f} / ₹{settings.CAPITAL_HARD_FLOOR:,.0f}")


def _print_execution_advisory(advisory: dict):
    print(f"\n--- Execution Advisory ---")
    print(f"   Preferred window (IST): {advisory['preferred_window_ist']}")
    print(f"   Avoid window (IST):     {advisory['avoid_window_ist']}")
    if advisory.get('leg_order'):
        print(f"   Leg order:              {advisory['leg_order']}")
    print(f"   {advisory['note']}")


def _assess_gap(hist_df) -> dict:
    """
    Pre-market gap assessment (Fix 18).
    Returns gap_pct and whether trading is restricted.
    """
    if hist_df is None or len(hist_df) < 2:
        return {'gap_pct': None, 'restricted': False, 'reason': None}
    prev_close = float(hist_df.iloc[-2]['close'])
    today_open = float(hist_df.iloc[-1].get('open', hist_df.iloc[-1]['close']))
    if prev_close <= 0:
        return {'gap_pct': None, 'restricted': False, 'reason': None}
    gap_pct = ((today_open - prev_close) / prev_close) * 100
    if abs(gap_pct) > 3.0:
        return {'gap_pct': round(gap_pct, 2), 'restricted': True,
                'reason': f"Gap {gap_pct:+.1f}% > ±3% — NO NEW TRADES today (regime shift risk)"}
    if abs(gap_pct) > 1.5:
        return {'gap_pct': round(gap_pct, 2), 'restricted': False,
                'reason': f"Gap {gap_pct:+.1f}% > ±1.5% — consider reduced size"}
    return {'gap_pct': round(gap_pct, 2), 'restricted': False, 'reason': None}


# ---------------------------------------------------------------------------
# log_trade_exit hook (unchanged API for backward compat)
# ---------------------------------------------------------------------------
def log_trade_exit(strategy, underlying, exit_reason, net_pnl, what_worked, what_didnt, lesson, exit_date=None):
    """Integration hook for Step-7 post-trade journaling workflow."""
    return record_trade_exit(
        strategy=strategy, underlying=underlying, exit_reason=exit_reason,
        net_pnl=net_pnl, what_worked=what_worked, what_didnt=what_didnt,
        lesson=lesson, exit_date=exit_date,
    )


# ---------------------------------------------------------------------------
# Main recommendation function (Fix 7 — fully parameterised)
# ---------------------------------------------------------------------------
def generate_recommendation(
    underlying: str = 'NIFTY',
    security_id: int = 13,
    segment: str = 'IDX_I',
    instrument_type: str = 'INDEX',
    lot_size_default: int = 25,
    capital_limit: float = settings.TOTAL_CAPITAL,
    total_capital: float = settings.TOTAL_CAPITAL,
    pre_fetched_governance: dict = None,
    skip_global_checks: bool = False,
    quiet: bool = False,
    dte_preference: tuple[int, int] = (21, 45),
    credit_enabled: bool = False,
):
    """
    Generate a daily options recommendation.

    Args:
        underlying: Underlying symbol (e.g. 'NIFTY', 'BANKNIFTY', 'RELIANCE')
        security_id: Dhan security ID for the underlying
        segment: Exchange segment ('IDX_I' for index, 'NSE_EQ' for stock)
        instrument_type: 'INDEX' or 'EQUITY'
        lot_size_default: Fallback lot size if dynamic fetch fails
        capital_limit: Max capital per trade
        total_capital: Total trading capital (for risk % calculations)
        pre_fetched_governance: (Optional) Hoisted governance data
        skip_global_checks: (Optional) If True, skip Circuit Breaker/Portfolio checks
        quiet: (Optional) If True, minimize print output
    """
    import os
    import sys
    import contextlib

    # Support quiet mode — use redirect_stdout for exception-safe suppression
    if quiet:
        with open(os.devnull, 'w', encoding='utf-8') as _devnull:
            with contextlib.redirect_stdout(_devnull):
                return _generate_recommendation_logic(
                    underlying, security_id, segment, instrument_type,
                    lot_size_default, capital_limit, total_capital,
                    pre_fetched_governance, skip_global_checks,
                    dte_preference,
                    credit_enabled,
                )

    return _generate_recommendation_logic(
        underlying, security_id, segment, instrument_type,
        lot_size_default, capital_limit, total_capital,
        pre_fetched_governance, skip_global_checks,
        dte_preference,
        credit_enabled,
    )


def _generate_recommendation_logic(
    underlying, security_id, segment, instrument_type,
    lot_size_default, capital_limit, total_capital,
    pre_fetched_governance, skip_global_checks,
    dte_preference,
    credit_enabled,
):
    run_id = _setup_logging()
    generated_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')  # CALC: IST timestamp for generated_at field

    print(_sep())
    print(f"OPTIONS RECOMMENDATION — {underlying}")
    print(f"Generated: {generated_at} | Capital: ₹{total_capital:,.0f}")
    print(_sep())

    # --- Step 0: Circuit Breaker --- # GATE: Circuit breaker pre-check
    if not skip_global_checks:
        circuit = CircuitBreaker(total_capital=total_capital)
        cb_status = circuit.can_trade()
        print(f"\n📊 Circuit Breaker: {circuit.status_summary()}")
        if not cb_status['allowed']:
            print("\n🔴 TRADING BLOCKED — Circuit Breaker Triggered:")
            for r in cb_status['reasons']:
                print(f"   • {r}")
            print(_sep())
            return {'recommendation': None, 'blocked_by': 'circuit_breaker',
                    'reasons': cb_status['reasons'], 'timestamp': generated_at}
    else:
        cb_status = {'allowed': True, 'reasons': []}

    # GATE: Optimal entry window 11:00-13:00 IST (Journal lesson: Trade 1 RELIANCE)
    now_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
    current_hour = now_ist.hour
    is_optimal_window = (11 <= current_hour < 13)
    entry_window_warning = None
    if not is_optimal_window:
        entry_window_warning = (
            f"⚠️  OUTSIDE OPTIMAL ENTRY WINDOW: Current time {now_ist.strftime('%H:%M')} IST. "
            f"Best entries are 11:00-13:00 IST (consolidation zone, tightest spreads). "
            f"Trading now carries wider bid-ask spreads and higher slippage risk. "
            f"(Journal lesson: RELIANCE Trade 1 — chased entry, paid ₹5 wider spread)"
        )
        logger.warning(entry_window_warning)
        print(f"\n{entry_window_warning}")
    
    # Blackout check via EventBlackoutChecker (single source: event-calendar.json)
    _pre_blackout = EventBlackoutChecker()
    is_blackout = _pre_blackout.is_in_blackout_window(underlying=underlying)
    effective_capital_limit = capital_limit
    if is_blackout:
        logger.warning("Blackout/Pre-event period detected. Reducing capital limit by 30%%.")
        effective_capital_limit = capital_limit * 0.70

    # --- Step 1: Data Fetch ---
    logger.info("Initialising data feeds for %s", underlying)
    feed = DhanDataFeed()
    governance_feed = MarketGovernanceFeed()

    hist = feed.get_historical_data(security_id=str(security_id), segment=segment, instrument_type=instrument_type)
    if hist is None or hist.empty:
        logger.error("Failed to fetch historical data (returned None or empty).")
        print("🔴 Failed to fetch historical data. Aborting.")
        return {'recommendation': None, 'blocked_by': 'data_error', 'timestamp': generated_at}

    latest_close = float(hist.iloc[-1]['close'])
    print(f"\n📈 {underlying} Spot: ₹{latest_close:,.2f}")

    # Pre-market gap check (Fix 18)
    gap = _assess_gap(hist)
    if gap['gap_pct'] is not None:
        print(f"   Gap vs prev close: {gap['gap_pct']:+.1f}%")
    if gap['restricted']:
        print(f"\n🔴 {gap['reason']}")
        print(_sep())
        return {'recommendation': None, 'blocked_by': 'gap_restriction',
                'reason': gap['reason'], 'timestamp': generated_at}
    if gap['reason']:
        print(f"   ⚠️  {gap['reason']}")

    expiry = feed.get_best_expiry(
        security_id=security_id,
        segment=segment,
        dte_range=dte_preference,
    )
    chain = feed.get_option_chain(security_id=security_id, segment=segment, expiry=expiry)
    
    if not chain or not chain.get('oc'):
        logger.error("Failed to fetch option chain (returned None or empty 'oc').")
        print("🔴 Failed to fetch option chain. Aborting.")
        return {'recommendation': None, 'blocked_by': 'data_error', 'timestamp': generated_at}

    # Extract dynamic lot size from the chain to avoid hitting the rate-limited API twice
    lot_size = lot_size_default
    try:
        for key in ['lot_size', 'lotsize', 'lotSize']:
            if key in chain and chain[key]:
                lot_size = int(chain[key])
                break
        else:
            meta = chain.get('meta', {}) if isinstance(chain, dict) else {}
            for key in ['lot_size', 'lotsize', 'lotSize']:
                if key in meta and meta[key]:
                    lot_size = int(meta[key])
                    break
    except Exception as e:
        logger.debug("Could not parse lot size from chain, using default=%d", lot_size)

    # Defensive check (Fix P0-5)
    if lot_size <= 0:
        logger.warning("Invalid lot size %d extracted, using default %d", lot_size, lot_size_default)
        lot_size = lot_size_default

    # --- Governance / VIX fetch (Fix 1) ---
    if pre_fetched_governance:
        governance_context = pre_fetched_governance
        # Update symbol-specific ban check if needed
        # (Though global check covers most, we re-run ban check specifically for stocks)
        if instrument_type == 'EQUITY':
            ban_snapshot = governance_feed.get_ban_mwpl(symbol=underlying)
            governance_context['ban_mwpl'] = ban_snapshot
    else:
        governance_snapshot = governance_feed.get_snapshot(symbol=underlying)
        governance_context = {
            'fii_dii':       governance_snapshot.fii_dii,
            'participant_oi': governance_snapshot.participant_oi,
            'ban_mwpl':      governance_snapshot.ban_mwpl,
            'vix':           governance_snapshot.vix,
            'source_health': governance_snapshot.source_health,
            'timestamp_utc': governance_snapshot.timestamp_utc,
        }

    vix_data = governance_context['vix']
    vix_display = f"VIX={vix_data['value']:.1f} [{vix_data['regime']}]" if vix_data.get('available') else "VIX=⚠️  unavailable"
    print(f"   India {vix_display}")

    # Step 1b context for high-VIX credit gating
    step1b_context = _build_step1b_context(hist, underlying, gap)
    # Effective credit flag (capital-gated; mode alone never unlocks below ₹1L)
    effective_credit = bool(credit_enabled) and (total_capital >= settings.CREDIT_MIN_CAPITAL)

    # --- Step 2: Portfolio state from PositionStore (Fix 23) ---
    pos_store = PositionStore()
    open_positions_list = pos_store.get_open()
    deployed_capital = pos_store.deployed_capital
    print(f"\n{pos_store.summary()}")

    # Equity / hard floor from circuit breaker state
    circuit_for_dial = CircuitBreaker(total_capital=total_capital)
    _print_regime_dial(
        vix_data=vix_data,
        capital=total_capital,
        credit_enabled=effective_credit,
        step1b=step1b_context,
        equity=circuit_for_dial.account_equity,
    )

    # --- Step 3: Analysis ---
    analyzer = OptionsAnalyzer(
        hist, chain, latest_close,
        lot_size=lot_size,
        capital_limit=effective_capital_limit,  # RISK: Capital limit passed to analyzer
        governance_context=governance_context,
        underlying_symbol=underlying,
        dte_preference=dte_preference,
        credit_enabled=effective_credit,
        step1b_context=step1b_context,
    )
    recommendations = analyzer.screen_trades(expiry_date=expiry, open_positions=open_positions_list)

    # DATA: Signal health transparency report
    print(f"\n--- Signal Health ---")
    print(analyzer.data_health.report())

    if hasattr(analyzer, '_regime_capital_warning') and analyzer._regime_capital_warning:
        print(f"\n{analyzer._regime_capital_warning}")

    # --- Portfolio capacity check (uses real position data now) ---
    risk_cfg = RiskConfig(total_capital=total_capital)
    if not skip_global_checks:
        portfolio_check = validate_portfolio_capacity(
            open_positions=len(open_positions_list),
            deployed_capital=deployed_capital,
            cfg=risk_cfg,
        )
    else:
        portfolio_check = {'passed': True, 'reasons': []}

    # --- Output ---
    print(f"\n{_sep()}")
    print(f"ANALYSIS RESULT — {underlying} | {generated_at}")
    print(_sep())

    if not portfolio_check['passed']:
        print("\n⛔ No new trades — portfolio capacity reached:")
        for reason in portfolio_check['reasons']:
            print(f"   • {reason}")

    elif not recommendations:
        print("\n✅ Analysis complete — No qualifying trades today.")
        print("   (Score < 8.0, or failed compound-phase gates: max loss / min net profit / ROC / strategy)")
        print(
            f"   Gates: max loss ≤ ₹{settings.MAX_LOSS_ABS:.0f}, "
            f"min net ≥ ₹{settings.MIN_NET_PROFIT:.0f}, "
            f"ROC ≥ {settings.MIN_ROC_HARD * 100:.0f}%"
        )

    else:
        # Fix 3C: Candidate fallback loop — try each candidate until one passes risk gate
        best = None
        best_trade_risk = None

        # Event blackout check (Fix 22) — done once against top candidate
        blackout_checker = EventBlackoutChecker()
        upcoming = blackout_checker.upcoming_events(days_ahead=10)

        for candidate in recommendations:
            blackout = blackout_checker.check(strategy=candidate['strategy'], underlying=underlying)
            if blackout['blocked']:
                print(f"\n⚠️  Blackout: {blackout['reason']} — trying next candidate")
                continue

            # Per-trade risk validation (tiered)
            trade_risk = validate_trade_risk(
                gross_risk=candidate['details'].get('gross_risk', 0.0),
                total_capital=risk_cfg.total_capital,
                score=candidate['score'],
            )
            if trade_risk['passed']:
                best = candidate
                best_trade_risk = trade_risk
                break
            else:
                logger.info(
                    "Candidate %s %s score=%.1f failed risk gate: %s — trying next",
                    candidate.get('strike'), candidate.get('type'),
                    candidate.get('score', 0), trade_risk['reason']
                )

        if best is None:
            print("\n✅ Analysis complete — No candidates pass risk validation.")
            print("   (All candidates exceeded tier stoploss cap, or were blocked by events)")
            print(_sep())
            return {'recommendation': None, 'blocked_by': 'risk_gate', 'timestamp': generated_at}

        # Import tier helper for display
        from src.risk.risk_controls import get_confidence_tier
        tier_info = get_confidence_tier(best['score'])

        print(f"\n🎯 TOP PICK: {underlying} {int(best['strike'])} {best['type'].upper()}")
        print(f"   Score:    {best['score']:.2f}/10")
        print(f"   Strategy: {best['strategy']}")
        print(f"   Expiry:   {expiry}")
        print(f"   Bias:     {best['bias'].upper()} | IV Regime: {best['iv_regime']}")
        print(f"   Delta:    {best['delta']:+.3f}")
        print(f"   OI Bias:  {best['oi_context'].get('oi_bias', 'n/a')}")
        if best.get('oi_contradiction'):
            print(f"   ⚠️  OI Contradiction: trade direction conflicts with OI build-up (Pillar 3 −1)")

        print(f"\n--- 4-Pillar Breakdown ---")
        for pillar in ['Pillar 1', 'Pillar 2', 'Pillar 3', 'Pillar 4']:
            print(f"   {pillar}: {best['details'][pillar]}/10")
        _print_weighted_math(best['details'])

        print(f"\n--- Trading Plan ---")
        print(f"   Entry Price (ask): ₹{best['entry']:.2f}")
        print(f"   Lot Size:          {lot_size}")
        print(f"   Confidence Tier:   {tier_info['label']} (Tier {tier_info['tier']})")
        print(f"   Optimal Lots:      {best['optimal_lots']} (tiered sizing)")
        print(f"   Capital Deployed:  ₹{best['capital']:.0f} ({tier_info['deploy_pct'] * 100:.0f}% of ₹{total_capital:,.0f})")
        print(f"   Target:            ₹{best['target']:.2f}")
        print(f"   Stoploss:          ₹{best['stoploss']:.2f}")
        print(f"   Risk:Reward:       1:{best['rr']:.2f}")
        print(f"   Stoploss Cap:      {tier_info['stoploss_cap_pct'] * 100:.1f}% of deployed")
        print(f"   Max Loss (gross):  ₹{best['details'].get('gross_risk', 0):.0f}")
        print(f"   Max Loss Cap:      ₹{settings.MAX_LOSS_ABS:.0f} absolute / ₹{best_trade_risk['allowed_risk']:.0f} allowed")
        print(f"   Est. Charges:      ₹{best['estimated_cost']:.2f}")
        print(f"   Net Profit (tgt):  ₹{best['net_profit']:.2f} (min ₹{settings.MIN_NET_PROFIT:.0f})")
        print(f"   ROC (after costs): {best['roc_after_cost']:.1f}%")

        if best['details'].get('stt_trap_warning'):
            print(f"\n   {best['details']['stt_trap_warning']}")

        execution_advisory = _build_execution_advisory(best['strategy'])
        best['execution_advisory'] = execution_advisory
        _print_execution_advisory(execution_advisory)

        print(f"\n--- Market Governance Context ---")
        fd = governance_context.get('fii_dii', {})
        sh = governance_context.get('source_health', {})
        print(f"   Snapshot UTC: {governance_context.get('timestamp_utc', 'n/a')}")
        vix_ok = '✅' if sh.get('vix_ok') else '⚠️ '
        fii_ok = '✅' if sh.get('fii_dii_ok') else '⚠️ '
        oi_ok  = '✅' if sh.get('participant_oi_ok') else '⚠️ '
        print(f"   Source Health: {vix_ok}VIX | {fii_ok}FII/DII | {oi_ok}Participant OI")
        print(f"   FII Net: ₹{fd.get('fii_net_cr', 'n/a')} Cr | DII Net: ₹{fd.get('dii_net_cr', 'n/a')} Cr | Regime: {fd.get('regime_hint', 'n/a')}")

        oi_ctx = best.get('oi_context', {})
        print(f"\n--- OI Context ---")
        print(f"   PCR:       {oi_ctx.get('pcr', 'n/a')}")
        print(f"   Max Pain:  ₹{oi_ctx.get('max_pain', 'n/a')}")
        print(f"   Put Wall:  ₹{oi_ctx.get('put_wall', 'n/a')} | Call Wall: ₹{oi_ctx.get('call_wall', 'n/a')}")
        if oi_ctx.get('pcr_warning'):
            print(f"   ⚠️  {oi_ctx['pcr_warning']}")

        if upcoming:
            print(f"\n--- Upcoming Events (next 10 days) ---")
            for ev in upcoming:
                print(f"   📅 {ev['name']} on {ev['date']} ({ev['days_away']}d away, {ev['blackout_days']}d blackout)")

        if recommendations[1:]:
            print(f"\n--- Runners-Up ---")
            for r in recommendations[1:4]:
                print(f"   {underlying} {int(r['strike'])} {r['type'].upper()} | Score={r['score']:.1f} | Δ={r['delta']:+.2f} | {r['strategy']}")

        print(_sep())
        return {
            'recommendation': best,
            'all_candidates': recommendations,
            'execution_advisory': execution_advisory,
            'gap': gap,
            'data_health': analyzer.data_health.signals,
            'circuit_breaker': cb_status,
            'timestamp': generated_at,
        }

    print(_sep())
    return {'recommendation': None, 'timestamp': generated_at}




# ---------------------------------------------------------------------------
# Market Scanner Loop
# ---------------------------------------------------------------------------
def get_absolute_recommendation(recommendations, capital_limit: float = None):
    """
    Selects the single best trade using hierarchical tie-breaking:
    1. Prefer stock debit spreads when capital < CREDIT_MIN_CAPITAL
    2. Highest 4-Pillar Score
    3. Highest Reward:Risk Ratio
    4. Lowest Capital Requirement (Efficiency)
    """
    if not recommendations:
        return None

    capital = capital_limit if capital_limit is not None else settings.TOTAL_CAPITAL
    index_syms = {'NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY'}
    debit_strats = {'bull_call_spread', 'bear_put_spread'}
    long_strats = {'long_call', 'long_put', 'long_option', 'long_strangle_candidate'}

    def _rank_key(x):
        underlying = (x.get('underlying') or '').upper()
        strategy = x.get('strategy') or ''
        is_index = underlying in index_syms
        is_debit = strategy in debit_strats
        is_long = strategy in long_strats

        # Lower is better for preference_penalty when capital is in compound phase
        preference_penalty = 0
        if capital < settings.CREDIT_MIN_CAPITAL:
            if is_index:
                preference_penalty += 2
            if is_long and not is_debit:
                preference_penalty += 1
            if not is_debit and strategy not in ('bull_put_spread', 'bear_call_spread'):
                # mild nudge toward debit over naked long
                if is_long:
                    preference_penalty += 0

        return (
            preference_penalty,
            -x['score'],
            -x.get('rr', 0),
            x.get('capital', 999999),
        )

    ranked = sorted(recommendations, key=_rank_key)
    return ranked[0]


def scan_market(capital=30000.0, budget_hard_limit=None, mode: str = "standard"):
    """
    Market Scanner — Wave 3 upgrade:
    - Estimation-based budget pre-filter (all eligible from Master 40, not just top 20).
    - Post-generation hard budget gate as safety net.
    - Absolute Pick logic with hierarchical tie-breaking.
    """
    import time
    start_time = datetime.now()

    # Mode config — credit NEVER unlocked by mode alone below CREDIT_MIN_CAPITAL
    mode = (mode or "standard").lower()
    if mode == "short_term":
        dte_preference = (10, 21)
    else:
        dte_preference = (21, 45)
    credit_enabled = _resolve_credit_enabled(capital, mode)

    # budget_hard_limit defaults to 100% of capital
    if budget_hard_limit is None:
        budget_hard_limit = capital

    # 1. Budget-Eligible Universe (Wave 3 — replaces top_n=20 cap)
    feed = DhanDataFeed()
    universe_builder = DynamicUniverseBuilder(feed)
    instruments = universe_builder.get_budget_eligible_instruments(budget=capital)

    print(_sep())
    print(f"🚀 STARTING MARKET SCAN — Budget-Eligible Instruments")
    print(f"Time: {start_time.strftime('%Y-%m-%d %H:%M:%S')} | Capital: ₹{capital:,.0f} | Hard Limit: ₹{budget_hard_limit:,.0f}")
    print(f"Credit strategies: {'ENABLED' if credit_enabled else 'LOCKED (compound phase / capital gate)'}")
    print(f"Instruments to scan: {len(instruments)} (budget-eligible universe)")
    print(_sep())

    # 2. Hoist Global Checks
    circuit = CircuitBreaker(total_capital=capital)
    cb_status = circuit.can_trade()
    if not cb_status['allowed']:
        print("\n🔴 SCAN ABORTED — Circuit Breaker / Hard Floor Triggered.")
        print(f"   Reason: {cb_status['reasons'][0]}")
        return

    # 3. Hoist Governance Fetch
    print("\n🌐 Fetching Market-wide Governance (VIX, FII/DII)...")
    gov_feed = MarketGovernanceFeed()
    snap = gov_feed.get_snapshot(symbol="NIFTY")
    gov_context = {
        'fii_dii': snap.fii_dii,
        'participant_oi': snap.participant_oi,
        'ban_mwpl': snap.ban_mwpl,
        'vix': snap.vix,
        'source_health': snap.source_health,
        'timestamp_utc': snap.timestamp_utc,
    }

    total_instruments = len(instruments)
    valid_recommendations = []

    for idx, inst in enumerate(instruments):
        symbol = inst['symbol']
        est_cap = inst.get('estimated_capital')
        est_str = f" ~₹{est_cap:,}" if est_cap else ""
        print(f"[{idx+1}/{total_instruments}] Scanning {symbol.ljust(12)}{est_str} ... ", end="", flush=True)

        try:
            res = generate_recommendation(
                underlying=symbol,
                security_id=inst['sec_id'],
                segment=inst['segment'],
                instrument_type=inst['type'],
                lot_size_default=inst['lot_size'],
                total_capital=capital,
                capital_limit=capital,
                pre_fetched_governance=gov_context,
                skip_global_checks=True,
                quiet=True,
                dte_preference=dte_preference,
                credit_enabled=credit_enabled,
            )

            if res and res.get('recommendation'):
                rec = res['recommendation']
                rec['underlying'] = symbol

                # Post-generation hard budget gate (safety net)
                if rec['capital'] > budget_hard_limit:
                    print(f"✖ Exceeds hard limit (₹{rec['capital']:,.0f})")
                else:
                    valid_recommendations.append(rec)
                    print(f"✅ Found! (Score: {rec['score']:.1f} | ₹{rec['capital']:,.0f})")
            else:
                reason = res.get('blocked_by') or "No setup"
                print(f"✖ {reason}")

        except Exception as e:
            print(f"⚠️ Error: {str(e)[:50]}")

        # Dhan API rate limit compliance
        time.sleep(3.2)

    # 4. Find and Display Absolute Recommendation
    elapsed = (datetime.now() - start_time).seconds
    print("\n" + _sep())
    print(f"🏁 SCAN COMPLETE — Scanned {total_instruments} instruments in {elapsed}s")
    print(_sep())

    if not valid_recommendations:
        print("\n✅ No high-probability trades meeting budget criteria found today.")
        print(
            f"   Compound gates: max loss ≤ ₹{settings.MAX_LOSS_ABS:.0f}, "
            f"min net ≥ ₹{settings.MIN_NET_PROFIT:.0f}, "
            f"ROC ≥ {settings.MIN_ROC_HARD * 100:.0f}%"
        )
    else:
        absolute_pick = get_absolute_recommendation(valid_recommendations, capital_limit=capital)
        
        print(f"\n🏆 ABSOLUTE RECOMMENDATION OF THE DAY")
        print(_sep('-'))
        print(f"   SYMBOL:   {absolute_pick['underlying']} {int(absolute_pick['strike'])} {absolute_pick['type'].upper()}")
        print(f"   SCORE:    {absolute_pick['score']:.2f}/10")
        print(f"   STRATEGY: {absolute_pick['strategy']}")
        print(f"   PLAN:     Entry ₹{absolute_pick['entry']:.2f} | Target ₹{absolute_pick['target']:.2f} | SL ₹{absolute_pick['stoploss']:.2f}")
        print(f"   RATIONALE: High {absolute_pick['iv_regime']} probability with 1:{absolute_pick['rr']:.1f} R:R")
        print(f"   CAPITAL:  ₹{absolute_pick['capital']:,.0f} (within ₹{budget_hard_limit:,.0f} limit)")
        print(f"   EST P&L:  ₹{absolute_pick['net_profit']:,.0f} ({absolute_pick['roc_after_cost']:.1f}% ROC)")
        print(f"   MAX LOSS: ₹{absolute_pick.get('gross_risk', absolute_pick.get('details', {}).get('gross_risk', 0)):,.0f} (cap ₹{settings.MAX_LOSS_ABS:.0f})")
        print(_sep('-'))

        # Show runners up briefly
        runners_up = [r for r in valid_recommendations if r != absolute_pick]
        if runners_up:
            print(f"\n📊 Runners-up:")
            for r in sorted(runners_up, key=lambda x: x['score'], reverse=True)[:2]:
                print(f"   • {r['underlying']} {int(r['strike'])} {r['type'].upper()} (Score: {r['score']:.1f})")

    print("\n" + _sep())


def run_pipeline(underlying: str = 'NIFTY', capital_limit: float = 30000.0, mode: str = "standard"):
    """
    Entry point for the trading pipeline.
    If underlying is 'ALL', runs a full market scan.
    Otherwise, runs analysis for the specific symbol.
    """
    if underlying.upper() == 'ALL':
        return scan_market(capital=capital_limit, mode=mode)
    
    # Map symbol to parameters
    from src.adapters.universe import MASTER_40_LIST
    inst = next((x for x in MASTER_40_LIST if x['symbol'] == underlying.upper()), None)
    
    if not inst:
        logger.error("Symbol %s not found in MASTER_40_LIST", underlying)
        return {'recommendation': None, 'error': f"Unknown symbol: {underlying}"}
    
    return generate_recommendation(
        underlying=inst['symbol'],
        security_id=inst['sec_id'],
        segment=inst['segment'],
        instrument_type=inst['type'],
        lot_size_default=inst['lot_size'],
        capital_limit=capital_limit,
        total_capital=capital_limit,
        dte_preference=(10, 21) if (mode or "").lower() == "short_term" else (21, 45),
        credit_enabled=_resolve_credit_enabled(capital_limit, mode),
    )


if __name__ == "__main__":
    # Scan all budget-eligible instruments from Master 40 with 30k capital
    scan_market(capital=30000.0)
