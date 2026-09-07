#!/usr/bin/env python3
"""Per-model margin, fitted rather than measured.

WHY THIS EXISTS AND WHAT IT IS NOT
----------------------------------
margin.py computes blended margin and documents why per-model margin is not
directly available: the OpenRouter /api/v1/key endpoint reports account-level
spend only, and /api/v1/activity answers 403 "Only management keys can fetch
activity for an account". So there is no per-model cost to read.

What IS available:
  * exact per-model REVENUE, from chain (price x billable seconds)
  * exact TOTAL spend every ~11 min, from morpheus-monitor journald output
  * exact per-model SESSION-HOURS, from chain

Morpheus prices wall-clock time, so session-hours is the right cost driver: a
regression on session *counts* fits far worse (R2 0.16 vs 0.47 over the same
hours). This script fits total hourly spend against per-model hourly
session-hours by non-negative least squares and reports each model's implied
cost per session-hour.

That is an ESTIMATE. It is reported with its R2 and with per-model session-hour
coverage so a thin fit can be disregarded, and models below MIN_HOURS are pooled
into a single "other" term rather than given a meaningless coefficient of their
own. The honest fix is one OpenRouter key per model, after which this file
should be deleted rather than maintained.
"""

import json
import os
import subprocess
import sys
import itertools
import random
import urllib.request
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

sys.path.insert(0, "/root/morpheus/census")
from rpc_endpoints import endpoints

DIAMOND = "0x6aBE1d282f72B474E54527D93b979A4f64d3030a"
ME = os.environ.get("PROVIDER_ADDRESS",
                    "0x2f144f3b192a2d2d2384de7007ee2cad943c601b").lower()
CACHE = os.environ.get("SESSION_CACHE", "/root/morpheus/sessions-cache.json")
OUT = os.environ.get("MODEL_MARGIN_FILE", "/root/morpheus/model-margin.json")
MIN_HOURS = 3.0          # below this a model gets no coefficient of its own
SOLO_EPS = 0.02          # session-hours below this do not make an hour "shared"
MIN_SOLO_HOURS = 2.0     # measured rate needs this much solo running time
MIN_SOLO_BUCKETS = 3     # ...spread over at least this many separate hours
BOOTSTRAP = 200          # resamples used to decide whether a rate is identified
_c = itertools.cycle(endpoints())


def call(data, tries=10):
    for a in range(tries):
        u = next(_c)
        try:
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                               "params": [{"to": DIAMOND, "data": data},
                                          "latest"]}).encode()
            r = urllib.request.Request(u, body, {"Content-Type": "application/json"})
            d = json.load(urllib.request.urlopen(r, timeout=30))
            if "result" in d:
                return d["result"]
        except Exception:
            time.sleep(0.2 * (a + 1))
    return None


def W(h):
    h = h[2:]
    return [h[i:i + 64] for i in range(0, len(h), 64)]


def load_sessions():
    """All sessions, incrementally. Only unknown ids cost an RPC round trip.

    Sessions and bids are immutable once closed, so the cache only ever grows.
    A partial RPC failure drops that session for this run and it is retried on
    the next one, rather than being recorded as missing.
    """
    cache = {"sess": {}, "bmap": {}, "names": {}}
    try:
        with open(CACHE) as f:
            cache = json.load(f)
    except Exception:
        pass

    pad = ME[2:].rjust(64, "0")
    raw = call("0x87bced7d" + pad + "%064x" % 0 + "%064x" % 5000)
    if not raw:
        raise SystemExit("could not read provider sessions")
    w = W(raw)
    o = int(w[0], 16) // 32
    ids = ["0x" + w[o + 1 + i] for i in range(int(w[o], 16))]
    # A session is cached the first time it is seen, which is usually while it
    # is still RUNNING - so "immutable once closed" does not save us: mor is 0
    # and closed is False at that moment, and nothing ever looked again. On
    # 2026-08-24 that left 59 sessions recorded open that had long since closed
    # and understated earned MOR by 1.1221 (7.3944 cached vs 8.5165 on chain).
    # It also silently deleted their session-hours from the fit, because the
    # duration below is derived from mor. Re-read anything still open.
    todo = [i for i in ids
            if i not in cache["sess"] or not cache["sess"][i].get("closed")]

    def gs(sid):
        r = call("0x39b240bd" + sid[2:])
        if not r:
            return None
        x = W(r)
        return (sid, {"bid": "0x" + x[2], "mor": int(x[6], 16) / 1e18,
                      "t": int(x[7], 16), "closed": bool(int(x[9], 16))})

    if todo:
        with ThreadPoolExecutor(8) as ex:
            for r in ex.map(gs, todo):
                if r:
                    cache["sess"][r[0]] = r[1]

    need = [b for b in {s["bid"] for s in cache["sess"].values()}
            if b not in cache["bmap"]]

    def bm(b):
        r = call("0x91704e1e" + b[2:])
        if not r:
            return None
        x = W(r)
        return b, ["0x" + x[1], int(x[2], 16) * 86400 / 1e18]

    if need:
        with ThreadPoolExecutor(8) as ex:
            for r in ex.map(bm, need):
                if r:
                    cache["bmap"][r[0]] = r[1]

    def mn(m):
        r = call("0x21e7c498" + m[2:])
        if not r:
            return m, m[:10]
        x = W(r)
        base = int(x[0], 16) // 32
        off = base + int(x[base + 4], 16) // 32
        ln = int(x[off], 16)
        return m, bytes.fromhex(x[off + 1][:ln * 2]).decode("utf8", "replace")

    miss = {v[0] for v in cache["bmap"].values()
            if v[0] and v[0] not in cache["names"]}
    if miss:
        with ThreadPoolExecutor(8) as ex:
            for m, s in ex.map(mn, miss):
                cache["names"][m] = s

    with open(CACHE, "w") as f:
        json.dump(cache, f)
    return cache


def active_bid_models():
    """Model ids we currently have a live bid on.

    The fit is trained on history, which legitimately includes models we have
    since stopped bidding -- Kimi K3 supplied roughly $19 of the $35 the current
    fit is trained on, and dropping it would throw away the single strongest cost
    signal we have. But a margin row for a bid that no longer exists reads as a
    live position, so every row is flagged and consumers filter on it.
    """
    pad = ME[2:].rjust(64, "0")
    raw = call("0xaf5b77ca" + pad + "%064x" % 0 + "%064x" % 200)
    if not raw:
        return None
    w = W(raw)
    o = int(w[0], 16) // 32
    out = set()
    for i in range(int(w[o], 16)):
        r = call("0x91704e1e" + w[o + 1 + i])
        if r:
            out.add("0x" + W(r)[1])
    return out


def spend_series():
    """Cumulative OpenRouter spend, from morpheus-monitor journald output.

    The monitor dumps metrics.json every ~11 min and nothing else records spend
    over time: margin.json is overwritten in place, so journald is the only
    history that exists. If the journal rotates this silently shortens, so the
    span actually used is reported in the output.
    """
    p = subprocess.run(["journalctl", "--no-pager", "-o", "short-iso"],
                       capture_output=True, text=True)
    out = []
    for ln in p.stdout.splitlines():
        if '"total_usage"' not in ln:
            continue
        try:
            ts = datetime.fromisoformat(ln.split()[0]).timestamp()
            v = float(ln.split('"total_usage":')[1].strip().rstrip(",").strip())
            out.append((ts, v))
        except Exception:
            pass
    out.sort()
    return out


def nnls(X, Y, n, sweeps=500):
    """Non-negative least squares by coordinate descent on the normal equations.

    Fast enough to bootstrap, which matters more than elegance here: these
    regressors are badly collinear (flash and Gemma run in almost exactly the
    same hours), so a single point estimate routinely lands on the w=0 boundary
    and would otherwise be read as "this model costs nothing" when it actually
    means "this model's cost is not separable from its neighbour's".
    """
    G = [[sum(x[j] * x[k] for x in X) for k in range(n)] for j in range(n)]
    c = [sum(x[j] * y for x, y in zip(X, Y)) for j in range(n)]
    w = [0.0] * n
    for _ in range(sweeps):
        delta = 0.0
        for j in range(n):
            if G[j][j] <= 1e-12:
                continue
            r = c[j] - sum(G[j][k] * w[k] for k in range(n) if k != j)
            nw = max(0.0, r / G[j][j])
            delta = max(delta, abs(nw - w[j]))
            w[j] = nw
        if delta < 1e-12:
            break
    return w


def interp(ser):
    def sp(t):
        if t <= ser[0][0]:
            return ser[0][1]
        if t >= ser[-1][0]:
            return ser[-1][1]
        lo, hi = 0, len(ser) - 1
        while lo + 1 < hi:
            mid = (lo + hi) // 2
            if ser[mid][0] <= t:
                lo = mid
            else:
                hi = mid
        t0, v0 = ser[lo]
        t1, v1 = ser[hi]
        return v0 + ((t - t0) / max(1, t1 - t0)) * (v1 - v0)
    return sp


def main():
    cache = load_sessions()
    sess, bmap, names = cache["sess"], cache["bmap"], cache["names"]
    live = active_bid_models()
    ser = spend_series()
    if len(ser) < 24:
        print("not enough spend history in journald", file=sys.stderr)
        return 1
    sp = interp(ser)

    # session-hours per model per clock hour, splitting each session across the
    # hours it actually ran. Cost accrues while a session runs; revenue credits
    # only when it closes, so open-time bucketing would misalign the two.
    byh = {}
    for s in sess.values():
        mp = bmap.get(s["bid"])
        if not mp or not mp[1]:
            continue
        m, price = mp
        st = s["t"]
        dur = s["mor"] / price * 86400
        t = st
        while t < st + dur:
            h = int(t // 3600) * 3600
            byh.setdefault(h, {})
            byh[h][m] = byh[h].get(m, 0.0) + (min(st + dur, h + 3600) - t) / 3600
            t = min(st + dur, h + 3600)

    if not byh:
        print("no session-hours", file=sys.stderr)
        return 1
    lo, hi = ser[0][0], ser[-1][0]
    hours = sorted(h for h in byh if h >= lo and h + 3600 <= hi)
    if len(hours) < 12:
        print("not enough overlapping hours", file=sys.stderr)
        return 1

    totalH = {}
    for h in hours:
        for m, v in byh[h].items():
            totalH[m] = totalH.get(m, 0.0) + v

    # Hours in which exactly one model held sessions. There is nothing to
    # attribute in such an hour: the spend IS that model's cost. This beats the
    # fit outright where it applies, and it rescues models the fit can only
    # bound - Gemma-4-31b sat at ">= -222%" from a cost CI of [0, 0.078] that
    # included zero, while 7 solo hours put it at $0.00464/session-hour, +81%.
    # Account-level spend means any non-session traffic in the hour is counted
    # too, so this OVERSTATES cost and understates margin.
    solo = {}
    for h in hours:
        busy = {m: v for m, v in byh[h].items() if v > SOLO_EPS}
        if len(busy) != 1:
            continue
        m, v = next(iter(busy.items()))
        e = solo.setdefault(m, {"spend": 0.0, "hours": 0.0, "rates": []})
        d = max(0.0, sp(h + 3600) - sp(h))
        e["spend"] += d
        e["hours"] += v
        e["rates"].append(d / v)
    solo = {m: e for m, e in solo.items()
            if e["hours"] >= MIN_SOLO_HOURS and len(e["rates"]) >= MIN_SOLO_BUCKETS}
    free = sorted([m for m in totalH if totalH[m] >= MIN_HOURS],
                  key=lambda m: -totalH[m])

    X, Y = [], []
    for h in hours:
        row = [byh[h].get(m, 0.0) for m in free]
        row.append(sum(v for m, v in byh[h].items() if m not in free))
        X.append(row)
        Y.append(max(0.0, sp(h + 3600) - sp(h)))

    n = len(free) + 1
    w = nnls(X, Y, n)

    ss = sum((sum(w[j] * x[j] for j in range(n)) - y) ** 2 for x, y in zip(X, Y))
    my = sum(Y) / len(Y)
    tot = sum((y - my) ** 2 for y in Y) or 1e-12
    r2 = 1 - ss / tot

    # Bootstrap over hours. A rate is only reported when resampling agrees it is
    # distinguishable from zero -- otherwise the collinearity above turns a
    # boundary solution into a spurious "+100% margin".
    rng = random.Random(20260822)
    boots = [[] for _ in range(n)]
    idx = range(len(X))
    for _ in range(BOOTSTRAP):
        pick = [rng.randrange(len(X)) for _ in idx]
        bw = nnls([X[i] for i in pick], [Y[i] for i in pick], n, sweeps=200)
        for j in range(n):
            boots[j].append(bw[j])
    lohi = []
    for j in range(n):
        b = sorted(boots[j])
        lohi.append((b[int(0.05 * len(b))], b[int(0.95 * len(b)) - 1]))

    usd = None
    try:
        with open("/root/morpheus/private/margin.json") as f:
            usd = json.load(f).get("morUsd")
    except Exception:
        pass

    # Price a model by the bid its most recent session was won under, not by the
    # newest bid on the model: a reprice opens a new bid id, and the sessions
    # being costed here were sold at the old one.
    latest = {}
    for s in sess.values():
        mp = bmap.get(s["bid"])
        if mp and mp[0] and s["t"] >= latest.get(mp[0], (0, 0))[0]:
            latest[mp[0]] = (s["t"], mp[1])

    # `pricePerDayMor` above is the price the COSTED SESSIONS SOLD AT, which is
    # the only price the margin can honestly be computed against. It is not the
    # bid we hold now, and on a model repriced since its last session the two
    # differ a lot: deepseek-v4-flash last sold at 0.4 and has been listed at
    # 2.36 since 08-27 with no sessions, so its margin describes a price that is
    # no longer on offer. Emit the live bid beside it, and a flag, so nothing
    # downstream has to guess which number it is holding.
    live_bid = {}
    try:
        with open("/root/morpheus/private/reputation.json") as f:
            live_bid = {m["model"]: m["myPrice"] for m in json.load(f).get("models", [])
                        if m.get("myPrice") is not None}
    except Exception:
        pass

    models = {}
    for i, m in enumerate(free):
        cost = w[i]
        lo_, hi_ = lohi[i]
        p = latest.get(m, (0, None))[1]
        rev = (p / 24 * usd) if (p and usd) else None
        # identified == the 90% bootstrap band clears zero. Without this a
        # collinear pair reports one model at cost 0 and "+100% margin".
        ident = lo_ > 1e-6
        # A model that ran alone for long enough is measured, not fitted. The
        # fit still runs for it -- its hours carry cost signal for everyone
        # else -- but its own row reports the measurement.
        sol = solo.get(m)
        meas = sol is not None and rev
        fit_cost = cost
        fit_margin = (round(100 * (rev - fit_cost) / rev, 1)
                      if (rev and ident) else None)
        if meas:
            cost = sol["spend"] / sol["hours"]
            cheap, dear = min(sol["rates"]), max(sol["rates"])
        models[m] = {
            "name": names.get(m, m[:10]),
            "sessionHours": round(totalH[m], 2),
            "pricePerDayMor": round(p, 4) if p else None,
            "pricedAtSession": latest.get(m, (0, None))[0] or None,
            "currentBidMor": live_bid.get(m),
            # true when the margin below describes a price we no longer offer
            "priceIsStale": bool(p and live_bid.get(m) is not None
                                 and abs(live_bid[m] - p) > 1e-6),
            "kind": "measured" if meas else ("fitted" if ident else
                    ("bound" if rev else None)),
            "soloHours": round(sol["hours"], 2) if sol else None,
            "soloBuckets": len(sol["rates"]) if sol else None,
            "soloSpendUsd": round(sol["spend"], 5) if sol else None,
            "costRangeUsd": ([round(cheap, 5), round(dear, 5)] if meas else None),
            "marginRangePct": ([round(100 * (rev - dear) / rev, 1),
                                round(100 * (rev - cheap) / rev, 1)] if meas else None),
            "measured": bool(meas),
            "fittedCostPerSessionHourUsd": (round(fit_cost, 5) if ident else None),
            "fittedMarginPct": fit_margin,
            "measuredVsFitted": (
                None if not (meas and fit_margin is not None) else
                ("agree" if abs(fit_margin - 100 * (rev - cost) / rev) <= 25
                 else "DISAGREE")),
            "costPerSessionHourUsd": round(cost, 5) if (meas or ident) else None,
            "costCi90Usd": [round(lo_, 5), round(hi_, 5)],
            "revPerSessionHourUsd": round(rev, 5) if rev else None,
            "marginPct": (round(100 * (rev - cost) / rev, 1)
                          if (rev and (meas or ident)) else None),
            "marginCi90Pct": ([round(100 * (rev - hi_) / rev, 1),
                               round(100 * (rev - lo_) / rev, 1)]
                              if (rev and ident) else None),
            "breakEvenMorPerDay": (round(cost * 24 / usd, 4)
                                   if (usd and (meas or ident)) else None),
            # When the band touches zero the point estimate is meaningless, but
            # its UPPER end still bounds cost -- and therefore floors margin.
            # On a dear model that bound is often the useful number: Sonnet 4.6
            # is >= +46% even at the worst end of its band.
            "marginLowerBoundPct": (round(100 * (rev - hi_) / rev, 1)
                                    if (rev and not ident and not meas) else None),
            "fitted": ident,
            "activeBid": (m in live) if live is not None else None,
            "why": (None if (meas or ident) else
                    "cost not separable from a co-running model"),
        }
    for m, h in totalH.items():
        if m not in models:
            models[m] = {"name": names.get(m, m[:10]), "sessionHours": round(h, 2),
                         "pricePerDayMor": None, "pricedAtSession": None,
                         "currentBidMor": live_bid.get(m), "priceIsStale": False,
                         "costPerSessionHourUsd": None,
                         "costCi90Usd": None, "revPerSessionHourUsd": None,
                         "marginPct": None, "marginCi90Pct": None,
                         "marginLowerBoundPct": None, "kind": None,
                         "soloHours": None, "soloBuckets": None,
                         "soloSpendUsd": None, "costRangeUsd": None,
                         "marginRangePct": None, "measured": False,
                         "fittedCostPerSessionHourUsd": None,
                         "fittedMarginPct": None, "measuredVsFitted": None,
                         "breakEvenMorPerDay": None, "fitted": False,
                         "activeBid": (m in live) if live is not None else None,
                         "why": "under %.0f session-hours" % MIN_HOURS}

    doc = {
        "asOf": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "morUsd": usd,
        "fit": {
            "method": "non-negative least squares: hourly spend ~ per-model session-hours",
            "r2": round(r2, 3),
            "hours": len(hours),
            "from": datetime.fromtimestamp(hours[0], timezone.utc).isoformat(timespec="seconds"),
            "to": datetime.fromtimestamp(hours[-1] + 3600, timezone.utc).isoformat(timespec="seconds"),
            "minHoursForOwnCoefficient": MIN_HOURS,
            "bootstrapResamples": BOOTSTRAP,
            "pooledOtherCostPerSessionHourUsd": round(w[-1], 5),
            "soloMeasured": {names.get(m, m[:10]): {
                "hours": round(e["hours"], 2), "buckets": len(e["rates"]),
                "costPerSessionHourUsd": round(e["spend"] / e["hours"], 5)}
                for m, e in solo.items()},
            "minSoloHours": MIN_SOLO_HOURS,
            "soloCaveat": "hours in which one model ran alone are not a random "
                          "sample of hours; where measured and fitted disagree the "
                          "row carries both and is flagged measuredVsFitted",
            "spendExplainedUsd": round(sum(sum(w[j] * x[j] for j in range(n)) for x in X), 4),
            "spendActualUsd": round(sum(Y), 4),
            # Models whose hours are in the fit but which we no longer bid on.
            # Their cost signal is still valid history; their margin row is not
            # a current position.
            "trainedOnRetiredBids": None,
        },
        "models": models,
        "note": "OpenRouter reports account-level spend only. A model that ran ALONE "
                "for at least %.1f session-hours across %d separate hours is MEASURED "
                "(kind=measured) - the spend in those hours is its cost, no attribution. "
                "Every other row is FITTED, or only bounded when the bootstrap band "
                "touches zero. Measured rows still overstate cost slightly: non-session "
                "traffic in a solo hour is counted too. One OpenRouter key per model "
                "makes all of this exact." % (MIN_SOLO_HOURS, MIN_SOLO_BUCKETS),
    }
    doc["fit"]["trainedOnRetiredBids"] = sorted(
        d["name"] for d in models.values() if d.get("activeBid") is False
        and d["sessionHours"] > 0)

    tmp = OUT + ".tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f, indent=1)
    os.replace(tmp, OUT)

    print("fit R2=%.3f over %d h (%s -> %s)"
          % (r2, len(hours), doc["fit"]["from"][:16], doc["fit"]["to"][:16]))
    print("explained $%.2f of $%.2f actual"
          % (doc["fit"]["spendExplainedUsd"], doc["fit"]["spendActualUsd"]))
    print()
    print("%-26s %7s %10s %10s %9s %10s  %s"
          % ("MODEL", "SESS-H", "$/sess-h", "REV/sess-h", "MARGIN", "B/E MOR/d", "NOTE"))
    for m, d in sorted(models.items(), key=lambda x: -x[1]["sessionHours"]):
        tag = "" if d.get("activeBid") is not False else "  [NO ACTIVE BID]"
        if not d["fitted"]:
            b = d.get("marginLowerBoundPct")
            print("%-26s %7.1f %10s %10s %9s %10s  %s"
                  % (d["name"], d["sessionHours"], "-",
                     "%.5f" % d["revPerSessionHourUsd"] if d["revPerSessionHourUsd"] else "-",
                     (">=%+.0f%%" % b) if b is not None else "-", "-",
                     (d["why"] or "") + tag))
            continue
        ci = d["marginCi90Pct"]
        print("%-26s %7.1f %10.5f %10.5f %8s%% %10s  90%% CI %+.0f%%..%+.0f%%"
              % (d["name"], d["sessionHours"], d["costPerSessionHourUsd"],
                 d["revPerSessionHourUsd"] or 0, "%+.1f" % d["marginPct"],
                 ("%.4f" % d["breakEvenMorPerDay"]) if d["breakEvenMorPerDay"] else "-",
                 ci[0], ci[1]) + tag)
    return 0


if __name__ == "__main__":
    sys.exit(main())
