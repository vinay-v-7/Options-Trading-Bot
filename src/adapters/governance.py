"""
Market governance feed for Indian derivatives context.

Primary focus:
- India VIX (regime foundation for all strategy decisions)
- FII/DII daily cash flows (regime context)
- Participant-wise derivatives OI (positioning context)
- F&O ban list and MWPL status (tradeability gate)

Design goals:
- Official-source first (NSE / NSE Clearing endpoints/pages)
- Defensive parsing with graceful degradation
- Runtime-safe output (never raise hard exceptions to strategy engine)
"""

from __future__ import annotations

import json
import logging
import os
import pandas as pd
import yfinance as yf
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen
from src.common.resilience import FallbackExecutor, EODCache

logger = logging.getLogger(__name__)

NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}


@dataclass
class GovernanceSnapshot:
    timestamp_utc: str
    fii_dii: dict
    participant_oi: dict
    ban_mwpl: dict
    vix: dict
    source_health: dict


class MarketGovernanceFeed:
    def __init__(self, timeout: int = 8):
        self.timeout = timeout

    def _fetch_text(self, url: str) -> str:
        req = Request(url, headers=NSE_HEADERS)
        with urlopen(req, timeout=self.timeout) as resp:
            return resp.read().decode("utf-8", errors="ignore")

    def _fetch_json(self, url: str) -> dict:
        return json.loads(self._fetch_text(url))

    def _try_json_sources(self, urls: list[str]) -> tuple[dict | None, str | None, str | None]:
        last_err = "no source configured"
        for url in urls:
            try:
                payload = self._fetch_json(url)
                return payload, url, None
            except Exception as e:  # noqa: BLE001
                last_err = str(e)
                logger.debug("Source %s failed: %s", url, last_err)
        return None, None, last_err

    # ------------------------------------------------------------------
    # India VIX  (Fix 1 — Phase 0)
    # ------------------------------------------------------------------
    def get_vix(self) -> dict:
        """
        Fetch India VIX with Fallback Strategy:
        1. NSE API (Primary)
        2. Yahoo Finance (^INDIAVIX)
        """
        def _fetch_from_nse():
            candidate_urls = [
                "https://www.nseindia.com/api/allIndices",
                "https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY",
            ]
            payload, source, error = self._try_json_sources(candidate_urls)
            vix_value = None
            if payload:
                data = payload.get("data", [])
                if isinstance(data, list):
                    for entry in data:
                        symbol = str(entry.get("index", "") or entry.get("symbol", "")).upper()
                        if "VIX" in symbol or "INDIAVIX" in symbol.replace(" ", ""):
                            try:
                                vix_value = float(entry.get("last") or entry.get("lastPrice") or 0)
                                if vix_value > 0: break
                            except (TypeError, ValueError): continue
                if vix_value is None or vix_value <= 0:
                    records = payload.get("records", {})
                    vix_raw = records.get("vix") or payload.get("vix")
                    if vix_raw is not None:
                        try: vix_value = float(vix_raw)
                        except (TypeError, ValueError): pass
            
            if vix_value and vix_value > 0:
                return {"value": round(vix_value, 2), "source": source}
            return None

        def _fetch_from_yahoo():
            try:
                ticker = yf.Ticker("^INDIAVIX")
                data = ticker.history(period="1d")
                if not data.empty:
                    vix_value = data['Close'].iloc[-1]
                    return {"value": round(vix_value, 2), "source": "Yahoo Finance"}
            except Exception as e:
                logger.warning(f"Yahoo Finance VIX fetch failed: {e}")
            return None

        result = FallbackExecutor.execute("India VIX", [_fetch_from_nse, _fetch_from_yahoo])

        if not result:
            return {
                "available": False,
                "source": None,
                "error": "All VIX sources failed",
                "value": None,
                "regime": "data_unavailable",
            }

        vix_value = result["value"]
        if vix_value > 20: regime = "high_iv"
        elif vix_value < 14: regime = "low_iv"
        else: regime = "normal_iv"

        return {
            "available": True,
            "source": result["source"],
            "error": None,
            "value": vix_value,
            "regime": regime,
        }

    # ------------------------------------------------------------------
    # FII / DII
    # ------------------------------------------------------------------
    def get_fii_dii_cash(self) -> dict:
        """
        Cash FII/DII context with caching fallback.
        """
        CACHE_KEY = "FII_DII_CASH"

        def _fetch_live():
            candidate_urls = [
                "https://www.nseindia.com/api/fiidiiTradeReact",
                "https://www.nseindia.com/api/fiidiiTradeReact?segment=equity",
            ]
            payload, source, error = self._try_json_sources(candidate_urls)
            if not payload: return None

            rows = payload.get("data", []) if isinstance(payload, dict) else []
            fii_net = None
            dii_net = None
            trade_date = None

            for row in rows:
                cat = str(row.get("category") or row.get("categoryName") or "").upper()
                net_val = row.get("netValue")
                date_val = row.get("date") or row.get("tradeDate")
                try: net_num = float(str(net_val).replace(",", "")) if net_val is not None else None
                except Exception: net_num = None

                if date_val: trade_date = trade_date or str(date_val)
                if "FII" in cat or "FPI" in cat: fii_net = net_num
                elif "DII" in cat: dii_net = net_num

            result = {
                "available": True,
                "source": source,
                "fii_net_cr": fii_net,
                "dii_net_cr": dii_net,
                "date": trade_date,
            }
            EODCache.save(CACHE_KEY, result)
            return result

        def _fetch_cache():
            cached = EODCache.get_cached_bias(CACHE_KEY)
            if cached:
                cached["source"] = "Local Cache (Yesterday's Bias)"
                return cached
            return None

        result = FallbackExecutor.execute("FII/DII Cash", [_fetch_live, _fetch_cache])
        
        if not result:
            return {
                "available": False,
                "source": None,
                "error": "No live or cached FII/DII data",
                "fii_net_cr": None,
                "dii_net_cr": None,
                "date": None,
                "regime_hint": "unknown",
            }

        fii_net = result.get("fii_net_cr")
        if fii_net is not None:
            EODCache.save_history(CACHE_KEY, fii_net)
        
        history = EODCache.get_history(CACHE_KEY)
        avg_fii = sum(history[-3:]) / len(history[-3:]) if history else 0
        
        regime_hint = "neutral"
        if len(history) >= 1: # Require at least 1 day for a hint, but use 3 for rolling
            if avg_fii <= -1500: regime_hint = "risk_off" # Slightly tighter threshold for average
            elif avg_fii >= 800: regime_hint = "risk_on"

        result["regime_hint"] = regime_hint
        result["avg_fii_3d"] = round(avg_fii, 2)
        result["error"] = None
        return result

    # ------------------------------------------------------------------
    # Participant OI
    # ------------------------------------------------------------------
    def get_participant_oi(self) -> dict:
        """
        Participant-wise derivatives positioning context with caching.
        """
        CACHE_KEY = "PARTICIPANT_OI"

        def _fetch_live():
            candidate_urls = [
                "https://www.nseindia.com/api/participant-wise-oi",
                "https://www.nseindia.com/api/participantholding?type=oi",
            ]
            payload, source, error = self._try_json_sources(candidate_urls)
            if not payload: return None

            call_delta = None
            put_delta = None
            rows = payload.get("data", []) if isinstance(payload, dict) else []
            for row in rows:
                name = str(row.get("instrument") or row.get("clientType") or row.get("category") or "").lower()
                if "index" in name and "option" in name:
                    try:
                        c = row.get("callChgOI") or row.get("call_oi_change") or row.get("ce_change")
                        p = row.get("putChgOI") or row.get("put_oi_change") or row.get("pe_change")
                        if c is not None: call_delta = float(str(c).replace(",", ""))
                        if p is not None: put_delta = float(str(p).replace(",", ""))
                    except Exception: pass

            result = {
                "available": True,
                "source": source,
                "fii_index_options_call_oi_change": call_delta,
                "fii_index_options_put_oi_change": put_delta,
            }
            EODCache.save(CACHE_KEY, result)
            return result

        def _fetch_cache():
            cached = EODCache.get_cached_bias(CACHE_KEY)
            if cached:
                cached["source"] = "Local Cache (Yesterday's Bias)"
                return cached
            return None

        result = FallbackExecutor.execute("Participant OI", [_fetch_live, _fetch_cache])

        if not result:
            return {
                "available": False,
                "source": None,
                "error": "No live or cached OI data",
                "fii_index_options_call_oi_change": None,
                "fii_index_options_put_oi_change": None,
                "directional_hint": "unknown",
                "raw": None,
            }

        call_delta = result.get("fii_index_options_call_oi_change")
        put_delta = result.get("fii_index_options_put_oi_change")
        directional_hint = "unknown"
        if call_delta is not None and put_delta is not None:
            if call_delta > put_delta: directional_hint = "bullish_call_buildup"
            elif put_delta > call_delta: directional_hint = "protective_or_bearish_put_buildup"
            else: directional_hint = "balanced"

        result["directional_hint"] = directional_hint
        result["error"] = None
        result["raw"] = None # Avoid bloat in return
        return result

    # ------------------------------------------------------------------
    # F&O Ban / MWPL
    # ------------------------------------------------------------------
    def get_ban_mwpl(self, symbol: str | None = None) -> dict:
        """
        F&O ban and MWPL status with static CSV fallback.
        """
        def _fetch_from_web():
            candidate_urls = [
                "https://www.nseclearing.in/risk-management/equity-derivatives/position-limits",
                "https://www.nseindia.com/reports/fobans",
            ]
            for url in candidate_urls:
                try:
                    text = self._fetch_text(url)
                    return {"text": text, "source": url}
                except Exception: continue
            return None

        def _fetch_static_csv():
            # NSE often hosts a static CSV that is more resilient
            url = "https://www.nseindia.com/api/marketStatus" # Dummy check for status
            # Real fallback would be a direct CSV link like:
            # https://www.nseindia.com/archives/fo/sec_ban/sec_ban.csv
            csv_url = "https://www.nseindia.com/archives/fo/sec_ban/sec_ban.csv"
            try:
                req = Request(csv_url, headers=NSE_HEADERS)
                with urlopen(req, timeout=self.timeout) as resp:
                    text = resp.read().decode("utf-8")
                    return {"text": text, "source": "NSE Static CSV"}
            except Exception: return None

        result = FallbackExecutor.execute("F&O Ban List", [_fetch_from_web, _fetch_static_csv])

        if not result:
            return {
                "available": False,
                "source": None,
                "error": "All Ban list sources failed",
                "is_in_ban": None,
                "mwpl_pct": None,
                "ban_symbols": [],
            }

        text = result["text"]
        upper = text.upper()
        is_in_ban = (symbol.upper() in upper) if symbol else None
        
        mwpl_pct = None
        if symbol:
            sym = symbol.upper()
            idx = upper.find(sym)
            if idx >= 0:
                window = upper[idx: idx + 240]
                parts = [p for p in window.replace("%", " % ").split() if p]
                for i, tok in enumerate(parts):
                    if tok == "%" and i > 0:
                        try:
                            mwpl_pct = float(parts[i - 1].replace(",", ""))
                            break
                        except Exception: continue

        return {
            "available": True,
            "source": result["source"],
            "error": None,
            "is_in_ban": is_in_ban,
            "mwpl_pct": mwpl_pct,
            "ban_symbols": [],
        }

    # ------------------------------------------------------------------
    # Snapshot (aggregates all sources)
    # ------------------------------------------------------------------
    def get_snapshot(self, symbol: str | None = None) -> GovernanceSnapshot:
        fii_dii = self.get_fii_dii_cash()
        participant_oi = self.get_participant_oi()
        ban_mwpl = self.get_ban_mwpl(symbol=symbol)
        vix = self.get_vix()

        source_health = {
            "fii_dii_ok": bool(fii_dii.get("available")),
            "participant_oi_ok": bool(participant_oi.get("available")),
            "ban_mwpl_ok": bool(ban_mwpl.get("available")),
            "vix_ok": bool(vix.get("available")),
        }

        return GovernanceSnapshot(
            timestamp_utc=datetime.utcnow().isoformat(timespec="seconds") + "Z",
            fii_dii=fii_dii,
            participant_oi=participant_oi,
            ban_mwpl=ban_mwpl,
            vix=vix,
            source_health=source_health,
        )

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    feed = MarketGovernanceFeed()
    print("\n--- Testing VIX ---")
    print(feed.get_vix())
    
    print("\n--- Testing FII/DII (should cache) ---")
    print(feed.get_fii_dii_cash())
    
    print("\n--- Testing Ban List ---")
    print(feed.get_ban_mwpl("RELIANCE"))
    
    print("\n--- Snapshot ---")
    snap = feed.get_snapshot("NIFTY")
    print(f"VIX: {snap.vix['value']} | FII Hint: {snap.fii_dii['regime_hint']}")
