#!/usr/bin/env python3
"""Set explicit prices on named bids.

reprice.py is a STRATEGY tool: it walks a fraction of the way toward a computed
tie point and only touches models on its LARGE_PAYMENT list. It cannot express
"put this model at exactly this number", which is what a break-even reprice is.
This does only that, and nothing else.

Dry run by default; --go sends. Each post costs the 0.3 MOR bid fee and the old
bid is replaced, not refunded. Quality/reputation survives a repost, so the only
cost of a step is the fee.
"""
import json, os, subprocess, sys, time

API = os.environ.get("ROUTER_API", "http://127.0.0.1:8082")
COOKIE = os.environ.get("ROUTER_COOKIE_FILE", "/root/morpheus/morpheus-data/.cookie")
ME = os.environ.get("PROVIDER_ADDRESS", "0x2f144f3b192a2d2d2384de7007ee2cad943c601b").lower()
DIAMOND = "0x6aBE1d282f72B474E54527D93b979A4f64d3030a"
MOR_TOKEN = "0x7431aDa8a591C955a994a21710752EF9b882b8e3"
# The local balancer first (see rpc_balancer.py): it round-robins the endpoints
# that work and retries throttling instead of returning it. base.drpc.org is no
# longer listed directly — it 408s on any request with a real body.
RPCS = [u.strip() for u in os.environ.get("SETPRICE_RPCS",
    "http://127.0.0.1:8545,https://mainnet.base.org,https://base-rpc.publicnode.com"
).split(",") if u.strip()]
_rr = [0]

TARGETS = {"gpt-oss-120b": 0.75, "Gemma-4-31b": 0.43,
           "deepseek-v4-flash": 2.36, "MiniMax-M2.5": 2.00}


def targets_from_argv(argv):
    """`--set "Gemma-4-31b=0.75"` repeated, else the TARGETS default.

    Repricing one model used to mean copying this file and editing TARGETS,
    because running it unmodified reposts all four bids and burns 1.2 MOR in
    fees for no reason. Naming the model on the command line is the same
    intent without the copy — and the fee estimate then covers what is actually
    being sent, not the size of the dict.
    """
    out = {}
    it = iter(range(len(argv)))
    for i, a in enumerate(argv):
        if a == "--set" and i + 1 < len(argv):
            name, _, val = argv[i + 1].partition("=")
            if not _:
                raise SystemExit("--set needs Name=price, got %r" % argv[i + 1])
            out[name.strip()] = float(val)
    return out or dict(TARGETS)


def cookie():
    with open(COOKIE) as f: return f.read().strip()


def api(path, method="GET", body=None):
    cmd = ["curl", "-s", "--max-time", "60", "-u", cookie(), "-X", method, API + path]
    if body is not None:
        cmd += ["-H", "content-type: application/json", "-d", json.dumps(body)]
    for _ in range(4):
        p = subprocess.run(cmd, capture_output=True)
        try: return json.loads(p.stdout)
        except Exception: time.sleep(1)
    return None


def rpc(method, params):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    for a in range(len(RPCS) * 4):
        url = RPCS[_rr[0] % len(RPCS)]; _rr[0] += 1
        try:
            p = subprocess.run(["curl", "-s", "--max-time", "30", "-X", "POST", url,
                                "-H", "content-type: application/json",
                                "--data-binary", json.dumps(body)], capture_output=True)
            return json.loads(p.stdout)["result"]
        except Exception:
            time.sleep(min(0.4 * (a + 1), 3))
    raise RuntimeError("all RPCs failed")


def W(h):
    h = h[2:]; return [h[i:i + 64] for i in range(0, len(h), 64)]


def main():
    go = "--go" in sys.argv
    targets = targets_from_argv(sys.argv[1:])
    rep = json.load(open("/root/morpheus/private/reputation.json"))
    byname = {m["name"]: m for m in rep["models"]}

    eth = int(rpc("eth_getBalance", [ME, "latest"]), 16) / 1e18
    morbal = int(W(rpc("eth_call", [{"to": MOR_TOKEN,
              "data": "0x70a08231" + ME[2:].rjust(64, "0")}, "latest"]))[0], 16) / 1e18
    fee = 0.3 * len(targets)
    print("wallet: %.6f ETH gas, %.4f MOR   |   fees for %d reprices: %.1f MOR"
          % (eth, morbal, len(targets), fee))
    if morbal < fee:
        print("!! not enough MOR for the bid fees"); return 1
    if eth < 0.002:
        print("!! gas looks too low"); return 1

    plan = []
    print("\n%-22s %10s %10s %8s %8s %s"
          % ("model", "now", "target", "change", "rank", "note"))
    for name, target in targets.items():
        m = byname.get(name)
        if not m and name.startswith("0x") and len(name) == 66:
            # A model we do not bid yet is absent from reputation.json, which is
            # built from OUR active bids — so a new listing can only be named by
            # its model id. There is no current price and no rated row for us.
            m = {"model": name.lower(), "name": name, "myPrice": None,
                 "rank": None, "of": 0, "rows": []}
            print("%-22s  NEW LISTING — no existing bid on this model" % (name[:6] + "\u2026" + name[-4:]))
        if not m:
            print("%-22s  NOT IN REPUTATION FEED — skipped "
                  "(pass the 66-char model id to open a new listing)" % name)
            continue
        cur = m["myPrice"]
        rows = m.get("rows") or []
        mine = next((r for r in rows if r["p"] == ME), None)
        rivals = [r for r in rows if r["p"] != ME]
        best = max((r["score"] for r in rivals), default=None)
        tie = (mine["score"] * mine["price"] / best) if (mine and best) else None
        # rank we would hold at the new price, from the same rated book
        newscore = (mine["score"] * mine["price"] / target) if mine else None
        newrank = 1 + sum(1 for r in rivals if r["score"] > newscore) if newscore else None
        note = ("stays #1" if newrank == 1
                else "rank %d of %d" % (newrank, len(rows))) if newrank else "?"
        if tie: note += " (tie at %.4f)" % tie
        print("%-22s %10s %10.4f %8s %8s %s"
              % ((name[:6] + "\u2026" + name[-4:]) if name.startswith("0x") and len(name) == 66 else name[:22], ("%.4f" % cur) if cur is not None else "none",
                 target,
                 ("%+.1f%%" % (100 * (target / cur - 1))) if cur else "NEW",
                 "%s/%s" % (m["rank"], m["of"]), note))
        plan.append((name, m["model"], target))

    if not go:
        print("\nDRY RUN — nothing sent. %d bids, %.1f MOR in fees. Pass --go." % (len(plan), fee))
        return 0

    for name, mid, target in plan:
        pps = int(target * 1e18 / 86400)
        for _ in range(5):
            r = api("/blockchain/bids", "POST", {"modelID": mid, "pricePerSecond": pps})
            if r and "error" not in r:
                print("  SET %-22s -> %.4f MOR/day  (pricePerSecond %d)" % (name, target, pps))
                break
            time.sleep(2)
        else:
            print("  FAILED %-22s  last response: %s" % (name, json.dumps(r)[:160]))
        time.sleep(3)
    return 0


if __name__ == "__main__":
    sys.exit(main())
