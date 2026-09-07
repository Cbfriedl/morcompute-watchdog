#!/usr/bin/env python3
"""Point the router at the local RPC balancer, the moment no session is open.

A restart during an open session closes it, and a session that closes while the
provider is down pays 0 MOR — the standing rule is that a reprice and a restart
are separate decisions and a restart waits for a zero-session window. Idle gaps
are scarce (about 19 minutes in 24 hours, mostly around 04:45-05:30 UTC), so
this waits for one rather than trying to pick it by hand.

What it changes, once and only once:
  morpheus-data/.env   ETH_NODE_ADDRESS uncommented and set to the balancer
  morpheus-router      recreated so the new env is read

Why the balancer and not a public URL: pinning ETH_NODE_ADDRESS disables the
router's own multi-RPC round-robin, and one public endpoint cannot absorb the
router's fan-out without throttling. The balancer keeps the fan-out and, unlike
the router's built-in pool, routes around a dead endpoint instead of returning
its error — which is the whole reason rank and reputation went blank when Lava
discontinued base.lava.build.

It refuses to act if the balancer is not answering, so a failed restart cannot
leave the router with no RPC at all.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

BAL = os.environ.get("BAL_URL", "http://172.18.0.1:8545")
BAL_LOCAL = os.environ.get("BAL_LOCAL", "http://127.0.0.1:8545")
DASH = os.environ.get("DASH_URL", "http://127.0.0.1:8090")
ME = os.environ.get("PROVIDER_ADDRESS", "0x2f144F3b192A2d2D2384de7007EE2cAd943C601b")
ENV = os.environ.get("ROUTER_ENV", "/root/morpheus/morpheus-data/.env")
COMPOSE_DIR = os.environ.get("COMPOSE_DIR", "/root/morpheus")
POLL = int(os.environ.get("POLL", "15"))
DEADLINE_H = float(os.environ.get("DEADLINE_H", "24"))


def log(msg):
    print("%s %s" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), msg), flush=True)


def open_count():
    """Sessions open on our provider right now, or None if we could not tell.

    None is not zero. A failed read must never be allowed to look like an idle
    window — that is how a restart lands on top of a live session.
    """
    try:
        with urllib.request.urlopen(
                "%s/opensessions?p=%s&cb=%d" % (DASH, ME, time.time()), timeout=120) as r:
            d = json.loads(r.read().decode())
        if d.get("error") or d.get("errors"):
            return None
        per = d.get("providers", {}).get(ME.lower())
        if per is None:
            return None
        return sum(per.values())
    except Exception as e:
        log("open-session read failed: %s" % e)
        return None


def balancer_ok(url):
    try:
        body = json.dumps({"jsonrpc": "2.0", "id": 1,
                           "method": "eth_chainId", "params": []}).encode()
        req = urllib.request.Request(url, data=body,
                                     headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode()).get("result") == "0x2105"
    except Exception:
        return False


def set_env():
    """Uncomment ETH_NODE_ADDRESS and point it at the balancer. Idempotent."""
    with open(ENV) as f:
        lines = f.read().split("\n")
    out, done = [], False
    for ln in lines:
        s = ln.strip()
        if s.startswith("ETH_NODE_ADDRESS=") or s.startswith("#ETH_NODE_ADDRESS="):
            if not done:
                out.append("ETH_NODE_ADDRESS=%s" % BAL)
                done = True
            continue          # drop any further copies; the last one would win
        out.append(ln)
    if not done:
        out.append("ETH_NODE_ADDRESS=%s" % BAL)
    bak = ENV + ".bak-" + time.strftime("%m%d-%H%M", time.gmtime())
    subprocess.run(["cp", "-p", ENV, bak], check=True)
    tmp = ENV + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(out))
    os.chmod(tmp, 0o600)
    os.replace(tmp, ENV)
    log("ETH_NODE_ADDRESS set to %s (backup %s)" % (BAL, bak))


def recreate():
    r = subprocess.run(["docker", "compose", "up", "-d", "--force-recreate",
                        "morpheus-router"], cwd=COMPOSE_DIR,
                       capture_output=True, text=True)
    log("docker compose rc=%d %s" % (r.returncode, (r.stderr or r.stdout).strip()[-300:]))
    return r.returncode == 0


def rated_book_alive():
    """The actual thing this restart is for: does /bids/rated answer again."""
    try:
        cookie = open("/root/morpheus/morpheus-data/.cookie").read().strip()
        mid = "0x76bb9ac6a871608a97684284a8872e1b0c319904e5342d8baa5d9a44dbe68dbe"
        p = subprocess.run(["curl", "-s", "--max-time", "40", "-u", cookie,
                            "http://127.0.0.1:8082/blockchain/models/%s/bids/rated" % mid],
                           capture_output=True)
        d = json.loads(p.stdout)
        return (not d.get("error")), str(d)[:160]
    except Exception as e:
        return False, str(e)[:160]


def main():
    if not balancer_ok(BAL_LOCAL):
        log("REFUSING: balancer not answering on %s" % BAL_LOCAL)
        return 1
    log("balancer healthy; waiting for a zero-session window (poll %ds, deadline %gh)"
        % (POLL, DEADLINE_H))

    deadline = time.time() + DEADLINE_H * 3600
    idle_streak = 0
    last_report = 0
    while time.time() < deadline:
        n = open_count()
        if n is None:
            idle_streak = 0
        elif n == 0:
            idle_streak += 1
        else:
            idle_streak = 0
        if time.time() - last_report > 600:
            log("open=%s idle_streak=%d" % (n, idle_streak))
            last_report = time.time()
        # Two clean consecutive zeros, not one: a single read can land in the
        # gap between one session closing and the next opening, and the
        # dashboard's own answer is cached for 30s.
        if idle_streak >= 2:
            log("zero-session window (2 consecutive reads) — acting")
            if not balancer_ok(BAL_LOCAL):
                log("balancer went unhealthy; standing down")
                return 1
            set_env()
            if not recreate():
                log("recreate FAILED — check `docker compose logs morpheus-router`")
                return 1
            for _ in range(24):
                time.sleep(5)
                ok, detail = rated_book_alive()
                if ok:
                    log("rated book ANSWERS again: %s" % detail)
                    subprocess.run(["/root/morpheus/private-refresh.sh"],
                                   capture_output=True)
                    log("private-refresh run; reputation.json regenerated")
                    return 0
            log("router back up but rated book still failing: %s" % rated_book_alive()[1])
            return 1
        time.sleep(POLL)
    log("deadline reached with no idle window; nothing changed")
    return 2


if __name__ == "__main__":
    sys.exit(main())
