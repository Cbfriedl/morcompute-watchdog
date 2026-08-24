#!/usr/bin/env python3
"""Per-window, per-provider and per-provider-x-model activity for the public
Compare page.

census-full.json carries only s10 and sAll per provider and nothing at all per
provider-x-model, so a side-by-side of two providers across the models they bid
cannot be built from it. history.db has every session on the network as
(provider, model, buyer, t, dur, mor) and answers all three windows.

This file is PUBLIC. It names no operator: every address in it is a bidder on a
public chain, exactly as census-full.json already lists them, and there is no
"me" field of any kind. mor is stored in history.db as MOR * 1e9.

The window ends at max(t), not at now -- the history job tops the DB up daily --
and the page states that.

usage: gen_provider_models.py <history.db> <out.json>
"""
import json, os, sqlite3, sys, time

MOR = 1e9
WINDOW_DAYS = int(os.environ.get("EARNER_WINDOW_DAYS", "10"))


def build(db, out):
    c = sqlite3.connect(db)
    as_of = c.execute("select max(t) from session").fetchone()[0]
    now = int(time.time())
    day = now // 86400 * 86400
    wins = {"d1": day, "d10": now - WINDOW_DAYS * 86400, "all": 0}

    mid2id, names = {}, {}
    for i, mid, nm in c.execute("select id, mid, name from model"):
        mid2id[i] = mid
        names[mid] = nm

    windows = {}
    for label, cut in wins.items():
        prov = {}
        for addr, n, mor, nm in c.execute(
                "select lower(p.addr), count(*), sum(s.mor), count(distinct s.m) "
                "from session s join provider p on p.id = s.p "
                "where s.t >= ? group by p.addr", (cut,)):
            prov[addr] = {"n": n, "mor": round((mor or 0) / MOR, 6), "models": nm}

        by = {}
        for addr, m, n, mor, dur in c.execute(
                "select lower(p.addr), s.m, count(*), sum(s.mor), sum(s.dur) "
                "from session s join provider p on p.id = s.p "
                "where s.t >= ? group by p.addr, s.m", (cut,)):
            if m not in mid2id:
                continue
            by.setdefault(addr, {})[mid2id[m]] = {
                "n": n, "mor": round((mor or 0) / MOR, 6),
                "hours": round((dur or 0) / 3600.0, 2)}

        windows[label] = {"sessions": sum(p["n"] for p in prov.values()),
                          "providers": prov, "byModel": by}

    doc = {"generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "asOf": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(as_of)),
           "asOfTime": as_of, "dayStart": day, "windowDays": WINDOW_DAYS,
           "modelNames": names, "windows": windows}
    tmp = out + ".tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f, separators=(",", ":"))
    os.replace(tmp, out)
    return doc


if __name__ == "__main__":
    d = build(sys.argv[1], sys.argv[2])
    for k, v in d["windows"].items():
        print("%-4s %7d sessions  %3d providers" % (k, v["sessions"], len(v["providers"])))
    print("asOf", d["asOf"], "->", sys.argv[2], os.path.getsize(sys.argv[2]), "bytes")
