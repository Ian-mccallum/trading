# Architecture

A modular monolith for paper-first algorithmic trading with a carefully gated
path to live trading. Two processes share one codebase and one PostgreSQL
database: the **API** (FastAPI — ingestion, dashboard, admin controls) and the
**worker** (arq over Redis — everything that actually trades).

```
TradingView ──POST──▶ /webhooks/tradingview ──▶ signals table ──▶ arq queue
                                                                   │
                       ┌───────────────────────────────────────────┘
                       ▼
              worker: process_signal / run_strategies
                       │
                       │  strategies produce TradeIntents (direction)
                       ▼
              profile sizing (how much) ── profile.toml
                       │
                       ▼
              ┌─ ExecutionService ────────────────────────────────┐
              │  1. persist Decision (full context, audit spine)  │
              │  2. RiskEngine.evaluate  ◀── system_controls      │
              │     kill switch · breaker · live gates · dup ·    │
              │     limits · staleness · allowlist · long-only    │
              │  3. approved → Order (idempotent client id)       │
              │  4. Broker adapter (paper|live|simulated)         │
              └───────────────────────────────────────────────────┘
                       │ fills / statuses (sync + reconciliation)
                       ▼
        decisions + orders + fills + risk_events + snapshots
                       │                     │
                       │                     └──▶ /dashboard (read-only)
                       ▼
       learning (offline): outcomes → performance_reports →
       train allocator → model_versions (REGISTERED, inert)
                       │ human admin action only
                       ▼
       SHADOW → CHAMPION promotion; the champion allocator picks among
       *competing* strategies per regime, while *cooperative* persona
       members (portfolio legs) all trade together
```

## Layers and dependency rules

| Layer | Package | May depend on |
|---|---|---|
| Domain/persistence | `app/db` | nothing internal |
| Shared schemas | `app/schemas` | `db` |
| Broker adapters | `app/brokers`, `app/marketdata` | `schemas`, `config` |
| Strategies | `app/strategies` | `schemas` only — **no broker, no DB, no risk** |
| Risk | `app/risk` | `db`, `config` — **never strategies or learning** |
| Profile | `app/profile` | `schemas`, `db` enums — pure sizing policy |
| Personas | `app/personas` | `db` — registry + mode orchestration |
| Execution | `app/execution` | brokers, risk, db |
| Learning | `app/learning` | db, schemas — **never execution or brokers** |
| Dashboard | `app/dashboard` | db, brokers (read-only) |
| Interfaces | `app/api`, `app/workers` | everything above |

Strategies are pure functions `(params, StrategyContext) → [TradeIntent]`:
deterministic, no I/O, no clock. That is what makes live, paper, shadow, and
backtest behavior identical and strategies interchangeable.

## The decision audit spine

Every proposed trade — webhook-driven, strategy-driven, or shadow — becomes a
`decisions` row **before** risk evaluation, carrying the feature vector,
market regime, account snapshot and strategy reasoning in `context`. The risk
verdict and per-rule results land on the same row; fills and evaluation later
fill in `outcome`. Retraining reads exclusively from this table (plus fills
and bars), so every model is reproducible from recorded history.

Partial implementations record their own gaps here — `letters_absent: C,A,I`,
`rs_skipped`, `peer_skipped`, `sized_by: profile` — so a screen that ran with
fewer checks than its name implies can never look like one that did not.

## Separation of concerns in the order path

Four distinct questions, four owners, in this order:

1. **Should we act?** — the strategy (pure, from bars).
2. **How much?** — the profile sizer (`app/profile`), from owner preferences.
3. **Which mode?** — persona orchestration (`app/personas`): cooperative legs
   all trade, competing candidates go through the allocator, inactive persona
   members are shadow-only.
4. **Is it allowed?** — the risk engine, which is independent of all three and
   is the final authority.

The profile and personas can only ever be *more* conservative than the risk
engine. Neither has a write path to `RISK_*` limits or `system_controls`.

## Risk engine

`RiskEngine.evaluate` is the single choke point; there is no bypass parameter
and no broker path that skips it.

Layer 1 — DB-backed halt checks the engine performs itself: kill switch,
circuit breaker (auto-trips after N broker errors, cools down, operator
reset), live-trading gates, and duplicate-order detection.

Layer 2 — pure limit rules over an immutable `RiskContext`: qty sanity,
symbol allowlist, stale-data (fail closed), order notional, per-symbol
position limit, gross exposure, daily loss, drawdown from peak, and
long-only/no-short enforcement. Risk-reducing orders are not blocked by
exposure limits.

Every rule outcome is persisted to `risk_events`.

**Calibration matters as much as the rules.** Two defaults were wrong in ways
that made trading impossible and were only found by running the platform:
`risk_max_data_age_seconds` was an intraday-scale 300s when the freshest
possible daily bar is hours old, and the notional caps were sized for
one-share test positions rather than any real allocation. Both are now
documented in `app/config.py` with the reasoning.

## Paper/live separation

1. Separate credential settings (`ALPACA_PAPER_*` vs `ALPACA_LIVE_*`).
2. Broker base URLs are hardcoded constants per environment, not config.
3. `Settings.broker_credentials()` raises rather than falling back from live
   to paper.
4. Live trading requires **all** of: `TRADING_ENV=live`,
   `LIVE_TRADING_ENABLED=true`, the exact confirmation phrase, live
   credentials, **and** the runtime arm switch set through the admin API.
5. `ExecutionService` refuses to construct if the broker's environment does
   not match the configured one; every execution table carries an
   `environment` column and no query crosses them.

## Persona engine

A persona is a **preset, not a privilege**: a named bundle of registered
strategy versions with parameter presets, a symbol set, and an honest
`fidelity_note` stored alongside the data. Members still produce ordinary
intents through the same risk engine.

Two kinds, and the distinction is load-bearing:

- **Cooperative** (All Weather, Permanent Portfolio) — members are legs of one
  portfolio and must all trade. Letting the allocator pick one would hold a
  single leg and silently shadow the rest.
- **Competing** (PTJ, Minervini, O'Neil…) — members are alternatives; the
  allocator selects one per regime, the rest run shadow.

Activation approves member strategies through the *existing* audited
transition path, writing the same `PromotionEvent` rows a manual approval
would. Deactivation is reversible: members stay approved but become
shadow-only, so pulling a persona out of production needs no terminal status
change.

## Learning system boundaries

The learner is **advisory only**:

- It reads decisions/fills/bars and writes `model_versions`,
  `performance_reports`, `experiments` — no import path to brokers or
  execution.
- Training registers artifacts as `REGISTERED` (inert). Promotion
  `REGISTERED→SHADOW→CHAMPION` happens only via the admin API with a named
  human actor; illegal transitions are rejected and there is no auto-promotion
  code path.
- Champion/challenger comparison writes a **recommendation** into
  `experiments`; it never changes statuses.
- The allocator only ranks human-approved strategies, and whatever it selects
  still passes the full risk engine.

## Order lifecycle

`TradeIntent → Decision(PROPOSED) → risk → APPROVED/REJECTED →
Order(PENDING_SUBMIT) → broker submit → SUBMITTED/ACCEPTED → fills →
FILLED → Decision outcome → CLOSED`.

- `client_order_id` = `qp-<decision uuid>` — deterministic, so retries cannot
  double-submit.
- Unknown-outcome submissions are marked `ERROR` and resolved by
  reconciliation; a broker failure while *building* the risk context resolves
  the decision to `FAILED` rather than leaving an orphaned `PROPOSED` row.
- `sync_orders` polls non-terminal orders and ingests fills idempotently.

## Backtesting and comparison

Bar-driven event loop with no lookahead: intents produced on bar *i* fill at
bar *i+1* open through the same `SimulatedBroker` implementing the same
`Broker` ABC. `scripts/compare_personas.py` runs every strategy across the
symbol set, sized from the profile so results are comparable, with
buy-and-hold as a reference line computed the same way.

## Dashboard

Read-only, server-shell plus a JSON endpoint. The shell carries no data so it
needs no token; `/dashboard/data` requires the admin token as a header so it
never lands in a URL or access log. Machine vocabulary is translated to
sentences server-side (`app/dashboard/copy.py`), and chart framing refuses to
zoom a near-flat equity series into false drama.

## Failure-mode defaults

- No market data / stale data → reject intents (fail closed).
- Broker errors accumulate → circuit breaker trips automatically.
- Kill switch halts all order flow in every process instantly (DB-backed).
- Webhooks: constant-time passphrase, optional HMAC, freshness window,
  dedupe, secrets redacted, duplicates return 200 so retries never storm.
- Admin API disabled unless a token is configured; all mutations require a
  named actor and reason and are audited.
