# Comparing personas

```bash
.venv/bin/python -m scripts.compare_personas --symbols SPY QQQ AAPL
.venv/bin/python -m scripts.compare_personas --save   # persist reports
```

Backtests every registered strategy across the symbol set on real stored bars
and ranks them, with two things a naive comparison would omit:

**Every strategy is sized from the profile.** Comparing a strategy holding one
$740 share against one holding $5,000 of the same symbol measures position
size, not skill. The runner overrides each strategy's `qty` so all entrants
risk similar capital.

**Buy-and-hold appears as the reference line, sized identically.** Returns are
expressed on the backtest's full capital, so the reference is computed the same
way — otherwise the comparison silently flatters every strategy.

## Reading it honestly

The first real run (SPY/QQQ/AAPL, Jul 2024 → Jul 2026) had **every strategy
trailing buy-and-hold**, by 1–3 percentage points. That is the correct result,
not a bug: the window was a strong bull market, and any strategy that spends
time in cash necessarily lags a rising asset. Trend and reversion systems earn
their keep in drawdowns, which this window barely contained.

Three cautions that belong next to any ranking from this tool:

- **Two years is far too short** to separate skill from luck. The mean-return
  column is a ranking aid, not evidence.
- **These are single-symbol backtests**, so they say nothing about how a
  persona behaves as part of a portfolio.
- **Backtests have no slippage beyond the configured model, no partial fills,
  and perfect data.** Live results will be worse.

The intended use is comparative and diagnostic — does this strategy behave the
way its source describes? — not predictive.
