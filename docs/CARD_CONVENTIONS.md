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

## Rule of thumb

Before assigning a category, ask *what is this card about*, not *is this number
good*. If the answer to the first question is "a session", it is `sess`, whatever
the value is doing.
