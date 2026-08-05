# Operations runbook

All admin calls need `-H "X-Admin-Token: $ADMIN_API_TOKEN"`. Every mutation
takes `{"actor": "<your name>", "reason": "<why>"}` and is written to the
audit tables (`risk_events`, `promotion_events`).

## Daily checks

```bash
curl -s localhost:8010/health
curl -s localhost:8010/admin/status -H "X-Admin-Token: $T" | jq
curl -s "localhost:8010/admin/risk-events?limit=50" -H "X-Admin-Token: $T" | jq
curl -s "localhost:8010/admin/decisions?limit=20" -H "X-Admin-Token: $T" | jq
```

Healthy state: kill switch off, breaker not tripped, decisions flowing with
`risk_verdict: approved` or explainable rejections.

## Incident: stop all trading NOW

```bash
curl -X POST localhost:8010/admin/kill-switch -H "X-Admin-Token: $T" \
  -H 'Content-Type: application/json' \
  -d '{"engage": true, "actor": "you", "reason": "incident"}'
```

Takes effect on the next risk evaluation in every process (DB-backed, no
restart needed). Open orders at the broker are NOT auto-canceled — cancel
them in the Alpaca dashboard if needed. Release with `"engage": false`.

## Circuit breaker tripped

`admin/status` shows `circuit_breaker.tripped: true` after
`RISK_CIRCUIT_BREAKER_ERRORS` broker errors within the window. It clears
itself after the cooldown, or manually once the cause (Alpaca outage, bad
credentials, network) is fixed:

```bash
curl -X POST localhost:8010/admin/circuit-breaker/reset -H "X-Admin-Token: $T" \
  -H 'Content-Type: application/json' -d '{"actor":"you","reason":"alpaca recovered"}'
```

## Common rejection reasons (`risk_events.detail.reason`)

| Rule | Meaning | Fix |
|---|---|---|
| `stale_data` | no bar newer than `RISK_MAX_DATA_AGE_SECONDS` | check worker `refresh_market_data`, Alpaca data keys |
| `duplicate_order` | same symbol+side within the window | expected on signal storms; widen/narrow via env |
| `daily_loss` / `drawdown` | loss limits hit | deliberate halt — investigate before loosening limits |
| `symbol_allowlist` | symbol not in `RISK_SYMBOL_ALLOWLIST` | extend the allowlist deliberately |
| `no_short_sell` | sell exceeds held qty | platform is long-only by design |
| `live_gate` | live intent while not fully gated+armed | expected unless you are going live |

## Strategy lifecycle

```text
candidate ──approve──▶ approved ──(optional)──▶ champion ──▶ retired
```

```bash
# find ids
docker compose exec postgres psql -U trading -d trading \
  -c "SELECT id, name, version, status FROM strategies;"
curl -X POST localhost:8010/admin/strategies/<id>/status -H "X-Admin-Token: $T" \
  -H 'Content-Type: application/json' \
  -d '{"to_status":"approved","actor":"you","reason":"backtest + shadow reviewed"}'
```

Approved strategies enter the worker loop (paper + shadow). New parameters =
new strategy version row (via `scripts/seed.py` pattern), never edit in place.

## Model lifecycle (allocator)

Nightly training registers inert versions. Review then promote:

```bash
docker compose exec postgres psql -U trading -d trading \
  -c "SELECT id, name, version, status, metrics FROM model_versions ORDER BY version DESC;"
# registered -> shadow (observe for a while) -> champion
curl -X POST localhost:8010/admin/models/<id>/promote -H "X-Admin-Token: $T" \
  -H 'Content-Type: application/json' \
  -d '{"to_status":"shadow","actor":"you","reason":"metrics reviewed"}'
```

Promoting a new champion automatically retires the old one (audited).
Champion-vs-challenger experiments write recommendations to `experiments`;
they never promote anything themselves.

## Going live (deliberately laborious)

Do not do this until weeks of paper history look sane.

1. Run a **separate deployment** (new DB/host) for live — never repoint the
   paper one.
2. `.env`: `TRADING_ENV=live`, `LIVE_TRADING_ENABLED=true`,
   `LIVE_TRADING_CONFIRMATION=<exact phrase from app/config.py>`,
   `ALPACA_LIVE_API_KEY/SECRET`. Tighten every `RISK_*` limit first.
3. Restart. Verify `admin/status` shows
   `config_gates_satisfied: true, runtime_armed: false, effective: false`.
4. Arm: `POST /admin/live/arm {"armed": true, ...}` (refused unless step 2 is
   complete).
5. Watch `admin/risk-events` closely. Disarm or kill-switch at the first
   anomaly. The drill: kill switch first, diagnose second.

## Backups & retention

The DB is the system of record (decisions, orders, fills, audit). Snapshot
PostgreSQL daily (`pg_dump`), before every schema migration, and before any
live-trading change.

## Owner profile and sizing

`profile.toml` decides how much capital the bot works with and how it sizes.
It can only ever be more conservative than the risk engine.

```bash
cp profile.example.toml profile.toml   # then edit
.venv/bin/python -m scripts.compare_personas --symbols SPY QQQ AAPL
```

**Keep the risk limits coherent with `capital_allocation`.** The shipped
defaults ($11k order/position caps, $26k gross) are sized for the default
$25,000 allocation, whose largest persona leg is All Weather's 40% bond sleeve
at $10,000. If you raise `capital_allocation`, raise the `RISK_*` values to
match — otherwise the engine will correctly reject the resulting orders and
the symptom will look like "the portfolio personas do nothing".

The reverse is safe: lowering `capital_allocation` never needs a risk change.

## Activating a portfolio persona

```bash
T="$ADMIN_API_TOKEN"
curl -s localhost:8010/admin/personas -H "X-Admin-Token: $T" | jq -r \
  '.[] | select(.name=="permanent_portfolio") | .id'
curl -X POST localhost:8010/admin/personas/<id>/activate -H "X-Admin-Token: $T" \
  -H 'Content-Type: application/json' \
  -d '{"actor":"you","reason":"backtests and shadow reviewed"}'
```

All member legs are approved through the audited path and trade together.
Deactivate to stop it: members stay approved but go shadow-only, so it is
fully reversible.

## Incident: positions grew far beyond their limits

Symptom: broker cash goes deeply negative, position value greatly exceeds
equity, and every order is rejected with `order_notional exceeds max`.

Two bugs caused this once and are now fixed, but the diagnosis is worth
keeping:

1. **Exits were being blocked.** `OrderNotionalRule` had no risk-reducing
   exemption, so a position that grew past the cap could never be closed —
   every exit itself exceeded the limit. A size cap must never block
   de-risking; the rule now approves any order that shrinks a position, as
   `position_limit` and `gross_exposure` always did.
2. **Re-entry while orders were queued.** Orders placed outside market hours
   stay working for hours. Until they fill the broker reports no position, so
   a "buy when flat" strategy bought again every 15-minute loop — 45 stacked
   entries in one day, all filling together at the open. The duplicate guard
   now rejects an entry whenever a same-side order is still working,
   regardless of age.

If you hit a stuck over-exposed account:

```bash
# 1. Halt everything first.
curl -X POST localhost:8010/admin/kill-switch -H "X-Admin-Token: $T" \
  -H 'Content-Type: application/json' \
  -d '{"engage":true,"actor":"you","reason":"over-exposed"}'

# 2. Inspect the damage.
.venv/bin/python -m scripts.status
```

Then either reset the Alpaca paper account (fastest, wipes the bad state) or
close positions from the Alpaca dashboard. Release the kill switch only once
positions and cash look sane.
