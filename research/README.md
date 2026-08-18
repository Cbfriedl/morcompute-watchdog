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
