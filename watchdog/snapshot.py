#!/usr/bin/env python3
"""Append a point-in-time snapshot of this provider's position to snapshots.jsonl.

One JSON object per line, append-only. That format is deliberate: it survives
concurrent writers, never rewrites history, and a partially-written final line
can be discarded without losing the rest — which matters for a file that will be
appended to for months and read for trends.

Everything here is read from chain, so it runs anywhere with no credentials and
no dependency on the router being reachable.

Bids are read via getProviderActiveBids rather than the router's
/blockchain/providers/{id}/bids/active, which silently caps at 10 results.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

DIAMOND = "0x6aBE1d282f72B474E54527D93b979A4f64d3030a"
ME = os.environ.get("PROVIDER_ADDRESS", "").lower()
if not ME:
    raise SystemExit("PROVIDER_ADDRESS is not set. Export it, or add it to the EnvironmentFile named by the systemd unit.")
OUT = os.environ.get("SNAPSHOT_FILE", "snapshots.jsonl")
RPCS = [u.strip() for u in os.environ.get(
    "SNAPSHOT_RPC",
    "https://base-rpc.publicnode.com,https://mainnet.base.org,"
    "https://developer-access-mainnet.base.org,https://base.lava.build"
).split(",") if u.strip()]
_rr = [0]

SEL_ACTIVE_BIDS = "0xaf5b77ca"
SEL_GET_BID = "0x91704e1e"
SEL_GET_MODEL = "0x21e7c498"
SEL_PROV_SESS = "0x87bced7d"
SEL_GET_SESSION = "0x39b240bd"
SEL_GET_PROVIDER = "0x55f21eb7"


def rpc(batch):
    last = None
    for attempt in range(len(RPCS) * 4):
        url = RPCS[_rr[0] % len(RPCS)]
        _rr[0] += 1
        try:
            p = subprocess.run(
                ["curl", "-s", "--max-time", "40", "-X", "POST", url,
                 "-H", "content-type: application/json", "--data-binary", "@-"],
                input=json.dumps(batch).encode(), capture_output=True)
            r = json.loads(p.stdout)
            if not isinstance(r, list):
                raise ValueError(str(r)[:120])
            if any("result" not in x for x in r):
                raise ValueError("partial")
            return r
        except Exception as e:
            last = e
            time.sleep(min(0.4 * (attempt + 1), 4))
    raise RuntimeError("all RPCs failed: %r" % last)


def call(data, i=1):
    return {"jsonrpc": "2.0", "id": i, "method": "eth_call",
            "params": [{"to": DIAMOND, "data": data}, "latest"]}


def W(h):
    h = h[2:] if h.startswith("0x") else h
    return [h[i:i + 64] for i in range(0, len(h), 64)]


def dec_str(w, base):
    n = int(w[base], 16)
    if not n:
        return ""
    hx = "".join(w[base + 1:base + 1 + (n + 31) // 32])[:n * 2]
    return bytes.fromhex(hx).decode("utf-8", "replace")


def main():
    pad = ME[2:].rjust(64, "0")

    pw = W(rpc([call(SEL_GET_PROVIDER + pad)])[0]["result"])
    b = int(pw[0], 16) // 32
    stake = int(pw[b + 1], 16) / 1e18
    earned = int(pw[b + 4], 16) / 1e18

    w = W(rpc([call(SEL_ACTIVE_BIDS + pad + "%064x" % 0 + "%064x" % 200)])[0]["result"])
    o = int(w[0], 16) // 32
    n = int(w[o], 16)
    bids = {}
    for i in range(n):
        bid = "0x" + w[o + 1 + i]
        r = W(rpc([call(SEL_GET_BID + bid[2:])])[0]["result"])
        mid = "0x" + r[1]
        name = ""
        try:
            m = W(rpc([call(SEL_GET_MODEL + r[1])])[0]["result"])
            base = int(m[0], 16) // 32
            name = dec_str(m, base + int(m[base + 4], 16) // 32)
        except Exception:
            pass
        bids[bid] = {"model": mid, "name": name,
                     "morPerDay": round(int(r[2], 16) * 86400 / 1e18, 6)}

    ws = W(rpc([call(SEL_PROV_SESS + pad + "%064x" % 0 + "%064x" % 5000)])[0]["result"])
    p = int(ws[0], 16) // 32
    ns = int(ws[p], 16)
    ids = ["0x" + ws[p + 1 + i] for i in range(ns)]
    per, mor = {}, {}
    open_n = 0
    for i in range(0, len(ids), 3):
        ch = ids[i:i + 3]
        try:
            res = {x["id"]: x for x in rpc(
                [call(SEL_GET_SESSION + s[2:], j) for j, s in enumerate(ch)])}
        except Exception:
            continue
        for j, sid in enumerate(ch):
            try:
                r = W(res[j]["result"])
                mid = bids.get("0x" + r[2], {}).get("model", "replaced")
                per[mid] = per.get(mid, 0) + 1
                mor[mid] = round(mor.get(mid, 0.0) + int(r[6], 16) / 1e18, 9)
                if not int(r[9], 16):
                    open_n += 1
            except Exception:
                pass

    snap = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "provider": ME,
        "stake": round(stake, 6),
        "earned": round(earned, 6),
        "headroom": round(stake - earned, 6),
        "sessionsTotal": ns,
        "sessionsOpen": open_n,
        "activeBids": n,
        "models": sorted(
            [{"model": v["model"], "name": v["name"], "morPerDay": v["morPerDay"],
              "sessions": per.get(v["model"], 0), "mor": mor.get(v["model"], 0.0)}
             for v in bids.values()],
            key=lambda x: -x["sessions"]),
        "fromReplacedBids": {"sessions": per.get("replaced", 0),
                             "mor": mor.get("replaced", 0.0)},
    }
    with open(OUT, "a") as f:
        f.write(json.dumps(snap, separators=(",", ":")) + "\n")

    print("%s  sessions=%d(%d open)  earned=%.4f  headroom=%.1f  bids=%d  -> %s"
          % (snap["ts"], ns, open_n, earned, stake - earned, n, OUT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
