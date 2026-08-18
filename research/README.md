# Research scripts

The scripts that produced the market analysis behind `docs/PRICING_STRATEGY.md`.

These are **research tools, not production code**. They were written to answer
specific questions against Base mainnet, they cache aggressively to survive free
RPC rate limits, and most expect to be run from a directory holding their JSON
caches. They are committed because the analysis they produced is the basis for
every pricing decision, and that reasoning should be reproducible.

Nothing here holds a credential. Everything reads public chain state.

## foundation/ — one-time base dataset builds

Run these first; they establish the dataset everything else queries.

| Script | What it builds |
|---|---|
| `keccak.py` | Minimal keccak-256, used to derive function selectors from signatures |
| `score.py` | **Replicates the router's bid scoring formula** from chain state. The core of the whole analysis |
| `target.py` | Safe rank-#1 entry price per model, with per-provider and per-model caches |
| `sessall.py` | All-time session count per provider. `getProviderSessions` is append-only, so the array length *is* the lifetime count — one call per provider |
| `history_build.py` | **The all-time session history.** Reads every session ever opened and builds `history.db` + `history-daily.json`. See below |
| `sesslib.py` | Shared session-reading helpers |
| `freshen.py` | Brings `sess_cache.json` up to the current block by walking only the tail of each provider's session list |
| `scan_models.py`, `scan2.py`, `scan3.py` | Enumerate registered models and their active bids |
| `flows_all.py` | Single-pass MOR `Transfer` scan separating contributed capital from claimed earnings |
| `outside.py`, `outside2.py` | Outside (non-recycled) capital per provider. `outside2` is the correct transfer-flow method; `outside` used a lower-bound approximation |
| `regs.py`, `reglogs.py` | Provider registration events and dates |
| `stakehist.py` | Stake and top-up history |

## analysis/ — queries over that dataset

| Script | Question it answers |
|---|---|
| `rank.py` | Where do I rank on a model, and why — full score breakdown per bidder |
| `run_all.py`, `run_target.py` | Entry price across every model with demand |
| `safeprice.py` | Worst-case-safe rank-#1 price, given the weight split is unknown |
| `oppo.py` | Sessions/day, median duration and MOR/day actually taken, per model |
| `daily.py` | Day-by-day session counts per provider on a model — this is what proved the cheapest live bid takes the flow |
| `bids.py`, `earners.py`, `attrib.py` | Bid listings, who earns, session attribution |
| `fetchamts.py`, `flows.py` | Payment amounts and MOR movements |
| `sess10.py` | Trailing 10-day session scan |

## The one result worth knowing

`daily.py` on `deepseek-v4-flash` showed the handover that established the whole
model: `0x010208ec` held the model at 1.452 MOR/day for eight days, repriced up to
4.56 on 08-14, and lost the entire session flow to `0x7ddd1ea5` at 1.426 within a
single day.

Price, not quality, decides who serves.


## Re-running the all-time history build

`foundation/history_build.py` produces `history.db` (the indexed all-time
archive, committed) and `history-daily.json` (a small daily rollup for first
paint). `docs/HISTORY_DB.md` documents the schema and how to query it from the
browser.

```bash
python3 research/foundation/history_build.py            # all stages, in order
python3 research/foundation/history_build.py --status   # progress, touches nothing
python3 research/foundation/history_build.py sess db    # just these stages
```

Stages are `ids`, `sess`, `bids`, `models`, `db`, and each is independently
resumable — everything network-bound writes to `histcache/` (gitignored) as it
goes, so an interrupted run continues rather than restarts. Only `db` is
offline; it rebuilds the database from the caches in a few seconds, which makes
schema changes cheap to iterate on.

### Endpoints

The keyed endpoints come from `rpc_endpoints.endpoints()` in the MORCompute
project root, which reads `.rpc-key` (chmod 600, gitignored). Nothing in this
repo contains a credential and the script never prints a URL — `describe()`
reports hostnames only.

`base-rpc.publicnode.com` is used alongside them as free extra capacity. It is
worth knowing why it works here: it sustained **70 eth_call/s** in testing,
roughly three times both keyed endpoints combined, and it more than halved the
runtime. Two caveats, both already accounted for:

* It **refuses `eth_getLogs` entirely.** Irrelevant to this job, which is
  nothing but `eth_call` — but do not copy the choice into a log-scanning script.
* Several public Base endpoints, this one included, return **403 to the default
  `Python-urllib` User-Agent** before they even parse the payload. That failure
  looks exactly like the endpoint being down. Send a normal User-Agent.

Each endpoint gets its own worker thread and its own adaptive pacer, because the
rate limits are independent. The pacer walks the delay down while responses stay
clean and backs off on throttling, and an endpoint that keeps erroring is
dropped for five minutes rather than stalling the run. Alchemy signals its
throttle *inside* a batch response — some entries come back, the rest carry a
"compute units per second" error — so partial batches are normal and the missing
ids are requeued rather than lost.

### The checksum that matters

`getProviderSessions` returns the complete id list in chronological order, so its
**array length is the lifetime session count** and needs no independent
derivation. The build checks its per-provider detail count against that length
and writes the true ratio to `meta.coverage`, with any shortfall itemised per
provider in `meta.incompleteProviders`. It never assumes completeness: a
silently partial history is worse than none, and paginated endpoints truncating
without an error is a trap this project has already hit more than once.

Because the id lists are append-only, re-running later is cheap — the `ids`
stage only walks past what it already holds, and `sess` only fetches sessions it
has never seen.

`foundation/history_validate.py` re-reads a random sample straight from chain
and checks the computed MOR against each session's own
`providerWithdrawnAmount` — an independent field, so it is a real check rather
than a restatement. It exits non-zero if the two stop agreeing.

One thing to weigh before re-running often: `history.db` is a ~23 MB binary, so
every rebuild commits another ~23 MB of git history that never goes away. It is
an archive of an append-only dataset, not a daily artefact — `census.json`
already covers the rolling window. Rebuild it when the extra depth is worth the
weight, not on a cron.
