#!/usr/bin/env python3
"""Gradual, selective repricing of provider bids.

Two rules, both deliberate:

  * ONLY the larger-payment models are touched. Revenue per session is
    bid_price * duration, so on a model paying under ~$0.03 a session a reprice
    can never earn back its own 0.3 MOR bid fee. Those bids stay at their entry
    price and simply supply volume and success history.

  * Price moves a FRACTION of the way to the tie point, not all of it. Jumping
    straight to "3% under the next rival" maximises revenue per session but
    leaves no margin, so any move by a competitor takes the slot. Stepping keeps
    a wide score buffer, which keeps the volume flowing.

Cold start matters here: successScore is (successCount/totalCount)^2, which is 0
until the first session closes and ~1 afterwards. So a bid is only worth
repricing once MIN_SESSIONS have actually completed against it.

Dry run by default. Pass --go to send transactions. Each reprice costs the
0.3 MOR bid fee and deleting a bid refunds nothing, so steps are not free.
"""

import json
import os
import subprocess
import sys
import time

API = os.environ.get("ROUTER_API", "http://127.0.0.1:8082")
COOKIE = os.environ.get("ROUTER_COOKIE_FILE", "/root/morpheus/morpheus-data/.cookie")
ME = os.environ.get("PROVIDER_ADDRESS", "0x2f144f3b192a2d2d2384de7007ee2cad943c601b").lower()

# fraction of the distance from current price to the tie price to move per step
STEP = float(os.environ.get("REPRICE_STEP", "0.40"))
# never price above this fraction of the exact tie point
CEILING = float(os.environ.get("REPRICE_CEILING", "0.90"))
# completed sessions required before a bid counts as matured
MIN_SESSIONS = int(os.environ.get("REPRICE_MIN_SESSIONS", "5"))
# don't bother if the step gains less than this fraction
MIN_GAIN = float(os.environ.get("REPRICE_MIN_GAIN", "0.15"))

# modelId -> label. Only these are ever touched; everything else supplies volume.
LARGE_PAYMENT = {
    "0xaca84fc6ae370a7e45c78dc9a72f5661e85fc27cb363c20c316a64d2dc90f8fe": "Claude Opus 4.7",
    "0xe8585e699a48aba75829ca8d0c3634cfa10a299bde0d0aa4558760f9144224b9": "glm-5.2",
    "0x50a50f20bee96c": "Claude Sonnet 5",
    "0x6f5ccab3bc046ec87073c6adcb6183bea09154f188786187c25d86de5ca620e4": "Claude Sonnet 4.6",
    "0x81274dfa4d5544": "Kimi K3",
    "0x9269824cb042b0": "grok-4.5",
}


def cookie():
    with open(COOKIE) as f:
        return f.read().strip()


def api(path, method="GET", body=None):
    cmd = ["curl", "-s", "--max-time", "60", "-u", cookie(), "-X", method, API + path]
    if body is not None:
        cmd += ["-H", "content-type: application/json", "-d", json.dumps(body)]
    for _ in range(4):
        p = subprocess.run(cmd, capture_output=True)
        try:
            return json.loads(p.stdout)
        except Exception:
            pass
    return None


DIAMOND = "0x6aBE1d282f72B474E54527D93b979A4f64d3030a"
# rotate: every free Base endpoint throttles under even light bursts, so a
# single URL fails intermittently. Same reason the router keeps a pool.
RPCS = [u.strip() for u in os.environ.get(
    "REPRICE_RPC",
    "https://mainnet.base.org,https://developer-access-mainnet.base.org,"
    "https://base.drpc.org,https://base.lava.build").split(",") if u.strip()]
_rr = [0]


def eth_call(data):
    body = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
            "params": [{"to": DIAMOND, "data": data}, "latest"]}
    last = None
    for attempt in range(len(RPCS) * 4):
        url = RPCS[_rr[0] % len(RPCS)]
        _rr[0] += 1
        try:
            p = subprocess.run(["curl", "-s", "--max-time", "30", "-X", "POST", url,
                                "-H", "content-type: application/json",
                                "--data-binary", json.dumps(body)], capture_output=True)
            h = json.loads(p.stdout)["result"][2:]
            return [h[i:i + 64] for i in range(0, len(h), 64)]
        except Exception as e:
            last = e
            time.sleep(min(0.5 * (attempt + 1), 4))
    raise RuntimeError("all RPCs failed: %r" % last)


def provider_model_sessions(mid):
    """(successCount, totalCount) for us on this model, from getProviderModelStats.

    The router API does not expose this, and it is the only honest maturity
    signal: successScore is (success/total)^2, so it is 0 until the first
    session closes. Repricing before that just pays a second bid fee later.
    """
    try:
        w = eth_call("0x1b26c116" + mid[2:] + ME[2:].rjust(64, "0"))
        return int(w[5], 16), int(w[6], 16)
    except Exception:
        return None, None


def is_large(mid):
    for k, v in LARGE_PAYMENT.items():
        if mid.lower().startswith(k.lower()[:18]):
            return v
    return None


def active_bids_onchain():
    """Read active bids straight from getProviderActiveBids.

    NOT from /blockchain/providers/{id}/bids/active — that endpoint silently
    caps at 10 results, so with more bids than that it drops some with no error
    and no indication. Chain is the only complete source.
    """
    w = eth_call("0xaf5b77ca" + ME[2:].rjust(64, "0") + "%064x" % 0 + "%064x" % 200)
    off = int(w[0], 16) // 32
    n = int(w[off], 16)
    out = []
    for i in range(n):
        rw = eth_call("0x91704e1e" + w[off + 1 + i])
        out.append({"ModelAgentId": "0x" + rw[1], "PricePerSecond": str(int(rw[2], 16))})
    return out


def main():
    go = "--go" in sys.argv
    bids = active_bids_onchain()
    if not bids:
        print("no active bids (or the chain could not be read)")
        return 1
    print("read %d active bids from chain\n" % len(bids))

    print("%-22s %10s %10s %10s %8s  %s"
          % ("model", "now", "tie", "step to", "sess", "action"))
    planned = []
    for b in bids:
        mid = b["ModelAgentId"]
        label = is_large(mid)
        cur = int(b["PricePerSecond"]) * 86400 / 1e18
        if not label:
            continue

        rated = api("/blockchain/models/%s/bids/rated" % mid) or {}
        rows = rated.get("bids") or []
        me = next((x for x in rows if x["Bid"]["Provider"].lower() == ME), None)
        others = [x for x in rows if x["Bid"]["Provider"].lower() != ME]
        if not me or not others:
            print("%-22s %10.4f %10s %10s %8s  no rival / not rated" % (label, cur, "-", "-", "-"))
            continue

        # score = quality / price, so quality is recoverable from our own row and
        # the tie price is the price at which our score equals the best rival's
        q = me["Score"] * int(me["Bid"]["PricePerSecond"]) / 1e18
        tie = q / others[0]["Score"] * 86400

        # maturity check comes from the chain stats the scorer itself uses
        succ, total = provider_model_sessions(mid)
        sess = "%s/%s" % (succ, total) if total is not None else "?"

        target = min(cur + (tie - cur) * STEP, tie * CEILING)
        gain = (target / cur - 1) if cur else 0
        if total is not None and total < MIN_SESSIONS:
            act = "wait (needs %d closed sessions)" % MIN_SESSIONS
        elif target <= cur or gain < MIN_GAIN:
            act = "hold (gain %.0f%%)" % (100 * gain)
        else:
            act = "STEP +%.0f%%" % (100 * gain)
            planned.append((label, mid, target))
        print("%-22s %10.4f %10.4f %10.4f %8s  %s" % (label, cur, tie, target, sess, act))

    if not planned:
        print("\nnothing to do")
        return 0
    if not go:
        print("\ndry run — %d bid(s) would be repriced, %.1f MOR in fees. Pass --go to send."
              % (len(planned), 0.3 * len(planned)))
        return 0

    for label, mid, target in planned:
        pps = int(target * 1e18 / 86400)
        for _ in range(5):
            r = api("/blockchain/bids", "POST", {"modelID": mid, "pricePerSecond": pps})
            if r and "error" not in r:
                print("  repriced %-22s -> %.4f MOR/day" % (label, target))
                break
        else:
            print("  FAILED   %-22s" % label)
    return 0


if __name__ == "__main__":
    sys.exit(main())
