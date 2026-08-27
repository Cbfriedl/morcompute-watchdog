# Card colour conventions

**Colour states what a card is about. It never states whether the number is
good.** A card must not change colour as its value moves.

This is the rule for both dashboards. It was violated twice and corrected twice;
the note exists so it is not re-derived a third time.

## The categories

`card(label, value, subtitle, category)` — the fourth argument is the category.

| Category | Colour | What belongs in it |
|---|---|---|
| `time` | amber | Dates and countdowns — Registration date, Active bids start, Days to unlock |
| `hold` | blue | Stake and balances — Base stake, Current stake, Headroom, Wallet gas, Wallet MOR |
| `earn` | green | Returns on capital — Total earned, Earned/day, ROI on base stake, APR on base stake |
| `bid` | **purple** | Anything about listings — Active bids, Earning bids, Ranked #1, Median bid price |
| `sess` | **light yellow** | Anything about sessions — Sessions, Capture rate, Session margin, Margin, Net, Inference cost/day |
| `choke` | red | **Fault state only.** Currently just Headroom when it drops to ≤0.5 MOR, where the provider keeps serving and stops being paid. |

Tokens live in `assets/style.css` as `--c-<cat>-bg` / `--c-<cat>-ink` /
`--c-<cat>-br`, defined for light and dark.

## What went wrong, twice

The four profitability cards — Session margin, Margin, Net, Inference cost/day —
were originally written as:

```js
card("Session margin", …, (perS ?? 0) >= 0 ? "earn" : "choke")
card("Inference cost / day", …, "choke")          // unconditional
```

Two consequences:

1. **The row changed colour as the numbers moved.** Green when positive, red when
   negative. That is value-coding, and it makes the category meaningless — a card
   about sessions stopped looking like a card about sessions.
2. **Inference cost / day rendered permanently alarm-red**, including at $0.00,
   because its category was hardcoded rather than derived. A cost of zero read as
   a fault.

All four are now `sess`. Sign is already carried by the `+`/`−` in the value
itself; it does not need the background as well.

`choke` is reserved for genuine fault states — a condition that requires action,
not a number that happens to be negative.

## Table cells are not cards

The colour rule governs **cards**. Two table markers were added on 2026-08-24 and
neither is a value judgement:

* **`--live-ink`** — a model with a session running *right now* shows its name in
  bright green with a filled dot on the My node table. `#08a13a` light,
  `#48e070` dark, defined in all three theme blocks. Deliberately brighter than
  `--good`, which is a muted status tint and does not read at a glance in a dense
  table. The marker ignores the timeframe radio: "open right now" is a fact about
  now, not about the window, and a model whose only session started before the
  window still gets a row so the marker cannot silently go missing.
* **Margin basis markers** — `m` = measured on hours that model ran alone,
  `m!` = measured but the regression disagrees, `≥` = bound only, plain = fitted.
  These say *how well we know the number*, not whether it is good.

## Card order on a comparison

The Compare tab shows the same eight cards per side, in the same order, so the eye
can compare position for position: **time, models, wallet, earnings, sessions** —
Start date, Models, Active bids, MOR staked, Headroom, MOR earned, MOR/session,
Sessions. Three to a row.

Median bid was removed from that set on 2026-08-24: a median across the unrelated
models one provider happens to bid is not a statistic. Same objection that removed
the cross-model price aggregates from the public pages on 2026-08-23.

## Sizing is part of the system

Card padding, radius and type sizes come from the My node page and must not be
overridden per page. A Compare panel that set its own padding and a smaller value
font read as a different card system even though every colour was correct — and
panels sitting on `--surface` instead of `--bg` changed how every category tint
appeared. If a layout needs three-to-a-row, change the grid template only.

## Rule of thumb

Before assigning a category, ask *what is this card about*, not *is this number
good*. If the answer to the first question is "a session", it is `sess`, whatever
the value is doing.
