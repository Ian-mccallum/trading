# Owner profile

`profile.toml` is the "customizable to me" layer: how much capital the bot may
work with, how aggressively to size, which symbols and personas matter, what to
never touch. Copy [profile.example.toml](../profile.example.toml) to
`profile.toml` and edit. A missing file is fine — the platform runs on
conservative defaults.

> **The profile can only ever be more conservative than the risk engine.** It
> feeds *intent construction*, never risk evaluation. Every intent it produces
> still passes `RiskEngine.evaluate`, so a mistake here cannot loosen a limit;
> at worst it proposes something the engine then rejects.

## Fields

| Field | Default | Meaning |
|---|---|---|
| `name` | `default` | label, appears in logs |
| `capital_allocation` | 25000 | notional the bot may work with — deliberately **not** your account size, so you can hand it a slice of a larger account |
| `risk_appetite` | `moderate` | `conservative` 0.5× / `moderate` 1.0× / `aggressive` 1.5× |
| `max_positions` | 5 | per-position budget = `capital_allocation / max_positions × multiplier` |
| `symbols` | (empty) | tradeable list; empty means "anything the risk allowlist permits" |
| `exclusions` | (empty) | never trade these, whatever a strategy says |
| `benchmark` | `SPY` | for relative-strength criteria |
| `max_position_fraction` | 0.35 | hard ceiling on one position as a fraction of `capital_allocation` |
| `min_order_notional` | 100 | orders below this are not worth the spread |
| `[[personas]]` | — | `name` + `weight` (0, 2]; scales sizing within that persona |

Even `aggressive` at weight 2.0 cannot exceed `max_position_fraction`, and
nothing here can exceed the `RISK_*` limits.

## The three sizing rules

1. **Only entries are sized.** A CLOSE or SELL carries a quantity derived from
   the position that actually exists; rewriting it could try to sell shares
   that are not held. Exits pass through untouched.
2. **Self-sizing strategies are left alone.** A target-weight sleeve computes
   its quantity from its portfolio weight (`self_sized = True`); re-sizing it
   would destroy the allocation it exists to express.
3. **The position cap never blocks an exit.** Refusing to close a position
   because a *count* limit was hit would be actively harmful.

Sized intents record `sized_by: profile`, the original `strategy_qty`, and the
`target_notional`, so the decision audit shows exactly what changed and why.

## Portfolio weights use allocated capital

Target-weight sleeves size against `capital_allocation`, **not** total account
equity. A 25% leg means a quarter of the money the bot was given: $25k of a
$100k account produces $6,250 legs, not $25,000 ones. Sizing off full equity
was the original reason portfolio personas could not trade — the legs were
four times larger than any sane order limit. Without a profile the sleeve
falls back to equity, which is correct for backtests.

## Keep risk limits coherent

The shipped `RISK_*` defaults are sized for the default $25,000 allocation
(largest persona leg: All Weather's 40% bond sleeve at $10,000). **Raise them
alongside `capital_allocation`** or the engine will correctly reject the
resulting orders — a symptom that looks like "the portfolio personas do
nothing". Lowering `capital_allocation` never needs a risk change.

Unlisted personas get weight 1.0 rather than 0: activation is an admin
decision, and a profile that forgot to mention one should not silently stop it
trading.
