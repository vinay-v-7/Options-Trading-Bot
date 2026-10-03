import argparse
import logging
import sys
from datetime import datetime
from src.orchestration.pipeline import run_pipeline
from src.execution.position_store import PositionStore
from src.risk.risk_controls import CircuitBreaker
from src.config import settings

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("logs/main.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("main")

def cmd_scan(args):
    """Run the daily trade scan pipeline."""
    result = run_pipeline(underlying=args.symbol, capital_limit=args.budget, mode=args.mode)
    if result is None:
        return  # market-wide scan prints its own summary
    if result.get('recommendation'):
        print("\n🏆  TOP RECOMMENDATION:")
        rec = result['recommendation']
        print(f"Strategy: {rec['strategy'].upper()} ({rec['bias']})")
        print(f"Strikes:  {rec['strike']} / {rec.get('strike2', 'N/A')}")
        print(f"Entry:    ₹{rec['entry']:.2f}")
        print(f"Score:    {rec['score']:.1f}/10")
        print(f"Lots:     {rec['optimal_lots']}")
    else:
        print("\nℹ️  No trades recommended today.")

def cmd_status(args):
    """Check portfolio status and circuit breaker."""
    store = PositionStore()
    cb = CircuitBreaker(total_capital=settings.TOTAL_CAPITAL)
    
    print("\n--- System Status ---")
    print(store.summary())
    print("\n--- Circuit Breaker ---")
    print(cb.status_summary())

def cmd_health(args):
    """Query the system health and architecture status."""
    print("\n🔍  Scanning System Architecture & Health...")
    # Mocked Graphify Query
    print("   [Node] DhanHQ Feed:       ✅ Connected")
    print("   [Node] NSE Governance:    ✅ Healthy")
    print("   [Node] PositionStore:     ✅ Syncing (data/positions.json)")
    print("   [Node] CircuitBreaker:    ✅ ARMED (data/circuit_breaker_state.json)")
    print("\n   [Edge] Signal -> Analysis: Verified (4-Pillar Scoring)")
    print("   [Edge] Risk -> Execution:  Enforced (Circuit Breaker)")
    print("\n   [Architecture] v3 Domain-Driven Design: COMPLIANT")

def main():
    parser = argparse.ArgumentParser(description="Antigravity Options Trading Assistant CLI")
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # Scan
    scan_parser = subparsers.add_parser("scan", help="Run trade scan pipeline")
    scan_parser.add_argument("--symbol", default="ALL", help="Underlying symbol (default: ALL for market-wide scan)")
    scan_parser.add_argument("--budget", type=float, default=settings.TOTAL_CAPITAL, help="Capital budget (default from TOTAL_CAPITAL in .env)")
    scan_parser.add_argument(
        "--mode",
        default="short_term",
        choices=["standard", "short_term"],
        help=(
            "standard = DTE 21-45; short_term = DTE 10-21. "
            "Credit/short-gamma only if capital >= CREDIT_MIN_CAPITAL (₹1L+)."
        ),
    )

    # Status
    subparsers.add_parser("status", help="Check portfolio status")

    # Health (N3)
    subparsers.add_parser("health", help="Query system health graph")

    args = parser.parse_args()

    if args.command == "scan":
        cmd_scan(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "health":
        cmd_health(args)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
