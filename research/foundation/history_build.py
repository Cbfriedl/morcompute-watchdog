#!/usr/bin/env python3
"""Build the all-time session history for the Morpheus provider market.

Reads every session ever opened against every registered provider on Base
mainnet and writes an indexed SQLite database (`history.db`) that
`sql.js-httpvfs` can query over HTTP range requests from GitHub Pages, plus a
tiny `history-daily.json` for the dashboard's first paint.

Why SQLite and not pre-baked aggregates: there are only 28 providers, 98 models
and a few dozen buyers in the whole dataset, so interning the strings collapses
~124 MB of verbose JSON to ~20 MB indexed. That is small enough to serve, and it
preserves arbitrary drill-down by provider / model / buyer / date range instead
of freezing one set of rollups.

Stages, each independently resumable:

  ids     getProviderSessions per provider, paged. The array is append-only and
          ordered oldest->newest, so its length IS the lifetime count. This is
          also the checksum target for everything downstream.
  sess    getSession for every id not already cached. The expensive stage:
          ~300k eth_calls.
  bids    getBid for each distinct bidId -> (modelId, pricePerSecond). Only a
          few thousand distinct bids, cached permanently.
  models  getModel for each distinct modelId -> name.
  db      build history.db + history-daily.json from the caches. Pure local
          work, no network.

Everything network-bound writes to `histcache/` (gitignored) as it goes, so an
interrupted run resumes rather than restarts.

Usage:
    python3 history_build.py              # run every stage in order
    python3 history_build.py sess db      # run only these stages
    python3 history_build.py --status     # show progress, touch nothing

Endpoints come from rpc_endpoints.endpoints() in the MORCompute project root.
Never printed — describe() gives hostnames only.
"""

import json
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, "/home/cbfriedl/Documents/Projects/MORCompute")
from rpc_endpoints import endpoints, describe, NoEndpointsError  # noqa: E402

DIAMOND = "0x6aBE1d282f72B474E54527D93b979A4f64d3030a"

# Several public Base endpoints 403 the default Python-urllib User-Agent before
# looking at the payload at all. Sending a normal one is the whole difference
# between "endpoint is down" and 70 calls/s.
UA = "Mozilla/5.0 (X11; Linux x86_64) morcompute-history/1.0"

# Free, keyless, and by a wide margin the fastest thing available for eth_call:
# measured 70 call/s at batch 100, against ~26/s for both keyed endpoints
# combined. It refuses eth_getLogs entirely, which does not matter here because
# this job is nothing but eth_call. The keyed endpoints stay in the rotation as
# the dependable backbone; this is opportunistic extra capacity and the pacer
# drops it automatically if it starts erroring.
EXTRA_RPCS = ["https://base-rpc.publicnode.com"]
EXTRA_WORKERS = 2

SEL_ACTIVE_PROVIDERS = "0xd5472642"
SEL_PROVIDER_SESSIONS = "0x87bced7d"
SEL_GET_SESSION = "0x39b240bd"
SEL_GET_BID = "0x91704e1e"
SEL_GET_MODEL = "0x21e7c498"

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CACHE = os.environ.get("HIST_CACHE", os.path.join(REPO, "histcache"))
WARM = os.environ.get("HIST_WARM", "/home/cbfriedl/Documents/Projects/MORCompute/warmstart")

IDS_FILE = os.path.join(CACHE, "ids.json")
SESS_FILE = os.path.join(CACHE, "sess.jsonl")
BIDS_FILE = os.path.join(CACHE, "bids.json")
MODELS_FILE = os.path.join(CACHE, "models.json")

DB_OUT = os.path.join(REPO, "history.db")
DAILY_OUT = os.path.join(REPO, "history-daily.json")

# getProviderSessions returns the whole slice in one response; the largest
# provider has >100k ids and the response exceeds what the node will encode.
ID_PAGE = 20000


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------

class Endpoint:
    """One RPC URL with its own adaptive pacer.

    Each provider's free tier has an independent rate limit, so endpoints are
    driven in parallel rather than round-robin: a slow one does not hold up a
    fast one. Measured on 2026-08-18: Alchemy sustains ~26 eth_call/s and
    accepts batches of 200; Dwellir sustains ~9/s and hard-rejects batch > 20.
    Rather than hard-coding those, each endpoint starts conservative and the
    pacer walks the delay down while responses are clean and up on throttling.
    """

    def __init__(self, url, batch, rate):
        self.url = url
        self.host = url.split("/")[2]
        self.batch = batch
        self.min_gap = batch / float(rate)   # seconds between requests at target rate
        self.gap = self.min_gap
        self.next_at = 0.0
        self.calls = 0
        self.throttles = 0
        self.errors = 0
        self.disabled_until = 0.0

    def wait(self):
        now = time.time()
        if now < self.next_at:
            time.sleep(self.next_at - now)

    def post(self, payload, timeout=90):
        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode(),
            headers={"content-type": "application/json", "user-agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())

    def throttled(self):
        self.throttles += 1
        self.gap = min(self.gap * 1.6 + 0.25, 30.0)
        self.next_at = time.time() + self.gap

    def clean(self, n):
        self.calls += n
        self.gap = max(self.min_gap, self.gap * 0.93)
        self.next_at = time.time() + self.gap

    def failed(self):
        self.errors += 1
        self.gap = min(self.gap * 1.6 + 0.5, 60.0)
        self.next_at = time.time() + self.gap
        if self.errors > 40 and self.errors > self.calls / 200.0:
            # this endpoint is doing more harm than good; sit it out a while
            self.disabled_until = time.time() + 300
            self.errors = 0


def make_endpoints():
    """One Endpoint per worker. Rates are the measured sustained ceilings."""
    eps = []
    for u in endpoints():
        host = u.split("/")[2]
        if "alchemy" in host:
            eps.append(Endpoint(u, batch=100, rate=24))
        elif "dwellir" in host:
            eps.append(Endpoint(u, batch=20, rate=9))
        else:
            eps.append(Endpoint(u, batch=25, rate=10))
    for u in EXTRA_RPCS:
        # two workers so a latency spike on one in-flight batch does not idle
        # the endpoint; the combined target rate is still the measured ceiling
        for _ in range(EXTRA_WORKERS):
            eps.append(Endpoint(u, batch=100, rate=70.0 / EXTRA_WORKERS))
    return eps


def call(data, i=1):
    return {"jsonrpc": "2.0", "id": i, "method": "eth_call",
            "params": [{"to": DIAMOND, "data": data}, "latest"]}


THROTTLE_MARKS = ("compute unit", "rate limit", "too many requests",
                  "429", "capacity", "exceeded")


def is_throttle(msg):
    m = str(msg).lower()
    return any(k in m for k in THROTTLE_MARKS)


def batch_call(ep, datas, timeout=90):
    """Send one batch; return {index: result_hex} for the entries that came back.

    Missing indexes are the caller's to requeue. Raises on a whole-request
    failure so the caller can decide whether to retry or switch endpoint.
    """
    payload = [call(d, i) for i, d in enumerate(datas)]
    ep.wait()
    try:
        r = ep.post(payload, timeout=timeout)
    except urllib.error.HTTPError as e:
        if e.code in (429, 503):
            ep.throttled()
            return {}
        ep.failed()
        raise
    except Exception:
        ep.failed()
        raise
    if not isinstance(r, list):
        msg = str(r)[:200]
        if is_throttle(msg):
            ep.throttled()
            return {}
        ep.failed()
        raise ValueError("non-list response: " + msg)
    out = {}
    throttled = False
    for x in r:
        if "result" in x:
            out[x["id"]] = x["result"]
        else:
            msg = x.get("error", {}).get("message", "")
            if is_throttle(msg):
                throttled = True
    if throttled and not out:
        ep.throttled()
    elif throttled:
        # partial: got some, backed off on the rest. Slow down but keep what we have.
        ep.calls += len(out)
        ep.gap = min(ep.gap * 1.4 + 0.1, 30.0)
        ep.next_at = time.time() + ep.gap
    else:
        ep.clean(len(out))
    return out


def W(h):
    h = h[2:] if h.startswith("0x") else h
    return [h[i:i + 64] for i in range(0, len(h), 64)]


# --------------------------------------------------------------------------
# parallel fetch driver
# --------------------------------------------------------------------------

def drive(items, encode, decode, sink, label, eps, report_every=5000):
    """Fetch `items` across all endpoints in parallel, calling sink(item, value).

    items  : list of opaque work items
    encode : item -> eth_call data hex
    decode : (item, result_hex) -> value, or raises to drop the item
    sink   : (item, value) -> None, called under a lock

    Items whose sub-response was dropped by the node are requeued; an item that
    fails to decode `MAX_ITEM_FAIL` times is reported and abandoned rather than
    spinning forever.
    """
    MAX_ITEM_FAIL = 6
    lock = threading.Lock()
    queue = list(reversed(items))
    fails = defaultdict(int)
    base_calls = {id(e): e.calls for e in eps}   # ep.calls is cumulative across stages
    dropped_items = []
    state = {"done": 0, "dropped": 0, "t0": time.time(), "last": 0}
    total = len(items)
    stop = threading.Event()

    def progress():
        d = state["done"]
        if d - state["last"] < report_every:
            return
        state["last"] = d
        el = time.time() - state["t0"]
        rate = d / el if el > 0 else 0
        eta = (total - d) / rate if rate > 0 else 0
        by_host = defaultdict(float)
        for e in eps:
            by_host[e.host.split(".")[0]] += e.calls - base_calls[id(e)]
        pace = " ".join("%s=%.1f/s" % (h, c / el) for h, c in sorted(by_host.items())
                        if el > 0)
        print("  %-6s %7d/%-7d %5.1f%%  %6.1f/s  eta %5.1f min  [%s]"
              % (label, d, total, 100.0 * d / total, rate, eta / 60.0, pace),
              flush=True)

    def worker(ep):
        while not stop.is_set():
            if time.time() < ep.disabled_until:
                time.sleep(5)
                continue
            with lock:
                if not queue:
                    return
                chunk = [queue.pop() for _ in range(min(ep.batch, len(queue)))]
            try:
                got = batch_call(ep, [encode(it) for it in chunk])
            except Exception as e:
                with lock:
                    queue.extend(chunk)
                if ep.errors % 20 == 1:
                    print("  %-6s %s error: %s" % (label, ep.host.split(".")[0],
                                                   str(e)[:90]), flush=True)
                time.sleep(1)
                continue
            retry = []
            for i, it in enumerate(chunk):
                if i not in got:
                    retry.append(it)
                    continue
                try:
                    val = decode(it, got[i])
                except Exception:
                    with lock:
                        fails[repr(it)] += 1
                        if fails[repr(it)] < MAX_ITEM_FAIL:
                            retry.append(it)
                        else:
                            state["dropped"] += 1
                            dropped_items.append(it)
                    continue
                with lock:
                    sink(it, val)
                    state["done"] += 1
            if retry:
                with lock:
                    queue.extend(retry)
            with lock:
                progress()

    threads = [threading.Thread(target=worker, args=(e,), daemon=True) for e in eps]
    for t in threads:
        t.start()
    try:
        for t in threads:
            while t.is_alive():
                t.join(timeout=1.0)
    except KeyboardInterrupt:
        stop.set()
        print("\n  interrupted — flushing cache", flush=True)
        raise
    el = time.time() - state["t0"]
    print("  %-6s done %d/%d  dropped %d  in %.1f min (%.1f/s)"
          % (label, state["done"], total, state["dropped"], el / 60.0,
             state["done"] / el if el else 0), flush=True)
    return dropped_items


# --------------------------------------------------------------------------
# stage: ids
# --------------------------------------------------------------------------

def stage_ids(eps):
    """Enumerate every provider's full session id list.

    getProviderSessions is append-only and chronological, so a re-run only has
    to walk past the count we already hold. The resulting per-provider length is
    authoritative and every later stage is checksummed against it.
    """
    print("[ids] enumerating provider session lists", flush=True)
    ep = eps[0]
    for attempt in range(8):
        try:
            got = batch_call(ep, [SEL_ACTIVE_PROVIDERS + "%064x" % 0 + "%064x" % 1000])
            if 0 in got:
                break
        except Exception as e:
            print("  provider list retry: %s" % str(e)[:80], flush=True)
        ep = eps[(eps.index(ep) + 1) % len(eps)]
        time.sleep(2)
    else:
        raise RuntimeError("could not read getActiveProviders")
    w = W(got[0])
    off = int(w[0], 16) // 32
    n = int(w[off], 16)
    provs = ["0x" + w[off + 1 + i][24:] for i in range(n)]
    print("  %d registered providers" % len(provs), flush=True)

    ids = {}
    if os.path.exists(IDS_FILE):
        ids = json.load(open(IDS_FILE))
        print("  resuming from cache: %d providers, %d ids"
              % (len(ids), sum(len(v) for v in ids.values())), flush=True)

    for a in provs:
        a = a.lower()
        have = ids.get(a, [])
        start = len(have)
        while True:
            page = None
            for attempt in range(10):
                e = eps[attempt % len(eps)]
                if time.time() < e.disabled_until:
                    continue
                try:
                    got = batch_call(e, [SEL_PROVIDER_SESSIONS
                                         + a[2:].rjust(64, "0")
                                         + "%064x" % start + "%064x" % ID_PAGE],
                                     timeout=180)
                    if 0 in got:
                        page = got[0]
                        break
                except Exception as ex:
                    if attempt >= 6:
                        print("  %s ids retry %d: %s" % (a[:10], attempt, str(ex)[:70]),
                              flush=True)
                time.sleep(1.5)
            if page is None:
                print("  !! %s could not read ids from %d — leaving partial (%d)"
                      % (a, start, len(have)), flush=True)
                break
            w = W(page)
            off = int(w[0], 16) // 32
            cnt = int(w[off], 16)
            have.extend("0x" + w[off + 1 + i] for i in range(cnt))
            start += cnt
            if cnt < ID_PAGE:
                break
        ids[a] = have
        if have:
            print("  %s %6d" % (a, len(have)), flush=True)
        json.dump(ids, open(IDS_FILE, "w"))

    tot = sum(len(v) for v in ids.values())
    print("[ids] %d providers, %d lifetime sessions" % (len(ids), tot), flush=True)

    warm = os.path.join(WARM, "sess_alltime.json")
    if os.path.exists(warm):
        prev = json.load(open(warm))
        pt = sum(v for v in prev.values() if v)
        print("  warmstart checksum target was %d; chain now reports %d (+%d since)"
              % (pt, tot, tot - pt), flush=True)
        for a, c in sorted(prev.items()):
            now = len(ids.get(a.lower(), []))
            if c and now < c:
                print("  !! %s SHRANK %d -> %d — id enumeration is incomplete"
                      % (a, c, now), flush=True)
    return ids


# --------------------------------------------------------------------------
# stage: sessions
# --------------------------------------------------------------------------

def parse_session(hexres):
    w = W(hexres)
    # tuple is dynamic (holds bytes closeoutReceipt) so word 0 is the offset;
    # struct words: id? no -> user, bidId, stake, receiptOff, closeoutType,
    #               providerWithdrawn, openedAt, endsAt, closedAt, ...
    base = int(w[0], 16) // 32
    return ("0x" + w[base][24:],            # user
            "0x" + w[base + 1],             # bidId
            int(w[base + 6], 16),           # openedAt
            int(w[base + 7], 16),           # endsAt
            int(w[base + 8], 16))           # closedAt


def load_sess_cache(ids):
    """Return {(pi, idx): row} already known, from jsonl cache + warm start."""
    provs = sorted(ids)
    pidx = {a: i for i, a in enumerate(provs)}
    have = {}

    if os.path.exists(SESS_FILE):
        bad = 0
        with open(SESS_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    have[(r[0], r[1])] = r[2:]
                except Exception:
                    bad += 1
        print("  cache: %d sessions from %s%s"
              % (len(have), os.path.basename(SESS_FILE),
                 " (%d unparseable lines skipped)" % bad if bad else ""), flush=True)

    warm = os.path.join(WARM, "sess_cache.json")
    if os.path.exists(warm):
        wc = json.load(open(warm))
        pos = {}
        for a in provs:
            for i, s in enumerate(ids[a]):
                pos[s] = (pidx[a], i)
        added = 0
        out = open(SESS_FILE, "a")
        for sid, r in wc.items():
            k = pos.get(sid)
            if k is None or k in have:
                continue
            row = [r["user"], r["bidId"], r["openedAt"], r["endsAt"], r["closedAt"]]
            have[k] = row
            out.write(json.dumps([k[0], k[1]] + row, separators=(",", ":")) + "\n")
            added += 1
        out.close()
        if added:
            print("  warm start contributed %d sessions" % added, flush=True)
    return provs, pidx, have


def stage_sess(eps, ids):
    print("[sess] fetching session detail", flush=True)
    provs, pidx, have = load_sess_cache(ids)

    todo = []
    for a in provs:
        pi = pidx[a]
        for i, sid in enumerate(ids[a]):
            if (pi, i) not in have:
                todo.append((pi, i, sid))
    total = sum(len(v) for v in ids.values())
    print("  have %d / %d, fetching %d" % (len(have), total, len(todo)), flush=True)
    if not todo:
        return

    out = open(SESS_FILE, "a")
    written = [0]

    def sink(it, val):
        pi, i, _ = it
        out.write(json.dumps([pi, i] + list(val), separators=(",", ":")) + "\n")
        written[0] += 1
        if written[0] % 2000 == 0:
            out.flush()
            os.fsync(out.fileno())

    try:
        drive(todo,
              encode=lambda it: SEL_GET_SESSION + it[2][2:],
              decode=lambda it, r: parse_session(r),
              sink=sink, label="sess", eps=eps)
    finally:
        out.flush()
        os.fsync(out.fileno())
        out.close()


# --------------------------------------------------------------------------
# stage: bids and models
# --------------------------------------------------------------------------

def stage_bids(eps, ids):
    print("[bids] resolving bidId -> (modelId, pricePerSecond)", flush=True)
    bids = {}
    if os.path.exists(BIDS_FILE):
        bids = json.load(open(BIDS_FILE))
    warm = os.path.join(WARM, "bid_to_model.json")
    if os.path.exists(warm):
        for b, r in json.load(open(warm)).items():
            if b not in bids:
                bids[b] = [r["modelId"], str(r["pps"])]
    print("  cached bids: %d" % len(bids), flush=True)

    want = set()
    with open(SESS_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                want.add(json.loads(line)[3])
            except Exception:
                pass
    dead_file = BIDS_FILE.replace(".json", "_unresolvable.json")
    dead = set(json.load(open(dead_file))) if os.path.exists(dead_file) else set()
    todo = sorted(want - set(bids) - dead)
    print("  distinct bids in sessions: %d, missing %d%s"
          % (len(want), len(todo),
             ", %d known unresolvable" % len(dead) if dead else ""), flush=True)
    if todo:
        def dec(_it, r):
            w = W(r)
            return ["0x" + w[1], str(int(w[2], 16))]

        failed = drive(todo, encode=lambda b: SEL_GET_BID + b[2:], decode=dec,
                       sink=lambda b, v: bids.__setitem__(b, v), label="bids",
                       eps=eps, report_every=500)
        if failed:
            # a bid the node will not decode is a permanent hole, not a retry:
            # record it so the next run does not burn calls on it again, and so
            # the coverage figure can account for the sessions it strands.
            dead |= set(failed)
            json.dump(sorted(dead), open(dead_file, "w"))
            print("  !! %d bids could not be read; their sessions cannot be priced"
                  % len(failed), flush=True)
    json.dump(bids, open(BIDS_FILE, "w"))
    return bids


def stage_models(eps, bids):
    print("[models] resolving modelId -> name", flush=True)
    models = {}
    if os.path.exists(MODELS_FILE):
        models = json.load(open(MODELS_FILE))
    want = {v[0] for v in bids.values()}
    todo = sorted(want - set(models))
    print("  distinct models: %d, missing %d" % (len(want), len(todo)), flush=True)

    def dec(_it, r):
        w = W(r)
        base = int(w[0], 16) // 32
        noff = base + int(w[base + 4], 16) // 32
        ln = int(w[noff], 16)
        raw = "".join(w[noff + 1:noff + 1 + (ln + 31) // 32])
        try:
            return bytes.fromhex(raw)[:ln].decode("utf8", "replace")
        except Exception:
            return ""

    if todo:
        drive(todo, encode=lambda m: SEL_GET_MODEL + m[2:], decode=dec,
              sink=lambda m, v: models.__setitem__(m, v), label="model", eps=eps,
              report_every=50)
    json.dump(models, open(MODELS_FILE, "w"))
    return models


# --------------------------------------------------------------------------
# stage: db
# --------------------------------------------------------------------------

def stage_db(ids):
    print("[db] building history.db", flush=True)
    provs = sorted(ids)
    pidx = {a: i for i, a in enumerate(provs)}
    bids = json.load(open(BIDS_FILE))
    models = json.load(open(MODELS_FILE)) if os.path.exists(MODELS_FILE) else {}

    as_of = int(time.time())

    rows = []
    per_prov_detail = defaultdict(int)
    seen = set()
    skipped_open = 0
    skipped_nobid = 0
    unresolved_bids = set()
    wall = 0        # sum of closedAt-openedAt, kept only to report the gap
    billable = 0    # sum of the term-capped duration actually billed
    n_lines = 0
    with open(SESS_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                pi, idx, user, bid, o, e, c = json.loads(line)
            except Exception:
                continue
            n_lines += 1
            if (pi, idx) in seen:
                continue
            seen.add((pi, idx))
            per_prov_detail[pi] += 1
            # Billable duration is capped at the session's own term.
            #
            # closedAt is when someone got round to calling close on chain, not
            # when the session stopped being paid for: 53% of sessions are
            # closed after endsAt, usually by ~23 seconds but with a tail
            # running to 8.8 days. Billing that wall-clock gap overstates total
            # MOR earned by 4.8x, and it is a handful of stale sessions doing
            # nearly all of the damage. Capping at endsAt reproduces the
            # providers' own recorded withdrawals to within 0.2% in aggregate
            # (median ratio 1.0000, p99 1.00) — see docs/HISTORY_DB.md.
            #
            # Still running -> no earnings yet, so leave it out entirely rather
            # than counting it as a zero-value closed session.
            if c:
                end = min(c, e) if e else c
            elif e and e <= as_of:
                end = e
            else:
                skipped_open += 1
                continue
            dur = end - o
            wall += (c - o) if c else 0
            if dur <= 0:
                skipped_open += 1
                continue
            billable += dur
            b = bids.get(bid)
            if not b:
                skipped_nobid += 1
                unresolved_bids.add(bid)
                continue
            mid, pps = b[0], int(b[1])
            mor = (pps * dur) // 10**9     # wei -> MOR*1e9, integer
            rows.append((pi, mid, user, o, dur, mor))

    print("  %d cache lines, %d unique sessions, %d priced rows"
          % (n_lines, len(seen), len(rows)), flush=True)
    print("  skipped: %d still open / zero-length, %d sessions on %d unresolved bids"
          % (skipped_open, skipped_nobid, len(unresolved_bids)), flush=True)
    if billable:
        print("  duration capped at session term: billing raw closedAt-openedAt "
              "instead would inflate MOR %.2fx" % (wall / billable), flush=True)
    if unresolved_bids:
        print("    (re-run the 'bids' stage; the sess stage probably advanced "
              "past the last bid resolution)", flush=True)

    mids = sorted({r[1] for r in rows})
    midx = {m: i for i, m in enumerate(mids)}
    users = sorted({r[2] for r in rows})
    uidx = {u: i for i, u in enumerate(users)}

    if os.path.exists(DB_OUT):
        os.remove(DB_OUT)
    db = sqlite3.connect(DB_OUT)
    db.executescript("""
        PRAGMA page_size = 1024;
        PRAGMA journal_mode = OFF;
        CREATE TABLE provider (id INTEGER PRIMARY KEY, addr TEXT);
        CREATE TABLE model    (id INTEGER PRIMARY KEY, mid  TEXT, name TEXT);
        CREATE TABLE buyer    (id INTEGER PRIMARY KEY, addr TEXT);
        CREATE TABLE session (
          p    INTEGER, m INTEGER, u INTEGER,
          t    INTEGER, dur INTEGER, mor INTEGER
        );
        CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
    """)
    db.executemany("INSERT INTO provider VALUES (?,?)",
                   [(i, a) for a, i in pidx.items()])
    db.executemany("INSERT INTO model VALUES (?,?,?)",
                   [(i, m, models.get(m, "")) for m, i in midx.items()])
    db.executemany("INSERT INTO buyer VALUES (?,?)",
                   [(i, u) for u, i in uidx.items()])
    db.executemany("INSERT INTO session VALUES (?,?,?,?,?,?)",
                   ((p, midx[m], uidx[u], t, d, mor) for p, m, u, t, d, mor in rows))

    # ---- coverage, computed against the authoritative id-array lengths ----
    total_chain = sum(len(v) for v in ids.values())
    total_detail = sum(per_prov_detail.values())
    gaps = []
    for a in provs:
        want = len(ids[a])
        got = per_prov_detail.get(pidx[a], 0)
        if want != got:
            gaps.append((a, want, got))
    coverage = (total_detail / total_chain) if total_chain else 0.0

    days = sorted({datetime.fromtimestamp(r[3], timezone.utc).strftime("%Y-%m-%d")
                   for r in rows})
    meta = {
        "asOf": datetime.fromtimestamp(as_of, timezone.utc)
                        .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "firstDay": days[0] if days else "",
        "lastDay": days[-1] if days else "",
        "sessionsTotal": str(total_chain),
        "sessionsDetailed": str(total_detail),
        "coverage": "%.6f" % coverage,
        "sessionsPriced": str(len(rows)),
        "sessionsOpenSkipped": str(skipped_open),
        "sessionsUnresolvedBid": str(skipped_nobid),
        "unresolvedBids": str(len(unresolved_bids)),
        "providers": str(len([a for a in provs if ids[a]])),
        "models": str(len(mids)),
        "buyers": str(len(users)),
        "morUnit": "MOR * 1e9",
        "morBasis": "gross earned = pricePerSecond * billable duration. Verified "
                    "against the chain's own providerWithdrawnAmount on a 2500-"
                    "session sample spanning the full history: where a withdrawal "
                    "exists the two agree to 0.011%. Roughly 44% of sessions "
                    "(26% of earned MOR) were never withdrawn, so this is what "
                    "providers earned, not what they received.",
        "durationBasis": "min(closedAt, endsAt) - openedAt; sessions still "
                         "running are excluded entirely",
        "wallClockOverstatement": "%.3f" % (wall / billable) if billable else "",
        "schema": "session(p,m,u,t,dur,mor); p->provider.id m->model.id u->buyer.id; "
                  "t=openedAt unix s; dur=seconds; mor=earned MOR*1e9",
        "incompleteProviders": json.dumps(
            [{"addr": a, "chain": w, "detailed": g} for a, w, g in gaps]),
    }
    db.executemany("INSERT INTO meta VALUES (?,?)", sorted(meta.items()))
    db.executescript("""
        CREATE INDEX i_t   ON session(t);
        CREATE INDEX i_mt  ON session(m, t);
        CREATE INDEX i_pmt ON session(p, m, t);
    """)
    db.commit()
    db.execute("VACUUM")
    db.commit()
    db.close()

    # ---- daily rollup for first paint ----
    agg = defaultdict(lambda: [0, 0, set()])
    for p, m, u, t, d, mor in rows:
        k = datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")
        a = agg[k]
        a[0] += 1
        a[1] += mor
        a[2].add(p)
    daily = [[k, v[0], round(v[1] / 1e9, 4), len(v[2])] for k, v in sorted(agg.items())]
    json.dump({"days": daily,
               "asOf": meta["asOf"],
               "coverage": float(meta["coverage"]),
               "sessionsTotal": total_chain,
               "sessionsDetailed": total_detail},
              open(DAILY_OUT, "w"), separators=(",", ":"))

    sz = os.path.getsize(DB_OUT) / 1e6
    print("  history.db  %.1f MB   %d sessions priced, %d days, %d models, %d buyers"
          % (sz, len(rows), len(daily), len(mids), len(users)), flush=True)
    print("  history-daily.json  %.1f KB" % (os.path.getsize(DAILY_OUT) / 1e3),
          flush=True)
    print("  COVERAGE %.4f%%  (%d detailed of %d on chain)"
          % (coverage * 100, total_detail, total_chain), flush=True)
    if gaps:
        print("  INCOMPLETE PROVIDERS:", flush=True)
        for a, w, g in sorted(gaps, key=lambda x: -(x[1] - x[2])):
            print("    %s  chain %7d  detailed %7d  missing %7d"
                  % (a, w, g, w - g), flush=True)
    else:
        print("  every provider matches its getProviderSessions length exactly",
              flush=True)
    return coverage, gaps


# --------------------------------------------------------------------------

def status():
    if not os.path.exists(IDS_FILE):
        print("no ids cache yet")
        return
    ids = json.load(open(IDS_FILE))
    tot = sum(len(v) for v in ids.values())
    have = 0
    if os.path.exists(SESS_FILE):
        with open(SESS_FILE) as f:
            have = sum(1 for _ in f)
    print("providers %d  chain sessions %d  cache lines %d  (%.2f%%)"
          % (len(ids), tot, have, 100.0 * have / tot if tot else 0))


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--status" in sys.argv:
        status()
        return
    stages = args or ["ids", "sess", "bids", "models", "db"]
    os.makedirs(CACHE, exist_ok=True)

    try:
        eps = make_endpoints()
    except NoEndpointsError as e:
        print("FATAL: %s" % e)
        sys.exit(2)
    print("endpoints: %s" % describe(), flush=True)

    ids = None
    if "ids" in stages:
        ids = stage_ids(eps)
    if ids is None:
        if not os.path.exists(IDS_FILE):
            print("FATAL: no ids cache — run the 'ids' stage first")
            sys.exit(2)
        ids = json.load(open(IDS_FILE))

    if "sess" in stages:
        stage_sess(eps, ids)
    bids = None
    if "bids" in stages:
        bids = stage_bids(eps, ids)
    if "models" in stages:
        if bids is None:
            bids = json.load(open(BIDS_FILE))
        stage_models(eps, bids)
    if "db" in stages:
        stage_db(ids)


if __name__ == "__main__":
    main()
