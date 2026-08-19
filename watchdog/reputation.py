#!/usr/bin/env python3
"""Write the router's own bid scores for this provider's models to reputation.json.

Deliberately does NOT recompute the score. The formula lives in
proxy-router/internal/rating/ and can be replicated (score = quality / price,
quality a weighted sum of tps/ttft/duration/success/stake), but the weights come
from a config we do not control. A replica would silently drift the day Morpheus
retunes them, and a reputation figure that is quietly wrong is worse than none.

So this asks the router for the real numbers via /bids/rated, which is the same
ranking a buyer sees. It must run ON THE PROVIDER BOX — that endpoint is bound to
localhost behind Basic Auth and is not reachable from a browser or from Actions.

Output is small and committable; the dashboard reads it as a static file.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

API = os.environ.get("ROUTER_API", "http://127.0.0.1:8082")
COOKIE = os.environ.get("ROUTER_COOKIE_FILE", "/root/morpheus/morpheus-data/.cookie")
ME = os.environ.get("PROVIDER_ADDRESS",
                    "0x2f144f3b192a2d2d2384de7007ee2cad943c601b").lower()
OUT = os.environ.get("REPUTATION_FILE", "reputation.json")
DIAMOND = "0x6aBE1d282f72B474E54527D93b979A4f64d3030a"
RPCS = [u.strip() for u in os.environ.get(
    "REP_RPC", "https://base-rpc.publicnode.com,https://mainnet.base.org"
).split(",") if u.strip()]
_rr = [0]


def cookie():
    with open(COOKIE) as f:
        return f.read().strip()


def api(path):
    for _ in range(5):
        p = subprocess.run(["curl", "-s", "--max-time", "40", "-u", cookie(), API + path],
                           capture_output=True)
        try:
            d = json.loads(p.stdout)
            if "error" not in d:
                return d
        except Exception:
            pass
        time.sleep(3)
    return None


def eth_call(data):
    body = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
            "params": [{"to": DIAMOND, "data": data}, "latest"]}
    for a in range(len(RPCS) * 4):
        url = RPCS[_rr[0] % len(RPCS)]
        _rr[0] += 1
        try:
            p = subprocess.run(["curl", "-s", "--max-time", "30", "-X", "POST", url,
                                "-H", "content-type: application/json",
                                "--data-binary", json.dumps(body)], capture_output=True)
            h = json.loads(p.stdout)["result"][2:]
            return [h[i:i + 64] for i in range(0, len(h), 64)]
        except Exception:
            time.sleep(min(0.4 * (a + 1), 3))
    raise RuntimeError("rpc failed")


def dec_str(w, base):
    n = int(w[base], 16)
    if not n:
        return ""
    hx = "".join(w[base + 1:base + 1 + (n + 31) // 32])[:n * 2]
    return bytes.fromhex(hx).decode("utf-8", "replace")


def main():
    # my active bids -> the models I care about. From chain, NOT from
    # /blockchain/providers/{id}/bids/active, which silently caps at 10 results.
    w = eth_call("0xaf5b77ca" + ME[2:].rjust(64, "0") + "%064x" % 0 + "%064x" % 200)
    o = int(w[0], 16) // 32
    n = int(w[o], 16)
    models = {}
    for i in range(n):
        r = eth_call("0x91704e1e" + w[o + 1 + i])
        mid = "0x" + r[1]
        name = ""
        try:
            m = eth_call("0x21e7c498" + r[1])
            base = int(m[0], 16) // 32
            name = dec_str(m, base + int(m[base + 4], 16) // 32)
        except Exception:
            pass
        models[mid] = {"name": name or mid[:14],
                       "myPrice": round(int(r[2], 16) * 86400 / 1e18, 6)}

    out = []
    for mid, meta in models.items():
        d = api("/blockchain/models/%s/bids/rated" % mid)
        bids = (d or {}).get("bids") or []
        rows = [{"p": b["Bid"]["Provider"].lower(),
                 "price": round(int(b["Bid"]["PricePerSecond"]) * 86400 / 1e18, 6),
                 "score": round(b["Score"], 1)} for b in bids]
        mine = next((r for r in rows if r["p"] == ME), None)
        rank = next((i + 1 for i, r in enumerate(rows) if r["p"] == ME), None)
        others = [r for r in rows if r["p"] != ME]
        # If I lead, the number that matters is whoever is closest behind me.
        # If I do not, it is whoever is ahead at number one.
        rival = others[0] if others else None
        out.append({
            "model": mid, "name": meta["name"], "myPrice": meta["myPrice"],
            "rank": rank, "of": len(rows),
            "myScore": mine["score"] if mine else None,
            "rivalScore": rival["score"] if rival else None,
            "rivalPrice": rival["price"] if rival else None,
            "rival": rival["p"] if rival else None,
            "rivalIsAhead": bool(rank and rank > 1),
        })
        print("  %-26s rank %s/%-2s  me %-10s rival %s"
              % (meta["name"][:26], rank or "?", len(rows),
                 out[-1]["myScore"], out[-1]["rivalScore"]), flush=True)

    doc = {"asOf": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "provider": ME,
           "source": "router /blockchain/models/{id}/bids/rated — the real ranking a buyer sees",
           "models": sorted(out, key=lambda r: (r["rank"] or 99, -(r["myScore"] or 0)))}
    with open(OUT, "w") as f:
        json.dump(doc, f, indent=1)
    r1 = sum(1 for r in out if r["rank"] == 1)
    print("\nwrote %s: %d models, rank #1 on %d" % (OUT, len(out), r1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
