#!/usr/bin/env python3
"""
External dead-man's switch for the Morpheus provider.

Runs on GitHub Actions (or any host that is NOT the provider box). Its whole
purpose is to detect the failure the on-box monitor structurally cannot report:
the box being dead.

It deliberately holds NO credential belonging to the provider host:
  * it reads public chain state via a public RPC
  * it probes the provider's public port the way a customer would
  * the only secret is the ntfy topic, supplied via env (Actions secret)

Because Actions runners are ephemeral there is no persistent state, so this
alerts on every failing run rather than edge-triggering. For "the box is
dead", repeated alerts are the correct behaviour.

Exit code: 0 if all checks pass, 1 if any check failed (so the Actions run
also shows red in the UI, giving a second, independent signal).
"""

import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone

DIAMOND = "0x6aBE1d282f72B474E54527D93b979A4f64d3030a"
SEL_GET_PROVIDER = "0x55f21eb7"
SEL_IS_ACTIVE = "0x63ef175d"
WEI = 10 ** 18

PROVIDER = os.environ.get("PROVIDER_ADDRESS", "").strip()
ENDPOINT = os.environ.get("PROVIDER_ENDPOINT", "").strip()   # host:port
RPCS = [r.strip() for r in os.environ.get(
    "WATCHDOG_RPCS",
    "https://mainnet.base.org,https://base.drpc.org,https://base.publicnode.com"
).split(",") if r.strip()]
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
HEADROOM_WARN_PCT = float(os.environ.get("HEADROOM_WARN_PCT", "30"))
TCP_TIMEOUT = float(os.environ.get("TCP_TIMEOUT", "12"))

problems = []
notes = []


def notify(title, message, priority="urgent", tags="rotating_light"):
    if not NTFY_TOPIC:
        sys.stderr.write("NTFY_TOPIC unset; cannot notify\n")
        return
    subprocess.run(
        ["curl", "-s", "-S", "--max-time", "20",
         "-H", "Title: %s" % title,
         "-H", "Priority: %s" % priority,
         "-H", "Tags: %s" % tags,
         "-d", message,
         "%s/%s" % (NTFY_SERVER, NTFY_TOPIC)],
        capture_output=True)


def rpc(method, params):
    """Try each RPC in turn. Only a failure of ALL of them counts as an
    outage — otherwise one flaky public endpoint would page us at 3am."""
    last = None
    for url in RPCS:
        try:
            payload = json.dumps({"jsonrpc": "2.0", "id": 1,
                                  "method": method, "params": params})
            p = subprocess.run(
                ["curl", "-s", "--max-time", "25", "-X", "POST", url,
                 "-H", "content-type: application/json", "--data-binary", "@-"],
                input=payload.encode(), capture_output=True)
            r = json.loads(p.stdout)
            if "error" in r:
                raise RuntimeError(str(r["error"])[:150])
            return r["result"]
        except Exception as e:
            last = "%s: %s" % (url, str(e)[:120])
    raise RuntimeError("all RPCs failed; last=%s" % last)


def words(h):
    h = h[2:] if h.startswith("0x") else h
    return [h[i:i + 64] for i in range(0, len(h), 64)]


def check_tcp(hostport):
    """Probe the provider port exactly as a customer's client would."""
    if ":" not in hostport:
        return False, "malformed endpoint %r" % hostport
    host, _, port = hostport.rpartition(":")
    t0 = time.time()
    try:
        with socket.create_connection((host, int(port)), timeout=TCP_TIMEOUT):
            return True, "connected in %.0f ms" % ((time.time() - t0) * 1000)
    except Exception as e:
        return False, str(e)[:120]


def main():
    if not PROVIDER:
        sys.stderr.write("PROVIDER_ADDRESS unset\n")
        return 2

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    addr = PROVIDER.lower().replace("0x", "").rjust(64, "0")

    # ---- chain state --------------------------------------------------
    endpoint = ENDPOINT
    try:
        w = words(rpc("eth_call", [{"to": DIAMOND,
                                    "data": SEL_GET_PROVIDER + addr}, "latest"]))
        b = int(w[0], 16) // 32
        eb = b + int(w[b], 16) // 32
        n = int(w[eb], 16)
        onchain_ep = bytes.fromhex(
            "".join(w[eb + 1: eb + 1 + (n + 31) // 32])[: n * 2]
        ).decode("utf8", "replace")
        stake = int(w[b + 1], 16)
        earned = int(w[b + 4], 16)
        deleted = bool(int(w[b + 5], 16))
        active = bool(int(rpc("eth_call", [{"to": DIAMOND,
                                            "data": SEL_IS_ACTIVE + addr},
                                           "latest"]), 16))

        headroom = stake - earned
        pct = (100.0 * headroom / stake) if stake else 0.0
        notes.append("stake %.2f MOR | earned %.2f | headroom %.2f (%.1f%%) | "
                     "active=%s | endpoint=%s"
                     % (stake / WEI, earned / WEI, headroom / WEI, pct,
                        active, onchain_ep))

        # Prefer the on-chain endpoint over any hardcoded value: that is the
        # address customers will actually dial.
        if onchain_ep:
            endpoint = onchain_ep

        if deleted or not active:
            problems.append("PROVIDER NOT ACTIVE on-chain (isDeleted=%s active=%s). "
                            "If unintended, treat as key compromise."
                            % (deleted, active))
        if stake and pct < HEADROOM_WARN_PCT:
            problems.append("Headroom %.1f%% (%.2f/%.2f MOR) below %.0f%% — the "
                            "reward limiter pays ZERO SILENTLY at 0."
                            % (pct, headroom / WEI, stake / WEI, HEADROOM_WARN_PCT))
    except Exception as e:
        problems.append("Cannot read chain state from any RPC: %s" % str(e)[:200])

    # ---- external liveness -------------------------------------------
    # This is the check the on-box monitor cannot perform for itself.
    if endpoint:
        ok, detail = check_tcp(endpoint)
        notes.append("tcp %s: %s" % (endpoint, detail))
        if not ok:
            problems.append("Provider endpoint %s is UNREACHABLE from the public "
                            "internet (%s). Box, container, or network is down — "
                            "customers cannot open sessions." % (endpoint, detail))
    else:
        problems.append("No provider endpoint known (chain read failed and "
                        "PROVIDER_ENDPOINT unset) — cannot probe liveness.")

    # ---- report -------------------------------------------------------
    body = "%s\n\n%s" % (stamp, "\n".join(notes))
    print(body)
    if problems:
        msg = "\n\n".join(problems) + "\n\n---\n" + body
        print("\nPROBLEMS:\n" + "\n".join(problems), file=sys.stderr)
        notify("Morpheus watchdog: %d problem(s)" % len(problems), msg)
        return 1

    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
