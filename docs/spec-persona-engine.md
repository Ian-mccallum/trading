# Spec — Investor Persona Engine

**Goal**: let the owner run the platform "as" any of several famous
methodologies, blend them, tune them, and have the learning layer discover
which persona suits which market regime — without weakening any safety
guarantee.

**Scope decision (owner)**: replicate *documented strategies derived from
price data*, **not** copy-trading anyone's real or disclosed trades. Stay
within current platform limits — single-symbol decisions, daily bars,
long-only, no leverage — and approximate where a method doesn't fit, labeling
the approximation honestly.

Research backing every rule below: [investor-personas-research.md](investor-personas-research.md).

---

## 1. What a "persona" is

A persona is **not** a new abstraction. It is a curated, named bundle of:

- one or more registered `Strategy` versions (existing `strategies` table),
- their parameter presets,
- a recommended symbol set,
- optional overlay settings (regime filter, stop/target bracket),
- documentation of fidelity and known gaps.

This keeps every existing invariant intact: personas produce `TradeIntent`s
that pass through the same risk engine, the same order lifecycle, the same
audit spine. **A persona is a preset, not a privilege.**

Persona metadata lives in a new `personas` table (name, description,
fidelity_note, symbol_set, member strategy ids + weights) plus a
`docs/personas/` entry. No persona can trade until its member strategies are
individually approved via the existing admin flow.

---

## 2. Minimal architectural additions

Three contained changes. Nothing else in the platform moves.

### 2.1 Benchmark series in `StrategyContext.features` — ✅ BUILT

Several personas need "is this symbol beating the market?" (Minervini
criterion 8, O'Neil's *L*, Dual Momentum's relative leg). The worker loads a
configurable benchmark symbol once per cycle and injects a compact summary:

```python
features["benchmark"] = {
    "symbol": "SPY",
    "returns": {"return_1m": 0.012, "return_3m": 0.041,
                "return_6m": 0.09, "return_12m": 0.183},
}
```

Summary-only, deliberately: shipping the benchmark's full close series would
bloat every context and tempt strategies into ad-hoc cross-symbol logic that
the single-symbol contract does not support. Lookbacks that lack history are
simply absent from `returns`.

Strategies **must degrade gracefully** when the key is absent — skip the
criterion and record `rs_skipped: "true"` in the intent context so the
decision audit shows the screen ran weaker than specified. `read_benchmark`
returns `None` for any malformed input rather than a half-valid view, so
consumers can trust the None. This keeps strategies pure: everything arrives
through the context, no I/O.

*Config*: `STRATEGY_BENCHMARK_SYMBOL=SPY` (empty disables injection).

### 2.2 Target-weight sleeve strategy — ✅ BUILT

The mechanism that makes multi-asset portfolios expressible one symbol at a
time. `TargetWeightSleeve` params:

| Param | Default | Meaning |
|---|---|---|
| `symbol` | null | binds the sleeve to its own leg (**required in live/paper**; see below) |
| `target_weight` | required | fraction of equity this symbol should hold |
| `band_rel` | 0.25 | band as a *fraction of target* — scales across large and small legs |
| `band_abs` | null | explicit band in portfolio-weight units; overrides `band_rel` |
| `min_trade_notional` | 100 | suppress dust trades |
| `regime_filter_sma` | null | optional: flatten the leg below this SMA |

**Two design corrections found while building** (both now enforced by tests):

1. **The band must scale with the target.** A fixed 0.10 absolute band is
   wider than All Weather's 7.5% gold target, so that leg could *never* be
   bought from flat. `band_rel` is therefore the default, and the constructor
   now **rejects** any config where the effective band ≥ target weight rather
   than silently producing a dead sleeve. Browne's canonical 15–35% rule is
   still expressible exactly as `target_weight: 0.25, band_abs: 0.10`.
2. **Sleeves must bind to a symbol.** The strategy loop offers every approved
   strategy every allowlisted symbol; an unbound 30% sleeve would try to hold
   30% of *each* symbol. `symbol` binds the leg and the sleeve returns no
   intents for anything else. Unbound is permitted only for backtests, which
   drive a single symbol explicitly.

Behavior: compute current weight = position market value ÷ equity. If below
target − band → BUY the shortfall; if above target + band → SELL the excess
(long-only sells are already permitted and enforced by `NoShortSellRule`).
Deterministic and pure — equity and position already arrive in the context.

All Weather = five sleeve instances (VTI .30, TLT .40, IEI .15, GLD .075,
DBC .075). Permanent Portfolio = four at .25 with band .10. Weights are
fractions of the profile's `capital_allocation`, not of total account equity —
a third correction found in Phase 4, and the real reason portfolio personas
were structurally unable to trade. The only fidelity
loss versus a true portfolio strategy is non-atomic rebalancing across legs,
which is acceptable at daily cadence and is documented.

### 2.2b Cooperative orchestration — ✅ FIXED in Phase 2

Phase 0 exposed this: the strategy loop asked the allocator to select **one**
strategy per symbol for PAPER and shadowed the rest. Right for *competing*
strategies, wrong for *cooperating* ones — All Weather's five legs are one
portfolio, so the loop would have held a single leg and silently shadowed the
other four.

Resolved by `app/personas/orchestration.py`, a pure module (no DB, broker, or
clock) that assigns each approved strategy its mode in priority order:

1. Member of an **inactive/retired** persona → SHADOW, always. This is what
   makes deactivation safe and reversible: no terminal status change is
   needed to pull a persona out of production, and members can never silently
   fall back into the competing pool.
2. Member of an **active cooperative** persona → PAPER, always. Legs are a
   portfolio, not candidates.
3. Everything else → the competing pool, where the allocator selects one per
   regime exactly as before. Non-selected members still run SHADOW so the
   learner keeps collecting counterfactuals.

Rule 1 deliberately outranks rule 2: a strategy in both an active cooperative
persona and an inactive one does not trade. The conservative reading wins.

### 2.3 Persona registry + admin surface

- `personas` table + `scripts/seed_personas.py`.
- `GET /admin/personas` — list with fidelity notes and member status.
- `POST /admin/personas/{id}/activate` — convenience that approves all member
  strategies **through the existing audited transition path** (same actor +
  reason requirement, same `promotion_events` rows). It is sugar over the
  existing endpoint, never a bypass.

---

## 3. Personas to implement

### Phase 1 — high fidelity, no new data

| Persona | Strategy class | Notes |
|---|---|---|
| **Paul Tudor Jones** ✅ | `TrendRegime200` | Long above 200-day SMA, flat below. Optional 5:1 bracket (off by default). Complete fidelity. |
| **Mark Minervini** ✅ | `MinerviniTrendTemplate` | All 8 criteria; #8 via benchmark RS, skipped **and flagged** if absent. VCP deferred — genuinely discretionary. |
| **Harry Browne** | `TargetWeightSleeve` ×4 | 25/25/25/25, 15–35% bands. |
| **Ray Dalio (retail AW)** | `TargetWeightSleeve` ×5 | 30/40/15/7.5/7.5. Labeled *unlevered retail approximation*. |

### Phase 2 — partial fidelity, clearly labeled

| Persona | Strategy class | Gap to document |
|---|---|---|
| **O'Neil (technical subset)** ✅ | `ONeilBreakout` | Base breakout to new high + volume ≥ 1.5× average + market filter + 7–8% stop + 20–25% target. **C, A, I letters absent** (no earnings/ownership data). |
| **Dual Momentum** ✅ | `DualMomentum` | Absolute leg only unless a peer series is supplied; relative leg needs `features["peers"]`. |
| **Daily sweep-reversal** ✅ | `SweepReversal` | Inspired by liquidity-sweep logic (and Raschke's Turtle Soup). **Explicitly NOT branded as TJR.** |

### Not implemented — and why (must stay documented)

**TJR / SMC proper**: session-based intraday methodology (Asia/London/NY
killzones, 1m–15m entries, FVGs). Daily bars destroy the structure that
defines it. Requires intraday bar ingestion + session-aware context — a real
platform expansion, out of the agreed scope. Anything built on daily bars and
called "TJR" would be a misrepresentation.

---

## 4. "Customizable to me" — the profile layer

A single `profile.toml` (TOML, not YAML: stdlib `tomllib` avoids a new
dependency) capturing owner-level
preferences, consumed at intent-construction time — **never** at risk-check
time, so it can only ever be more conservative than the risk engine:

```yaml
name: ian
capital_allocation: 25000        # notional the bot may work with
risk_appetite: moderate          # maps to sizing multiplier + which presets load
max_positions: 5
symbols: [SPY, QQQ, AAPL, MSFT]
benchmark: SPY
personas:
  - {name: paul_tudor_jones, weight: 0.4}
  - {name: minervini,        weight: 0.4}
  - {name: permanent_portfolio, weight: 0.2}
exclusions: [TSLA]               # never trade these
trading_hours: regular
notifications: {on_rejection: true, on_fill: true}
```

Persona weights scale position sizing within that persona's sleeve; they do
**not** override any `RISK_*` limit. The risk engine remains the final,
independent authority — profile weights are inputs to sizing, and the engine
still evaluates every resulting intent.

---

## 5. "Insanely smart" — how this feeds the learning layer

The existing learning subsystem already does the hard part; personas make its
job meaningful:

- **Regime attribution**: every decision already records the market regime.
  With several personas live, `performance_reports` accumulate per
  (persona-strategy × regime) — exactly the shape the allocator trains on.
- **Shadow everything**: non-selected personas run in SHADOW mode, so the
  platform learns how Minervini *would* have done during a Dalio-favoring
  regime, at zero risk.
- **Champion/challenger over personas**: existing machinery, new subjects —
  "does Minervini beat PTJ in `trend_up`?" answered from recorded outcomes
  with a recommendation, never an auto-promotion.
- **The allocator's artifact already keys on strategy name per regime**, so
  persona members slot in with no model changes.

Deliberately unchanged: learning still cannot promote itself, bypass risk, or
write code. Personas are additional *candidates*, not additional authority.

---

## 6. Phasing

| Phase | Deliverable | Depends on |
|---|---|---|
| **0** ✅ | Benchmark injection + `TargetWeightSleeve` + 50 tests + seeded All Weather / Permanent Portfolio legs | — |
| **1** ✅ | PTJ (`ptj_trend`) + Minervini (`minervini_trend_template`) + 39 tests + seeds; portfolios seeded in Phase 0 | 0 |
| **2** ✅ | `personas` + `persona_members` tables, orchestration fix, admin endpoints, 5 seeded personas, 24 tests | 1 |
| **3** ✅ | O'Neil technical subset, sweep reversal, dual momentum + peer-series injection, 57 tests | 0 |
| **4** ✅ | `profile.toml` layer + position sizing + 46 tests | 2 |
| **5** ✅ | `scripts.compare_personas`: all strategies backtested on real history vs buy-and-hold | 1–4 |

Phase 5 is the point of the whole exercise: real data, real backtests, then
shadow, then a human promotion decision.

---

## 7. Explicit non-goals

- No copy-trading of disclosed or real-time trades (owner's decision).
- No options, shorting, leverage, or intraday execution.
- No persona may bypass the risk engine, the kill switch, or the live gates.
- No claim of fidelity where the research says fidelity is impossible.
