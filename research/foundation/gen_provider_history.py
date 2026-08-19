#!/usr/bin/env python3
"""Collapse history.db into providers-history.json for the public site.

history.db is ~21 MB — fine to commit, far too big to make a browser download
before first paint. Every question the public site actually asks of it is an
aggregate over one provider, so precompute those and ship ~50 KB instead.

Emitted per provider: lifetime and windowed session/MOR totals, the per-model
breakdown, and buyer concentration (how many distinct buyers, how much the top
one accounts for, and how much the provider bought from itself). The last of
those is the single most important fact about this market and it is invisible
in the live-bid census, which knows nothing about who paid.

Usage:  python3 gen_provider_history.py history.db providers-history.json
"""
import json, sqlite3, sys, time

DB  = sys.argv[1] if len(sys.argv) > 1 else "history.db"
OUT = sys.argv[2] if len(sys.argv) > 2 else "providers-history.json"

c = sqlite3.connect(DB)
meta = dict(c.execute("select k,v from meta"))
MORU = 1e9                                    # meta.morUnit: MOR * 1e9

# Anchor the windows to the newest session in the data, not to wall-clock now.
# The database is rebuilt on a timer; pinning to now() would silently shrink
# "today" to nothing in the hours after midnight before the next build lands.
last = c.execute("select max(t) from session").fetchone()[0] or int(time.time())
DAY, WIN = 86400, 10
cut1, cut10 = last - DAY, last - WIN * DAY

prov  = {i: a for i, a in c.execute("select id,addr from provider")}
model = {i: n for i, n, in c.execute("select id,name from model")}
buyer = {i: a for i, a in c.execute("select id,addr from buyer")}

def agg(where, params=()):
    q = ("select p, count(*), sum(mor) from session where 1=1 " + where + " group by p")
    return {r[0]: (r[1], (r[2] or 0) / MORU) for r in c.execute(q, params)}

allT = agg("")
w10  = agg("and t >= ?", (cut10,))
w1   = agg("and t >= ?", (cut1,))

out = {}
for pid, addr in prov.items():
    sA, mA = allT.get(pid, (0, 0.0))
    s10, m10 = w10.get(pid, (0, 0.0))
    s1, m1 = w1.get(pid, (0, 0.0))

    rows = [{"n": model.get(mi, "?"), "sAll": s, "morAll": (mo or 0) / MORU,
             "s10": 0, "mor10": 0.0}
            for mi, s, mo in c.execute(
                "select m,count(*),sum(mor) from session where p=? group by m", (pid,))]
    by = {r["n"]: r for r in rows}
    for mi, s, mo in c.execute(
            "select m,count(*),sum(mor) from session where p=? and t>=? group by m", (pid, cut10)):
        r = by.get(model.get(mi, "?"))
        if r: r["s10"], r["mor10"] = s, (mo or 0) / MORU
    rows.sort(key=lambda r: -r["morAll"])

    # Buyer concentration, lifetime. `self` counts sessions the provider opened
    # against its own listing — the address is both sides of the trade.
    bs = list(c.execute(
        "select u,count(*),sum(mor) from session where p=? group by u order by 2 desc", (pid,)))
    tot = sum(b[1] for b in bs) or 1
    selfid = next((i for i, a in buyer.items() if a.lower() == addr.lower()), None)
    selfN = next((b[1] for b in bs if b[0] == selfid), 0)

    first = c.execute("select min(t),max(t) from session where p=?", (pid,)).fetchone()

    out[addr.lower()] = {
        "sAll": sA, "morAll": round(mA, 4),
        "s10": s10, "mor10": round(m10, 4),
        "s1": s1,  "mor1": round(m1, 4),
        "buyers": len(bs),
        "topBuyer": buyer.get(bs[0][0], "").lower() if bs else None,
        "topShare": round(100 * bs[0][1] / tot, 2) if bs else 0,
        "selfShare": round(100 * selfN / tot, 2),
        "first": first[0], "last": first[1],
        "rows": rows[:60],
    }

doc = {"asOf": meta.get("asOf"), "coverage": float(meta.get("coverage", 0)),
       "sessionsTotal": int(meta.get("sessionsTotal", 0)),
       "firstDay": meta.get("firstDay"), "lastDay": meta.get("lastDay"),
       "windowDays": WIN, "anchor": last,
       "morBasis": meta.get("morBasis"), "providers": out}
json.dump(doc, open(OUT, "w"), separators=(",", ":"))
print("wrote %s  %d providers  %.0f KB" %
      (OUT, len(out), __import__("os").path.getsize(OUT) / 1024))
