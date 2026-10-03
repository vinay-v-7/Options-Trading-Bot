# Options Trading Bot

Rules-based NSE F&O options recommendation engine. It scans a liquid universe, scores candidate structures with a weighted 4-pillar model, and refuses the trade when hard gates fail.

Live market data comes from [DhanHQ](https://dhanhq.co/). The bot does **not** place orders. You still decide whether to execute at the broker.

**Not financial advice.** Options can lose money quickly. This repo is a personal research / systems project, not a signal service.

## Why it exists

Discretionary options trading fails in boring ways: entering into a gap day, ignoring ban/MWPL, selling premium under ₹1L capital, or forcing a trade when the score is weak. This project encodes those refusals in code. The default outcome when nothing clears the gates is plain: no recommendation.

## What it does

1. Pulls governance context (India VIX, FII/DII, F&O ban / MWPL).
2. Ranks a curated Master-40 F&O list by recent turnover and scans the top names.
3. Builds candidate strategies (long options, debit/credit spreads, strangle candidates).
4. Runs pre-qualification gates, then 4-pillar scoring.
5. Emits at most one Absolute Pick for a market-wide scan, sized by confidence tier and capital limits.
6. Tracks open positions and circuit-breaker state locally under `data/` (created at runtime; not shipped).

Credit / short-gamma structures stay locked until capital is at least `CREDIT_MIN_CAPITAL` (default ₹1,00,000).

## Architecture

```text
CLI (src/main.py)
  scan | status | health
        │
        ▼
Orchestration (pipeline.py)
  auth → governance → CB / gap / blackout
  → universe + Dhan feed → screen_trades()
  → portfolio capacity → Absolute Pick
        │
   ┌────┼────────────┐
   ▼    ▼            ▼
Adapters  Analysis    Risk
DhanHQ    4-Pillar    CircuitBreaker
Gov feed  Costs       Event blackout
Universe              Capacity gates
        │
        ▼
Execution
PositionStore · trade manager · journal helpers
```

| Package | Role |
|---------|------|
| `src/adapters/` | DhanHQ feed, NSE governance, universe ranking, mock feed, validators |
| `src/analysis/` | Strategy selection, 4-pillar scoring, NSE/Dhan transaction-cost model |
| `src/risk/` | Tiered sizing, circuit breaker, event blackout, portfolio caps |
| `src/orchestration/` | End-to-end `scan` pipeline and Absolute Pick |
| `src/execution/` | Local positions, exits, journal utilities |
| `src/config/` | Env-backed capital and risk settings |

## Scoring and gates

Weighted score (minimum **8.0 / 10** to recommend):

| Pillar | Weight | Focus |
|--------|--------|--------|
| Statistical edge | 40% | Risk:reward / POP by strategy family |
| Volatility context | 30% | IV vs HV, India VIX regime |
| Technical confluence | 20% | VWAP, 9-EMA, RSI, OI walls |
| Execution / ROC | 10% | After-cost ROC, liquidity, cost drag |

Gates that can veto a trade before or after scoring include: circuit breaker (daily / weekly / monthly loss and consecutive losses), pre-market gap > ±3%, F&O ban or high MWPL, VIX regime vs short-gamma, DTE band, OI / bid-ask liquidity, event blackout (see `resources/event-calendar.json`), and portfolio capacity (default max 3 positions).

### DTE cycle

| Band | Intent |
|------|--------|
| 30–45 DTE (`standard` mode) | Preferred swing entry window |
| 10–21 DTE (`short_term` mode) | Tighter window; still gated |
| ~21 DTE | Review / exit checkpoint for open risk |
| < 14 DTE | High gamma; no new short premium |

Stock options on NSE are monthly. Far-month contracts are used when needed to stay inside the band; liquidity still has to clear the OI / spread gate.

### Confidence tiers (capital deploy)

| Score | Deploy fraction | Stoploss cap (of deployed) |
|-------|-----------------|----------------------------|
| 8.0–8.4 | 50% | 5% |
| 8.5–8.9 | 70% | 6.5% |
| ≥ 9.0 | 85% | 8% |

Absolute rupee caps (`MAX_LOSS_ABS`, hard equity floor) still apply on top of the percentages.

## Strategies

Long call / put, bull call / bear put debit spreads, bull put / bear call credit spreads (capital-gated), and long-strangle candidates. Strategy min-capital and direction rules live in `src/analysis/analyzer.py`.

## Setup

Requirements: Python **3.10+**, a DhanHQ API client id and access token.

```bash
git clone https://github.com/vinay-v-7/Options-Trading-Bot.git
cd Options-Trading-Bot
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
# source .venv/bin/activate

pip install -e ".[dev]"
copy .env.example .env   # or: cp .env.example .env
```

Edit `.env` with your `DHAN_CLIENT_ID` and `DHAN_ACCESS_TOKEN`. Dhan tokens expire; refresh them when the API starts returning auth errors. Do not commit `.env`.

Create empty runtime dirs the first time you run:

```bash
mkdir data logs
```

## CLI

```bash
# Market-wide Absolute Pick (default mode: short_term, DTE 10–21)
python -m src.main scan

# Single underlying, standard DTE band (21–45), custom budget
python -m src.main scan --symbol INFY --mode standard --budget 30000

# Open positions + circuit breaker summary
python -m src.main status

# Lightweight architecture / health printout
python -m src.main health
```

`scan` needs live DhanHQ credentials. Offline unit tests for risk gates do not.

## Configuration

| Variable | Purpose | Default |
|----------|---------|---------|
| `DHAN_CLIENT_ID` | Broker client id | (required) |
| `DHAN_ACCESS_TOKEN` | API token | (required) |
| `TOTAL_CAPITAL` | Default budget for scan/status | `30000` |
| `MIN_SCORE_THRESHOLD` | Min weighted score | `8.0` |
| `CREDIT_MIN_CAPITAL` | Floor for credit / short-gamma | `100000` |
| `MAX_POSITIONS` | Concurrent opens | `3` |
| `MAX_DEPLOYED_FRACTION` | Max capital in play | `0.85` |
| `MAX_LOSS_ABS` | Absolute max loss per trade (₹) | `800` |
| `CAPITAL_HARD_FLOOR` | Stop new risk below this equity | `25000` |

All tunables are loaded in `src/config/settings.py`. CLI `--budget` overrides capital for a single scan.

## Testing

```bash
pytest
```

Current coverage is focused on compound-phase risk gates and credit enablement (`tests/test_compound_phase_gates.py`). A `MockDataFeed` exists under `src/adapters/mock_feed.py` for offline experiments. Live `scan` against the market is not part of CI.

## Project layout

```text
Options-Trading-Bot/
├── src/
│   ├── main.py                 # CLI
│   ├── adapters/               # DhanHQ, governance, universe, mock feed
│   ├── analysis/               # Scoring + transaction costs
│   ├── risk/                   # Circuit breaker, blackout, capacity
│   ├── orchestration/          # Pipeline
│   ├── execution/              # Positions / journal helpers
│   ├── config/                 # Settings from .env
│   └── common/                 # Cache / resilience helpers
├── tests/
├── resources/
│   └── event-calendar.json     # Macro blackout dates for short-gamma
├── .env.example
├── pyproject.toml
└── LICENSE
```

## Optional tooling

Private development also uses one-off helpers (live quote checks, spread backtests, journal scripts). Those are **not** in this public repo. The supported interface here is `python -m src.main`.

## Limitations

- No broker order placement or position sync from the exchange.
- `health` is a static architecture printout, not a live connectivity probe.
- Test suite is narrow relative to the pipeline surface area.
- Governance scrapes / third-party feeds can break when upstream sites change.
- Defaults assume a small rupee account and India market microstructure; retune before using other capital sizes.

## License

MIT. See [LICENSE](LICENSE).
