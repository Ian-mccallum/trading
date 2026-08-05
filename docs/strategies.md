# Strategy catalog

Researched, published strategy archetypes implemented as configurable
options. Every strategy is a versioned DB row (`strategies` table): the row's
`params` are the settings, and changing them means registering a new version.
All are **long-only, unleveraged, daily-bar, pure functions** — and none of
them is a recommendation. They exist so the platform's backtest → shadow →
champion pipeline has credible, well-understood material to evaluate.

> The famous quant firms' actual production signals (RenTec, Two Sigma,
> D.E. Shaw, AQR's live blends) are secret. What this catalog encodes is the
> *published* canon those styles rest on: the academic papers, the Turtle
> rules (the one famous firm-grade system whose exact rules are public), and
> Connors' published systems — with every simplification flagged.

## How to use

```bash
.venv/bin/python -m scripts.seed                     # registers all as CANDIDATE
.venv/bin/python -m scripts.run_backtest tsmom --symbol SPY
# approve via admin API to enter the paper/shadow loop (see docs/runbook.md)
```

New parameterizations = new versions (e.g. an aggressive Connors variant with
`entry_threshold: 5`), so results always attribute to an exact config.

---

## `tsmom` — Time-Series Momentum

**Lineage**: Moskowitz, Ooi & Pedersen, *Time Series Momentum*, JFE 2012
(AQR); Hurst/Ooi/Pedersen, *A Century of Evidence on Trend-Following*. The
core of the managed-futures industry (AQR, Man AHL, Winton, Aspect).

**Rule**: long while the trailing 12-month return is positive; flat when not
(the paper's short leg maps to flat). Evaluated monthly at the first bar of a
new month, from month-end data.

| Param | Default | Meaning |
|---|---|---|
| `lookback_days` | 252 | trailing-return window (~12 months) |
| `skip_days` | 0 | **0 is canonical for TSMOM** — the skip-month belongs to cross-sectional momentum (Jegadeesh-Titman 12-2), not TSMOM |
| `eval_frequency` | `monthly` | `daily` enables the common non-canonical generalization |
| `dead_band` | 0.0 | ignore signals with trailing return inside ±band (churn control) |
| `vol_target_annual` | null | optional MOP-style scaling `min(1, target/vol)` — shrink-only |
| `vol_window_days` | 60 | realized-vol window for the overlay (vol lagged one bar) |
| `qty` | 1 | base shares |

**Simplifications**: raw return instead of excess-over-T-bill; monthly
evaluation approximated as first-bar-of-month acting on month-end data.
**Encoded pitfalls**: no skip month; vol estimate lagged one bar (MOP chose
σ_{t-1} explicitly for "lack of look-ahead bias"); expect whipsaw when the
trailing return hovers near zero (use `dead_band`).

## `turtle_s1` / `turtle_s2` — Donchian breakout (class `DonchianBreakout`)

**Lineage**: Richard Donchian's 4-week rule → Dennis & Eckhardt's Turtle
systems (1983), published verbatim by Curtis Faith (*The Original Turtle
Trading Rules*). Seeded presets: System 1 (20/10) and System 2 (55/20).

**Rule**: buy when the close exceeds the highest high of the **preceding**
`entry_days` bars (window excludes the current bar — the family's canonical
lookahead trap, encoded correctly and tested). Exit on a close below the
preceding `exit_days` low, or on the Turtle 2N stop (close below entry −
`stop_atr_mult` × Wilder-ATR).

| Param | Default | Meaning |
|---|---|---|
| `entry_days` / `exit_days` | 20 / 10 | channel windows (S2 preset: 55/20) |
| `atr_period` | 20 | Wilder ATR ("N") period |
| `stop_atr_mult` | 2.0 | Turtle 2N hard stop; `null` disables |
| `chandelier_mult` | null | optional Chandelier exit (LeBeau/Elder): close < rolling `chandelier_lookback` high − mult×ATR |
| `chandelier_lookback` | 22 | Chandelier anchor window (rolling, StockCharts convention) |
| `vol_target_annual` | null | optional shrink-only vol-targeted sizing |
| `qty` | 1 | base shares |

**Simplifications**: no pyramiding units; no System-1 "last breakout was a
winner" skip filter (path-dependent hypothetical-trade simulation); intraday
tick-through entries approximated by close confirmation on daily bars.
**Expect**: low win rate, long flat stretches, big giveback on the 20-day
exit — canonical behavior, per Faith, not bugs to tune away.

## `connors_rsi2` — RSI(2) pullback

**Lineage**: Larry Connors & Cesar Alvarez, *Short Term Trading Strategies
That Work* (2008); RSI per Wilder (1978).

**Rule**: buy when close > 200-day SMA **and** RSI(2) < 10; exit when the
close crosses above its 5-day SMA (`exit_mode: "sma"`, canonical) or when
RSI(2) > 70 (`exit_mode: "rsi"`). Below the 200-SMA the published system
shorts — here that maps to no signal.

| Param | Default | Meaning |
|---|---|---|
| `rsi_period` | 2 | |
| `entry_threshold` | 10 | book's aggressive variant: 5 |
| `trend_sma` | 200 | integral to the published rules |
| `exit_mode` / `exit_sma` / `exit_rsi` | `sma` / 5 / 70 | |
| `qty` | 1 | |

**Encoded pitfalls**: deliberately **no stop-loss** — Connors' testing found
stops hurt this system; the risk engine's independent limits are the
guardrails. Expect ~many small wins vs rare deep losers (fat left tail).

## `double7` — Connors Double 7s

**Lineage**: same book (2008); convention pinned to Alvarez's replication.

**Rule**: buy when close > 200-day SMA and today's close is the lowest close
of the last 7 days (window includes today); exit when today's close is the
highest close of the last 7 days. Only exit — no stop, no target.
Precedence is deterministic: flat → entry check only, long → exit check only
(prevents re-entry pyramiding as new lows print).

Params: `entry_lookback` (7), `exit_lookback` (7), `trend_sma` (200), `qty`.

## `bollinger_reversion` — Bollinger band reversion

**Lineage**: bands per John Bollinger (*Bollinger on Bollinger Bands*,
2001); the lower-band-tag trade is standard practitioner construction.

**Rule**: buy when close < SMA(20) − 2×σ (σ = **population** stdev of the
same 20 closes, Bollinger's own convention); exit at the middle band.
Default 200-SMA trend gate encodes Bollinger's warning that "tags of the
bands are not signals in and of themselves" — without it a tag-buyer
averages into crashes ("walking the band"). `trend_sma: null` disables.

| Param | Default | Meaning |
|---|---|---|
| `period` / `num_std` | 20 / 2.0 | Bollinger's guidance: ~1.9σ at period 10, ~2.1σ at 50 |
| `trend_sma` | 200 | falling-knife gate; null disables |
| `min_bandwidth` | 0.0 | skip entries when (upper−lower)/middle is below this — quiet-market noise floor |
| `qty` | 1 | |

## `high_52w` — 52-week-high momentum

**Lineage**: George & Hwang, *The 52-Week High and Momentum Investing*,
Journal of Finance 2004 (anchoring bias).

**Adaptation, flagged honestly**: the published effect is cross-sectional
(rank a universe, buy the top 30%). One symbol can't be ranked, so this is
the practitioner proximity-band adaptation: buy when close is within 5% of
the 52-week high (`entry_ratio` 0.95), exit when it falls 20% below it
(`exit_ratio` 0.80). The wide hysteresis is deliberate — symmetric bands
whipsaw because the ratio oscillates just under 1.0 near highs.

Params: `window` (252), `entry_ratio` (0.95), `exit_ratio` (0.80),
`high_basis` (`high` = George-Hwang daily-high convention; `close` softer),
`qty`.

## Shared overlays

- **Volatility targeting** (`vol_target_annual` on `tsmom` and
  `DonchianBreakout`): `qty × min(1, target/realized_vol)`, vol computed
  through the *prior* bar. Shrink-only by construction — on a no-leverage
  platform the overlay can only de-risk, so calm bull markets show lower
  vol *and* lower return than unscaled; that is correct behavior (Man
  Group's research finds the Sharpe benefit concentrates in risk assets).
- **Chandelier exit** (`chandelier_mult` on `DonchianBreakout`): rolling-
  window anchor variant; the stop is evaluated per-bar (LeBeau's ratcheting
  since-entry anchor needs entry-date state the pure interface doesn't
  carry — a deliberate trade for determinism and identical backtest/live
  behavior).

## What was researched and deliberately left out

- **Short-term reversal** (Jegadeesh 1990 / Lehmann 1990): needs time-based
  exits (entry-date state), the single-symbol echo of a cross-sectional,
  small-cap-driven effect is weak, and much of the measured profit is
  bid-ask bounce. Revisit if entry metadata is added to `StrategyContext`.
- **Pairs / stat-arb, cross-sectional ranking**: require multi-symbol
  context the interface doesn't provide yet.
- **Turtle pyramiding & System-1 winner filter**: stateful; documented above.

## Sources

MOP TSMOM: https://www.sciencedirect.com/science/article/pii/S0304405X11002613 ·
AQR Century of Evidence: https://www.aqr.com/Insights/Research/Journal-Article/A-Century-of-Evidence-on-Trend-Following-Investing ·
Original Turtle Rules (Faith): https://oxfordstrat.com/coasdfASD32/uploads/2016/01/turtle-rules.pdf ·
Connors RSI-2 (ChartSchool): https://chartschool.stockcharts.com/table-of-contents/trading-strategies-and-models/trading-strategies/rsi-2 ·
Double 7s (Alvarez): https://alvarezquanttrading.com/blog/double-7s-strategy/ ·
Chandelier: https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-overlays/chandelier-exit ·
Vol targeting (Man): https://www.man.com/insights/the-impact-of-volatility-targeting ·
George & Hwang 52w-high: https://onlinelibrary.wiley.com/doi/10.1111/j.1540-6261.2004.00695.x

---

## `target_weight_sleeve` — portfolio leg (Phase 0 of the persona engine)

Not a signal strategy: a **portfolio primitive**. Holds one symbol at a target
fraction of equity and rebalances only when drift leaves a band. Multi-asset
personas are built by running one sleeve per leg — see
[spec-persona-engine.md](spec-persona-engine.md).

| Param | Default | Meaning |
|---|---|---|
| `symbol` | null | binds the sleeve to its leg; required outside backtests |
| `target_weight` | required | fraction of equity to hold (0 < w ≤ 1) |
| `band_rel` | 0.25 | band as a fraction of target (scales across leg sizes) |
| `band_abs` | null | explicit band in weight units; overrides `band_rel` |
| `min_trade_notional` | 100 | dust suppression |
| `regime_filter_sma` | null | flatten the leg below this SMA |

Seeded portfolios (all CANDIDATE until approved):

- **`permanent_portfolio_*`** — Browne: VTI/TLT/GLD/BIL at 25% each,
  `band_abs: 0.10` (his canonical "act outside 15–35%" rule).
- **`all_weather_*`** — Dalio retail approximation, **unlevered**:
  VTI 30 / TLT 40 / IEI 15 / GLD 7.5 / DBC 7.5, `band_rel: 0.25`.

Constructor refuses any config whose effective band ≥ target weight, since
such a sleeve could never be bought from flat.

---

## Persona strategies (Phase 1)

See [investor-personas-research.md](investor-personas-research.md) for lineage
and fidelity assessments, and [spec-persona-engine.md](spec-persona-engine.md)
for the engine design.

### `ptj_trend` — Paul Tudor Jones' 200-day rule

**Lineage**: PTJ's documented public heuristic — *"nothing good happens below
the 200-day moving average."* Above it he is willing to be long; below it he
turns defensive. Long-only maps this exactly, so fidelity is **complete**.

| Param | Default | Meaning |
|---|---|---|
| `sma_period` | 200 | the regime line |
| `buffer_pct` | 0.0 | dead band around the line to damp whipsaw (a deviation) |
| `stop_pct` | null | optional bracket stop; enables the 5:1 rule |
| `reward_ratio` | 5.0 | target = `stop_pct × reward_ratio` above entry |
| `qty` | 1 | |

**Interpretation flag**: PTJ's 5:1 risk/reward is a trade-selection and sizing
discipline, not a published mechanical bracket. `stop_pct` encodes its spirit
and is **off by default**.

Doubles as a benchmark: a strategy that cannot beat "long above the 200-day"
is not earning its complexity.

### `minervini_trend_template` — Minervini's Stage 2 screen

**Lineage**: Mark Minervini's SEPA Trend Template. All eight published
criteria are implemented; seven are exact price/MA relationships.

| Param | Default | Meaning |
|---|---|---|
| `sma_fast` / `sma_mid` / `sma_slow` | 50 / 150 / 200 | the three MAs |
| `slow_rising_days` | 21 | criterion 3: 200-MA rising for ~1 month |
| `window_52w` | 252 | 52-week window |
| `low_multiple` | 1.25 | ≥25% above the 52-week low (book variant: 1.30) |
| `high_ratio` | 0.75 | within 25% of the 52-week high |
| `rs_lookback` | `return_12m` | window for the RS proxy |
| `require_rs_data` | false | refuse entries when no benchmark is available |
| `exit_mode` | `template_fail` | or `sma_fast` (exit on a 50-day break) |
| `qty` | 1 | |

**Criterion 8 is an approximation, flagged at runtime.** IBD's RS rating is a
1–99 percentile against the whole market; one symbol cannot be ranked against
a universe. This uses benchmark-relative trailing return (outperformance) as
the proxy. With no benchmark the criterion is skipped and every intent carries
`rs_skipped: "true"` into the decision audit — never a silent pass. Set
`require_rs_data: true` to block entries instead.

**Scope note**: the Trend Template is a *screen* for Stage 2 uptrends.
Minervini's real entries are VCP breakouts and his exits are stop-driven.
Using the template itself as entry/exit is a documented extension, not his
complete system.

### `oneil_breakout` — CAN SLIM, technical subset

**Lineage**: William O'Neil, *How to Make Money in Stocks*. Named
`ONeilBreakout` rather than `CanSlim` on purpose — three of the seven letters
are **absent**, not approximated.

| Letter | Here? |
|---|---|
| **C** quarterly EPS +25% | ❌ needs earnings data |
| **A** annual EPS +25% / 3y | ❌ needs earnings data |
| **N** new high from a base | ✅ breakout above an N-day high |
| **S** volume surge | ✅ volume ≥ 1.5× the prior average |
| **L** leader not laggard | ⚠️ benchmark-relative return, not IBD's RS percentile |
| **I** institutional sponsorship | ❌ needs 13F/ownership data |
| **M** market direction | ✅ benchmark above its own 200-day average |

Params: `base_lookback` (50), `volume_window` (50), `volume_multiple` (1.5),
`stop_pct` (0.08), `target_pct` (0.25), `require_market_uptrend`,
`require_leader`, `rs_lookback`, `qty`.

The 7–8% stop and 20–25% target are O'Neil's risk spine and are implemented
exactly. **The stop is evaluated before the target**, so a bar through both
exits as a loss rather than being flattered into a win. Every intent records
`letters_evaluated` and `letters_absent`, and flags `market_filter_skipped` /
`leader_check_skipped` when benchmark data is unavailable.

### `sweep_reversal` — daily failed breakdown

**Not TJR.** TJR's method is session-based intraday: Asia-session liquidity
pools swept during the London/NY opens, fair value gaps, 1m–15m entries. Daily
bars have no sessions, so that structure does not exist here and anything
built on daily bars called "TJR" would misrepresent it.

**Lineage of what this actually is**: Linda Raschke's **Turtle Soup**
(*Street Smarts*, 1995) — the same underlying idea (price runs the stops under
an obvious low, then fails to hold) on a timeframe where it is a documented
standalone pattern.

Rule: today's low pierces the lowest low of the prior `lookback` (20) bars,
that low is at least `min_low_age` (4) bars old — Raschke's condition, since a
low set yesterday has not accumulated resting orders — and price closes back
**above** the swept level. Exits: `stop_pct` (3%), `target_pct` (6%), or
`give_up_below_sweep` when price closes back under the swept low and the
premise is simply wrong.

`max_hold_bars` is deliberately rejected: time stops need entry-date state the
pure strategy interface does not carry.

### `dual_momentum_spy` — Antonacci GEM, adapted

**Lineage**: Gary Antonacci, *Dual Momentum Investing* (2014). Monthly, on
12-month total returns: pick the stronger of US vs foreign equities (relative
momentum), and hold it only if it also beat cash (absolute momentum),
otherwise hold bonds.

**Adaptation**: a single-symbol platform cannot rotate, so this asks "should
this symbol be held?" — the symbol's trailing return must beat both
`cash_symbol`'s return (absolute leg) and `peer_symbol`'s (relative leg), read
from the peer summaries the worker injects.

| Param | Default | Meaning |
|---|---|---|
| `lookback_days` | 252 | ~12 months, per the paper |
| `peer_symbol` | null | the relative-leg comparison |
| `cash_symbol` | null | T-bill proxy for the absolute floor (else 0) |
| `require_peer_data` | false | refuse entries when peer data is missing |
| `eval_frequency` | `monthly` | canonical cadence |

Without peer data the relative leg is **skipped and flagged** (`peer_skipped`),
at which point this is plain absolute momentum, equivalent to `tsmom`. The bond
leg is not modelled — "hold bonds instead" is portfolio rotation, which the
target-weight sleeve expresses; pair this with a bond sleeve for the full
intent.

## Shared: peer and benchmark features

The worker loads every allowlisted symbol once per cycle and injects compact
summaries into `StrategyContext.features`:

- `features["benchmark"]` — trailing returns of `STRATEGY_BENCHMARK_SYMBOL`
  plus `above_trend` (is it above its own 200-day average), which is what makes
  O'Neil's *M* letter genuinely implementable.
- `features["peers"]` — the same summary per symbol, enabling cross-symbol
  comparisons without breaking the single-symbol strategy contract.

Both are optional. Every consumer degrades gracefully and records that it did.
