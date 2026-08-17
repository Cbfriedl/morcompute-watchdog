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
SEL_ACTIVE_PROVIDERS = "0xd5472642"   # getActiveProviders(uint256,uint256)
SEL_PROVIDER_SESSIONS = "0x87bced7d"  # getProviderSessions(address,uint256,uint256)
SEL_GET_SESSION = "0x39b240bd"        # getSession(bytes32)
SEL_GET_PROVIDER = "0x55f21eb7"       # getProvider(address)
HIST = os.environ.get("CENSUS_HISTORY", "census-history.json")
EARNER_WINDOW_DAYS = int(os.environ.get("EARNER_WINDOW_DAYS", "10"))

# Per-endpoint JSON-RPC batch ceilings, measured empirically 2026-08-17:
#   mainnet.base.org  -> 10 ("maximum 10 calls" above that)
#   base.drpc.org     -> 3  (free plan rejects larger)
# Session scanning is thousands of calls, so the difference decides whether the
# job finishes inside the Actions timeout.
BATCH_FOR = {"mainnet.base.org": 10, "base.drpc.org": 3, "1rpc.io": 3}
SESSION_DAYS = int(os.environ.get("SESSION_DAYS", "10"))   # widest window scanned
SESSION_SHORT_DAYS = int(os.environ.get("SESSION_SHORT_DAYS", "7"))
SESSION_CAP_PER_PROVIDER = int(os.environ.get("SESSION_CAP", "4000"))

RPCS = [r.strip() for r in os.environ.get(
    "CENSUS_RPCS",
    # mainnet.base.org first: it accepts batches of 10 against dRPC's 3, which
    # is a 3x reduction in request count over thousands of session reads.
    "https://mainnet.base.org,https://base.drpc.org"
).split(",") if r.strip()]

OUT = os.environ.get("CENSUS_FILE", "census.json")
BATCH = 3          # conservative default; see BATCH_FOR
_rpc_i = [0]


def batch_limit():
    """Ceiling of the PRIMARY endpoint, not the weakest.

    Taking the minimum across all endpoints meant dRPC's limit of 3 capped
    everything, tripling the request count and blowing the job's time budget.
    rpc() now splits a batch automatically if the serving endpoint rejects its
    size, so sizing to the primary is safe.
    """
    for host, n in BATCH_FOR.items():
        if RPCS and host in RPCS[0]:
            return n
    return 3


BATCH_ERR = ("maximum 10 calls", "batch of more than", "batch size",
             "too many", "not allowed on free plan")


def rpc(batch):
    """Round-robin across endpoints; only a failure of all of them raises.

    If an endpoint rejects the batch for being too large, the payload is split
    and the halves retried, so a slower endpoint never forces the whole job
    down to its own batch ceiling.
    """
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
                msg = str(r).lower()
                if len(batch) > 1 and any(k in msg for k in BATCH_ERR):
                    mid = len(batch) // 2
                    return rpc(batch[:mid]) + rpc(batch[mid:])
                raise ValueError(str(r)[:140])
            if any("result" not in x for x in r):
                msg = str(r).lower()
                if len(batch) > 1 and any(k in msg for k in BATCH_ERR):
                    mid = len(batch) // 2
                    return rpc(batch[:mid]) + rpc(batch[mid:])
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


def scan_sessions(log):
    """Session activity over the trailing window.

    Returns (per_model, per_provider, note) where
      per_model    = {modelId: {"s7": n, "s10": n}}
      per_provider = {addr: {"s10": n, "by": {modelId: n}}}

    getSession is one call per session and there are thousands, so this is the
    expensive half of the census. It walks each provider's list newest-first and
    stops once an entire batch predates the cutoff.

    getSession is one call per session and there are thousands, so this is the
    expensive half of the census. It walks each provider's session list from the
    newest backwards and stops at the cutoff.

    Returns (counts_by_model, coverage_note).
    """
    B = batch_limit()
    now = int(time.time())
    cutoff = now - SESSION_DAYS * 86400
    short_cut = now - SESSION_SHORT_DAYS * 86400
    # (provider, bidId) -> [n_in_short_window, n_in_full_window]
    counts, scanned, capped_provs = {}, 0, []

    try:
        w = W(rpc([call(SEL_ACTIVE_PROVIDERS + "%064x" % 0 + "%064x" % 500)])[0]["result"])
        off = int(w[0], 16) // 32
        n = int(w[off], 16)
        provs = ["0x" + w[off + 1 + i][24:] for i in range(n)]
    except Exception as e:
        log("  cannot list providers: %s" % str(e)[:80])
        return {}, "provider list unavailable"
    log("active providers: %d" % len(provs))

    bid_model = {}
    for prov in provs:
        # session ids, newest last
        try:
            sw = W(rpc([call(SEL_PROVIDER_SESSIONS + prov[2:].rjust(64, "0")
                             + "%064x" % 0 + "%064x" % 100000)])[0]["result"])
            so = int(sw[0], 16) // 32
            sn = int(sw[so], 16)
            ids = ["0x" + sw[so + 1 + i] for i in range(sn)]
        except Exception:
            continue
        if not ids:
            continue
        take = ids[-SESSION_CAP_PER_PROVIDER:]
        if len(take) < len(ids):
            capped_provs.append(prov[:10])

        # walk backwards; stop once a whole batch predates the cutoff
        stop = False
        idx = len(take)
        while idx > 0 and not stop:
            lo = max(0, idx - B)
            ch = take[lo:idx]
            idx = lo
            try:
                res = {x["id"]: x for x in rpc(
                    [call(SEL_GET_SESSION + sid[2:], i) for i, sid in enumerate(ch)])}
            except Exception:
                continue
            older = 0
            for i, sid in enumerate(ch):
                try:
                    r = W(res[i]["result"])
                    opened = int(r[7], 16)
                    if opened < cutoff:
                        older += 1
                        continue
                    bid = "0x" + r[2]
                    key = (prov, bid)
                    c = counts.setdefault(key, [0, 0])
                    if opened >= short_cut:
                        c[0] += 1
                    c[1] += 1
                    scanned += 1
                except Exception:
                    pass
            if older == len(ch):
                stop = True

    # bid -> model, resolved once per distinct bid
    need = sorted({b for (_p, b) in counts})
    bid2model = {}
    for ch in [need[i:i + B] for i in range(0, len(need), B)]:
        try:
            res = {x["id"]: x for x in rpc(
                [call(SEL_GET_BID + b[2:], i) for i, b in enumerate(ch)])}
        except Exception:
            continue
        for i, b in enumerate(ch):
            try:
                bid2model[b] = "0x" + W(res[i]["result"])[1]
            except Exception:
                pass

    per_model, per_provider = {}, {}
    for (prov, bid), (n7, n10) in counts.items():
        mid = bid2model.get(bid)
        if not mid:
            continue
        m = per_model.setdefault(mid, {"s7": 0, "s10": 0})
        m["s7"] += n7
        m["s10"] += n10
        p = per_provider.setdefault(prov, {"s10": 0, "by": {}})
        p["s10"] += n10
        p["by"][mid] = p["by"].get(mid, 0) + n10

    note = "%d sessions in %dd across %d models, %d providers" % (
        scanned, SESSION_DAYS, len(per_model), len(per_provider))
    if capped_provs:
        note += "; capped at %d for %s" % (SESSION_CAP_PER_PROVIDER, ",".join(capped_provs))
    log("  " + note)
    return per_model, per_provider, note


T_PROVIDER_REGISTERED = ("0x70abce74777b3838ae60a33a6b9a87d9d25532668"
                         "fe4fea548554c55868579c0")
BLOCKSCOUT = os.environ.get("BLOCKSCOUT", "https://base.blockscout.com")


def _bs(url, tries=5):
    """Blockscout intermittently answers `{"status":"0","message":"Something went
    wrong."}` and succeeds on retry, so an API-level failure is retried too —
    not just transport errors."""
    for a in range(tries):
        try:
            r = subprocess.run(["curl", "-s", "-L", "--max-time", "45",
                                "-H", "accept: application/json", url],
                               capture_output=True)
            d = json.loads(r.stdout)
            # v1 endpoints report failure in-band; v2 have no "status" field
            if isinstance(d, dict) and d.get("status") == "0":
                raise ValueError(d.get("message", "blockscout status 0"))
            return d
        except Exception:
            time.sleep(1.5 * (a + 1))
    return None


def last_stake(addr):
    """Most recent providerRegister with a non-zero amount.

    The ProviderRegistered event carries only the address, so the amount has to
    come from the transaction input. Blockscout's v1 logs endpoint is used
    because every public RPC caps eth_getLogs at 10k blocks, while this serves
    millions in one call. Returns (unix_ts, amount_MOR) or (None, None).
    """
    t1 = "0x" + addr[2:].lower().rjust(64, "0")
    d = _bs("%s/api?module=logs&action=getLogs&fromBlock=0&toBlock=latest"
            "&address=%s&topic0=%s&topic1=%s&topic0_1_opr=and"
            % (BLOCKSCOUT, DIAMOND, T_PROVIDER_REGISTERED, t1))
    logs = (d or {}).get("result")
    if not isinstance(logs, list) or not logs:
        return None, None
    # walk back from the newest until one carries an amount
    for lg in sorted(logs, key=lambda x: int(x["blockNumber"], 16), reverse=True)[:12]:
        tx = _bs("%s/api/v2/transactions/%s" % (BLOCKSCOUT, lg["transactionHash"]))
        # v2 sometimes answers with a bare string on error rather than an object
        if not isinstance(tx, dict):
            continue
        di = tx.get("decoded_input") or {}
        for prm in di.get("parameters") or []:
            if prm.get("name") == "amount_":
                amt = int(prm["value"]) / 1e18
                if amt > 0:
                    ts = int(lg.get("timeStamp", "0x0"), 16) if lg.get("timeStamp") else None
                    return ts, round(amt, 4)
    return None, None


def provider_snapshot(log):
    """stake / earned / headroom for every active provider.

    Headroom is stake - limitPeriodEarned. At zero a provider keeps serving and
    keeps paying its upstream bill while earning nothing, so 'zero headroom' is
    the single most useful liveness signal about a competitor.
    """
    B = batch_limit()
    try:
        w = W(rpc([call(SEL_ACTIVE_PROVIDERS + "%064x" % 0 + "%064x" % 500)])[0]["result"])
        off = int(w[0], 16) // 32
        n = int(w[off], 16)
        provs = ["0x" + w[off + 1 + i][24:] for i in range(n)]
    except Exception as e:
        log("  provider list failed: %s" % str(e)[:80])
        return []
    out = []
    for ch in chunked(provs, B):
        try:
            res = {x["id"]: x for x in rpc(
                [call(SEL_GET_PROVIDER + a[2:].rjust(64, "0"), i) for i, a in enumerate(ch)])}
        except Exception:
            continue
        for i, a in enumerate(ch):
            try:
                pw = W(res[i]["result"])
                b = int(pw[0], 16) // 32
                stake = int(pw[b + 1], 16) / 1e18
                earned = int(pw[b + 4], 16) / 1e18
                out.append({
                    "a": a,
                    "stake": round(stake, 4),
                    "earned": round(earned, 4),
                    "head": round(stake - earned, 4),
                    "pct": round(100 * earned / stake, 2) if stake else 0.0,
                    "deleted": int(pw[b + 5], 16) == 1,
                })
            except Exception:
                pass
    if os.environ.get("CENSUS_LAST_STAKE", "1") == "1":
        got = 0
        for p in out:
            try:
                ts, amt = last_stake(p["a"])
                if ts:
                    p["lastStakeAt"] = ts
                    p["lastStakeAmt"] = amt
                    got += 1
            except Exception:
                pass
        log("  last-stake resolved for %d/%d providers" % (got, len(out)))
    log("provider snapshot: %d" % len(out))
    return out


def earner_count(providers, log):
    """How many addresses actually earned over the trailing window.

    limitPeriodEarned only moves when a provider is paid, so comparing today's
    value against a stored snapshot gives a true earner count. Needs history,
    so the first few runs report None rather than guessing.
    """
    today = {p["a"]: p["earned"] for p in providers}
    try:
        with open(HIST) as f:
            hist = json.load(f)
    except Exception:
        hist = {"snapshots": []}
    snaps = hist.get("snapshots") or []
    now = int(time.time())
    snaps.append({"t": now, "earned": today})
    snaps = [s for s in snaps if now - s["t"] <= 45 * 86400][-60:]
    hist["snapshots"] = snaps
    with open(HIST, "w") as f:
        json.dump(hist, f, separators=(",", ":"))

    target = now - EARNER_WINDOW_DAYS * 86400
    older = [s for s in snaps if s["t"] <= target]
    if not older:
        span = round((now - snaps[0]["t"]) / 86400.0, 1) if snaps else 0
        log("  earner count: no snapshot %dd old yet (have %.1fd)"
            % (EARNER_WINDOW_DAYS, span))
        return None, span
    base = older[-1]
    n = sum(1 for a, e in today.items()
            if e > base["earned"].get(a, 0) + 1e-9)
    log("  earners in %dd: %d" % (EARNER_WINDOW_DAYS, n))
    return n, round((now - base["t"]) / 86400.0, 1)


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

    sess_model, sess_prov, sess_note = ({}, {}, "skipped")
    if os.environ.get("CENSUS_SESSIONS", "1") == "1":
        try:
            sess_model, sess_prov, sess_note = scan_sessions(log)
        except Exception as e:
            log("session scan failed: %s" % str(e)[:120])
            sess_note = "failed: %s" % str(e)[:80]

    providers_snap = provider_snapshot(log)
    zero_head = sum(1 for p in providers_snap
                    if not p["deleted"] and p["head"] <= 0.0001)
    earners, earner_span = earner_count(providers_snap, log)

    # provider addresses per model, so the dashboard can filter models by
    # whether anyone bidding on them still has headroom to earn
    head_by = {p["a"]: p["head"] for p in providers_snap}

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
            "s7": (sess_model.get(mid) or {}).get("s7", 0),
            "s10": (sess_model.get(mid) or {}).get("s10", 0),
            "px": pr,
            "pv": sorted(([x["provider"], round(x["pps"] * 86400 / 1e18, 4)]
                          for x in bl), key=lambda y: -y[1]),
            # live = at least one bidder can still be paid
            "live": 1 if any(head_by.get(x["provider"], 0) > 0.0001 for x in bl) else 0,
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
        "registeredModels": len(model_ids),
        "activeProviders": len([p for p in providers_snap if not p["deleted"]]),
        "zeroHeadroom": zero_head,
        "earners10d": earners,
        "earnerWindowDays": EARNER_WINDOW_DAYS,
        "earnerSpanDays": earner_span,
        "providers": [dict(p,
                           s10=(sess_prov.get(p["a"]) or {}).get("s10", 0),
                           by=(sess_prov.get(p["a"]) or {}).get("by", {}))
                      for p in providers_snap],
        "sessions7d": sum(v["s7"] for v in sess_model.values()),
        "sessions10d": sum(v["s10"] for v in sess_model.values()),
        "sessionsNote": sess_note,
        "sessionDays": SESSION_DAYS,
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
