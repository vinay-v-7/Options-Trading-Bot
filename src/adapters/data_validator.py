import logging
import re
from typing import Optional, Dict, Any
# Using search_web tool would be ideal, but here we provide the structure to use it.
# In a real agent environment, the agent calls the tool and passes the results here.

logger = logging.getLogger(__name__)

class MetricValidator:
    """Verifies locally calculated metrics against market consensus via web search."""
    
    @staticmethod
    def verify_max_pain(symbol: str, local_value: float, web_search_text: str) -> Dict[str, Any]:
        """
        Extracts Max Pain from web search results and compares it to local value.
        """
        logger.info(f"Verifying Max Pain for {symbol} against web consensus...")
        
        # Regex to find numbers near keywords like "Max Pain" or "Pain"
        # Example: "Max Pain is 22000", "Max Pain: 22000", "Pain at 22000"
        patterns = [
            rf"max\s*pain\s*(?:is|at|:)?\s*(\d{{4,5}})",
            rf"pain\s*(?:at|:)?\s*(\d{{4,5}})"
        ]
        
        web_value = None
        for pattern in patterns:
            match = re.search(pattern, web_search_text, re.IGNORECASE)
            if match:
                try:
                    web_value = float(match.group(1))
                    break
                except (ValueError, TypeError):
                    continue
        
        if web_value is None:
            logger.warning("Could not extract Max Pain value from web search text.")
            return {"verified": False, "reason": "No consensus found", "penalty": 0}
            
        delta = abs(web_value - local_value)
        # Threshold: 1% of the strike price or 100 points, whichever is smaller?
        # Let's stick to 1% as a general rule for indices.
        threshold = local_value * 0.01 
        
        if delta > threshold:
            logger.warning(f"Divergence detected for {symbol} Max Pain! Local: {local_value}, Web: {web_value}")
            return {
                "verified": False, 
                "reason": "Significant divergence",
                "local_val": local_value,
                "web_val": web_value,
                "penalty": -15 # Pillar 4 penalty
            }
            
        logger.info(f"Max Pain verification passed for {symbol}. Divergence: {delta}")
        return {"verified": True, "local_val": local_value, "web_val": web_value, "penalty": 0}

    @staticmethod
    def verify_rsi(symbol: str, local_rsi: float, web_search_text: str) -> Dict[str, Any]:
        """Extracts RSI from web text and compares."""
        # Simple extraction for RSI (usually 0-100)
        match = re.search(rf"{symbol}\s*RSI\s*(?:is|at|:)?\s*(\d{{1,2}}(?:\.\d+)?)", web_search_text, re.IGNORECASE)
        if match:
            web_rsi = float(match.group(1))
            delta = abs(web_rsi - local_rsi)
            if delta > 5: # RSI divergence > 5 points is worth noting
                return {"verified": False, "web_rsi": web_rsi, "penalty": -5}
        return {"verified": True, "penalty": 0}
