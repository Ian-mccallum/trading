# Spec — manual trading and strategy control

Two capabilities the platform does not have today. Everything else in this
doc's scope already works: automated trading runs on a 15-minute loop, and
strategy selection exists as an audited admin API.

| Capability | Status |
|---|---|
| Automated trading (strategy loop places orders) | ✅ working |
| TradingView webhook → order | ✅ working |
| Choosing which strategies run (personas) | ✅ working, API only |
| **Placing a one-off manual trade** | ❌ does not exist |
| **Choosing strategies from the dashboard** | ❌ read-only, curl required |

---

## 1. Manual trade endpoint

### Why it does not exist yet

Deliberate. Every order in the system originates from a `Decision` row with
recorded reasoning, which is what makes the audit spine complete and the
learning layer trainable. A manual order has no strategy, no market context,
and no reasoning — so it needs a defined place in that model rather than a
side door around it.

### The design

`POST /admin/orders` — admin token, actor and reason required, like every
other mutating admin endpoint.

```json
{
  "symbol": "SPY",
  "action": "buy",
  "qty": 10,
  "actor": "ian",
  "reason": "manual entry ahead of earnings"
}
```

**It creates a real Decision and goes through the full risk engine.** This is
the non-negotiable part: a manual order is not a bypass. It gets
`mode: paper`, `strategy_id: null`, and a context recording that a human
originated it:

```json
{"origin": "manual", "actor": "ian", "reason": "manual entry ahead of earnings"}
```

Consequences that fall out of reusing the existing path:

- The kill switch stops manual orders too.
- Notional, position, exposure, allowlist and long-only limits all apply.
- The duplicate guard applies, so a double-submitted manual order is caught.
- It appears in the dashboard activity feed with an "M" origin marker,
  expandable like any other decision.
- Its outcome is evaluated by the same hourly job.

**Excluded from allocator training.** Manual decisions carry no strategy, so
the trainer must filter them out or a human's discretionary call would be
attributed to whatever model version happened to be champion. One line in
`train_allocator`'s query; called out here because it is easy to miss.

### Sizing

`qty` is taken literally — the profile sizer is **not** applied. A human
naming a quantity means that quantity. The risk engine still caps it.

If `qty` is omitted, size from the profile's per-position target, and record
`sized_by: profile` exactly as the strategy path does.

### Cancel

`POST /admin/orders/{client_order_id}/cancel` — routes to
`Broker.cancel_order`, marks the local row, writes a `RiskEvent` for the
audit. Needed because the platform can currently place orders it has no way
to withdraw, which is the gap that turned an ordinary bug into a stuck
account.

### Not included

- No market/limit choice beyond what `TradeIntent` already carries.
- No brackets, OCO, or trailing stops. Those belong to strategies.
- No "close all positions" button. Tempting, but a single click that
  liquidates a portfolio is exactly the kind of irreversible action that
  should require deliberate per-symbol intent. A `--all` flag on a script
  with a typed confirmation is the right shape if it is ever wanted.

---

## 2. Strategy control in the dashboard

### Why it does not exist yet

The dashboard was specced read-only on purpose: nothing on it can place or
cancel an order, so it is safe to leave open. That principle holds. But
"which strategies are running" is configuration, not execution, and forcing
the operator into curl for a routine decision is friction with no safety
benefit — they already hold the admin token to see the page at all.

### The design

A **Personas** section that becomes interactive, keeping the read-only
guarantee for *order flow* while allowing *configuration*:

```
┌────────────────────────────────────────────────────────┐
│ PERSONAS                                               │
│                                                        │
│ ● paul_tudor_jones      competing   1 strategy   [Stop]│
│   Long above the 200-day. Complete fidelity.           │
│                                                        │
│ ○ all_weather           cooperative 5 legs      [Start]│
│   Unlevered retail approximation, NOT Bridgewater's.   │
└────────────────────────────────────────────────────────┘
```

Behavior:

- **Start** / **Stop** call the existing
  `/admin/personas/{id}/activate|deactivate`. No new backend logic; the
  dashboard becomes a client of the audited path it already has.
- **A reason is required.** The endpoints demand `actor` and `reason`, and the
  UI must not fabricate them. Clicking Start opens a small inline field, not a
  modal, prefilled with nothing. The actor comes from a one-time "who are
  you?" prompt stored in `sessionStorage` alongside the token.
- **The fidelity note is always visible**, not a tooltip. The whole point of
  recording that All Weather is an unlevered approximation is that it should
  be in front of someone at the moment they switch it on.
- **Confirmation only for activation**, not deactivation. Turning something
  off is the safe direction and should never be slowed down — the same
  reasoning that makes the kill switch a single click.

### What stays out of the dashboard

- Kill switch and live-arming. These stay API-only. A page left open on a
  second monitor should not have a control that can halt or arm trading one
  errant click away, and the runbook's muscle memory is `curl` for
  emergencies.
- Model promotion. Reviewing a model version means reading its metrics and
  training metadata; a button divorced from that review invites rubber-stamping.
- Editing strategy parameters. Parameters are versioned rows by design;
  editing in place would destroy the attribution that makes the learning
  layer work. New parameters mean a new version, which is a seed-script
  change.

---

## 3. Phasing

| Phase | Deliverable | Notes |
|---|---|---|
| **A** | `POST /admin/orders` + cancel, full risk path, manual origin in context | Backend only; usable via curl immediately |
| **B** | Trainer excludes manual decisions | One query filter, plus a test |
| **C** | Dashboard persona Start/Stop with actor + reason capture | Frontend only, reuses existing endpoints |
| **D** | Manual-order form in the dashboard | Depends on A; last because it is the highest-risk surface |

A and B are small and self-contained. C is the one with the most day-to-day
value. D is deliberately last.

## 4. Non-goals

- No bypass of the risk engine, ever, including for manual orders.
- No order entry without an audit row.
- No control that can arm live trading from a browser.
