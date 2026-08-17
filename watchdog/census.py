#!/usr/bin/env python3
"""
Daily on-chain census of the Morpheus compute market.

Enumerates every active model, every active bid on each, and resolves each
bid's price and provider. Writes a compact census.json the dashboard reads.

Runs on GitHub Actions. Holds no credential — everything here is public chain
state read through public RPCs.

Why this exists rather than querying the router's API: the router's `offset`
pagination silently returns 0 results, which once produced a "2 active bids"
reading when the real number was 728. Direct chain enumeration is the only
trustworthy source.

Rate limits are the main obstacle. dRPC's free plan caps JSON-RPC batches at 3
and times out under load, so this rotates endpoints, batches by 3, and retries
hard. A partial run is written out rather than lost.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

DIAMOND = "0x6aBE1d282f72B474E54527D93b979A4f64d3030a"

SEL_ACTIVE_MODEL_IDS = "0x3839d3dc"   # getActiveModelIds(uint256,uint256)
SEL_MODEL_ACTIVE_BIDS = "0x8a683b6e"  # getModelActiveBids(bytes32,uint256,uint256)
SEL_GET_BID = "0x91704e1e"            # getBid(bytes32)
SEL_GET_MODEL = "0x21e7c498"          # getModel(bytes32)

RPCS = [r.strip() for r in os.environ.get(
    "CENSUS_RPCS",
    "https://base.drpc.org,https://mainnet.base.org,https://1rpc.io/base"
).split(",") if r.strip()]

OUT = os.environ.get("CENSUS_FILE", "census.json")
BATCH = 3          # dRPC free plan rejects batches larger than this
_rpc_i = [0]


def rpc(batch):
    """Round-robin across endpoints; only a failure of all of them raises."""
    last = None
    for attempt in range(len(RPCS) * 4):
        url = RPCS[_rpc_i[0] % len(RPCS)]
        _rpc_i[0] += 1
        try:
            p = subprocess.run(
                ["curl", "-s", "--max-time", "70", "-X", "POST", url,
                 "-H", "content-type: application/json", "--data-binary", "@-"],
                input=json.dumps(batch).encode(), capture_output=True)
            r = json.loads(p.stdout)
            if not isinstance(r, list):
                raise ValueError(str(r)[:140])
            if any("result" not in x for x in r):
                raise ValueError("partial: " + str(r)[:140])
            return r
        except Exception as e:
            last = e
            time.sleep(min(1.5 * (attempt + 1), 12))
    raise RuntimeError("all RPCs failed: %r" % last)


def call(data, i=1):
    return {"jsonrpc": "2.0", "id": i, "method": "eth_call",
            "params": [{"to": DIAMOND, "data": data}, "latest"]}


def W(h):
    h = h[2:] if h.startswith("0x") else h
    return [h[i:i + 64] for i in range(0, len(h), 64)]


def dec_str(w, base):
    n = int(w[base], 16)
    if n == 0:
        return ""
    raw = "".join(w[base + 1: base + 1 + (n + 31) // 32])
    return bytes.fromhex(raw[: n * 2]).decode("utf8", "replace")


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def main():
    t0 = time.time()
    log = lambda m: print(m, flush=True)

    # ---- 1. active models -------------------------------------------------
    w = W(rpc([call(SEL_ACTIVE_MODEL_IDS + "%064x" % 0 + "%064x" % 1000)])[0]["result"])
    off = int(w[0], 16) // 32
    n = int(w[off], 16)
    model_ids = ["0x" + w[off + 1 + i] for i in range(n)]
    log("active models: %d" % len(model_ids))

    # ---- 2. metadata ------------------------------------------------------
    models = {}
    for ch in chunked(model_ids, BATCH):
        try:
            res = {x["id"]: x for x in rpc(
                [call(SEL_GET_MODEL + m[2:], i) for i, m in enumerate(ch)])}
        except Exception as e:
            log("  model meta batch failed: %s" % str(e)[:80])
            continue
        for i, m in enumerate(ch):
            try:
                mw = W(res[i]["result"])
                b = int(mw[0], 16) // 32
                tb = b + int(mw[b + 5], 16) // 32
                tn = int(mw[tb], 16)
                models[m] = {
                    "name": dec_str(mw, b + int(mw[b + 4], 16) // 32),
                    "owner": "0x" + mw[b + 3][24:],
                    "tags": [dec_str(mw, tb + 1 + int(mw[tb + 1 + j], 16) // 32)
                             for j in range(tn)],
                    "real": int(mw[b], 16) != 0,
                }
            except Exception:
                pass
    log("model metadata: %d" % len(models))

    # ---- 3. active bids per model ----------------------------------------
    bid_ids, by_model = [], {}
    for ch in chunked(model_ids, BATCH):
        try:
            res = {x["id"]: x for x in rpc(
                [call(SEL_MODEL_ACTIVE_BIDS + m[2:] + "%064x" % 0 + "%064x" % 100, i)
                 for i, m in enumerate(ch)])}
        except Exception as e:
            log("  bid-id batch failed: %s" % str(e)[:80])
            continue
        for i, m in enumerate(ch):
            try:
                bw = W(res[i]["result"])
                o = int(bw[0], 16) // 32
                k = int(bw[o], 16)
                ids = ["0x" + bw[o + 1 + j] for j in range(k)]
                by_model[m] = ids
                bid_ids += ids
            except Exception:
                pass
    log("bid ids: %d across %d models" % (len(bid_ids), sum(1 for v in by_model.values() if v)))

    # ---- 4. resolve each bid ---------------------------------------------
    bids = {}
    for ch in chunked(bid_ids, BATCH):
        try:
            res = {x["id"]: x for x in rpc(
                [call(SEL_GET_BID + b[2:], i) for i, b in enumerate(ch)])}
        except Exception as e:
            log("  bid detail batch failed: %s" % str(e)[:80])
            continue
        for i, b in enumerate(ch):
            try:
                r = W(res[i]["result"])
                bids[b] = {"provider": "0x" + r[0][24:], "model": "0x" + r[1],
                           "pps": int(r[2], 16), "createdAt": int(r[4], 16),
                           "deletedAt": int(r[5], 16)}
            except Exception:
                pass
    log("bids resolved: %d" % len(bids))

    # ---- 5. shape it for the dashboard -----------------------------------
    import statistics
    live = [b for b in bids.values() if b["deletedAt"] == 0]
    per = {}
    for b in live:
        per.setdefault(b["model"], []).append(b)

    rows = []
    for mid, bl in per.items():
        m = models.get(mid, {})
        pr = sorted(round(x["pps"] * 86400 / 1e18, 4) for x in bl)
        rows.append({
            "id": mid,
            "n": (m.get("name") or "(unnamed)")[:46],
            "b": len(bl),
            "p": len({x["provider"] for x in bl}),
            "mn": pr[0], "md": round(statistics.median(pr), 4), "mx": pr[-1],
            "t": "|".join(m.get("tags") or []),
            "r": 1 if m.get("real") else 0,
            "px": pr,
        })
    rows.sort(key=lambda r: -r["b"])

    allp = sorted(x["pps"] * 86400 / 1e18 for x in live)
    def q(p):
        return round(allp[min(len(allp) - 1, int(len(allp) * p))], 4) if allp else None

    out = {
        "asOf": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "activeModels": len(model_ids),
        "modelsWithBids": len(rows),
        "totalBids": len(live),
        "providers": len({x["provider"] for x in live}),
        "p10": q(.10), "p25": q(.25), "p50": q(.50), "p75": q(.75), "p90": q(.90),
        "atCeiling": sum(1 for v in allp if v >= 863.9),
        "below10": sum(1 for v in allp if v < 10),
        "coverage": {"modelsScanned": len(by_model), "bidsResolved": len(bids),
                     "bidIdsFound": len(bid_ids)},
        "runSeconds": round(time.time() - t0),
        "models": rows,
    }
    with open(OUT, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    log("wrote %s: %d models, %d bids, %d providers, %ds"
        % (OUT, len(rows), len(live), out["providers"], out["runSeconds"]))

    # A badly partial run is worse than none — fail loudly rather than publish it.
    if len(by_model) < len(model_ids) * 0.9 or len(bids) < len(bid_ids) * 0.9:
        log("WARNING: coverage below 90% — census is partial")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
