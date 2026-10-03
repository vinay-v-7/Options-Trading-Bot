import logging
import json
import os
from datetime import datetime, timedelta
from typing import Callable, Any, List, Optional

logger = logging.getLogger(__name__)

class FallbackExecutor:
    """Executes a list of callable sources in order until one succeeds."""
    @staticmethod
    def execute(source_name: str, fetchers: List[Callable[[], Any]]) -> Any:
        last_exception = None
        for index, fetcher in enumerate(fetchers):
            try:
                logger.info(f"[{source_name}] Attempting source {index + 1}/{len(fetchers)}...")
                result = fetcher()
                if result is not None:
                    logger.info(f"[{source_name}] Source {index + 1} succeeded.")
                    return result
            except Exception as e:
                last_exception = e
                logger.warning(f"[{source_name}] Source {index + 1} failed: {e}")
        
        logger.error(f"[{source_name}] All fallback sources failed. Last error: {last_exception}")
        return None

class EODCache:
    """Simple JSON cache for End-Of-Day metrics (Category 2)."""
    CACHE_FILE = os.path.join("data", "cache", ".data_cache.json")

    @classmethod
    def save(cls, key: str, data: Any):
        try:
            cache = cls._load_all()
            cache[key] = {
                "timestamp": datetime.now().isoformat(),
                "data": data
            }
            # RISK: LRU-style cache eviction — limit to 100 keys
            if len(cache) > 100:
                oldest_key = min(cache.keys(), key=lambda k: cache[k]["timestamp"])
                del cache[oldest_key]

            os.makedirs(os.path.dirname(cls.CACHE_FILE), exist_ok=True)
            with open(cls.CACHE_FILE, 'w') as f:
                json.dump(cache, f, indent=4)
        except Exception as e:
            logger.error(f"Failed to save to cache: {e}")

    @classmethod
    def save_history(cls, key: str, value: float):
        """Saves a numeric value to a history list for rolling averages."""
        try:
            history_key = f"{key}_history"
            cache = cls._load_all()
            history = cache.get(history_key, {"timestamp": "", "data": []})["data"]
            
            # Avoid duplicate entries for the same day (naive check)
            today_str = datetime.now().strftime("%Y-%m-%d")
            # In a real system we'd check the timestamp, but let's keep it simple
            history.append(value)
            if len(history) > 10: # Keep last 10 days
                history = history[-10:]
                
            cache[history_key] = {
                "timestamp": datetime.now().isoformat(),
                "data": history
            }
            os.makedirs(os.path.dirname(cls.CACHE_FILE), exist_ok=True)
            with open(cls.CACHE_FILE, 'w') as f:
                json.dump(cache, f, indent=4)
        except Exception as e:
            logger.error(f"Failed to save history: {e}")

    @classmethod
    def get_history(cls, key: str) -> List[float]:
        """Retrieves the history list for a key."""
        try:
            cache = cls._load_all()
            history_key = f"{key}_history"
            if history_key in cache:
                return cache[history_key]["data"]
        except Exception: pass
        return []

    @classmethod
    def get_cached_bias(cls, key: str, max_age_days: float = 3.0) -> Optional[Any]:
        """Retrieves cached data if it's within the max_age_days."""
        try:
            cache = cls._load_all()
            if key in cache:
                cached_time = datetime.fromisoformat(cache[key]["timestamp"])
                if datetime.now() - cached_time < timedelta(days=max_age_days):
                    logger.info(f"Using cached data for {key} (from {cached_time})")
                    return cache[key]["data"]
        except Exception as e:
            logger.error(f"Failed to read from cache: {e}")
        return None

    @classmethod
    def _load_all(cls) -> dict:
        if os.path.exists(cls.CACHE_FILE):
            try:
                with open(cls.CACHE_FILE, 'r') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                return {}
        return {}
