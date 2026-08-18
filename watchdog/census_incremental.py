#!/usr/bin/env python3
"""
Incremental daily census.

The full census does not fit in a GitHub Actions run — two attempts were
cancelled at the 50 and 110 minute timeouts, because scanning ~8,600 sessions
and 160 days of token transfers from scratch takes hours. So it does not scan
from scratch.

Instead a foundation state file (`census-state.json`) is built once, offline,
and each daily run only advances it:

  * sessions   — getProviderSessions is append-only and ordered oldest->newest,
                 so a per-provider index is stored and only entries beyond it
                 are fetched.
  * MOR flows  — a block cursor is stored and only new blocks are scanned for
                 Transfer events, which is what separates contributed capital
                 (any non-treasury inbound) from claimed earnings.
  * bids/models— cheap enough to re-read in full each run.

State is committed back to the repo, so the next run resumes exactly where this
one stopped. There is deliberately no full-refresh path: rebuilding the
foundation is an offline job, not something a 10-minute cron should attempt.

Sessions older than the retention window are pruned so the file cannot grow
without bound.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

DIAMOND = "0x6aBE1d282f72B474E54527D93b979A4f64d3030a"
MOR_TOKEN = "0x7431aDa8a591C955a994a21710752EF9b882b8e3"
TREASURY = "0x5160c0311a95e0a1072fa85df23712a7ba1cd4b1"
TRANSFER_TOPIC = ("0xddf252ad1be2c89b69c2b068fc378daa"
                  "952ba7f163c4a11628f55a4df523b3ef")

SEL_ACTIVE_PROVIDERS = "0xd5472642"
SEL_PROVIDER_SESSIONS = "0x87bced7d"
SEL_GET_SESSION = "0x39b240bd"
SEL_GET_PROVIDER = "0x55f21eb7"
SEL_GET_BID = "0x91704e1e"

STATE = os.environ.get("CENSUS_STATE", "census-state.json")
OUT = os.environ.get("CENSUS_FILE", "census.json")
RETAIN_DAYS = int(os.environ.get("RETAIN_DAYS", "30"))
WINDOW_DAYS = int(os.environ.get("WINDOW_DAYS", "10"))
# leave headroom under the job timeout so state is always committed
BUDGET_SEC = int(os.environ.get("BUDGET_SEC", "1500"))

RPCS = [r.strip() for r in os.environ.get(
    "CENSUS_RPCS", "https://base.drpc.org,https://mainnet.base.org"
).split(",") if r.strip()]
BATCH_FOR = {"mainnet.base.org": 10, "base.drpc.org": 3}
_i = [0]
T0 = time.time()


def out_of_time():
    return time.time() - T0 > BUDGET_SEC


def rpc(batch):
    last = None
    for attempt in range(len(RPCS) * 4):
        url = RPCS[_i[0] % len(RPCS)]
        _i[0] += 1
        try:
            p = subprocess.run(
                ["curl", "-s", "--max-time", "55", "-X", "POST", url,
                 "-H", "content-type: application/json", "--data-binary", "@-"],
                input=json.dumps(batch).encode(), capture_output=True)
            r = json.loads(p.stdout)
            if not isinstance(r, list):
                raise ValueError(str(r)[:120])
            if any("result" not in x for x in r):
                raise ValueError("partial " + str(r)[:120])
            return r
        except Exception as e:
            last = e
            time.sleep(min(1.0 * (attempt + 1), 6))
    raise RuntimeError("all RPCs failed: %r" % last)


def call(data, i=1):
    return {"jsonrpc": "2.0", "id": i, "method": "eth_call",
            "params": [{"to": DIAMOND, "data": data}, "latest"]}


def batch_limit():
    for host, n in BATCH_FOR.items():
        if RPCS and host in RPCS[0]:
            return n
    return 3


def W(h):
    h = h[2:] if h.startswith("0x") else h
    return [h[i:i + 64] for i in range(0, len(h), 64)]


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {"version": 1, "sessions": {}, "provIdx": {},
                "outside": {}, "claimed": {}, "lastBlock": None}


def save_state(st):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, separators=(",", ":"))
    os.replace(tmp, STATE)


def advance_sessions(st, provs, log):
    """Fetch only sessions past each provider's stored index."""
    B = batch_limit()
    added = 0
    for a in provs:
        if out_of_time():
            log("  session scan: out of time, will resume next run")
            break
        start = int(st["provIdx"].get(a, 0))
        try:
            w = W(rpc([call(SEL_PROVIDER_SESSIONS + a[2:].rjust(64, "0")
                            + "%064x" % start + "%064x" % 5000)])[0]["result"])
            off = int(w[0], 16) // 32
            n = int(w[off], 16)
            ids = ["0x" + w[off + 1 + i] for i in range(n)]
        except Exception:
            continue
        new = [s for s in ids if s not in st["sessions"]]
        for i in range(0, len(new), B):
            if out_of_time():
                break
            ch = new[i:i + B]
            try:
                res = {x["id"]: x for x in rpc(
                    [call(SEL_GET_SESSION + s[2:], j) for j, s in enumerate(ch)])}
            except Exception:
                continue
            for j, sid in enumerate(ch):
                try:
                    r = W(res[j]["result"])
                    st["sessions"][sid] = {"p": a, "b": "0x" + r[2],
                                           "t": int(r[7], 16),
                                           "c": int(r[9], 16),
                                           "u": "0x" + r[1][24:],
                                           "w": int(r[6], 16) / 1e18}
                    added += 1
                except Exception:
                    pass
        st["provIdx"][a] = start + len(ids)
    log("  sessions: +%d new (total %d)" % (added, len(st["sessions"])))
    return added


def advance_flows(st, provs, log, step=9000):
    """Scan only blocks after the stored cursor for MOR Transfers."""
    pad = {("0x" + a[2:].lower().rjust(64, "0")): a.lower() for a in provs}
    try:
        head = int(rpc([{"jsonrpc": "2.0", "id": 1,
                         "method": "eth_blockNumber", "params": []}])[0]["result"], 16)
    except Exception as e:
        log("  flows: cannot read head (%s)" % str(e)[:60])
        return
    blk = st.get("lastBlock") or (head - int(160 * 86400 / 2))
    scanned = 0
    while blk < head and not out_of_time():
        to_blk = min(blk + step - 1, head)
        try:
            logs = rpc([{"jsonrpc": "2.0", "id": 1, "method": "eth_getLogs",
                         "params": [{"address": MOR_TOKEN,
                                     "fromBlock": hex(blk), "toBlock": hex(to_blk),
                                     "topics": [TRANSFER_TOPIC, None, list(pad.keys())]}]}])[0]["result"]
        except Exception:
            blk = to_blk + 1
            continue
        for lg in logs:
            a = pad.get(lg["topics"][2])
            if not a:
                continue
            frm = "0x" + lg["topics"][1][26:]
            v = int(lg["data"], 16) / 1e18
            key = "claimed" if frm == TREASURY else "outside"
            st[key][a] = round(st[key].get(a, 0.0) + v, 6)
        blk = to_blk + 1
        scanned += 1
    st["lastBlock"] = blk
    log("  flows: advanced %d chunks to block %d%s"
        % (scanned, blk, " (caught up)" if blk >= head else " (more next run)"))


def prune(st, log):
    cut = int(time.time()) - RETAIN_DAYS * 86400
    before = len(st["sessions"])
    st["sessions"] = {k: v for k, v in st["sessions"].items()
                      if (v.get("t") or 0) >= cut}
    if before != len(st["sessions"]):
        log("  pruned %d sessions older than %dd" % (before - len(st["sessions"]), RETAIN_DAYS))


def main():
    log = lambda m: print(m, flush=True)
    st = load_state()
    log("state: %d sessions, cursor %s" % (len(st["sessions"]), st.get("lastBlock")))

    try:
        w = W(rpc([call(SEL_ACTIVE_PROVIDERS + "%064x" % 0 + "%064x" % 500)])[0]["result"])
        off = int(w[0], 16) // 32
        n = int(w[off], 16)
        provs = ["0x" + w[off + 1 + i][24:] for i in range(n)]
    except Exception as e:
        log("cannot list providers: %s" % e)
        return 1
    log("active providers: %d" % len(provs))

    advance_sessions(st, provs, log)
    advance_flows(st, provs, log)
    prune(st, log)
    save_state(st)

    # ---- derive the published census from state ----
    B = batch_limit()
    cut = int(time.time()) - WINDOW_DAYS * 86400
    recent = [s for s in st["sessions"].values() if (s.get("t") or 0) >= cut]
    bids = sorted({s["b"] for s in recent})
    bid2model = {}
    for i in range(0, len(bids), B):
        if out_of_time():
            break
        ch = bids[i:i + B]
        try:
            res = {x["id"]: x for x in rpc(
                [call(SEL_GET_BID + b[2:], j) for j, b in enumerate(ch)])}
            for j, b in enumerate(ch):
                bid2model[b] = "0x" + W(res[j]["result"])[1]
        except Exception:
            pass

    per_model, per_prov = {}, {}
    for s in recent:
        m = bid2model.get(s["b"])
        p = per_prov.setdefault(s["p"], {"s": 0, "by": {}})
        p["s"] += 1
        if m:
            per_model[m] = per_model.get(m, 0) + 1
            p["by"][m] = p["by"].get(m, 0) + 1

    census = {
        "asOf": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "windowDays": WINDOW_DAYS,
        "sessionsInWindow": len(recent),
        "sessionsHeld": len(st["sessions"]),
        "flowsCursor": st.get("lastBlock"),
        "sessionsByModel": per_model,
        "providers": {a: {"sessions": v["s"], "byModel": v["by"],
                          "outside": st["outside"].get(a),
                          "claimed": st["claimed"].get(a)}
                      for a, v in per_prov.items()},
    }
    with open(OUT, "w") as f:
        json.dump(census, f, separators=(",", ":"))
    log("wrote %s: %d sessions in %dd window, %d models, %d providers"
        % (OUT, len(recent), WINDOW_DAYS, len(per_model), len(per_prov)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
