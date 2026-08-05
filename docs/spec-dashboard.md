# Design brief — operator dashboard

Read-only monitoring surface for quantplatform. Strategic context in
[PRODUCT.md](../PRODUCT.md).

**Direction (revised):** consumer-fintech — dark surface, real charts,
green/red for direction, heavily edited so nothing competes for attention.

---

## 1. Feature summary

A single dark page that answers, in this order: *what is the account doing*,
*what does it hold*, *what did the bot decide and why*, and *is anything
blocked*. It replaces `scripts/status.py`, `docker compose logs`, and SQL as
the routine way to check on the system.

Read-only. It places, cancels, and modifies nothing; state changes stay on the
audited admin API.

## 2. Primary user action

**Read the account's trajectory at a glance, then drill into any decision to
see the reasoning behind it.** The chart earns the first look; the reasoning
is what makes this platform's dashboard different from a brokerage app.

## 3. Design direction

**Color strategy: Committed.** The dark surface is the identity, with green
and red carrying real semantic weight (direction and outcome), and one cobalt
primary for interactive affordances. Not Restrained — the fintech vocabulary
needs its color to mean something.

**Scene sentence:** A developer at a desk, laptop open beside their editor,
glancing over to see the line and confirm the bot did something sensible. The
dark surface makes the chart the light source on the screen — the same reason
Robinhood, Coinbase, and every trading terminal go dark: **luminous data on a
recessive ground.**

**Anchor references:**
- **Robinhood's portfolio screen** — the anchor. One large number, one large
  line chart, a range selector, and almost nothing else above the fold.
  Scrubbing the chart updates the number.
- **Coinbase's asset list** — holdings as a clean list with value, weight, and
  direction per row; no table chrome.
- **Stripe's payment detail view** — the one non-fintech borrow, for the
  expand-into-reasoning pattern where the audit record *is* the interface.

**Palette** (OKLCH, cobalt seed `seed-129` adapted for dark):

| Role | Value | Use |
|---|---|---|
| `bg` | `oklch(0.120 0.000 0)` | page — true near-black, chart reads as light |
| `surface` | `oklch(0.180 0.010 260)` | panels, list rows |
| `surface-2` | `oklch(0.235 0.012 260)` | hover, expanded regions |
| `hairline` | `oklch(0.290 0.010 260)` | dividers, chart gridlines |
| `ink` | `oklch(0.970 0.002 260)` | primary text (≈17:1 on bg) |
| `muted` | `oklch(0.680 0.008 260)` | labels, timestamps (≈7:1, clears AA) |
| `primary` | `oklch(0.620 0.160 260)` | cobalt: selection, focus, links |
| `up` | `oklch(0.720 0.180 150)` | gains, executed, healthy (≈7.9:1) |
| `down` | `oklch(0.650 0.200 25)` | losses, rejected, halted (≈5.6:1) |

Green and red are lifted to L 0.72 / 0.65 so both clear AA **as text** on
black — the standard failure of dark trading UIs is a red that only passes as
a fill. Neither is pushed to neon; saturation stops well short of the
acid-bright zone so the chart reads as data, not signage.

**Typography:** one family (Inter or `system-ui`). Fixed rem scale, 1.2 ratio,
except the portfolio value which jumps to ~2.75rem — a deliberate single
break in the scale, the way consumer apps anchor the screen. Tabular numerals
everywhere numbers align or animate. Mono only for symbols, IDs, and rule
names.

## 4. Scope

- **Fidelity:** production-ready.
- **Breadth:** one route (`GET /dashboard`), no navigation.
- **Interactivity:** chart scrubbing, range selection, expandable decisions,
  feed filter, auto-refresh.
- **Time intent:** build properly once; shouldn't need redesign when Phase 3
  lands.

## 5. Layout strategy

Single column, generous vertical rhythm, four zones. Each zone is one idea.
This is the anti-clutter mechanism: no rails, no dashboards-within-dashboards,
nothing side-by-side competing for the same glance.

```
┌────────────────────────────────────────────────────┐
│ ▸ system strip — one line, silent unless blocked   │
│                                                    │
│   $100,000.00                                      │
│   +$1,247.10  +1.25%  today            ← up/down   │
│                                                    │
│   ╱╲                                               │
│  ╱  ╲    ╱╲                                        │
│ ╱    ╲__╱  ╲___                    ← equity chart  │
│                                                    │
│   1D   1W   1M   3M   1Y   ALL         ← range     │
├────────────────────────────────────────────────────┤
│ HOLDINGS                                           │
│   SPY   1 sh    $740.80    32%   +0.4%             │
│   QQQ   1 sh    $612.40    27%   −0.2%             │
├────────────────────────────────────────────────────┤
│ ACTIVITY                    [All｜Paper｜Shadow｜✕] │
│ ▸ 15:49  SPY  buy   paper   executed               │
│ ▸ 15:49  GLD  buy   shadow  rejected               │
│     └ expanded: the reasoning, in sentences        │
├────────────────────────────────────────────────────┤
│ PERSONAS   ● paul_tudor_jones   ○ all_weather …    │
└────────────────────────────────────────────────────┘
```

**System strip.** Pinned at top, one line, `muted` text when healthy — easy to
ignore, which is correct for a working system. When the kill switch or breaker
trips it expands to a full-width `down` band naming the control, the reason,
and who set it. The asymmetry is the design.

**Portfolio.** Value at ~2.75rem, change beneath in `up`/`down` with an arrow
glyph, then the chart at ~220px tall. No card, no border: it sits directly on
`bg` so the chart line is the brightest thing on screen.

**Holdings.** Rows on `surface`, hairline-separated. Symbol (mono), quantity,
value, portfolio weight, day change. No sparkline per row — that is the
clutter reflex; weight as a thin bar behind the row communicates allocation
faster.

**Activity.** Same row rhythm. Expanding pushes content down on `surface-2`;
never a modal.

**Personas.** A single compact line of dot-plus-name chips, since the only
question is which are on. Cooperative personas group their legs behind one
chip with a count rather than listing five.

Below 720px: identical order, chart drops to 160px, holdings drop the weight
column. Structural, never fluid type.

## 6. Charts

**Real charts, three of them, and nothing more.**

1. **Equity line (hero).** From `position_snapshots`. Line in `up`/`down` by
   period direction, 2px, with a subtle gradient fill fading to transparent —
   the one gradient in the design, and it earns its place by giving the line
   visual mass. Hover/drag scrubs: a vertical hairline follows the cursor, the
   value and change above update to that point, and release restores the
   period. Touch-draggable.
2. **Allocation bar.** A single horizontal stacked bar under holdings showing
   portfolio weights including cash. Communicates concentration in one glance;
   a donut would take twice the space for the same information.
3. **Decision volume.** A small bar strip in Activity: last 14 days, stacked
   executed vs rejected. Answers "has it been working?" without a table.

Everything is inline SVG generated server-side, with ~60 lines of vanilla JS
for scrubbing. No charting library, no build step, no new dependencies.

**Honest state:** with `qty: 1` the equity line is near-flat and holdings are
tiny. The chart must render that truthfully — no auto-zoomed y-axis that
manufactures drama from $12 of movement. Y-axis includes a sensible zero-ish
reference so flat looks flat. When sizing becomes meaningful the same chart
starts showing real shape.

## 7. Key states

| State | What the operator needs |
|---|---|
| **Healthy** | Default. Strip quiet, chart populated, activity flowing. |
| **Halted** | Strip becomes a `down` band: which control, why, who set it. Everything below stays readable. |
| **Not enough history for a chart** | Under two snapshots, show the value with "Not enough history yet — snapshots are taken every 15 minutes." Never a broken axis or an empty box. |
| **No positions** | "No open positions." Plus, when true, "No personas are active, so nothing will trade" — the most likely confusing-but-correct state. |
| **No decisions** | "The strategy loop runs every 15 minutes; next run 16:02." Plus the approved-strategy count, since zero is the usual cause. |
| **Worker not running** | Distinct from halted and must not read as it: "No worker heartbeat in 22 minutes." |
| **Broker unreachable** | Holdings show unavailable with the error, never an empty list that reads as "no positions". |
| **Loading** | Skeleton at real heights: a chart-shaped block and three row-shaped blocks. Never a centered spinner. |

Status pairs **shape + label + color**, never color alone: `● executed`,
`✕ rejected`, `◦ shadow`, `⏸ halted`. Red/green discrimination is the most
common color-vision deficiency and this domain is built on approved/rejected
pairs, so hue alone would be a predictable failure.

## 8. Interaction & motion

- **Chart scrub:** cursor or touch. Value/change track the cursor; release
  restores. No easing on scrub — it must feel directly attached to the pointer.
- **Range selector:** the chart path morphs over 250ms ease-out-quart; the
  value counts to its new figure over 300ms with tabular numerals so digits
  don't jitter.
- **Expand decision:** click or Enter/Space. 200ms height transition.
- **Auto-refresh:** 30s, patched in place. Open rows stay open. New activity
  rows fade in over 200ms; nothing else moves. "Updated 12s ago" makes a
  stalled page obvious.
- **Reduced motion:** every transition becomes an instant swap or crossfade.
  The count-up becomes a direct set.
- **Keyboard:** activity rows are a focusable list, arrows move, Enter
  expands. Range selector is a real radio group. Focus rings in `primary`.

Nothing animates on page load. There is no orchestrated entrance.

## 9. Content requirements

The differentiator remains **translating machine vocabulary into sentences.**
The database says `order_notional`; the operator reads:

> Rejected by the order size limit. This order was **$24,922**; the per-order
> cap is **$5,000**.

A translation table maps every risk rule and decision status to plain
language; strategy criteria get the same treatment (`c5_above_fast` → "Price
above the 50-day average — failed").

Copy rules: no exclamation marks, no encouragement, no commentary on gains or
losses beyond the number and its direction. Units always present. Timestamps
local with a relative hint ("15:49 · 8m ago").

Ranges to design against: 0–200 decisions/day, 0–10 positions, 5–20 personas,
0–8 risk rules firing, 0–2 years of snapshots. Activity virtualizes past 100
rows.

## 10. Implementation notes

Server-rendered HTML from FastAPI plus a JSON endpoint for refresh. No build
step, no framework, no new Python dependencies.

- `GET /dashboard` — the page (admin token, same guard as `/admin/*`)
- `GET /dashboard/data` — JSON for refresh and range changes

References during implementation: `layout.md` (vertical rhythm, zone
proportions), `colorize.md` (semantic green/red on dark), `clarify.md`
(translation table, empty states), `harden.md` (unreachable broker, no
heartbeat).

## 11. Decisions taken

- **Auth:** reuses `X-Admin-Token`, held in `sessionStorage`. Single-operator
  localhost tool, not a login system.
- **Dark only.** The scene sentence and the reference set both pin it; a light
  variant would double the contrast surface to verify for no stated benefit.
- **No per-holding sparklines.** The allocation bar carries concentration
  better and costs a fraction of the space.
- **Chart shows dollars, not percent, by default.** Percent at this scale
  amplifies noise into apparent volatility.
