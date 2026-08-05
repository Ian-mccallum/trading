# The learning system

## What it learns

**Which approved strategy to run under which market conditions.** Not new
strategies, not code, not risk limits. The unit of learning is the
*strategy allocator*: a versioned artifact scoring each strategy per market
regime, trained offline from recorded decision outcomes.

## The data loop

```
decision (context: features + regime)          ← stamped at trade time
    → fills / shadow reference price
    → evaluate_decision_outcomes (hourly)      ← realized / hypothetical PnL
    → decisions CLOSED with outcome
    → train_allocator (nightly, offline)       ← regime × strategy weighted returns
    → model_versions (REGISTERED, inert)
    → human review → shadow → champion         ← admin API only
    → champion allocator picks strategy per regime next loop
```

Every decision row carries the exact features/regime the platform saw at
decision time, so training never needs to reconstruct the past — and any
model version can be reproduced or audited from the `decisions` table alone.

## Regimes and features

`app/learning/features.py` computes deterministic features (returns, SMA
ratio, annualized realized vol, Wilder RSI, ATR%) and classifies four
regimes: `trend_up`, `trend_down`, `high_vol_range`, `low_vol_range`
(`unknown` when history is short). Thresholds are constants, not learned —
simple, inspectable, and stable for attribution.

## Counterfactual data: shadow mode

Each strategy-loop tick, the champion-selected strategy trades on paper.
Every *other* approved strategy still runs — in `SHADOW` mode: risk-checked,
recorded with a reference price, never routed to a broker. Shadow outcomes
are evaluated exactly like real ones, so the trainer sees how every strategy
would have done in every regime, not just the incumbent.

## The artifact (`regime_scores_v1`)

```json
{
  "type": "regime_scores_v1",
  "scores":  {"trend_up": {"sma_cross": 0.031, "rsi_reversion": -0.004}},
  "default": {"sma_cross": 0.012, "rsi_reversion": 0.003}
}
```

Scores are exponentially time-weighted mean returns (half-life 7 days,
lookback 30 days). JSON on purpose: inspectable in a psql query, diffable
across versions, no pickle/opaque weights. A richer model (e.g. a contextual
bandit) would slot in behind the same registry + allocator interface.

## Champion vs challenger

`compare_champion_challenger` compares closed-decision performance attributed
to two model versions over a window and writes an `experiments` row with
`recommendation: promote_challenger | keep_champion | insufficient_data`
(minimum 10 closed decisions per side). **It never changes statuses** — the
recommendation is input to a human decision.

## Hard boundaries (enforced, not policy)

| The learner cannot… | Because |
|---|---|
| submit or modify orders | no import path to brokers/execution; only `ExecutionService` submits, always through the risk engine |
| promote itself | `promote()` requires a non-empty human actor; called only by the admin API; illegal transitions raise |
| skip shadow | transitions limited to registered→shadow→champion (plus →retired) |
| trade unapproved strategies | allocator defensively filters to APPROVED/CHAMPION rows |
| rewrite production code | artifacts are data (JSON in `model_versions`); strategy classes load only from the hardcoded registry |
| loosen risk limits | risk engine reads config + `system_controls` only; learning has no write path to either |

Every status change lands in `promotion_events` with actor and reason.
