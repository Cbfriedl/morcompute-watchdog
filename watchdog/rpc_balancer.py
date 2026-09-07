#!/usr/bin/env python3
"""One RPC URL for the router, fanned out over the Base endpoints that work.

Why this exists
---------------
The router's own pool still contains base.lava.build, which Lava discontinued on
2026-08-28. It answers every request with `410 Gone: This endpoint has been
discontinued`, and /blockchain/models/{id}/bids/rated does not retry past it —
so rank and reputation have been blank on the dashboard ever since, because
reputation.py has nothing to read.

Pinning ETH_NODE_ADDRESS to a single public endpoint fixes the 410 and creates a
worse problem: it disables the router's own round-robin, and one endpoint cannot
absorb the router's startup burst without throttling. So the router points at
THIS process instead, which keeps the fan-out and, unlike the router, treats a
throttled or dead endpoint as something to route around rather than a result.

The rules it enforces, each of which is a bug that has actually happened here:

  * A 429 is never returned. It is a request to wait, not an answer, and the
    router treats it as a failed chain read. Retry elsewhere, then back off.
  * A dead endpoint is dropped from rotation and reported. base.lava.build
    silently ate one slot in four for who knows how long precisely because
    nothing looked.
  * eth_getLogs goes only where it works. publicnode wants a paid token, 1rpc
    caps the range at 50 blocks, drpc times out; mainnet.base.org serves it at
    up to 10,000 blocks. Round-robining it would fail two times in three.
  * Concurrency per endpoint is capped, so the burst queues here instead of
    turning into throttling upstream.
  * A deterministic execution result — a revert — is returned as-is. Retrying
    it on three more endpoints only makes the same answer arrive later.

Bound to loopback and the docker bridge gateway only. The box has no firewall,
so an unauthenticated RPC proxy on 0.0.0.0 would be an open relay.
"""
import http.server
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

# Probed 2026-09-06. drpc 408s on anything with a body, llamarpc is 521, meowrpc
# 429s immediately; none of them is in here. Order is preference, not priority —
# they are used round-robin.
GENERAL = [u.strip() for u in os.environ.get("BAL_RPCS",
    "https://mainnet.base.org,https://base-rpc.publicnode.com,https://1rpc.io/base"
).split(",") if u.strip()]
# eth_getLogs works nowhere else. See the module docstring.
LOGS = [u.strip() for u in os.environ.get("BAL_LOG_RPCS",
    "https://mainnet.base.org").split(",") if u.strip()]
LOG_METHODS = {"eth_getLogs"}

PORT = int(os.environ.get("BAL_PORT", "8545"))
# 127.0.0.1 for anything on the box, 172.18.0.1 (the morpheus_default bridge
# gateway) so the router container can reach it. Never 0.0.0.0.
BINDS = [b.strip() for b in os.environ.get("BAL_BIND", "127.0.0.1,172.18.0.1").split(",") if b.strip()]

PER_HOST = int(os.environ.get("BAL_CONCURRENCY", "8"))
TIMEOUT = float(os.environ.get("BAL_TIMEOUT", "25"))
COOLDOWN = float(os.environ.get("BAL_COOLDOWN", "20"))     # after a failure
DEAD_AFTER = int(os.environ.get("BAL_DEAD_AFTER", "8"))    # consecutive fails
ROUNDS = int(os.environ.get("BAL_ROUNDS", "4"))            # passes over the pool

UA = "Mozilla/5.0 (morcompute rpc balancer)"

# A JSON-RPC error that is a fact about the transaction, not about the endpoint.
# Asking a different node gives the same answer, so return it immediately.
FINAL_MARKS = ("execution reverted", "insufficient funds", "nonce too low",
               "already known", "replacement transaction underpriced",
               "intrinsic gas too low", "gas required exceeds")


class Endpoint:
    def __init__(self, url):
        self.url = url
        self.sem = threading.Semaphore(PER_HOST)
        self.lock = threading.Lock()
        self.cool_until = 0.0
        self.fails = 0          # consecutive
        self.ok = 0
        self.err = 0
        self.dead_since = None
        self.last_error = ""

    def available(self):
        return time.time() >= self.cool_until

    def note_ok(self):
        with self.lock:
            self.ok += 1
            self.fails = 0
            self.cool_until = 0.0
            self.dead_since = None
            self.last_error = ""

    def note_fail(self, why):
        with self.lock:
            self.err += 1
            self.fails += 1
            self.last_error = str(why)[:200]
            # Back off further the longer it keeps failing, but keep probing:
            # an endpoint that 429s under load is fine again in seconds.
            self.cool_until = time.time() + min(COOLDOWN * self.fails, 300)
            if self.fails >= DEAD_AFTER and self.dead_since is None:
                self.dead_since = time.time()
                print("[balancer] %s marked dead after %d failures: %s"
                      % (self.url, self.fails, self.last_error), flush=True)

    def stat(self):
        return {"url": self.url, "ok": self.ok, "err": self.err,
                "consecutiveFails": self.fails,
                "cooldownFor": max(0, round(self.cool_until - time.time(), 1)),
                "dead": self.dead_since is not None,
                "lastError": self.last_error}


POOLS = {"general": [Endpoint(u) for u in GENERAL],
         "logs": [Endpoint(u) for u in LOGS]}
_rr = {"general": 0, "logs": 0}
_rr_lock = threading.Lock()


def _methods(payload):
    return [p.get("method") for p in payload] if isinstance(payload, list) \
        else [payload.get("method")]


def _pool_for(payload):
    return "logs" if any(m in LOG_METHODS for m in _methods(payload)) else "general"


def _post(ep, body):
    """One attempt. Returns (parsed, None) or (None, reason-to-retry)."""
    req = urllib.request.Request(ep.url, data=body, headers={
        "content-type": "application/json", "User-Agent": UA})
    try:
        with ep.sem:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                parsed = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        # 429 throttling, 403 no-token, 410 discontinued, 5xx — all "ask someone
        # else", none of them an answer.
        return None, "HTTP %s" % e.code
    except Exception as e:
        return None, str(e)[:120]

    # A JSON-RPC error may still be an endpoint problem: a range cap, an
    # unimplemented method, a rate limit expressed in the body rather than the
    # status line. Only a deterministic execution result is final.
    errs = [p.get("error") for p in (parsed if isinstance(parsed, list) else [parsed])
            if isinstance(p, dict) and p.get("error")]
    if errs:
        msg = " ".join(str(e.get("message", "")) for e in errs).lower()
        if not any(m in msg for m in FINAL_MARKS):
            return None, msg[:120] or "rpc error"
    return parsed, None


def forward(payload):
    pool_name = _pool_for(payload)
    pool = POOLS[pool_name]
    body = json.dumps(payload).encode()
    tried = []

    for rnd in range(ROUNDS):
        with _rr_lock:
            _rr[pool_name] += 1
            start = _rr[pool_name]
        # first pass: only endpoints not in cooldown. later passes: anything,
        # because "everyone is cooling down" must not become a 429 for the router.
        order = [pool[(start + i) % len(pool)] for i in range(len(pool))]
        cands = [e for e in order if e.available()] or (order if rnd else [])
        for ep in cands:
            parsed, why = _post(ep, body)
            if parsed is not None:
                ep.note_ok()
                return parsed, None
            ep.note_fail(why)
            tried.append("%s: %s" % (ep.url.split("//")[-1], why))
        if rnd + 1 < ROUNDS:
            time.sleep(min(0.4 * (2 ** rnd), 3.0))
    return None, tried


def _error_response(payload, tried):
    def one(p):
        return {"jsonrpc": "2.0", "id": (p or {}).get("id"),
                "error": {"code": -32603,
                          "message": "balancer: every endpoint failed — " + "; ".join(tried[-4:])}}
    return [one(p) for p in payload] if isinstance(payload, list) else one(payload)


def _health_probe():
    """Bring a cooled-down endpoint back on its own, and say so.

    Without this, an endpoint that failed during a burst stays penalised until
    something happens to retry it, and a permanently dead one is only ever
    noticed by whoever reads the log.
    """
    body = json.dumps({"jsonrpc": "2.0", "id": 1,
                       "method": "eth_blockNumber", "params": []}).encode()
    while True:
        time.sleep(30)
        for pool in POOLS.values():
            for ep in pool:
                if ep.available():
                    continue
                parsed, _why = _post(ep, body)
                if parsed is not None and not isinstance(parsed, list) and "result" in parsed:
                    was_dead = ep.dead_since is not None
                    ep.note_ok()
                    if was_dead:
                        print("[balancer] %s back in rotation" % ep.url, flush=True)


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0] not in ("/health", "/"):
            self.send_error(404)
            return
        self._send(200, {"pools": {k: [e.stat() for e in v] for k, v in POOLS.items()},
                         "logMethods": sorted(LOG_METHODS)})

    def do_POST(self):
        try:
            n = int(self.headers.get("content-length") or 0)
            payload = json.loads(self.rfile.read(n).decode())
        except Exception:
            self.send_error(400)
            return
        out, tried = forward(payload)
        # Always 200 with a JSON-RPC body. A transport-level failure code here
        # is what the router turns into "410 Gone" further up.
        self._send(200, out if out is not None else _error_response(payload, tried))

    def log_message(self, *a):
        pass


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    address_family = socket.AF_INET
    # socketserver defaults the listen backlog to 5. At 40 concurrent clients
    # the kernel resets the overflow, and 9 of 120 test requests came back
    # "connection reset by peer" — from THIS process, not from any endpoint.
    # The router's startup fan-out is exactly that shape, so a balancer that
    # drops connections under burst would have replaced one failure with
    # another.
    request_queue_size = 512


def main():
    threading.Thread(target=_health_probe, daemon=True).start()
    servers = []
    for b in BINDS:
        try:
            s = Server((b, PORT), H)
        except OSError as e:
            print("[balancer] cannot bind %s:%d — %s" % (b, PORT, e), flush=True)
            continue
        servers.append(s)
        threading.Thread(target=s.serve_forever, daemon=True).start()
        print("[balancer] listening on %s:%d" % (b, PORT), flush=True)
    if not servers:
        print("[balancer] no listeners, giving up", flush=True)
        return 1
    print("[balancer] general: %s" % ", ".join(GENERAL), flush=True)
    print("[balancer] logs:    %s" % ", ".join(LOGS), flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
