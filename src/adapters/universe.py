import os
import json
import time
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

from dhanhq import dhanhq as DhanHQ

logger = logging.getLogger(__name__)

# Curated list of 40 most liquid/popular F&O instruments on NSE.
# Ranked daily by DynamicUniverseBuilder.
MASTER_40_LIST = [
    {"symbol": "NIFTY", "sec_id": 13, "segment": "IDX_I", "type": "INDEX", "lot_size": 25},
    {"symbol": "BANKNIFTY", "sec_id": 25, "segment": "IDX_I", "type": "INDEX", "lot_size": 15},
    {"symbol": "FINNIFTY", "sec_id": 27, "segment": "IDX_I", "type": "INDEX", "lot_size": 25},
    {"symbol": "MIDCPNIFTY", "sec_id": 50, "segment": "IDX_I", "type": "INDEX", "lot_size": 50},
    {"symbol": "RELIANCE", "sec_id": 2885, "segment": "NSE_EQ", "type": "EQUITY", "lot_size": 250},
    {"symbol": "HDFCBANK", "sec_id": 1333, "segment": "NSE_EQ", "type": "EQUITY", "lot_size": 550},
    {"symbol": "ICICIBANK", "sec_id": 4963, "segment": "NSE_EQ", "type": "EQUITY", "lot_size": 700},
    {"symbol": "INFY", "sec_id": 1594, "segment": "NSE_EQ", "type": "EQUITY", "lot_size": 400},
    {"symbol": "TCS", "sec_id": 11536, "segment": "NSE_EQ", "type": "EQUITY", "lot_size": 175},
    {"symbol": "SBIN", "sec_id": 3045, "segment": "NSE_EQ", "type": "EQUITY", "lot_size": 750},
    {"symbol": "BHARTIARTL", "sec_id": 10604, "segment": "NSE_EQ", "type": "EQUITY", "lot_size": 475},
    {"symbol": "AXISBANK", "sec_id": 5900, "segment": "NSE_EQ", "type": "EQUITY", "lot_size": 625},
    {"symbol": "BAJFINANCE", "sec_id": 317, "segment": "NSE_EQ", "type": "EQUITY", "lot_size": 125},
    {"symbol": "KOTAKBANK", "sec_id": 1922, "segment": "NSE_EQ", "type": "EQUITY", "lot_size": 400},
    {"symbol": "LT", "sec_id": 11483, "segment": "NSE_EQ", "type": "EQUITY", "lot_size": 150},
    {"symbol": "ITC", "sec_id": 1660, "segment": "NSE_EQ", "type": "EQUITY", "lot_size": 1600},
    {"symbol": "TATAMOTORS", "sec_id": 3456, "segment": "NSE_EQ", "type": "EQUITY", "lot_size": 712},
    {"symbol": "MARUTI", "sec_id": 10999, "segment": "NSE_EQ", "type": "EQUITY", "lot_size": 50},
    {"symbol": "SUNPHARMA", "sec_id": 3351, "segment": "NSE_EQ", "type": "EQUITY", "lot_size": 350},
    {"symbol": "HUL", "sec_id": 1330, "segment": "NSE_EQ", "type": "EQUITY", "lot_size": 150},
    {"symbol": "ADANIENT", "sec_id": 15083, "segment": "NSE_EQ", "type": "EQUITY", "lot_size": 150},
    {"symbol": "NTPC", "sec_id": 24948, "segment": "NSE_EQ", "type": "EQUITY", "lot_size": 1500},
    {"symbol": "POWERGRID", "sec_id": 14977, "segment": "NSE_EQ", "type": "EQUITY", "lot_size": 1800},
    {"symbol": "ONGC", "sec_id": 2475, "segment": "NSE_EQ", "type": "EQUITY", "lot_size": 1925},
    {"symbol": "COALINDIA", "sec_id": 2031, "segment": "NSE_EQ", "type": "EQUITY", "lot_size": 2100},
    {"symbol": "JSWSTEEL", "sec_id": 11723, "segment": "NSE_EQ", "type": "EQUITY", "lot_size": 200},
    {"symbol": "JIOFIN", "sec_id": 242, "segment": "NSE_EQ", "type": "EQUITY", "lot_size": 1250},
    {"symbol": "BAJAJ-AUTO", "sec_id": 16669, "segment": "NSE_EQ", "type": "EQUITY", "lot_size": 125},
    {"symbol": "DRREDDY", "sec_id": 881, "segment": "NSE_EQ", "type": "EQUITY", "lot_size": 125},
    {"symbol": "WIPRO", "sec_id": 3787, "segment": "NSE_EQ", "type": "EQUITY", "lot_size": 1500},
    {"symbol": "TECHM", "sec_id": 13538, "segment": "NSE_EQ", "type": "EQUITY", "lot_size": 600},
    {"symbol": "ADANIPORTS", "sec_id": 1503, "segment": "NSE_EQ", "type": "EQUITY", "lot_size": 200},
    {"symbol": "GRASIM", "sec_id": 1232, "segment": "NSE_EQ", "type": "EQUITY", "lot_size": 225},
    {"symbol": "HINDALCO", "sec_id": 1363, "segment": "NSE_EQ", "type": "EQUITY", "lot_size": 1400},
    {"symbol": "TATACONSUM", "sec_id": 3432, "segment": "NSE_EQ", "type": "EQUITY", "lot_size": 450},
    {"symbol": "BPCL", "sec_id": 526, "segment": "NSE_EQ", "type": "EQUITY", "lot_size": 900},
    {"symbol": "APOLLOHOSP", "sec_id": 157, "segment": "NSE_EQ", "type": "EQUITY", "lot_size": 125},
    {"symbol": "ASIANPAINT", "sec_id": 236, "segment": "NSE_EQ", "type": "EQUITY", "lot_size": 200},
    {"symbol": "TITAN", "sec_id": 3506, "segment": "NSE_EQ", "type": "EQUITY", "lot_size": 175},
    {"symbol": "ULTRACEMCO", "sec_id": 11523, "segment": "NSE_EQ", "type": "EQUITY", "lot_size": 100},
]

class DynamicUniverseBuilder:
    def __init__(self, data_feed, cache_path=os.path.join("data", "cache", "universe_cache.json")):
        self.feed = data_feed
        self.cache_path = cache_path
        self.master_list = MASTER_40_LIST

        self._fno_prefilter_cache_path = Path("data/cache/fno_universe_prefilter_cache.json")
        self._security_list_csv_path = Path("data/cache/security_id_list.csv")

    def _load_valid_cache(self):
        if not os.path.exists(self.cache_path):
            return None
        try:
            with open(self.cache_path, 'r') as f:
                data = json.load(f)
            
            cache_date = data.get('date')
            today = datetime.now().strftime('%Y-%m-%d')
            
            if cache_date == today:
                logger.info("Found valid universe cache for %s", today)
                return data.get('universe')
        except Exception as e:
            logger.warning("Error reading cache: %s", e)
        return None

    def _save_cache(self, universe):
        try:
            data = {
                'date': datetime.now().strftime('%Y-%m-%d'),
                'universe': universe
            }
            os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
            with open(self.cache_path, 'w') as f:
                json.dump(data, f, indent=4)
            logger.info("Saved fresh universe cache.")
        except Exception as e:
            logger.warning("Error saving cache: %s", e)

    def get_top_liquid_instruments(self, top_n=20):
        """
        Returns top N instruments ranked by average turnover (5-day).
        Uses cache to avoid hitting Dhan rate limits (1 req / 3s).
        """
        cached_universe = self._load_valid_cache()
        if cached_universe:
            return cached_universe[:top_n]

        logger.info("No valid cache found. Refreshing top liquid instruments (40 items)...")
        logger.info("This will take ~2 minutes due to rate limiting (3.2s per symbol).")
        
        ranked_list = []
        for i, instrument in enumerate(self.master_list):
            logger.info("[%d/40] Fetching turnover for %s...", i+1, instrument['symbol'])
            
            turnover = self.feed.get_turnover_approximation(
                security_id=instrument['sec_id'],
                segment=instrument['segment'],
                instrument_type=instrument['type']
            )
            
            item = instrument.copy()
            item['turnover'] = turnover
            ranked_list.append(item)
            
            # Rate limit compliance (3.2s)
            if i < len(self.master_list) - 1:
                time.sleep(3.2)

        # Sort by turnover descending
        ranked_list.sort(key=lambda x: x.get('turnover', 0), reverse=True)
        
        self._save_cache(ranked_list)
        return ranked_list[:top_n]

    def _get_cached_spot(self, symbol: str):
        """
        Retrieve last known spot price for a symbol from the universe cache.
        Used by the estimation-based budget pre-filter to avoid extra API calls.
        Returns None if not found.
        """
        try:
            if not os.path.exists(self.cache_path):
                return None
            with open(self.cache_path, 'r') as f:
                data = json.load(f)
            universe = data.get('universe', data) if isinstance(data, dict) else data
            for item in universe:
                if isinstance(item, dict) and item.get('symbol') == symbol:
                    return item.get('spot_price') or item.get('last_close')
        except Exception:
            pass
        return None

    def get_budget_eligible_instruments(self, budget: float):
        """
        Build a F&O underlyings universe within budget.

        This replaces the hard-coded `MASTER_40_LIST` scan limitation by:
          - downloading DhanHQ's security master CSV (no auth required)
          - extracting all option underlyings (OPTIDX/OPTSTK)
          - estimating nearest-expiry "ATM" spot from strike median
          - estimating 1-lot capital = lot_size * spot_estimate * premium_pct

        Output fields match what `scan_market()` expects:
          - symbol, sec_id, segment, type, lot_size, estimated_capital
        """
        today = datetime.now().strftime("%Y-%m-%d")

        # 1) Load precomputed underlyings (daily)
        if self._fno_prefilter_cache_path.exists():
            try:
                cached = json.loads(self._fno_prefilter_cache_path.read_text(encoding="utf-8"))
                if cached.get("date") == today and isinstance(cached.get("universe"), list):
                    universe_all = cached["universe"]
                    return [u for u in universe_all if u.get("estimated_capital") is not None and u["estimated_capital"] <= budget]
            except Exception:
                pass

        logger.info("Building F&O universe prefilter from dhanhq security master (one-time per day)...")
        cache_dir = self._security_list_csv_path.parent
        cache_dir.mkdir(parents=True, exist_ok=True)

        # 2) Download security master CSV (cached on disk)
        #    Note: dhanhq fetch_security_list downloads from a static URL; no token required.
        df = DhanHQ.fetch_security_list(mode="compact", filename=str(self._security_list_csv_path))
        if df is None or df.empty:
            logger.error("Failed to fetch security master CSV; falling back to MASTER_40_LIST.")
            # Fallback: keep prior behavior
            eligible = []
            for inst in self.master_list:
                is_index = inst.get("type") == "INDEX"
                spot = self._get_cached_spot(inst['symbol'])
                if spot is None:
                    continue
                premium_pct = 0.01 if is_index else 0.03
                estimated_capital = inst['lot_size'] * spot * premium_pct
                if estimated_capital <= budget:
                    inst_copy = inst.copy()
                    inst_copy['estimated_capital'] = round(estimated_capital)
                    inst_copy['spot_estimate'] = spot
                    eligible.append(inst_copy)
            return eligible

        # 3) Build underlying lookup maps
        index_rows = df[(df.get("SEM_INSTRUMENT_NAME") == "INDEX") & df.get("SEM_TRADING_SYMBOL").notna()]
        eq_rows = df[(df.get("SEM_INSTRUMENT_NAME") == "EQUITY") & df.get("SEM_TRADING_SYMBOL").notna()]

        index_map = {}
        for sym, sid in index_rows[["SEM_TRADING_SYMBOL", "SEM_SMST_SECURITY_ID"]].dropna().itertuples(index=False):
            index_map[str(sym)] = int(sid)

        eq_map = {}
        for sym, sid in eq_rows[["SEM_TRADING_SYMBOL", "SEM_SMST_SECURITY_ID"]].dropna().itertuples(index=False):
            eq_map[str(sym)] = int(sid)

        # 4) Extract option contract rows and compute per-underlying nearest expiry "ATM" estimate
        opt = df[df.get("SEM_INSTRUMENT_NAME").isin(["OPTIDX", "OPTSTK"])].copy()
        if opt.empty:
            logger.error("Security master parsing found no option contracts; falling back to MASTER_40_LIST.")
            return self.master_list[:]

        opt["underlying"] = opt["SEM_TRADING_SYMBOL"].astype(str).str.split("-").str[0]
        opt["exp_date"] = pd.to_datetime(opt.get("SEM_EXPIRY_DATE"), errors="coerce")
        opt["strike"] = pd.to_numeric(opt.get("SEM_STRIKE_PRICE"), errors="coerce")
        opt["lot_units"] = pd.to_numeric(opt.get("SEM_LOT_UNITS"), errors="coerce")

        opt = opt[opt["exp_date"].notna() & opt["strike"].notna() & (opt["strike"] > 0) & opt["lot_units"].notna()]
        if opt.empty:
            logger.error("Option contract subset empty after cleaning; falling back to MASTER_40_LIST.")
            return self.master_list[:]

        # Nearest expiry per underlying (vectorized)
        nearest_exp = opt.groupby("underlying")["exp_date"].min()
        opt["nearest_exp"] = opt["underlying"].map(nearest_exp)
        nearest = opt[opt["exp_date"] == opt["nearest_exp"]].copy()

        # Aggregate to get an approximate spot estimate and lot size
        agg = (
            nearest.groupby("underlying")
            .agg(
                spot_estimate=("strike", "median"),
                lot_size=("lot_units", "median"),
            )
            .reset_index()
        )

        universe_all = []
        for _, row in agg.iterrows():
            sym = str(row["underlying"])
            spot_est = float(row["spot_estimate"])
            lot_size = float(row["lot_size"])
            if spot_est <= 0 or lot_size <= 0:
                continue

            if sym in index_map:
                sec_id = int(index_map[sym])
                utype = "INDEX"
                segment = "IDX_I"
                premium_pct = 0.01
            elif sym in eq_map:
                sec_id = int(eq_map[sym])
                utype = "EQUITY"
                segment = "NSE_EQ"
                premium_pct = 0.03
            else:
                continue

            estimated_capital = lot_size * spot_est * premium_pct
            if estimated_capital <= 0:
                continue

            universe_all.append({
                "symbol": sym,
                "sec_id": sec_id,
                "segment": segment,
                "type": utype,
                "lot_size": int(round(lot_size)),
                "spot_estimate": spot_est,
                "estimated_capital": int(round(estimated_capital)),
            })

        # Cache for the day
        try:
            payload = {"date": today, "universe": universe_all}
            self._fno_prefilter_cache_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning("Could not cache universe prefilter: %s", e)

        eligible = [u for u in universe_all if u.get("estimated_capital") is not None and u["estimated_capital"] <= budget]
        logger.info("F&O prefilter: %d underlyings eligible (budget ₹%d)", len(eligible), budget)
        return eligible


if __name__ == "__main__":
    from src.adapters.dhan_feed import DhanDataFeed
    logging.basicConfig(level=logging.INFO)
    feed = DhanDataFeed()
    builder = DynamicUniverseBuilder(feed)
    top_20 = builder.get_top_liquid_instruments(20)
    print("\n--- TOP 20 LIQUID INSTRUMENTS ---")
    for item in top_20:
        print(f"{item['symbol'].ljust(12)}: ₹{item.get('turnover', 0):,.0f} avg turnover")

