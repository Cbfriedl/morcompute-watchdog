# Session summary — MORCompute — bid expansion & pricing model — 2026-08-18

## Summary

Went from 1 active bid to 14, rank #1 on every one, after working out exactly how
buyers pick a provider.

**The selection mechanism.** From `proxy-router/internal/rating/`:

    score = (w_tps*tps + w_ttft*ttft + w_dur*duration + w_success*success + w_stake*stake) / price

Quality terms are each clamped to [0,1]; price is an unbounded divisor, so price
dominates. Verified against the router's own `/blockchain/models/{id}/bids/rated`.

**Cold start.** Every zero-history provider on a model shows the same implied
quality — measured 0.2357 on deepseek-v4-flash, 0.280 on glm-5.2 — versus
0.58-0.75 for established ones. The gap is almost entirely `successScore`, which
is `(successCount/totalCount)^2` and therefore **0 until the first session closes,
then ~1**. Quality roughly doubles at that point. Stake contributes nothing:
`getProviderMinimumStake()` is 0.2 MOR and the stake term maxes out at 10x that,
so anything above 2 MOR staked is already pinned at 1.0.

**Safe entry pricing.** With the weight split unknown, treat quality as
`0.560 * min(tps, ttft, dur)` — the 0.560 measured on glm-5.2 where every stat SD
is 0 — and assume no stake credit. That is a floor, so a price derived from it
wins under any weight split. It predicted a score of 6203 on glm-5.2; actual was
6203.1.

**Economics.** Revenue per session is `bid_price * duration / 86400`. Measured
cost is ~$0.047/session. That makes most contested models unwinnable profitably:
incumbents have already bid them to 0.28-1.4 MOR/day, where a session pays under
2 cents. The exceptions are models an incumbent holds at a high price with no
competition — glm-5.2 (BB03 at 10.38) and Claude Opus 4.7 (incumbents at 43-344).

**Where the money went in.** OpenRouter spend climbed to ~$1.42 of $50 credits
against 0.53 MOR earned. Roughly break-even after paying the cold-start entry toll
on 14 models at once. The apparent large deficit mid-session was an artefact:
cost is incurred while a session runs, revenue credits only at close, so measuring
with 10 sessions open shows all the cost and none of the income.

## Status

**Live: 14 bids, rank #1 on all 14.**

    Claude Opus 4.7        11.5381   1/6      38.1 sess/day
    glm-5.2                 8.0000   1/3      53.8   (matured, 14/14)
    Claude Sonnet 5         5.4207   1/6       1.4
    grok-4.5                3.8371   1/7       1.2
    Claude Sonnet 4.6       2.1477   1/7      28.4
    Kimi K3                 1.7297   1/5      23.0
    qwen3-235b              1.0533   1/5       3.3
    venice-uncensored       0.8036   1/4      35.3
    hermes-3-405b           0.6737   1/8       2.7
    MiniMax-M2.5            0.5118   1/9       5.4
    deepseek-v4-flash       0.5000   1/15    103.9
    gpt-oss-120b            0.4314   1/8       6.5
    llama-3.3-70b           0.3142   1/8       6.1
    Gemma-4-31b             0.2767   1/7      59.4

~370 sessions/day of addressable demand, about 43% of network volume. All models
have real 10-day session history — that was an explicit rule.

**Deliberately not bid**
- `deepseek-v4-pro` — 952 sessions/10d, the largest remaining block. -3% margin
  even at maturity; incumbents hold it at 3.01 and 30-minute sessions at
  $1.65/Mtok consume the entire payment.
- `DeepSeek V4 Flash 0731` — incumbent at 0.276, no room underneath.
- All `:web` / `:online` variants — the search plugin injects ~1000 prompt tokens
  into every request ($0.0071 for a one-word answer), and
  `deepseek-v4-pro:online` returned an empty reply that would fail a health probe.

**Repricing plan (agreed: slow, selective, within 24h)**
`watchdog/reprice.py`, dry run by default, `--go` to send.
- Only 6 larger-payment models are ever touched (>= ~$0.09/session). The other 8
  pay under $0.03/session, where a 0.3 MOR fee needs 20-250 sessions to break even
  on itself. Those stay at entry price and supply volume.
- Steps 40% of the way to the tie price, capped at 90% of it, minimum 15% gain.
- Waits for 5 closed sessions per model: quality doubles at maturity, so
  repricing earlier just pays a second fee.
- Current state: glm-5.2 holds (only 2% gain available); the other five are at
  0/0 sessions and waiting.

**Fixed this session**
- Router RPC: `ETH_NODE_ADDRESS` pinned it to one endpoint AND disabled the
  built-in 8-endpoint round-robin. Commented out -> 1,022 errors per 4,000 log
  lines became 0; `/bids/rated` went from 100% failing to reliable.
- Monitor RPC moved off base.drpc.org (was firing rpc_down every run).
- Monitor no longer treats `skipped` model probes as a fault.
- `reprice.py` reads active bids from `getProviderActiveBids`, not
  `/blockchain/providers/{id}/bids/active` — that endpoint silently caps at 10
  and was dropping 4 of 14 bids including glm-5.2.

**Known issues**
- **Wallet 1.90 MOR.** The binding constraint. One step on each of the five
  waiting models costs 1.5 MOR. Recommend topping up to ~25 MOR.
- Posting a bid intermittently fails with `replacement transaction underpriced`
  — round-robin means consecutive txs can hit different nodes and grab the same
  pending nonce. Retrying works; every post loop retries 4-5 times.
- `hasActiveBid` in the health cache only refreshes at router startup, so the
  router must be restarted after posting bids.
- Three models can't be health-probed (no LLM tag on chain): Claude Sonnet 4.6,
  Claude Sonnet 5, Kimi K3. User has accepted this.
- Gemma-4-31b shows 1 success of 2 sessions — one failed session, cause unknown.
- OpenRouter key still has no spend cap against $50 of credits.
- Claim -> re-stake automation still unbuilt. Headroom is 699.8 of 700, so not
  yet binding.

**Next steps**
1. Within 24h: fund the wallet, then run `python3 /root/morpheus/reprice.py` and,
   if the output looks right, `--go`. Expect Claude Sonnet 4.6, Kimi K3 and
   Claude Opus 4.7 to mature first (highest session rates).
2. After each maturity the tie price roughly doubles, so re-run the dry run after
   any step rather than assuming the printed target is final.
3. Watch OpenRouter spend against earnings daily; set a spend cap.
4. Consider paid RPC — dRPC pay-as-you-go, flat $6/1M requests, ~$8-30/month at
   the measured ~1.3-5M requests/month. Flat rate suits a getLogs-polling load.
