# Investor persona research

Researched methodologies of famous traders/investors, documented for
implementation as **strategy rules derived from price data** — not
copy-trading of their real-time or disclosed trades.

Scope decided with the owner: **technical traders** and **macro/portfolio
allocators**, implemented within the platform's current limits (single-symbol
decisions, daily bars, long-only, no leverage). Where a methodology cannot be
faithfully represented under those limits, this document says so plainly and
describes the honest approximation instead of pretending otherwise.

> Every persona below is a *documented public methodology*, not an endorsement
> and not the person's actual current trading. Educator track records are
> generally unverified. Backtest results in the source material are subject to
> data-mining, survivorship, and post-publication decay.

---

## Family A — Technical traders

### A1. Mark Minervini — Trend Template (SEPA)

The single most mechanizable persona in this set: 7 of its 8 criteria are pure
price/moving-average relationships computable from daily bars alone.

**The 8 published criteria** (all must pass simultaneously; failing one
eliminates the stock regardless of other merits):

1. Price is above both the 150-day and 200-day moving averages.
2. The 150-day MA is above the 200-day MA.
3. The 200-day MA is trending up for at least 1 month (preferably 4–5 months).
4. The 50-day MA is above both the 150-day and 200-day MAs.
5. Price is above the 50-day MA.
6. Price is at least 25% above its 52-week low (30% in *Trade Like a Stock
   Market Wizard*).
7. Price is within 25% of its 52-week high (closer is better).
8. Relative Strength ranking ≥ 70 (as reported by Investor's Business Daily).

**Implementable today**: criteria 1–7 completely. **Criterion 8** requires a
relative-strength percentile against a universe — impossible for one symbol in
isolation. Honest approximation: relative strength *versus a benchmark index*
(e.g. 12-month return of the symbol minus SPY's), which captures the intent
(outperformance) but is **not** IBD's 1–99 universe percentile. The
implementation should skip criterion 8 when no benchmark data is available and
record that it did so.

**VCP (Volatility Contraction Pattern)** — successive pullbacks of decreasing
depth on decreasing volume before a breakout. Partially mechanizable (measure
each pullback's peak-to-trough depth and require each contraction to be
smaller than the last, typically 2–4 contractions), but *which* swings count
as contractions is discretionary. Treat any VCP implementation as an
approximation of a discretionary pattern.

Sources: [ChartMill guide](https://www.chartmill.com/documentation/stock-screener/technical-analysis-trading-strategies/496-Mark-Minervini-Trend-Template-A-Step-by-Step-Guide-for-Beginners) ·
[Deepvue](https://deepvue.com/screener/minervini-trend-template/) ·
[AskLivermore](https://asklivermore.com/docs/minervini)

---

### A2. William O'Neil — CAN SLIM

A hybrid fundamental + technical system. **Only the technical half is
implementable without fundamental data.**

| Letter | Criterion | Implementable on daily bars? |
|---|---|---|
| **C** — Current quarterly earnings | EPS up ≥ 25% YoY, accelerating | ❌ needs earnings data |
| **A** — Annual earnings | ≥ 25% growth over 3 years | ❌ needs earnings data |
| **N** — New high / new product | Breakout to new highs from a base | ✅ price-based |
| **S** — Supply & demand | Breakout volume ≥ 50% above average | ✅ volume-based |
| **L** — Leader vs laggard | High relative strength | ⚠️ needs benchmark |
| **I** — Institutional sponsorship | Increasing fund ownership | ❌ needs 13F/ownership data |
| **M** — Market direction | Trade only in confirmed uptrends | ✅ index trend filter |

**Risk rules (fully implementable, and the system's spine)**:
- Cut losses at **7–8% below the purchase price**, no exceptions.
- Take profits around **20–25%** gains (O'Neil's general guidance).
- **Buy point**: a "proper pivot" — close above the highest point of the
  handle/base on volume ≥ 50% above average.

**Cup-with-handle geometry** is partially mechanizable (base depth, duration,
handle in the upper half of the base, handle drift downward), but identifying
"a sound base" is substantially discretionary.

**What to build**: an honest **"CAN SLIM technical subset"** — base breakout
to new highs + volume confirmation + market/trend filter + the 7–8% stop and
20–25% target — explicitly named so it is never mistaken for full CAN SLIM.
The fundamental letters (C, A, I) are simply absent and must be documented as
absent.

Sources: [AAII](https://www.aaii.com/journal/article/william-oneil-can-slim-approach-to-selecting-growth-stocks) ·
[Wikipedia](https://en.wikipedia.org/wiki/CAN_SLIM) ·
[TraderLion cup-and-handle](https://traderlion.com/technical-analysis/cup-and-handle-pattern/) ·
[7% sell rule](https://marketgenius.app/articles/explainers/what-is-the-7-percent-sell-rule)

---

### A3. Paul Tudor Jones — the 200-day rule

The simplest and most robust persona here, and genuinely his documented
public heuristic.

- **"Nothing good happens below the 200-day moving average."** Above it, the
  trend is favorable and he is willing to be long; below it he becomes
  defensive and reduces size. He applies it across asset classes.
- **5:1 risk/reward**: risking 1 to make 5, which permits a 20% hit rate
  while remaining profitable.

**Implementable**: entirely, and it maps cleanly to long-only — long above the
200-day MA, flat below it, with an optional 5:1 target/stop bracket. This
doubles as an excellent **regime filter overlay** for every other strategy.

Sources: [Meb Faber on PTJ](https://mebfaber.com/2014/11/06/paul-tudor-jones-on-the-200-day-moving-average/) ·
[TurtleTrader](https://www.turtletrader.com/trader-jones/) ·
[SG Markets](https://insight-public.sgmarkets.com/alternative-view/nothing-good-happens-below-the-200-day-moving-average)

---

### A4. TJR — Smart Money Concepts (⚠️ largely NOT implementable here)

**What the research actually shows**: TJR is an intraday trader teaching a
"smart money concepts" (SMC/ICT-derived) methodology built around:

- **Session liquidity pools** — the Asia session high/low plotted as liquidity
  to be swept during London and New York sessions.
- **Liquidity sweeps** — price piercing a session extreme, signaling
  institutional liquidity was tapped, most reliable near session opens
  (Asia 18:00, London 03:00, New York 09:30 ET).
- **Fair Value Gaps** — 3-candle imbalances left by an impulse move.
- **Break of Structure (BoS)** confirming a reversal, with entries on the
  retracement into the FVG/order block.
- Key levels drawn on 1H/4H, entries confirmed on lower timeframes.
- Risk ≈ 1% per trade, partial profit-taking.

**Why it does not fit this platform as-is**: the entire method is
**session-based and intraday** (1m–15m entries around specific clock times).
On daily bars there are no sessions, no killzones, and no intraday sweeps —
the defining structure of the strategy disappears. Building a "TJR strategy"
on daily bars would be a strategy that is *not* TJR's.

**Additional honesty**: SMC/ICT concepts are widely criticized as repackaged
supply/demand price action with rules loose enough to be unfalsifiable after
the fact, and retail educator performance claims are typically unverified.

**The honest approximation available today** — a **daily failed-breakdown /
liquidity-sweep reversal**: price trades below the prior N-day swing low
(sweeping the stops resting there) but *closes back above it*, with a
follow-through confirmation. This borrows the *idea* of a liquidity sweep and
is a well-known standalone pattern (cf. Raschke's "Turtle Soup"), but it must
be named for what it is — a daily sweep-reversal pattern, **not TJR's
strategy**. Faithful TJR replication requires intraday bars and session
awareness, which is a genuine platform expansion, not an approximation.

Sources: [Forex.in.rs on TJR](https://www.forex.in.rs/tjr-trader-strategy/) ·
[TJR liquidity sweep notes](https://coconote.app/notes/072850b2-e54c-4ae8-816c-4d6b0a67cdb7) ·
[TradingView TJR indicator](https://www.tradingview.com/script/M6EyQhlQ-TJR-Trades-Strategy/)

---

## Family B — Macro & portfolio allocators

All three below are **multi-asset by nature**. The platform decides one symbol
at a time, so they are implemented via a **target-weight sleeve** pattern: one
strategy instance per symbol holding that symbol's target weight, with
rebalance bands. Run the full set together and the portfolio emerges. This is
a faithful decomposition — the only thing lost is simultaneous atomic
rebalancing across legs.

### B1. Ray Dalio — All Weather (retail approximation)

Widely published retail allocation:

| Asset | Weight | ETF proxy |
|---|---|---|
| US stocks | 30% | VTI / SPY |
| Long-term Treasuries | 40% | TLT |
| Intermediate Treasuries | 15% | IEI |
| Gold | 7.5% | GLD |
| Commodities | 7.5% | DBC |

Built on risk parity — equalizing each asset's *risk* contribution rather than
its dollar weight — designed around a four-quadrant framework (growth and
inflation, each rising or falling).

**Honest caveat**: this retail version is **not** Bridgewater's actual All
Weather, which uses leverage and futures to equalize risk. The unlevered
retail version is a fixed-weight portfolio inspired by the concept. Its
backtested record is also flattered by the multi-decade bond bull market that
ended in 2022 — the 40% long-duration sleeve was severely punished that year.

Sources: [LazyPortfolioETF](http://www.lazyportfolioetf.com/allocation/ray-dalio-all-weather/) ·
[Optimized Portfolio](https://www.optimizedportfolio.com/all-weather-portfolio/) ·
[PortfoliosLab](https://portfolioslab.com/portfolio/ray-dalio-all-weather)

### B2. Harry Browne — Permanent Portfolio

25% each: stocks / long-term bonds / gold / cash. Designed to hold something
that thrives in each of prosperity, recession, inflation, deflation.

**Rebalancing rule (the distinctive part)**: Browne explicitly did *not*
advocate calendar rebalancing. Rebalance only when any asset falls below
**15%** or rises above **35%** of the portfolio — otherwise leave it alone.

Sources: [QuantifiedStrategies](https://www.quantifiedstrategies.com/harry-brownes-permanent-portfolio/) ·
[Optimized Portfolio](https://www.optimizedportfolio.com/permanent-portfolio/)

### B3. Gary Antonacci — Dual Momentum (GEM)

Monthly, using 12-month total returns:

1. **Relative momentum** — compare trailing 12-month return of US equities
   (S&P 500) vs foreign equities (ACWI ex-US); select the higher.
2. **Absolute momentum** — if that winner's 12-month return exceeds cash
   (T-bills), hold it; otherwise hold investment-grade bonds (US Aggregate).

Antonacci cites Jegadeesh & Titman and Moskowitz/Ooi/Pedersen for the
12-month lookback, preferring it for out-of-sample support, fewer trades, and
tax efficiency.

**Constraint note**: relative momentum is inherently a *comparison between two
symbols*, which a single-symbol strategy cannot do alone. Implementable via a
benchmark/peer series supplied in strategy features (see spec) — otherwise
only the **absolute momentum** half is available, which reduces to the TSMOM
strategy already built.

Sources: [Antonacci extended backtest](https://medium.com/@garyantonacci_30463/extended-backtest-of-global-equities-momentum-dual-momentum-eb12902612e0) ·
[ReSolve craftsman's perspective](https://investresolve.com/inc/uploads/pdf/global-equity-momentum-a-craftsmans-perspective.pdf) ·
[QuantifiedStrategies](https://www.quantifiedstrategies.com/dual-momentum-trading-strategy/)

---

## Implementability summary

| Persona | Fidelity achievable now | Blocker |
|---|---|---|
| Minervini Trend Template | **High** (7/8 criteria exact) | RS rank needs universe percentile |
| PTJ 200-day rule | **Complete** | — |
| O'Neil CAN SLIM | **Partial** (technical half + risk rules) | earnings & ownership data |
| Permanent Portfolio | **High** via sleeves | atomic multi-leg rebalance |
| All Weather | **High** via sleeves | unlevered ≠ Bridgewater's |
| Dual Momentum GEM | **Partial** (absolute half) | relative leg needs peer series |
| TJR / SMC | **Low — do not claim fidelity** | intraday bars + sessions |
