# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install (from project root)
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .

# Start the FastAPI server (Linux control plane)
./scripts/start.sh
# Or directly:
./.venv/bin/python -m ashare_system.run serve

# Other run commands
python -m ashare_system.run healthcheck
python -m ashare_system.run scheduler [--dry-run]
python -m ashare_system.run refresh-profiles [--trade-date YYYY-MM-DD] [--symbols SH600000 ...]

# Tests (no tests/ directory currently exists — tests were planned)
pytest
pytest tests/test_strategy/
pytest --cov=src/ashare_system --cov-report=html

# Service management (systemd, Ubuntu Server)
./scripts/ashare_service.sh start|stop|status
./scripts/openclaw_gateway_service.sh start|stop|status
./scripts/install_linux_systemd_service.sh
./scripts/install_openclaw_gateway_service.sh

# Operational scripts
./scripts/health_check.sh
./scripts/daily_pipeline.sh
./scripts/compute_factors.sh
./scripts/train_models.sh
./scripts/run_backtest.sh
./scripts/print_windows_gateway_handoff.sh   # outputs Windows Gateway onboarding summary

# API health check
curl http://localhost:8100/health
```

## Architecture

The system has a **Linux control plane + Windows Execution Gateway** split:

- **Linux** runs the FastAPI server (port 8100), scheduler, AI models, risk checks, and decision logic
- **Windows** runs the QMT trading terminal; orders are forwarded to it via the Windows Execution Gateway
- **OpenClaw** (optional) provides a natural-language agent interface via Feishu (Lark)

### Key modes (`.env` / environment variables)

| Variable | Values | Effect |
|---|---|---|
| `ASHARE_RUN_MODE` | `dry-run` / `paper` / `live` | Controls whether real orders are placed |
| `ASHARE_EXECUTION_MODE` | `mock` / `xtquant` | Mock adapter vs. real QMT |
| `ASHARE_EXECUTION_PLANE` | `windows_gateway` | Linux side acts as control plane only |
| `ASHARE_MARKET_MODE` | `mock` / `xtquant` | Mock market data vs. real QMT feed |

### Source layout (`src/ashare_system/`)

- **`settings.py`** — All config loaded from env vars via Pydantic models (`AppSettings`, `XtQuantSettings`, `ServiceSettings`). Auto-loads `.env` from the project root.
- **`container.py`** — DI container using `@lru_cache`. All singletons (adapters, stores, services) are obtained here. Import from here, never instantiate directly.
- **`contracts.py`** — Shared Pydantic models: `PlaceOrderRequest`, `OrderSnapshot`, `BalanceSnapshot`, `PositionSnapshot`, `TradeSnapshot`, `ExecutionIntentPacket`, etc. These flow through the entire system.
- **`run.py`** — CLI entry point. Handles `serve`, `healthcheck`, `scheduler`, `refresh-profiles` sub-commands.
- **`app.py`** — FastAPI application factory (`create_app()`). Registers all routers and runs startup recovery on lifespan.
- **`scheduler.py`** — APScheduler-based daily trading pipeline (premarket → screener → strategy → orders → postmarket).

### Sub-packages

| Package | Purpose |
|---|---|
| `apps/` | FastAPI routers: `system_api`, `market_api`, `execution_api`, `runtime_api`, `research_api`, `strategy_api`, `monitor_api`, `report_api`, `data_api` |
| `infra/` | Adapters: `ExecutionAdapter` (base/mock/xtquant), `MarketAdapter`, `AuditStore`, `StateStore`, `QMTLauncher` |
| `strategy/` | Three strategies (momentum, reversion, breakout) + `StockScreener`, `StrategyRegistry`, `ExitEngine`, `PositionManager`, playbooks |
| `risk/` | `RiskGuard` (6 rules), `EmergencyStop`, `EmotionShield` |
| `factors/` | 150+ factor computations: `base/`, `micro/`, `behavior/`, `macro/` |
| `ai/` | XGBoost scorer, LSTM/Transformer trend models |
| `ai_advanced/` | Ensemble models, PPO position optimizer |
| `data/` | Market data fetching (akshare), K-line, caching, cleaning, archiving |
| `monitor/` | Real-time stock pool monitoring, alert engine, persistence |
| `backtest/` | Backtesting engine |
| `discussion/` | Multi-agent discussion/voting cycle for candidate stocks |
| `governance/` | Approval workflows for execution intents |
| `sentiment/` | Market sentiment analysis |
| `learning/` | Continuous learning pipeline |
| `notify/` | Feishu (Lark) webhook notifications |
| `report/` | Daily report generation |

### Execution flow for orders

1. `StockScreener` runs the selection funnel (filter → environment → factors → AI → sector diversification → Top-N)
2. Candidates go through `discussion/` (multi-agent scoring) and `risk/RiskGuard`
3. Approved intents are wrapped as `ExecutionIntentPacket` and either:
   - Sent directly via `XtQuantExecutionAdapter` (local QMT), or
   - Queued as pending intents for the Windows Execution Gateway to poll and execute
4. `StartupRecoveryService` reconciles orphaned/pending orders on every server start

### State persistence

All state is stored under `ASHARE_STORAGE_ROOT` (default `.ashare_state/`):
- `audits.json` — append-only audit log
- `runtime_state.json` / `research_state.json` / `meeting_state.json` / `monitor_state.json` — JSON `StateStore` files
- `runtime_config.json` — dynamic config overrides (managed via `POST /system/config`)

Dynamic config parameters (adjustable at runtime without restart via `POST /system/config`):
`max_buy_count`, `max_hold_count`, `max_total_position`, `max_single_amount`, `scope.allow_chinext`, `scope.allow_star`, `daily_loss_limit`, `emergency_stop`
