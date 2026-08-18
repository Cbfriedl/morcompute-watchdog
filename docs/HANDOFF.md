# Operations Handoff

Everything needed to run, diagnose and extend the MORCompute provider.
Current as of 2026-08-18.

No secrets appear in this repository. Where a credential is needed, its
**location** is given, never its value.

---

## 1. What exists

| Thing | Where | Purpose |
|---|---|---|
| Provider node | `2.28.8.173:3333` | Serves compute. Docker container `morpheus-router`, image v7.5.0 |
| Management API | `127.0.0.1:8082` on the box | REST API, Basic Auth. **Not** exposed publicly |
| Provider address | `0x2f144F3b192A2d2D2384de7007EE2cAd943C601b` | On Base mainnet (chain 8453) |
| On-box monitor | `/root/morpheus/monitor/` | systemd timer, every 10 min, read-only |
| Watchdog | GitHub Actions, every 13 min | External TCP probe, writes `status.json` |
| Dashboard | `cbfriedl.github.io/morcompute-watchdog` | Reads chain from the browser |
| Daily census | GitHub Actions, 06:17 UTC | Writes `census.json` |

### Key addresses

```
Diamond proxy   0x6aBE1d282f72B474E54527D93b979A4f64d3030a
MOR token       0x7431aDa8a591C955a994a21710752EF9b882b8e3   (Base only)
Treasury        0x5160c0311a95e0a1072fa85df23712a7ba1cd4b1   (source of claims)
```

### Credentials — locations only

```
Wallet key        inside the router's badgerdb at /root/morpheus/morpheus-data/data/
                  NEVER touched by the monitor or any script here
API Basic Auth    /root/morpheus/morpheus-data/.cookie
OpenRouter key    /root/morpheus/morpheus-data/models-config.json  (per model entry)
ntfy topic        /root/morpheus/monitor/monitor.env  (NTFY_TOPIC)
SSH               ~/.ssh/morpheus_ed25519  on the operator machine
```

---

## 2. Current position

14 active bids, rank #1 on all 14. Roughly 43% of network session volume is
addressable. See `docs/PRICING_STRATEGY.md` for why each price is what it is.

```
Claude Opus 4.7        11.5381 MOR/day     Claude Sonnet 5      5.4207
glm-5.2                 8.0000             grok-4.5             3.8371
Claude Sonnet 4.6       2.1477             Kimi K3              1.7297
qwen3-235b              1.0533             venice-uncensored    0.8036
hermes-3-405b           0.6737             MiniMax-M2.5         0.5118
deepseek-v4-flash       0.5000             gpt-oss-120b         0.4314
llama-3.3-70b           0.3142             Gemma-4-31b          0.2767
```

Deliberately **not** bid: `deepseek-v4-pro` (negative margin even at maturity,
despite being the largest block on the board), `DeepSeek V4 Flash 0731` (no room
under the incumbent), and every `:web` / `:online` variant (the search plugin
adds ~1,000 prompt tokens to every request).

---

## 3. Runbook

### Check state

```bash
ssh -i ~/.ssh/morpheus_ed25519 root@2.28.8.173

curl -s localhost:8082/healthcheck | python3 -m json.tool     # models + probes
C=$(cat /root/morpheus/morpheus-data/.cookie)
curl -s -u "$C" localhost:8082/blockchain/balance             # wallet MOR + ETH
python3 -c "import json;d=json.load(open('/root/morpheus/monitor/metrics.json'));print(d['alerts_active'])"
```

### Reprice (the routine operation)

```bash
python3 /root/morpheus/reprice.py          # dry run — ALWAYS look first
python3 /root/morpheus/reprice.py --go     # send
```

Only touches the six larger-payment models, steps 40% toward the tie price, and
waits for 5 completed sessions per model. Re-run the dry run after every step:
the tie price roughly doubles once a model matures, so printed targets go stale.

Tunable by environment variable: `REPRICE_STEP` (0.40), `REPRICE_MIN_SESSIONS`
(5), `REPRICE_MIN_GAIN` (0.15), `REPRICE_CEILING` (0.90).

### Add a model

1. Confirm it has sessions in the trailing 10 days.
2. Test the OpenRouter slug with a real request. Confirm a sensible reply **and**
   check the token count — that is how the `:web` trap was caught.
3. Add to `models-config.json`, then `docker compose restart`.
4. Compute the entry price (see `research/foundation/target.py`).
5. `POST /blockchain/bids` with `{"modelID", "pricePerSecond"}`. Retry on
   `replacement transaction underpriced` — it is a nonce race, it clears.
6. `docker compose restart` again so `hasActiveBid` refreshes.
7. Verify rank via `/blockchain/models/{id}/bids/rated`.

### Claim earnings

Not yet automated. This is the largest outstanding gap. Endpoints exist:
`/proxy/sessions/{id}/providerClaimableBalance` and
`/proxy/sessions/{id}/providerClaim`.

---

## 4. Things that will bite you

**The API silently truncates.** `/blockchain/providers/{id}/bids/active` caps at
10 results with no error — it was dropping 4 of our 14 bids. Read
`getProviderActiveBids` from chain. Blockscout v2 pagination has the same flaw.

**`hasActiveBid` is cached until restart.** Restart the router after posting.

**Nonce races.** Round-robin RPC means consecutive transactions can hit different
nodes and reuse a pending nonce. Retry.

**`skipped` health status is normal for some models.** A model whose on-chain
registry entry has no `LLM` tag makes the router report `modelType: UNKNOWN` and
never probe it. Affects Claude Sonnet 4.6, Claude Sonnet 5, Kimi K3. It is a
property of the listing, not a fault. The monitor was patched to stop alerting.

**Open sessions look like pure loss.** Cost accrues while a session runs; revenue
credits only at close. Never judge profitability with sessions open.

**RPC endpoints.** `ETH_NODE_ADDRESS` in `.env` pins the router to ONE endpoint
*and* disables its built-in 8-endpoint round-robin. It is commented out
deliberately — leave it that way. `POST /config/ethNode` does not work in this
container (needs a system keyring; fails with `dbus-launch not found`).

Free Base endpoints, measured from the box:

```
base-rpc.publicnode.com   30/30 under burst, ~550ms   REFUSES eth_getLogs
mainnet.base.org           5/30                        getLogs OK
developer-access-...       5/30                        getLogs OK
base.lava.build            1/30                        getLogs OK
base.drpc.org              1/30                        getLogs OK   ← was the bottleneck
```

The monitor uses publicnode (it never calls `getLogs`). The router needs
`getLogs` for its polling log watcher, so it uses the mixed pool.

---

## 5. Repository map

```
index.html                     dashboard (GitHub Pages)
census.json, status.json       data written by the Actions workflows
watchdog/
  watchdog.py                  external TCP dead-man's switch
  census.py                    full daily market census
  census_incremental.py        incremental census (built, not yet scheduled)
  reprice.py                   gradual selective repricing
  morpheus_monitor.py          on-box monitor (deployed to /root/morpheus/monitor)
  workflow-*.yml               GitHub Actions definitions
docs/
  PRICING_STRATEGY.md          how pricing works, bid to steady state
  HANDOFF.md                   this file
research/
  foundation/                  one-time base dataset builds
  analysis/                    queries run on top of that dataset
```

---

## 6. Open items

1. **Wallet funding.** 1.90 MOR at handoff. Each bid post or reprice costs 0.3
   MOR and deletion refunds nothing. One reprice pass over the five waiting
   models costs 1.5. Recommend ~25 MOR.
2. **Claim → re-stake automation.** Unbuilt. Headroom is 699.3 of 700 so not yet
   binding, but at ~30 MOR/day it would choke in about 23 days.
3. **OpenRouter spend cap.** The key is uncapped against $50 of credits. Usage
   was ~$1.42 at handoff.
4. **Incremental census not scheduled.** `census_incremental.py` is written and
   dry-run tested. The full census does not fit in an Actions run.
5. **Paid RPC.** Free tiers are marginal at the measured ~1.3–5M requests/month.
   Cheapest usable is dRPC pay-as-you-go, flat $6 per 1M requests (~$8–30/month),
   which suits a `getLogs`-polling workload better than credit-weighted plans.
6. **Gemma-4-31b showed 1 success in 2 sessions.** One failure, cause unknown.
   Worth checking if it recurs — `success` is squared, so failures hurt twice.
