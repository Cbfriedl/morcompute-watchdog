#!/usr/bin/env python3
"""Serve the private dashboard on localhost + tailnet, and proxy chain reads.

Bound to loopback/tailnet deliberately. The box has no firewall and no TLS, so
opening a port to the internet would expose OpenRouter spend — the one number
that is not already public on chain — over plaintext HTTP.

  ssh -N -L 8090:127.0.0.1:8090 -i ~/.ssh/morpheus_ed25519 root@2.28.8.173
  then open http://127.0.0.1:8090/

POST /rpc proxies JSON-RPC to the Base pool. The page used to call the public
RPCs directly from the browser: ~1,530 eth_calls per load (one per session, and
there are now 1,523 of them) every 60s. That blew past every public rate limit,
and each failure was retried 8x, so throttling fed itself. Proxying here lets
one cache serve every tab and reload, and keeps the browser on one same-origin
host so an ad-blocker or shield cannot break the page with "Failed to fetch".
"""
import http.server, functools, os, subprocess, json, time, threading, urllib.request
import urllib.parse
import concurrent.futures as cf

ROOT = os.environ.get("PRIVATE_ROOT", "/root/morpheus/private")
PORT = int(os.environ.get("PRIVATE_PORT", "8090"))
BIND = os.environ.get("PRIVATE_BIND", "")

# base.lava.build was dropped 2026-08-29: it returns 410 "endpoint has been
# discontinued" permanently, so it was silently wasting 1 in 4 round-robin slots.
RPCS = ["https://base-rpc.publicnode.com",
        "https://mainnet.base.org",
        "https://base.drpc.org"]

CACHE_FILE = os.environ.get("RPC_CACHE", "/root/morpheus/rpc-cache.json")
SEL_SESS  = "0x39b240bd"   # getSession(bytes32)
SEL_BID   = "0x91704e1e"   # bid struct — immutable; a reprice mints a new id
SEL_MODEL = "0x21e7c498"   # model metadata

_lock = threading.Lock()
_perm = {}          # immutable results, persisted to disk
_soft = {}          # everything else, 30s TTL, collapses concurrent tabs
_dirty = False
_rr = 0

try:
    with open(CACHE_FILE) as fh:
        _perm = json.load(fh)
except Exception:
    _perm = {}


def _words(res):
    h = res[2:] if res.startswith("0x") else res
    return [h[i * 64:(i + 1) * 64] for i in range(len(h) // 64)]


def _immutable(method, params, result):
    """True only for results that can never change again.

    Sessions are cached ONLY once closed. Caching a running session is exactly
    the bug that froze `mor` at 0 for 59 sessions on 2026-08-27 — while a
    session is open its payout is still accruing.
    """
    if method != "eth_call" or not result or result == "0x":
        return False
    data = (params[0].get("data") or "").lower()
    if data.startswith(SEL_BID) or data.startswith(SEL_MODEL):
        return True
    if data.startswith(SEL_SESS):
        w = _words(result)
        return len(w) > 9 and int(w[9], 16) != 0     # closedAt set => finished
    return False


def _upstream(payload):
    global _rr
    last = None
    body = json.dumps(payload).encode()
    for _ in range(len(RPCS)):
        url = RPCS[_rr % len(RPCS)]
        _rr += 1
        try:
            # urllib's default User-Agent is 403'd by every Base RPC. Set one.
            req = urllib.request.Request(url, data=body, headers={
                "content-type": "application/json",
                "User-Agent": "Mozilla/5.0 (morcompute private dashboard)"})
            with urllib.request.urlopen(req, timeout=20) as r:
                j = json.loads(r.read().decode())
            if "error" in j:
                last = j["error"].get("message", "rpc error")
                continue
            return j.get("result"), None
        except Exception as e:
            last = str(e)
    return None, (last or "all RPCs failed")


def handle_rpc(payload):
    global _dirty
    method = payload.get("method")
    params = payload.get("params") or []
    key = method + "|" + json.dumps(params, sort_keys=True)

    with _lock:
        if key in _perm:
            return {"jsonrpc": "2.0", "id": payload.get("id"), "result": _perm[key]}
        hit = _soft.get(key)
        if hit and hit[0] > time.time():
            return {"jsonrpc": "2.0", "id": payload.get("id"), "result": hit[1]}

    result, err = _upstream(payload)
    if err:
        return {"jsonrpc": "2.0", "id": payload.get("id"),
                "error": {"code": -32000, "message": err}}

    with _lock:
        if _immutable(method, params, result):
            _perm[key] = result
            _dirty = True
        else:
            _soft[key] = (time.time() + 30, result)
    return {"jsonrpc": "2.0", "id": payload.get("id"), "result": result}


SEL_PROV_SESS = "0x87bced7d"   # getProviderSessions(address,uint256,uint256)
SEL_PROV_BIDS = "0xaf5b77ca"   # getProviderActiveBids(address,uint256,uint256)
DIAMOND = "0x6aBE1d282f72B474E54527D93b979A4f64d3030a"
PROVIDER = os.environ.get("PROVIDER_ADDRESS",
                          "0x2f144F3b192A2d2D2384de7007EE2cAd943C601b")

_chain_cache = (0.0, None)


def _ecall(data):
    r = handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                    "params": [{"to": DIAMOND, "data": data}, "latest"]})
    return r.get("result")


def _decode_str(w, base):
    n = int(w[base], 16)
    if not n:
        return ""
    hx = "".join(w[base + 1: base + 1 + (n + 31) // 32])[:n * 2]
    return bytes.fromhex(hx).decode("utf-8", "replace")


def chaindata():
    """Everything load() used to gather in ~1,530 browser round-trips.

    Assembled here, where the cache is, and returned as one response. Over the
    tailnet each round-trip costs ~340ms, so doing this in the browser meant
    ~22s of "loading..." even with every session already cached.
    """
    global _chain_cache
    ts, val = _chain_cache
    if val is not None and ts > time.time():
        return val

    pada = PROVIDER[2:].lower().rjust(64, "0")
    off0, cnt = "0" * 64, format(5000, "064x")

    sess = []
    raw = _ecall(SEL_PROV_SESS + pada + off0 + cnt)
    if raw:
        w = _words(raw)
        o = int(w[0], 16) // 32
        ids = ["0x" + w[o + 1 + i] for i in range(int(w[o], 16))]

        def _one(sid):
            r = _ecall("0x39b240bd" + sid[2:])
            if not r:
                return None
            rw = _words(r)
            return {"bid": "0x" + rw[2], "t": int(rw[7], 16),
                    "mor": int(rw[6], 16) / 1e18,
                    "open": int(rw[9], 16) == 0}

        # cache hits are dict lookups, but a miss is a real upstream call; doing
        # ~1,500 of those one at a time on a cold cache took minutes
        with cf.ThreadPoolExecutor(max_workers=4) as ex:
            sess.extend(x for x in ex.map(_one, ids) if x)

    bid_model, bid_price = {}, {}
    for bid in {x["bid"] for x in sess}:
        r = _ecall(SEL_BID + bid[2:])
        if not r:
            continue
        rw = _words(r)
        bid_model[bid] = "0x" + rw[1]
        bid_price[bid] = int(rw[2], 16) * 86400 / 1e18
    for x in sess:
        x["model"] = bid_model.get(x["bid"])
        x["price"] = bid_price.get(x["bid"])

    bid_ids = []
    raw = _ecall(SEL_PROV_BIDS + pada + off0 + format(200, "064x"))
    if raw:
        w = _words(raw)
        o = int(w[0], 16) // 32
        bid_ids = ["0x" + w[o + 1 + i] for i in range(int(w[o], 16))]

    bid_meta, model_name = {}, {}
    for bid in bid_ids:
        r = _ecall(SEL_BID + bid[2:])
        if not r:
            continue
        rw = _words(r)
        mid, name = "0x" + rw[1], "\u2014"
        m = _ecall(SEL_MODEL + rw[1])
        if m:
            mw = _words(m)
            b = int(mw[0], 16) // 32
            name = _decode_str(mw, b + int(mw[b + 4], 16) // 32) or "(unnamed)"
        bid_meta[bid] = {"model": mid, "name": name,
                         "day": int(rw[2], 16) * 86400 / 1e18,
                         "createdAt": int(rw[4], 16)}
        model_name[mid] = name

    # a model with sessions but no live bid still needs its name (Kimi K3)
    for mid in {x["model"] for x in sess if x["model"]} - set(model_name):
        m = _ecall(SEL_MODEL + mid[2:])
        nm = mid[:10] + "\u2026"
        if m:
            mw = _words(m)
            b = int(mw[0], 16) // 32
            nm = _decode_str(mw, b + int(mw[b + 4], 16) // 32) or "(unnamed)"
        model_name[mid] = nm

    val = {"sess": sess, "bidIds": bid_ids,
           "bidMeta": bid_meta, "modelName": model_name}
    _chain_cache = (time.time() + 30, val)
    return val


# ---- sessions open right now, for any provider ---------------------------
# The Compare tab needs this for the provider being compared against, not just
# for us, and some of them have 15,000+ sessions. Walking a whole history per
# provider is exactly the read that broke this dashboard at 1,548 sessions, so
# this walks only the TAIL: getProviderSessions returns ids in the order the
# sessions opened (verified ascending by openedAt), so every session still open
# is at the end of the list. 250 is far more than any provider has run
# concurrently, and closed ones in that tail are permanently cached after the
# first pass, so a repeat call costs about as many upstream reads as there are
# genuinely open sessions.
OPEN_TAIL = int(os.environ.get("OPEN_TAIL", "250"))
# getProviderSessions returns the FIRST `limit` ids, so a limit below the
# provider's real total silently hands back the oldest sessions and none of the
# open ones — a wrong answer that looks like "nothing running". Ask for more
# than anyone has (the largest provider on the network is at 19,652) and treat
# a full array as a truncation and an error, not as data.
SESS_MAX = int(os.environ.get("SESS_MAX", "60000"))
_open_cache = {}          # addr -> (expires, {model: n})


def open_sessions(addr):
    """{model id: how many sessions this provider has open right now}.

    Reads the provider's session id list in one call and looks at the TAIL
    only. Ids come back in the order the sessions opened, so everything still
    running is at the end; 250 is far more than any provider has run at once.
    Walking a whole history one getSession at a time is the read that broke this
    dashboard at 1,548 sessions and is not repeated here — and after the first
    pass the closed sessions in that tail are permanently cached, so a repeat
    call costs about as many upstream reads as there are genuinely open
    sessions.
    """
    a = addr.lower()
    hit = _open_cache.get(a)
    if hit and hit[0] > time.time():
        return hit[1]

    pada = a[2:].rjust(64, "0")
    raw = _ecall(SEL_PROV_SESS + pada + format(0, "064x") + format(SESS_MAX, "064x"))
    if not raw:
        raise RuntimeError("session list read failed")
    w = _words(raw)
    o = int(w[0], 16) // 32
    n = int(w[o], 16)
    if n >= SESS_MAX:
        raise RuntimeError("session list truncated at %d — raise SESS_MAX" % SESS_MAX)
    ids = ["0x" + w[o + 1 + i] for i in range(n)][-OPEN_TAIL:]

    def _bid_if_open(sid):
        r = _ecall(SEL_SESS + sid[2:])
        if not r:
            return None
        rw = _words(r)
        return None if int(rw[9], 16) else "0x" + rw[2]   # closedAt == 0 => open

    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        bids = [b for b in ex.map(_bid_if_open, ids) if b]

    out = {}
    for bid in bids:
        r = _ecall(SEL_BID + bid[2:])
        if not r:
            continue
        mid = "0x" + _words(r)[1]
        out[mid] = out.get(mid, 0) + 1
    _open_cache[a] = (time.time() + 30, out)
    return out


def open_sessions_for(addrs):
    res, errs = {}, {}
    for a in addrs:
        try:
            res[a.lower()] = open_sessions(a)
        except Exception as e:
            # A provider we cannot read is not a reason to fail the whole
            # response: the page draws the other side's markers and says so.
            res[a.lower()], errs[a.lower()] = {}, str(e)
    out = {"providers": res}
    if errs:
        out["errors"] = errs
    return out


def _flusher():
    global _dirty
    while True:
        time.sleep(20)
        with _lock:
            # _soft entries were only ever checked for expiry on read, so a
            # provider's 600KB session-id blob stayed resident forever once
            # fetched. Sweep the expired ones here rather than growing without
            # bound in a process that runs for weeks.
            now = time.time()
            for k in [k for k, v in _soft.items() if v[0] <= now]:
                _soft.pop(k, None)
            if not _dirty:
                continue
            snap, _dirty = dict(_perm), False
        try:
            # private-dash.service and private-dash-tailnet.service both run
            # this file and both flush here. Writing our own snapshot straight
            # out made each instance delete whatever the other had learned
            # (1,567 entries fell back to 1,358). Merge, then write the union,
            # and adopt it so both processes converge instead of fighting.
            disk = {}
            try:
                with open(CACHE_FILE) as fh:
                    disk = json.load(fh)
            except Exception:
                disk = {}
            disk.update(snap)
            tmp = CACHE_FILE + ".%d.tmp" % os.getpid()
            with open(tmp, "w") as fh:
                json.dump(disk, fh)
            os.replace(tmp, CACHE_FILE)
            with _lock:
                _perm.update(disk)
        except Exception:
            pass


def tailscale_ip():
    """The box's tailnet address, or None if Tailscale is not up yet."""
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True,
                             timeout=10).stdout.decode().strip().split()
        return out[0] if out else None
    except Exception:
        return None


class H(http.server.SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        if self.path.split("?")[0] != "/rpc":
            self.send_error(404)
            return
        try:
            n = int(self.headers.get("content-length") or 0)
            payload = json.loads(self.rfile.read(n).decode())
        except Exception:
            self.send_error(400)
            return
        out = ([handle_rpc(p) for p in payload] if isinstance(payload, list)
               else handle_rpc(payload))
        body = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path, _, qs = self.path.partition("?")
        if path == "/opensessions":
            q = urllib.parse.parse_qs(qs)
            addrs = [a for a in (q.get("p", [""])[0]).split(",")
                     if a.startswith("0x") and len(a) == 42][:4]
            try:
                body = json.dumps(open_sessions_for(addrs)).encode()
            except Exception as e:
                body = json.dumps({"providers": {}, "error": str(e)}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/chaindata":
            try:
                body = json.dumps(chaindata()).encode()
            except Exception as e:
                body = json.dumps({"error": str(e)}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        return super().do_GET()

    def send_head(self):
        # SimpleHTTPRequestHandler honours If-Modified-Since and answers 304
        # even with Cache-Control: no-store, so a long-open tab kept rendering
        # stale markup after a deploy. Drop the header before it is consulted.
        self.headers.replace_header("If-Modified-Since", "") \
            if "If-Modified-Since" in self.headers else None
        if "If-None-Match" in self.headers:
            del self.headers["If-None-Match"]
        return super().send_head()

    def end_headers(self):
        # never cache: these files are rewritten by cron and a stale margin
        # figure is worse than a slow one
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        super().end_headers()

    def log_message(self, *a):
        pass


class Dual(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    os.chdir(ROOT)
    threading.Thread(target=_flusher, daemon=True).start()
    host = BIND or tailscale_ip() or "127.0.0.1"
    srv = Dual((host, PORT), functools.partial(H, directory=ROOT))
    where = "%s:%d" % (host, PORT)
    print("private dashboard on %s%s  (%d cached chain reads)" % (
        where, "  (tailnet + loopback)" if host != "127.0.0.1" else "  (localhost only)",
        len(_perm)), flush=True)
    srv.serve_forever()
