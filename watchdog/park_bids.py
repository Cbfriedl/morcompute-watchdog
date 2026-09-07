#!/usr/bin/env python3
"""Park bids at the top of their book when OpenRouter credit is about to run out.

Revenue is priced per SECOND of session, cost is priced per TOKEN. A heavy buyer
on an underpriced model therefore burns money for as long as it keeps opening
sessions, and the only lever that acts fast enough is price: it cannot stop the
session already running, but it can stop the next one.

Measured across 26 models with >=20 sessions and >=3 bidders (2026-09-07): the
MAX bid on a model captured 3 of 7,331 sessions - 0.04%, and exactly zero on
24 of the 26. Parking at the top of the book is therefore a reliable way to stay
listed and stop winning work. It is not a guarantee; deepseek-v4-flash's max
bid at 864 took 2 sessions.

It is also safe in the other direction: if somebody does buy at a parked price,
the revenue is enormous relative to any plausible token cost, so the trade is
fine either way.

What this does NOT do:
  * It never restarts anything and never touches a running session. An open
    session keeps burning until it closes on its own.
  * It does not delete bids. A delete would stop sessions outright but risks the
    quality/reputation that survives a mere reprice.

`--restore` puts the original prices back. That is deliberately a separate,
human-run step: automatically un-parking would walk straight back into the burn.
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

API = os.environ.get("ROUTER_API", "http://127.0.0.1:8082")
COOKIE = os.environ.get("ROUTER_COOKIE_FILE", "/root/morpheus/morpheus-data/.cookie")
DASH = os.environ.get("DASH_URL", "http://127.0.0.1:8090")
ME = os.environ.get("PROVIDER_ADDRESS",
                    "0x2f144F3b192A2d2D2384de7007EE2cAd943C601b")
CENSUS = os.environ.get("CENSUS_FILE", "/root/morpheus/private/census-full.json")
REPUTATION = os.environ.get("REPUTATION_FILE", "/root/morpheus/private/reputation.json")
PARK_STATE = os.environ.get("PARK_STATE", "/root/morpheus/monitor/parked.json")

CEILING = float(os.environ.get("PARK_CEILING", "864"))     # network max MOR/day
FEE = 0.3                                                   # MOR per bid post
MAX_PARKS = int(os.environ.get("PARK_MAX_MODELS", "4"))     # fee blast radius


def log(m):
    print("%sZ %s" % (datetime.now(timezone.utc).strftime("%H:%M:%S"), m), flush=True)


def api(path, method="GET", body=None, tries=4):
    cookie = open(COOKIE).read().strip()
    cmd = ["curl", "-s", "--max-time", "60", "-u", cookie, "-X", method, API + path]
    if body is not None:
        cmd += ["-H", "content-type: application/json", "-d", json.dumps(body)]
    for _ in range(tries):
        p = subprocess.run(cmd, capture_output=True)
        try:
            return json.loads(p.stdout)
        except Exception:
            time.sleep(1.5)
    return None


def open_models():
    """Model ids we have a session open on right now — the burn suspects."""
    try:
        p = subprocess.run(["curl", "-s", "--max-time", "120",
                            "%s/opensessions?p=%s&cb=%d" % (DASH, ME, time.time())],
                           capture_output=True)
        d = json.loads(p.stdout)
        if d.get("error") or d.get("errors"):
            return None                      # a failed read is not "nothing open"
        return {m: n for m, n in (d.get("providers", {}).get(ME.lower()) or {}).items() if n}
    except Exception as e:
        log("open-session read failed: %s" % e)
        return None


def my_bids():
    """model id -> our live price, straight off chain via reputation.json."""
    try:
        r = json.load(open(REPUTATION))
        return {m["model"]: m["myPrice"] for m in r.get("models", [])
                if m.get("myPrice") is not None}
    except Exception:
        return {}


# MORDIEM bids at the 864 MOR/day ceiling and trades only with itself. Including
# it would drag a park price to the ceiling on any model it touches, which is
# both absurd next to a book that tops out at 5 and WEAKER suppression — capped
# at the ceiling we would tie it rather than sit strictly above it. Excluding it
# here is not the same as netting it out of a total, which stays forbidden.
EXCLUDE = {a.strip().lower() for a in os.environ.get(
    "PARK_EXCLUDE_BIDDERS",
    "0xd01c1b0eedbe341c409369177478f2eabbeee848").split(",") if a.strip()}


def max_bid(mid):
    """Top of this model's book, excluding EXCLUDE, from the census. Daily, but
    a panic action needs only a number comfortably above every real rival."""
    try:
        for m in json.load(open(CENSUS)).get("models", []):
            if m["id"] != mid:
                continue
            px = [pr for a, pr in (m.get("pv") or [])
                  if str(a).lower() not in EXCLUDE]
            # fall back to the census max only if pv is missing entirely
            return (max(px) if px else m.get("mx")), m.get("n")
    except Exception:
        pass
    return None, None


def park_price(mid, mine):
    top, _ = max_bid(mid)
    want = max(top or 0, mine) * 1.05          # strictly above every known rival
    return round(min(max(want, mine * 2), CEILING), 4)


def post(mid, mor_day):
    pps = int(mor_day * 1e18 / 86400)
    for _ in range(5):
        r = api("/blockchain/bids", "POST", {"modelID": mid, "pricePerSecond": pps})
        if r and "error" not in r:
            return True, r
        time.sleep(2)
    return False, r


def load_parked():
    try:
        return json.load(open(PARK_STATE))
    except Exception:
        return {}


def save_parked(d):
    tmp = PARK_STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=1)
    os.replace(tmp, PARK_STATE)


def do_park(go, only=None):
    """only: park these model ids regardless of whether a session is open.

    The session-open filter is right for a burn — it finds what is spending.
    It is wrong for a bid the router cannot serve, which must be parked whether
    or not it is busy, and which is exactly the case this was first needed for.
    """
    parked = load_parked()
    mine = my_bids()
    if only:
        targets = [m for m in only if m in mine and m not in parked]
        opens = {m: 0 for m in targets}
        if not targets:
            log("nothing to park from --model (not ours, or already parked)")
            return 0, []
        return _park(targets, mine, opens, parked, go)
    opens = open_models()
    if opens is None:
        log("REFUSING: could not read open sessions; a failed read is not an empty one")
        return 1, []
    # Only models we are actually serving right now can be the ones burning.
    targets = [m for m in opens if m in mine and m not in parked]
    if not targets:
        log("nothing to park (open on %d model(s), %d already parked)"
            % (len(opens), len(parked)))
        return 0, []
    if len(targets) > MAX_PARKS:
        log("%d models open; parking only the %d max per token is not "
            "determinable here, so parking all is capped at %d — parking the "
            "first %d by session count" % (len(targets), MAX_PARKS, MAX_PARKS, MAX_PARKS))
        targets = sorted(targets, key=lambda m: -opens[m])[:MAX_PARKS]
    return _park(targets, mine, opens, parked, go)


def _park(targets, mine, opens, parked, go):
    done = []
    for mid in targets:
        cur = mine[mid]
        new = park_price(mid, cur)
        _top, name = max_bid(mid)
        log("%s %s: %.4f -> %.4f MOR/day (%d session(s) open)"
            % ("WOULD PARK" if not go else "PARKING", name or mid[:10],
               cur, new, opens[mid]))
        if not go:
            done.append((mid, name, cur, new))
            continue
        ok, resp = post(mid, new)
        if ok:
            parked[mid] = {"name": name, "original": cur, "parked_at": new,
                           "ts": int(time.time())}
            save_parked(parked)
            done.append((mid, name, cur, new))
        else:
            log("  FAILED: %s" % json.dumps(resp)[:160])
    return 0, done


def do_restore(go):
    parked = load_parked()
    if not parked:
        log("nothing is parked")
        return 0, []
    done = []
    for mid, rec in list(parked.items()):
        log("%s %s: %.4f -> %.4f MOR/day"
            % ("WOULD RESTORE" if not go else "RESTORING",
               rec.get("name") or mid[:10], rec["parked_at"], rec["original"]))
        if not go:
            done.append((mid, rec)); continue
        ok, resp = post(mid, rec["original"])
        if ok:
            del parked[mid]
            save_parked(parked)
            done.append((mid, rec))
        else:
            log("  FAILED: %s" % json.dumps(resp)[:160])
    return 0, done


def main():
    go = "--go" in sys.argv
    only = [a for a in sys.argv[1:] if a.startswith("0x") and len(a) == 66]
    if "--restore" in sys.argv:
        rc, done = do_restore(go)
    elif "--status" in sys.argv:
        p = load_parked()
        log("parked: %s" % (json.dumps(p, indent=1) if p else "nothing"))
        return 0
    else:
        rc, done = do_park(go, only or None)
    if not go:
        log("DRY RUN — nothing sent (%d bid(s), %.1f MOR in fees). Pass --go."
            % (len(done), FEE * len(done)))
    return rc


if __name__ == "__main__":
    sys.exit(main())
