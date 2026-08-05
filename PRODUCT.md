# Product

## Register

product

## Users

One operator: the developer who owns this platform. Checks it from a desk on a
laptop, a few times a day, between other work. Not a trading professional
staring at it all session, and not a team, so there are no roles, permissions,
or collaboration surfaces to design for.

Their job on any given visit, in order: (1) confirm nothing is broken and
nothing is halted, (2) see what the bot decided since they last looked, (3)
when something looks wrong, understand *why* it happened without opening a
terminal or writing SQL.

## Product Purpose

quantplatform is paper-trading infrastructure that runs strategies derived
from published investor methodologies, routes every proposed trade through an
independent risk engine, and records the full reasoning behind every decision
so models can be retrained from history.

The read-only dashboard exists because that reasoning is currently trapped in
Postgres and JSON logs. Success is: the operator can answer "is it running,
and does its reasoning look sane?" in under ten seconds, and can drill into any
single decision to see the argument behind it without leaving the page.

## Brand Personality

Precise, legible, unexcitable. The interface is an instrument for judgment,
not a feed of stimulation. It states what happened and why, in plain language,
and stays quiet when everything is fine. Nothing is dramatized: not gains, not
losses, not rejections. A rejected order is a normal event and should look
like one.

Voice: complete sentences with real numbers. "Rejected: the order was $24,922,
above the $5,000 per-order limit" rather than "risk_verdict: rejected
(order_notional)".

## Anti-references

- **Generic AI-generated SaaS**: purple gradients, glassmorphism, identical
  icon-plus-heading card grids, tiny uppercase tracked eyebrows above every
  section.
- **Enterprise admin panel**: undesigned grey tables, everything at the same
  visual weight, no hierarchy, cramped forms.
- **Pro terminal density**: twelve panels at once, needs a manual. Consumer
  fintech clarity is the target, not Bloomberg.
- **Gambling energy**: confetti, streak counters, celebratory animation on
  gains, anything that rewards watching. The consumer-fintech *form* is the
  reference; its engagement-maximizing instincts are not.

## Visual direction

Consumer investing apps (Robinhood, Fidelity, Coinbase): dark surface, a real
equity chart as the anchor, green/red for direction, and aggressive editing so
each screen carries few things at a comfortable size. Familiarity is the
feature — the operator already knows how to read this vocabulary.

## Design Principles

1. **Reasoning is the product.** Most trading UIs show numbers and discard the
   argument. Here the argument is the differentiator, so decisions and their
   justifications are the primary surface; balances are supporting context.
2. **Quiet when healthy, loud when not.** The default state of a working
   system is unremarkable, so it should look unremarkable. Visual weight is
   reserved for things that actually need a human.
3. **Translate the machine's vocabulary.** Rule names, enums, and verdicts are
   implementation details. The interface speaks in sentences with the specific
   numbers that triggered the outcome.
4. **Progressive disclosure over density.** One screen, summary by default,
   full reasoning on demand. Never make the operator parse a wall to find the
   one thing that changed.
5. **Never imply an action the API won't honor.** This surface is read-only.
   Controls that change state (kill switch, persona activation) live on the
   audited admin API and must not be faked here.

## Accessibility & Inclusion

WCAG 2.1 AA: body text ≥4.5:1, large text ≥3:1, full keyboard navigation,
visible focus rings, `prefers-reduced-motion` honored on every transition.

Beyond the stated requirement, status is never encoded in color alone: every
state carries a text label and a distinct shape or icon. Red/green
discrimination is the single most common color-vision deficiency and this is a
domain built on approved/rejected pairs, so relying on hue would be a
predictable failure.
