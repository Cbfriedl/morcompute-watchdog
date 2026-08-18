# Session summary — MORCompute — glm-5.2 slot + RPC pool — 2026-08-18

## Summary

Worked out why the provider had taken zero sessions since going live, then took a
contested market slot.

**How buyers actually pick a provider.** Read `proxy-router/internal/rating/`
(`scorer_default.go`, `common.go`). The score is

    score = (w_tps*tps + w_ttft*ttft + w_dur*duration + w_success*success + w_stake*stake) / price

The five quality terms are each clamped to [0,1]; **price is a straight divisor and
is unbounded**, so it dominates. Confirmed empirically against the router's own
`/blockchain/models/{id}/bids/rated` endpoint.

**Why we had zero sessions.** We bid 2.90 MOR/day on `deepseek-v4-flash` where the
floor was 1.426 — 11th of 15. Node health was never the problem. The 10-day session
log shows a clean handover: 0x010208ec held the model at 1.452/day until it repriced
to 4.56 on 08-14 and lost the entire flow to 0x7ddd1ea5 at 1.426 within a day.

**Cold start is real but small and self-clearing.** Every zero-history provider on
`deepseek-v4-flash` shows an identical implied quality of **0.2357**, versus ~0.65-0.75
for established ones. The gap is almost entirely the `success` term, which is
`(successCount/totalCount)^2` and therefore **0 until the first session completes, then 1**.
One completed session removes the whole handicap. Stake contributes ~nothing: the
stake term is `normMinMax(stake, minStake, 10*minStake)` with `getProviderMinimumStake() = 0.2 MOR`,
so anything at or above 2 MOR staked is already pinned at 1.0.

**Target chosen: glm-5.2, modelId `0xe8585e699a48aba75829ca8d0c3634cfa10a299bde0d0aa4558760f9144224b9`.**
122 sessions/day, and only one live rival (0x010208ec at 10.3776 MOR/day). The other
bidder, 0xbae6c427 at 4.0 MOR/day, is choked — staked exactly the 0.2 MOR minimum and
earned it all — but the rating endpoint does NOT filter choked bids, so it still had to
be beaten on score (6048.0).

Bid placed at **3.9 MOR/day** (pricePerSecond 45138888888888), bid id
`0x387ef17eefc08220148c8f703684975b6688f0fc306c64355d0f8089214e3745`.
Resulting score **6203.1 — rank #1**, ahead of 0xbae6c427 (6048.0) and 0x010208ec (5994.4).
The predicted worst-case score was 6203, so the model of the scorer was exact.

**RPC was badly broken and is now fixed.** `ETH_NODE_ADDRESS=https://base.drpc.org` in
`.env` pinned the router to a single free-tier endpoint AND disabled the router's own
built-in multi-RPC round-robin with failover (`ConfigureRPCClientStore` only falls
through to the pool when that env var is unset). Symptom: 1,022 rate-limit/timeout
errors per 4,000 log lines, and `/bids/rated` failing 100% of the time. Commenting the
variable out restores the built-in 8-endpoint pool. Error count in the 5 minutes after
restart: **0**. Health-check latency on deepseek-v4-flash fell 3066ms -> 1188ms.

`POST /config/ethNode` (for a curated URL list) does NOT work in this container — it
persists to a system keyring and fails with `exec: "dbus-launch": executable file not found`.
The built-in public pool is the working configuration.

**RPC endpoint survey (run from the provider box).** No free public endpoint does both
jobs well. `base-rpc.publicnode.com` and `base.publicnode.com` are the only ones that
survive a burst (30/30 concurrent eth_calls, ~550ms) but they refuse `eth_getLogs`
entirely ("Archive requests require a personal token") — disqualifying, because
`ETH_NODE_USE_SUBSCRIPTIONS=false` means `log_watcher_polling.go` polls getLogs
continuously. Endpoints serving both, all weak under burst (1-5 of 30):
mainnet.base.org, developer-access-mainnet.base.org, base.lava.build, base.drpc.org,
base.public.blockpi.network. Dead: ankr.com/base, blockpi v1, omniatech, therpc.io,
diamondswap, stackup.

**Measured load:** ~910 bytes/sec inbound to the container at idle, i.e. roughly
1.3-5M RPC requests/month.

## Status

**Done and verified**
- Router healthy, v7.5.0. Both models `status: healthy`, `promptCorrect: true`.
  deepseek-v4-flash 2570ms, glm-5.2 815ms.
- Two active bids on chain: `0x387ef17e` glm-5.2 @ 3.9 MOR/day, `0x1bedb61a`
  deepseek-v4-flash @ 2.9 MOR/day.
- Rank #1 on glm-5.2 by the router's own scorer.
- Wallet 6.055 MOR (0.3 spent on the bid fee), 0.0500 ETH.
- `models-config.json` now has both models; backups at
  `models-config.json.bak.glm.20260818-004652`.
- `.env` backup at `.env.bak.rpcpool.<ts>`; `ETH_NODE_ADDRESS` commented out.
- Zero RPC errors post-restart.

**Known issues**
- Posting a bid failed twice with `replacement transaction underpriced` before
  succeeding on the third try. Cause: round-robin means consecutive transactions
  (approve, then postModelBid) can go to different nodes, and both grab the same
  pending nonce. Retrying works. Worth a proper fix if bidding becomes frequent.
- The health cache's `hasActiveBid` only refreshes at startup — after posting a bid the
  model sat at `no_bid` for 4+ minutes until the router was restarted. Restart after
  every bid until this is understood.
- Cost per session is still unmeasured. No buyer has ever sent this node a request.
  glm-5.2 is a reasoning model (36 reasoning tokens for a one-word answer, $0.00028).
  Break-even is ~48k tokens/session at the 3.9 MOR/day price.
- OpenRouter key still has no spend cap.
- Claim -> re-stake automation still unbuilt. This is the binding constraint on
  everything: 700 MOR of headroom at ~30 MOR/day is ~23 days.

**Next steps**
1. Watch for the first session on glm-5.2. The moment one completes, `success` goes
   0 -> 1 and quality jumps ~0.28 -> ~0.72, at which point the bid can be repriced up to
   just under 10.3 MOR/day and still hold rank #1 — roughly 2.5x the revenue. Repricing
   costs another 0.3 MOR bid fee.
2. Measure actual OpenRouter spend against MOR earned over the first full day before
   adding more models.
3. Remaining shortlist, in order of MOR/day available, with the price needed to take
   rank #1 (each costs 0.3 MOR in bid fees):
   deepseek-v4-pro `0xb2c4a603` @ 2.92 (5.2 MOR/d);
   glm-5.2 second id `0x0eab02cc` @ 3.88 (4.9);
   deepseek-v4-pro:web `0xd17aae69` @ 3.61 (3.5);
   deepseek-v4-flash `0xc2c4b037` @ 1.38 (2.7, requires repricing the existing bid);
   Claude Sonnet 4.6 `0x6f5ccab3` @ 24.44 (2.3, highest cost risk — 15k token break-even);
   deepseek-v4-flash:web `0x11a14c87` @ 3.84 (2.0);
   Gemma-4-31b `0x76bb9ac6` @ 1.13 (2.0).
   Winning all eight is ~586 sessions/day = 68% of network volume at ~30 MOR/day.
4. Top the wallet up to ~25 MOR if defending positions in a price war — every reprice
   is 0.3 MOR and there is no refund on bid deletion.
5. Consider a paid RPC. Free keyed tiers are marginal at our volume (Alchemy free is
   30M CU/month ~= 1.8M requests). Cheapest genuinely usable paid option is dRPC
   pay-as-you-go at a flat $6/1M requests regardless of method — the right shape for a
   getLogs-polling workload, ~$8-30/month here.
