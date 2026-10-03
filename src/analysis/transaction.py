"""
Transaction Cost Model for Indian Options (Dhan Broker)

Cost Components (Per Order / Per Leg):
  - Brokerage: ₹20 per executed order (flat Dhan fee for Equity Options)
  - STT: 0.15% on sell-side premium × Lot Size (option sell transactions only)
  - Exchange Charges: 0.0495% on premium × Lot Size (both buy and sell sides)
  - GST: 18% on (Brokerage + Exchange + SEBI)
  - SEBI Turnover Charge: 0.0001% on premium × Lot Size (both sides)
  - Stamp Duty: 0.003% on buy-side premium × Lot Size (buy side only)

STT Trap Warning:
  If ITM option held to expiry, STT = 0.125% of settlement price × lot size.
  Always close ITM positions before 3:00 PM on expiry day.

Fix B-NEW-3: spread cost estimation now accepts per-leg pricing.
"""

MIN_COST_BUFFER_SINGLE_LEG = 150   # ₹
MIN_COST_BUFFER_TWO_LEG = 400      # ₹


def estimate_buy_cost(premium: float, lot_size: int) -> float:
    """Estimate cost for buying 1 lot of options (buy-side only)."""
    turnover = premium * lot_size
    brokerage = 20.0
    exchange_charges = turnover * 0.000495
    sebi_charge = turnover * 0.000001
    gst = (brokerage + exchange_charges + sebi_charge) * 0.18
    stamp_duty = turnover * 0.00003
    stt = 0.0
    return round(brokerage + exchange_charges + sebi_charge + gst + stamp_duty + stt, 2)


def estimate_sell_cost(premium: float, lot_size: int) -> float:
    """Estimate cost for selling 1 lot of options (sell-side only)."""
    turnover = premium * lot_size
    brokerage = 20.0
    exchange_charges = turnover * 0.000495
    sebi_charge = turnover * 0.000001
    gst = (brokerage + exchange_charges + sebi_charge) * 0.18
    stamp_duty = 0.0
    stt = turnover * 0.0015
    return round(brokerage + exchange_charges + sebi_charge + gst + stamp_duty + stt, 2)


def estimate_round_trip_cost(
    entry_price: float,
    exit_price: float,
    lot_size: int,
    num_legs: int = 1,
    leg_prices: list | None = None,
) -> float:
    """
    Estimate total round-trip transaction costs for a strategy.

    Fix B-NEW-3: For spread strategies, pass leg_prices as a list of
    (entry_premium, exit_premium) tuples — one per leg. This avoids the
    incorrect assumption that all legs share the same premium.

    Args:
        entry_price: Entry price for single-leg strategies.
        exit_price: Exit price for single-leg strategies.
        lot_size: Number of shares per lot.
        num_legs: Number of legs (used only when leg_prices is None).
        leg_prices: Optional list of (entry, exit) tuples per leg.

    Returns:
        Total estimated round-trip cost in ₹
    """
    if leg_prices:
        total = 0.0
        for leg_entry, leg_exit in leg_prices:
            total += estimate_buy_cost(leg_entry, lot_size)
            total += estimate_sell_cost(leg_exit, lot_size)
        return round(total, 2)

    # Single-leg path (unchanged for backward compat)
    buy_cost = estimate_buy_cost(entry_price, lot_size)
    sell_cost = estimate_sell_cost(exit_price, lot_size)
    return round((buy_cost + sell_cost) * num_legs, 2)


def get_min_cost_buffer(num_legs: int = 1) -> float:
    """Get the minimum cost buffer for a strategy."""
    if num_legs <= 1:
        return MIN_COST_BUFFER_SINGLE_LEG
    return MIN_COST_BUFFER_TWO_LEG


def estimate_stt_trap_cost(settlement_price: float, lot_size: int) -> float:
    """
    Estimate the STT cost if an ITM option is held to expiry.

    WARNING: STT on exercised ITM options = 0.125% of settlement price × lot size.
    This can be devastating. Always close before 3:00 PM on expiry day.
    """
    return round(settlement_price * lot_size * 0.00125, 2)


def cost_breakdown(entry_price: float, exit_price: float, lot_size: int) -> dict:
    """Provide a detailed breakdown of all transaction cost components."""
    buy_turnover = entry_price * lot_size
    sell_turnover = exit_price * lot_size

    breakdown = {
        'buy_side': {
            'brokerage': 20.0,
            'exchange_charges': round(buy_turnover * 0.000495, 2),
            'sebi_charge': round(buy_turnover * 0.000001, 2),
            'gst': round((20.0 + buy_turnover * 0.000495 + buy_turnover * 0.000001) * 0.18, 2),
            'stamp_duty': round(buy_turnover * 0.00003, 2),
            'stt': 0.0,
        },
        'sell_side': {
            'brokerage': 20.0,
            'exchange_charges': round(sell_turnover * 0.000495, 2),
            'sebi_charge': round(sell_turnover * 0.000001, 2),
            'gst': round((20.0 + sell_turnover * 0.000495 + sell_turnover * 0.000001) * 0.18, 2),
            'stamp_duty': 0.0,
            'stt': round(sell_turnover * 0.0015, 2),
        },
    }

    breakdown['buy_total'] = round(sum(breakdown['buy_side'].values()), 2)
    breakdown['sell_total'] = round(sum(breakdown['sell_side'].values()), 2)
    breakdown['round_trip_total'] = round(breakdown['buy_total'] + breakdown['sell_total'], 2)
    return breakdown


if __name__ == "__main__":
    entry, exit_price, lot = 200.0, 300.0, 25
    print("=" * 60)
    print("TRANSACTION COST ESTIMATOR — Dhan Broker (NSE Options)")
    print("=" * 60)
    print(f"\nExample: NIFTY Option | Entry: ₹{entry} | Exit: ₹{exit_price} | Lot: {lot}")
    print(f"\nRound-Trip Cost (1 leg):  ₹{estimate_round_trip_cost(entry, exit_price, lot, 1)}")
    print(f"Round-Trip Cost (2 legs): ₹{estimate_round_trip_cost(entry, exit_price, lot, 2)}")
    print(f"\nSpread example (leg1=200/300, leg2=150/180):")
    print(f"  ₹{estimate_round_trip_cost(entry, exit_price, lot, leg_prices=[(200,300),(150,180)])}")
    print(f"\nMin Cost Buffer (1 leg):  ₹{get_min_cost_buffer(1)}")
    print(f"Min Cost Buffer (2 legs): ₹{get_min_cost_buffer(2)}")
