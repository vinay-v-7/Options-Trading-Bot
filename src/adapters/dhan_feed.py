import os
import logging
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from dotenv import load_dotenv
from dhanhq import dhanhq, DhanContext
from src.common.resilience import EODCache  # DATA: EOD cache integration for OI snapshot delta

logger = logging.getLogger(__name__)


class DhanDataFeed:
    def __init__(self):
        load_dotenv()
        self.client_id = os.getenv("DHAN_CLIENT_ID")
        self.access_token = os.getenv("DHAN_ACCESS_TOKEN")
        if not self.client_id or not self.access_token:
            raise ValueError("DHAN_CLIENT_ID or DHAN_ACCESS_TOKEN missing in .env")

        self.ctx = DhanContext(self.client_id, self.access_token)
        self.dhan = dhanhq(self.ctx)

    def get_lot_size(self, security_id=13, segment='IDX_I', default=25):
        """
        Fetch current lot size where available; fallback safely to provided default.
        """
        try:
            security_id = int(security_id)
            expiry = self.get_best_expiry(security_id=security_id, segment=segment)
            if not expiry:
                return default

            chain = self.get_option_chain(security_id=security_id, segment=segment, expiry=expiry)
            if not chain:
                return default

            for key in ['lot_size', 'lotsize', 'lotSize']:
                if key in chain and chain[key]:
                    return int(chain[key])

            meta = chain.get('meta', {}) if isinstance(chain, dict) else {}
            for key in ['lot_size', 'lotsize', 'lotSize']:
                if key in meta and meta[key]:
                    return int(meta[key])

        except Exception as e:
            logger.debug("Unable to fetch dynamic lot size (%s). Using default=%d.", e, default)

        return default

    def get_historical_data(self, security_id='13', segment='IDX_I', instrument_type='INDEX', days=60):
        """
        Fetch historical daily data and calculate technicals.

        Indicators computed (Fixes 2, B-NEW-1):
        - 9-EMA, 20-SMA, HV (existing)
        - RSI (14-period Wilder smoothing) — Phase 0 Fix 2
        - VWAP daily approximation (H+L+C)/3 × Volume weighted — Phase 0 Fix 2
        """
        to_date = datetime.now().strftime('%Y-%m-%d')
        from_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')

        resp = self.dhan.historical_daily_data(
            security_id=str(security_id),  # DATA: Normalise security_id to string for historical API
            exchange_segment=segment,
            instrument_type=instrument_type,
            from_date=from_date,
            to_date=to_date
        )

        if resp['status'] == 'success':
            df = pd.DataFrame(resp['data'])
            df['date'] = pd.to_datetime(df['timestamp'], unit='s')

            # --- Trend indicators ---
            df['9_EMA'] = df['close'].ewm(span=9, adjust=False).mean()
            df['20_SMA'] = df['close'].rolling(window=20).mean()

            # --- Historical Volatility (Annualised) ---
            df['returns'] = np.log(df['close'] / df['close'].shift(1))
            df['HV'] = df['returns'].rolling(window=20).std() * np.sqrt(252) * 100

            # --- RSI (14-period, Wilder smoothing) — Fix 2 ---
            delta = df['close'].diff()
            gain = delta.where(delta > 0, 0.0).ewm(alpha=1 / 14, adjust=False).mean()
            loss = (-delta.where(delta < 0, 0.0)).ewm(alpha=1 / 14, adjust=False).mean()
            rs = gain / loss.replace(0, np.nan)
            df['RSI'] = 100 - (100 / (1 + rs))
            df['RSI'] = df['RSI'].fillna(50)  # neutral fallback on cold-start rows

            # --- VWAP daily approximation — Fix 2 ---
            # Uses (H+L+C)/3 as single-day typical price proxy (not cumulative)
            if 'volume' in df.columns and df['volume'].sum() > 0:
                # CALC: VWAP — single-day typical price proxy (H+L+C)/3
                df['VWAP'] = (df['high'] + df['low'] + df['close']) / 3
            else:
                # Fallback: VWAP ≈ 20-SMA when volume unavailable
                df['VWAP'] = df['20_SMA']

            logger.info("Historical data fetched: %d rows. Last close=%.2f, RSI=%.1f, VWAP=%.2f",
                        len(df), df.iloc[-1]['close'], df.iloc[-1]['RSI'], df.iloc[-1]['VWAP'])
            return df
        else:
            logger.error("Error fetching historical data: %s", resp)
            return None

    def get_best_expiry(self, security_id=13, segment='IDX_I', dte_range=(30, 45)):
        """
        Find the best expiry within a target DTE range.

        Fixes (C6, Fix 8):
        - Fallback expiry has a 5-day DTE floor (no 1-DTE weeklies)
        - Monthly expiries preferred over weeklies when both in 30-45 DTE range
        """
        security_id = int(security_id)  # DATA: Normalise security_id to int for expiry API
        resp = self.dhan.expiry_list(under_security_id=security_id, under_exchange_segment=segment)
        if resp['status'] != 'success':
            logger.error("Expiry list call failed: %s", resp)
            return None

        json_data = resp['data']
        if isinstance(json_data, dict) and 'data' in json_data:
            expiries = json_data['data']
        else:
            expiries = json_data

        today = datetime.now().date()
        logger.debug("Found %d expiries. Today is %s", len(expiries), today)
        valid_expiries = []

        for exp_str in expiries:
            if not isinstance(exp_str, str):
                continue
            try:
                exp_date = datetime.strptime(exp_str, '%Y-%m-%d').date()
            except ValueError:
                continue
            dte = (exp_date - today).days
            if dte_range[0] <= dte <= dte_range[1]:
                valid_expiries.append((exp_str, dte, exp_date))

        if valid_expiries:
            # Prefer monthly expiries: monthly = last Thursday of the month.
            # A simple heuristic: expiry day is the last Thursday
            def _is_monthly(exp_date):
                """True if exp_date is in the last 7 days of its month (monthly expiry heuristic)."""
                import calendar
                last_day = calendar.monthrange(exp_date.year, exp_date.month)[1]
                return exp_date.day >= last_day - 6

            monthly = [(e, dte, d) for e, dte, d in valid_expiries if _is_monthly(d)]
            preferred = monthly if monthly else valid_expiries
            preferred_sorted = sorted(preferred, key=lambda x: x[1])
            chosen = preferred_sorted[0][0]
            logger.info("Best expiry: %s (DTE=%d, monthly=%s)", chosen, preferred_sorted[0][1], bool(monthly))
            return chosen

        # Fallback: nearest future with DTE floor of 5 (no near-expiry weeklies)
        fallback_min = max(5, int(dte_range[0]))
        logger.warning(
            "No expiry in target DTE range %s–%s. Attempting fallback with DTE >= %s.",
            dte_range[0],
            dte_range[1],
            fallback_min,
        )
        future_expiries = []
        for exp_str in expiries:
            if not isinstance(exp_str, str):
                continue
            try:
                exp_date = datetime.strptime(exp_str, '%Y-%m-%d').date()
                dte = (exp_date - today).days
                if fallback_min <= dte <= 90:
                    future_expiries.append((exp_str, dte))
            except ValueError:
                continue

        if future_expiries:
            future_expiries.sort(key=lambda x: x[1])
            logger.info("Fallback expiry: %s (DTE=%d)", future_expiries[0][0], future_expiries[0][1])
            return future_expiries[0][0]

        logger.error("No valid expiry found.")
        return None

    def get_option_chain(self, security_id=13, segment='IDX_I', expiry=None):
        """
        Fetch option chain for the specified expiry.
        Caches per-instrument per-expiry to optimise API throughput.
        Computes change-in-OI by diffing current vs cached snapshot.
        """
        security_id = int(security_id)
        if not expiry:
            expiry = self.get_best_expiry(security_id, segment)
            if not expiry:
                return None

        CACHE_KEY = f"OC_{security_id}_{expiry}"
        # We check for a very fresh cache (e.g., 2 minutes) to avoid redundant API hits in a single scan loop
        fresh_cached = EODCache.get_cached_bias(CACHE_KEY, max_age_days=0.0014) # ~2 minutes
        if fresh_cached:
            logger.debug("Using very fresh cached option chain for %s", expiry)
            return fresh_cached

        # Get older cache for delta computation
        old_cached = EODCache.get_cached_bias(CACHE_KEY, max_age_days=1.0)

        logger.info("Fetching fresh option chain for %s...", expiry)
        resp = self.dhan.option_chain(under_security_id=security_id, under_exchange_segment=segment, expiry=expiry)
        
        if resp['status'] == 'success':
            json_data = resp['data']
            current_data = json_data.get('data', json_data) if isinstance(json_data, dict) else json_data
            
            # DATA: Compute change-in-OI by diffing current vs cached snapshot
            if old_cached and isinstance(old_cached, list) and isinstance(current_data, list):
                prev_oi_map = { (d['strike_price'], d['option_type']): d['open_interest'] for d in old_cached }
                for item in current_data:
                    key = (item['strike_price'], item['option_type'])
                    if key in prev_oi_map:
                        item['change_in_oi'] = item['open_interest'] - prev_oi_map[key]
                    else:
                        item['change_in_oi'] = 0
            
            EODCache.save(CACHE_KEY, current_data)
            return current_data
        
        # Fallback to stale cache if API fails
        if old_cached:
            logger.warning("API failed, falling back to stale cache for %s", expiry)
            return old_cached
            
        return None

    def get_turnover_approximation(self, security_id, segment, instrument_type='EQUITY'):
        """
        Fetch last 5 days of data to compute average daily turnover (Volume * Close).
        Used for liquidity ranking in the DynamicUniverseBuilder.
        """
        df = self.get_historical_data(security_id=security_id, segment=segment, 
                                     instrument_type=instrument_type, days=5)
        if df is not None and not df.empty:
            # Turnover = Avg(Close * Volume)
            turnover = (df['close'] * df['volume']).mean()
            return turnover
        return 0


if __name__ == "__main__":
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO, format='%(levelname)s %(name)s: %(message)s')
    feed = DhanDataFeed()

    hist = feed.get_historical_data()
    if hist is not None:
        print("Historical Technicals (Latest):")
        cols = ['date', 'close', '9_EMA', '20_SMA', 'HV', 'RSI', 'VWAP']
        existing = [c for c in cols if c in hist.columns]
        print(hist[existing].tail(1).to_string())

    chain = feed.get_option_chain()
    if chain:
        oc = chain.get('oc', {})
        print(f"\nSuccessfully fetched option chain with {len(oc)} strikes.")
