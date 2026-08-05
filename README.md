# quantplatform

Infrastructure for a **self-learning algorithmic trading platform**: paper
trading against Alpaca by default, TradingView webhook ingestion, an
independent risk engine in front of every order, versioned strategies and
models, shadow testing, champion-vs-challenger evaluation, and offline
retraining from a complete decision audit trail.

This repository is *infrastructure*, not a profitable strategy. The builtin
strategies exist to exercise the platform.

> **Not investment advice. Live trading is disabled by default behind five
> independent gates and is entirely at your own risk.**

## Stack

Python 3.12+ · FastAPI · PostgreSQL (SQLAlchemy 2 async + Alembic) ·
Redis + arq workers · httpx (Alpaca REST) · structlog · Docker Compose · pytest.

## Quickstart (Docker)

```bash
cp .env.example .env          # then edit: at minimum set TRADINGVIEW_WEBHOOK_SECRET,
                              # ADMIN_API_TOKEN, and your Alpaca *paper* keys
docker compose up --build     # postgres + redis + migrations + api + worker
curl localhost:8010/health
```

Host ports are non-default to avoid collisions: Postgres on **5433**, Redis
on **6380**, API on **8010**.

## Quickstart (local dev)

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
docker compose up -d postgres redis
cp .env.example .env
.venv/bin/alembic upgrade head
.venv/bin/python -m scripts.seed                  # register builtin strategies + controls
.venv/bin/uvicorn app.main:app --port 8010        # API
.venv/bin/arq app.workers.settings.WorkerSettings # worker (separate shell)
.venv/bin/python -m pytest                        # 485 tests, no network/DB needed
```

## Getting Alpaca paper keys

1. Sign up at [alpaca.markets](https://alpaca.markets) (paper trading needs no funding).
2. **Switch the dashboard to "Paper Trading" using the toggle in the upper-left
   BEFORE generating keys.** This is the step most people miss — keys generated
   while the dashboard is on Live are live keys and will not work here.
3. In the right-hand sidebar find **API Keys** → **Generate New Keys**.
4. Copy both values immediately — the secret is shown **once**. Lost it? Just
   regenerate. Paper key IDs start with `PK`; live ones start with `AK`.
5. Paste into `.env` as `ALPACA_PAPER_API_KEY` / `ALPACA_PAPER_API_SECRET`.
6. Verify: `.venv/bin/python -m scripts.check_keys`

## First trading loop (paper)

1. Put your **paper** keys in `.env` (`ALPACA_PAPER_API_KEY/SECRET`).
2. Backfill data: `.venv/bin/python -m scripts.backfill_bars SPY QQQ --days 365`
3. Backtest an example: `.venv/bin/python -m scripts.run_backtest sma_cross --symbol SPY`
4. Approve a strategy (human action, audited):
   ```bash
   curl -X POST localhost:8010/admin/strategies/<id>/status \
     -H "X-Admin-Token: $ADMIN_API_TOKEN" -H 'Content-Type: application/json' \
     -d '{"to_status":"approved","actor":"you","reason":"backtest reviewed"}'
   ```
5. The worker's strategy loop now trades it on paper; every other approved
   strategy runs in **shadow** (recorded, never routed). Outcomes are
   evaluated hourly; the allocator retrains nightly into an **inert** model
   version that you can promote to shadow → champion via the admin API.

Point a TradingView alert at `POST /webhooks/tradingview` with message:

```json
{"secret": "<TRADINGVIEW_WEBHOOK_SECRET>", "symbol": "{{ticker}}",
 "action": "buy", "qty": 1, "signal_id": "{{id}}", "time": "{{timenow}}"}
```

## Making it yours

```bash
cp profile.example.toml profile.toml   # then edit capital, symbols, persona weights
.venv/bin/python -m scripts.compare_personas --symbols SPY QQQ AAPL
```

`profile.toml` sets how much capital the bot may use, how aggressively to size,
which symbols it may touch, and per-persona weights. It can only ever be *more*
conservative than the risk engine. See [docs/profile.md](docs/profile.md).

## Watching it run

Open **<http://localhost:8010/dashboard>** and paste your `ADMIN_API_TOKEN`.

Dark, chart-first, read-only: account value with a scrubbable equity chart and
range selector, holdings with an allocation bar, and an activity feed where any
decision expands to show *why* it happened in plain language ("Rejected by the
order size limit. This order was $24,922; the per-order cap is $5,000") rather
than raw rule names. Nothing on it can place or cancel an order.

For terminal use: `.venv/bin/python -m scripts.status`, plus
`docker compose logs -f worker` for the live loop.

## Safety model (read before touching anything)

- **Paper/live separation** — separate credential variables; hardcoded
  per-environment API hosts; no fallback from live to paper credentials; every
  execution row tagged with its environment.
- **Live trading requires all five**: `TRADING_ENV=live`,
  `LIVE_TRADING_ENABLED=true`, `LIVE_TRADING_CONFIRMATION` set to the exact
  phrase in [app/config.py](app/config.py), live credentials present, and the
  runtime **arm switch** set through the admin API. Missing any one → paper.
- **Risk engine** ([app/risk](app/risk)) — every intent passes
  `RiskEngine.evaluate`, no bypass parameter exists. Kill switch, circuit
  breaker (auto-trips on repeated broker errors), duplicate-order detection,
  position/exposure/notional limits, daily-loss and drawdown halts,
  stale-data fail-closed, symbol allowlist, long-only enforcement.
- **Kill switch** — `POST /admin/kill-switch` halts all order flow platform-wide
  instantly (DB-backed, seen by every process).
- **The learning system cannot**: submit orders, touch production code,
  promote itself, or skip risk checks. It writes inert model versions;
  humans promote via the audited admin API. See
  [docs/learning.md](docs/learning.md).

## Repository layout

| Path | Contents |
|---|---|
| `app/config.py` | Settings + the live-trading gates |
| `app/db/` | SQLAlchemy models (15 tables), session plumbing |
| `app/schemas/` | Shared Pydantic types (bars, orders, intents) |
| `app/brokers/` | `Broker` ABC, Alpaca adapter, backtest simulator |
| `app/marketdata/` | Alpaca data API provider + bar persistence |
| `app/webhooks/` | TradingView validation pipeline |
| `app/strategies/` | Strategy interface, registry, indicators, researched strategy catalog |
| `app/risk/` | Risk types, rules, engine, system controls |
| `app/execution/` | Order lifecycle, sizing, reconciliation |
| `app/dashboard/` | Operator dashboard: chart geometry, plain-language translation, data assembly, static assets |
| `app/profile/` | Owner profile and position sizing |
| `app/personas/` | Persona registry service + cooperative/competing mode orchestration |
| `app/learning/` | Features/regimes, model registry, allocator, evaluation, training |
| `app/backtest/` | Event-driven backtester + metrics |
| `app/api/` | FastAPI routes: health, webhooks, admin |
| `app/workers/` | arq task definitions + cron schedule |
| `scripts/` | check_keys, status, seed, seed_personas, backfill_bars, run_backtest, compare_personas |
| `docs/` | architecture, runbook, learning, webhooks, strategies, profile, comparison |
| `tests/` | 485 pytest tests (SQLite + respx; no network) |

## HTTP surface

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /health` | none | liveness + env summary |
| `GET /dashboard` | none (shell only) | operator dashboard UI |
| `GET /dashboard/data` | `X-Admin-Token` | dashboard JSON |
| `POST /webhooks/tradingview` | payload secret (+ optional HMAC) | signal ingestion |
| `GET /admin/status` | `X-Admin-Token` | controls + gate state |
| `POST /admin/kill-switch` | token | engage/release |
| `POST /admin/circuit-breaker/reset` | token | operator reset |
| `POST /admin/live/arm` | token | runtime live arm/disarm |
| `POST /admin/strategies/{id}/status` | token | candidate→approved→champion→retired |
| `POST /admin/models/{id}/promote` | token | registered→shadow→champion→retired |
| `GET /admin/personas` | token | personas, fidelity notes, member status |
| `POST /admin/personas/{id}/activate` | token | activate + approve members (audited) |
| `POST /admin/personas/{id}/deactivate` | token | stop trading; reversible, members stay approved |
| `GET /admin/risk-events`, `GET /admin/decisions` | token | recent audit rows |

All admin mutations require a human `actor` and `reason` and write audit rows.

## Documentation

- [docs/architecture.md](docs/architecture.md) — layers, data flow, invariants
- [docs/runbook.md](docs/runbook.md) — operations: incidents, promotions, going live
- [docs/learning.md](docs/learning.md) — how learning works and what bounds it
- [docs/webhooks.md](docs/webhooks.md) — TradingView setup and payload contract
- [docs/investor-personas-research.md](docs/investor-personas-research.md) — researched methodologies of Minervini, O'Neil, Paul Tudor Jones, TJR/SMC, Dalio All Weather, Permanent Portfolio, Dual Momentum, with honest fidelity assessments
- [docs/spec-persona-engine.md](docs/spec-persona-engine.md) — spec for the persona engine: presets, target-weight sleeves, profile layer, learning integration
- [docs/spec-control-surface.md](docs/spec-control-surface.md) — spec for manual trading and dashboard strategy control (not built yet)
- [docs/profile.md](docs/profile.md) — the owner profile: capital, risk appetite, persona weights, and the three sizing rules
- [docs/comparison.md](docs/comparison.md) — ranking personas against buy-and-hold on real history, and how to read it honestly
- [docs/strategies.md](docs/strategies.md) — the researched strategy catalog: TSMOM (Moskowitz/Ooi/Pedersen), Turtle/Donchian, Connors RSI-2 & Double 7s, Bollinger reversion, 52-week-high momentum, vol-targeting overlay — with lineage, params, and flagged simplifications

## Known limitations (deliberate, this phase)

Polling-based order sync (no websocket streams yet); mark-to-market outcome
evaluation at a fixed horizon; daily-bar strategy loop; single-instance
workers; no options, shorting, margin, or leverage anywhere.
