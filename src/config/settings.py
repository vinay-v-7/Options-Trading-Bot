"""
Central configuration loader for the Options Trading pipeline.

All runtime tunables are sourced from environment variables (`.env` file).
Import from this module instead of reading `os.getenv()` directly anywhere else.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root (two levels up from this file: src/config → src → project root)
_env_path = Path(__file__).resolve().parent.parent.parent / ".env"
load_dotenv(dotenv_path=_env_path, override=True)

# ---------------------------------------------------------------------------
# DhanHQ Credentials
# ---------------------------------------------------------------------------
DHAN_CLIENT_ID: str = os.getenv("DHAN_CLIENT_ID", "")
DHAN_ACCESS_TOKEN: str = os.getenv("DHAN_ACCESS_TOKEN", "")

# ---------------------------------------------------------------------------
# Capital Configuration
# ---------------------------------------------------------------------------
#: Default total trading capital. Override with TOTAL_CAPITAL in .env
#: or via --budget CLI argument in main.py for high-conviction trades (score ≥ 9.0).
TOTAL_CAPITAL: float = float(os.getenv("TOTAL_CAPITAL", "30000.0"))

#: Hard maximum capital per trade (same as total in Phase 0).
#: CLI --budget flag may raise this up to 40000 for exceptional setups.
CAPITAL_PER_TRADE_DEFAULT: float = TOTAL_CAPITAL

# ---------------------------------------------------------------------------
# Risk Limits
# ---------------------------------------------------------------------------
#: Maximum portfolio positions open simultaneously
MAX_POSITIONS: int = int(os.getenv("MAX_POSITIONS", "3"))

#: Maximum fraction of total capital that can be deployed at once.
#: Increased from 0.5 → 0.85; protected by CircuitBreaker drawdown limits.
MAX_DEPLOYED_FRACTION: float = float(os.getenv("MAX_DEPLOYED_FRACTION", "0.85"))

#: Minimum required 4-Pillar weighted score to generate a recommendation
MIN_SCORE_THRESHOLD: float = float(os.getenv("MIN_SCORE_THRESHOLD", "8.0"))

# ---------------------------------------------------------------------------
# Compound-Phase Hard Gates (first-principles capital preservation)
# ---------------------------------------------------------------------------
#: Absolute max loss per trade (rupees). Tier % caps are clamped to this.
MAX_LOSS_ABS: float = float(os.getenv("MAX_LOSS_ABS", "800.0"))

#: Stop all new trades when tracked equity reaches this level.
CAPITAL_HARD_FLOOR: float = float(os.getenv("CAPITAL_HARD_FLOOR", "25000.0"))

#: Minimum expected net profit after modeled transaction costs.
MIN_NET_PROFIT: float = float(os.getenv("MIN_NET_PROFIT", "1000.0"))

#: Credit / short-gamma strategies require at least this capital.
CREDIT_MIN_CAPITAL: float = float(os.getenv("CREDIT_MIN_CAPITAL", "100000.0"))

#: Hard reject if ROC after costs is below this fraction (5%).
MIN_ROC_HARD: float = float(os.getenv("MIN_ROC_HARD", "0.05"))

# ---------------------------------------------------------------------------
# Confidence-Tiered Capital Allocation
# Tier 1 (Score 8.0–8.4): Conservative — 50% deployment, 5% stoploss cap
# Tier 2 (Score 8.5–8.9): Strong      — 70% deployment, 6.5% stoploss cap
# Tier 3 (Score ≥ 9.0):   High Conv.  — 85% deployment, 8% stoploss cap
# ---------------------------------------------------------------------------
#: Tier 1 — Score 8.0-8.4
CAPITAL_DEPLOY_TIER_1: float = float(os.getenv("CAPITAL_DEPLOY_TIER_1", "0.50"))
STOPLOSS_CAP_TIER_1: float = float(os.getenv("STOPLOSS_CAP_TIER_1", "0.05"))   # 5%

#: Tier 2 — Score 8.5-8.9
CAPITAL_DEPLOY_TIER_2: float = float(os.getenv("CAPITAL_DEPLOY_TIER_2", "0.70"))
STOPLOSS_CAP_TIER_2: float = float(os.getenv("STOPLOSS_CAP_TIER_2", "0.065"))  # 6.5%

#: Tier 3 — Score >= 9.0
CAPITAL_DEPLOY_TIER_3: float = float(os.getenv("CAPITAL_DEPLOY_TIER_3", "0.85"))
STOPLOSS_CAP_TIER_3: float = float(os.getenv("STOPLOSS_CAP_TIER_3", "0.08"))   # 8%
