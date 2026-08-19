# Pricing Strategy: From First Bid to Steady State

How a Morpheus compute provider actually wins work, what it costs, and how to
price a listing through its whole life cycle.

Written 2026-08-18 from measurements taken on Base mainnet, not from theory.
Every number below was read off the chain or off the router's own scoring
endpoint.

---

## Part 1 — How a buyer chooses you

### The short version

Buyers do not pick the cheapest provider, and they do not pick the best one.
They pick the highest **score**, and score is:

```
score  =  quality  ÷  price
```

That single division is the whole game. Everything else is detail.

### The long version

The buyer's router computes a score for every bid on a model. The code lives in
`proxy-router/internal/rating/scorer_default.go`. In full:

```
score = ( w_tps      × tps_score
        + w_ttft     × ttft_score
        + w_duration × duration_score
        + w_success  × success_score
        + w_stake    × stake_score )  ÷  price_per_second
```

The five terms in brackets are the **quality** half. Each one is squeezed into
the range 0 to 1, and the five weights add up to 1. So no matter what happens,
**quality lands somewhere between 0 and 1.** It can never be 3, or 10.

Price sits underneath as a plain divisor and has no ceiling at all.

That asymmetry is the reason price dominates. The best possible provider in the
world has quality 1.0. The worst has 0.0. That is a difference of at most
**one hundred percent**. But halving your price **doubles** your score. Price is
simply a bigger lever than quality can ever be.

### What the five quality terms mean

| Term | What it measures | How it behaves |
|---|---|---|
| `tps` | Your tokens-per-second on this model, compared with everyone else serving it | Better than average → above 0.5 |
| `ttft` | Your time-to-first-token, compared with everyone else | Faster than average → above 0.5 |
| `duration` | Total time you have served this model | More → above 0.5 |
| `success` | `(successful sessions ÷ total sessions)²` | **0 until your first session closes, then ~1** |
| `stake` | Your stake, scaled between the minimum and ten times the minimum | Pinned at 1.0 for anyone above 2 MOR |

Two of these matter far more than the rest, for opposite reasons.

**`stake` is a red herring.** The minimum provider stake is 0.2 MOR, and the
term maxes out at ten times that — 2 MOR. Our stake is 700 MOR. It has been
pinned at 1.0 since the day we registered. Staking more buys **no ranking
whatsoever**. Confirmed by measurement: a provider with 1,466 MOR staked and one
with 700 MOR staked showed identical quality.

**`success` is the one that moves.** It is a ratio *squared*, so it is exactly
zero until your very first session closes successfully, then jumps to
approximately one. This single term is most of the gap between a new provider
and an established one.

### The three stats are *per model*, not per provider

This is the detail that costs the most money if you miss it.

The chain stores `getProviderModelStats(modelId, provider)`. Your success
history on `glm-5.2` does **nothing** for you on `deepseek-v4-pro`. Every new
model starts you back at zero.

You pay the cold-start toll once per model, not once per provider.

---

## Part 2 — The cold start problem

### Measured numbers

Read directly from the router's rated-bids endpoint on `deepseek-v4-flash`:

```
provider              MOR/day     score     quality
0x7ddd1ea5...          1.4256    39489      0.6516
0x666e6887...          1.5598    34893      0.6299
0x6a00cef9...          2.9280    22364      0.7579
0x010208ec...          4.5600    13432      0.7089
0xOURADDR… (us)     2.9000     7022      0.2357   ← every zero-history bid
0xd3bdd21c...          3.1601     6444      0.2357   ← identical
0xdc34045b...          3.1968     6370      0.2357   ← identical
```

Three different providers, three different stakes, three different prices —
**identical quality of 0.2357**, because all three had never completed a session
on that model.

Established providers sit at 0.58 to 0.76. So a new bid carries roughly a
**2.5× quality handicap**.

### What that handicap costs

Score is quality ÷ price. To beat a rival you must offset a 2.5× quality
disadvantage with price. In practice that means bidding roughly **60% below**
the incumbent to take rank #1 on a contested model.

That is the single most important fact about entering this market.

### And it disappears after one session

Because `success` is zero-then-one, a single completed session removes almost
all of the gap. Measured on glm-5.2:

```
before first session:  quality 0.280
after 7 sessions:      quality 0.6374     ← 2.3× jump
```

So the cold start is a **toll**, not a wall. You pay it once per model, in the
form of a few underpriced sessions, and then you are on equal footing.

---

## Part 3 — The money

### Revenue

```
revenue per session  =  bid_price_per_day  ×  session_duration  ÷  86400
```

That is it. There is no other source of income. **Your bid price is a hard cap
on what you can earn**, and you chose it when you were at your weakest.

Worked example. Bidding 0.277 MOR/day on a model whose sessions run 30 minutes:

```
0.277  ×  (30 × 60)  ÷  86400  =  0.0058 MOR  ≈  $0.011 per session
```

Just over one cent.

### Cost

```
cost per session  =  tokens_used  ×  price_per_million_tokens  ÷  1,000,000
```

You are reselling OpenRouter. Measured rate of consumption: roughly **2,140
tokens per minute** of session (from a 9-minute glm-5.2 session that used about
19,300 tokens). Measured average cost across all our models: **$0.047 per
session**.

The catch is that token price varies by a factor of nearly a thousand:

```
google/gemma-4-31b-it              $0.28 per million tokens (blended)
deepseek/deepseek-v4-flash         $0.14
z-ai/glm-5.2                       $2.49
moonshotai/kimi-k3                $12.00
anthropic/claude-sonnet-4.6       $12.00
anthropic/claude-opus-4.7         $20.00
anthropic/claude-opus-4.7-fast   $120.00      ← same model, six times the price
```

### Putting them together

A model is worth bidding on only if:

```
bid_price × duration ÷ 86400 × MOR_price_in_dollars   >   tokens × $/Mtok ÷ 1e6
```

Most contested models **fail this test**, because the incumbents have already
bid the price down to 0.28–1.4 MOR/day, where a session pays under two cents and
inference costs more.

### The trap that decides everything

You must undercut to win, but undercutting is exactly what makes a model
unprofitable. The market's price floor is set by whoever is willing to lose the
most money.

**So the models worth taking are not the busiest ones. They are the ones an
incumbent is holding at a high price with little or no competition.**

Two real examples:

- **glm-5.2** — one live rival, holding it at 10.38 MOR/day. We entered at 3.9,
  took rank #1, and later repriced to 8.0. Now +44% margin.
- **Claude Opus 4.7** — 381 sessions in ten days, and the incumbents were bidding
  **44, 43, 46, 344 and 288 MOR/day**. Nobody was competing on price at all. An
  11.54 bid took rank #1 outright.

Compare with what to avoid:

- **deepseek-v4-pro** — 952 sessions in ten days, the biggest block on the board.
  Incumbent at 3.01 MOR/day. Thirty-minute sessions at $1.65 per million tokens
  eat the entire payment. **Negative margin even after maturity.** Left alone.

---

## Part 4 — The life cycle of a bid

### Phase 0 — Selection

For each model with real recent demand, ask three questions in order:

1. **Is there demand?** Only bid models with sessions in the trailing ten days.
   Of 407 registered models, only 67 saw a single session, and demand is heavily
   concentrated in the top dozen.
2. **What is the top rival's score?** This is what you must beat, and it sets
   your entry price. A **low** top score is good — it means the incumbents are
   priced high. This is the single best predictor of a profitable model.
3. **Does the arithmetic work at maturity?** Compute revenue per session at the
   matured price and compare it with token cost. If it is negative there, it will
   never work — skip regardless of how much traffic the model has.

Also check the model actually serves. Test the OpenRouter slug with a real
request before bidding. Two things this caught for us:

- The `:web` / `:online` variants inject about 1,000 prompt tokens into **every**
  request for web search — $0.0071 for a one-word answer. No bid price on this
  network covers that.
- `deepseek-v4-pro:online` returned an **empty** reply, which would have failed
  the health probe.

### Phase 1 — Entry

Bid low enough to take rank #1 with certainty.

Because the weight split is not public, compute a **floor** on your own quality
and price against that:

```
quality_floor = 0.560 × min(tps_score, ttft_score, duration_score)
entry_price   = quality_floor ÷ top_rival_score × 86400 × 0.97
```

The 0.560 is the measured sum of the three timing weights. Assuming the whole
0.560 sits on whichever of your three features is worst, and giving yourself no
credit for stake or success, produces a price that wins under **every possible**
weight split.

This method predicted a score of 6,203 on glm-5.2. The actual score was
**6,203.1**.

Expect entry to be unprofitable. That is the toll. It is small — typically under
a dollar per model, because maturity arrives after about seven sessions.

### Phase 2 — Maturity

After roughly five to seven completed sessions, `success` flips from 0 to 1 and
your quality roughly doubles. Your score doubles with it, which means **the price
at which you still hold rank #1 also roughly doubles.**

This is where the money is. Do not miss it — until you reprice, you are serving
at a price you set when you were at your weakest.

How fast maturity arrives depends entirely on the model's traffic:

```
Gemma-4-31b       85 sessions/day   →  ~2 hours
Claude Sonnet 4.6 28 sessions/day   →  ~6 hours
Kimi K3           23 sessions/day   →  ~7 hours
llama-3.3-70b     11 sessions/day   →  ~15 hours
grok-4.5           3 sessions/day   →  ~60 hours
```

### Phase 3 — Stepping up

Do **not** jump straight to the tie price. Two reasons.

**Reason one — you would have no margin.** The tie price is where your score
exactly equals your rival's. Sit 3% under it and any move by them takes your
slot. Moving 40% of the way keeps a wide score buffer, and the buffer is what
protects your volume.

**Reason two — every step costs 0.3 MOR.** Posting a bid charges the bid fee and
deleting one refunds nothing. Repricing is not free, so each step must earn back
its own fee.

That fee is also why **you should not reprice cheap models at all**. On a model
paying $0.011 per session, a 0.3 MOR fee (about $0.58) needs more than fifty
sessions just to break even on the fee itself. Those bids stay at entry price
forever and do a different job — they supply volume and success history.

Our split:

```
REPRICE  (≥ $0.09 per session)      LEAVE ALONE (< $0.03 per session)
  Claude Opus 4.7                     Gemma-4-31b
  Claude Sonnet 5                     deepseek-v4-flash
  Claude Sonnet 4.6                   venice-uncensored
  Kimi K3                             llama-3.3-70b
  glm-5.2                             gpt-oss-120b
  grok-4.5                            MiniMax-M2.5, qwen3-235b, hermes-3-405b
```

### Phase 4 — Steady state

Once matured and stepped up, a listing needs only monitoring:

- **Watch for undercutting.** The `score ÷ price` mechanic means a rival can take
  your slot by cutting price. Re-check ranks periodically.
- **Watch the margin.** Token consumption can change as buyer behaviour changes.
- **Watch headroom.** See below.

---

## Part 5 — The headroom ceiling

Separate from pricing, but it caps everything.

```
headroom = stake − limitPeriodEarned
```

You can only ever earn up to your stake. At zero headroom the node keeps serving
and keeps paying OpenRouter while **earning nothing**.

Three things to understand:

1. **There is no annual maximum.** None. The limit is on cumulative earnings
   against stake, not on a calendar period.
2. **Claiming and re-staking restores headroom one-for-one.** `providerRegister`
   adds to stake and never touches `limitPeriodEarned`, so re-staking your
   earnings reopens the meter completely. The loop is self-sustaining and you
   never need fresh outside capital to keep earning.
3. **The catch:** recycled earnings are locked in the stake. To take profit out
   you must stop growing headroom.

At 700 MOR of stake and, say, 30 MOR/day of earnings, you would choke in about
23 days without the claim-and-re-stake loop.

---

## Part 6 — What can go wrong

**Bidding on a model you cannot serve.** Sessions fail, `success` drops, and
because it is a *squared* ratio, one failure in two sessions drops that term from
1.0 to 0.25. Always test the slug first.

**Expensive models.** A model at $12–20 per million tokens can lose money on a
single long session. Match the bid price to the inference cost, not to the
traffic.

**Confusing "no revenue yet" with "losing money."** Cost is incurred while a
session runs; revenue credits only when it **closes**. Measuring with ten
sessions open shows all of the cost and none of the income. We nearly cut back
the whole operation on this mistake — the real position turned out to be
profitable once the sessions closed.

**Silent truncation.** The router's
`/blockchain/providers/{id}/bids/active` endpoint caps at 10 results with no
error. It was dropping 4 of our 14 bids. Read `getProviderActiveBids` from chain
instead. (Blockscout's v2 pagination has the same failure mode.)

**Stale health cache.** `hasActiveBid` only refreshes at router startup, so
restart the router after posting bids.

**Nonce collisions.** With a round-robin RPC pool, consecutive transactions can
hit different nodes and grab the same pending nonce, giving
`replacement transaction underpriced`. Retry; it clears.

---

## Appendix — The formula in one place

```
score          = quality ÷ price_per_second
quality        = Σ (weight_i × term_i),  each term in [0,1],  Σ weights = 1
success_term   = (successes ÷ total)²        0 before first session, ~1 after
stake_term     = irrelevant above 2 MOR staked

revenue/session = bid_price_per_day × duration_seconds ÷ 86400
cost/session    = ~2,140 tokens/minute × minutes × $per_million ÷ 1e6

entry_price     = 0.560 × min(tps, ttft, dur) ÷ top_rival_score × 86400 × 0.97
matured_price   ≈ 2× entry price (quality roughly doubles at first success)
step_price      = current + (tie − current) × 0.40

headroom        = stake − limitPeriodEarned      restored 1:1 by re-staking
bid fee         = 0.3 MOR, charged on every post, never refunded
```
