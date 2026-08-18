#!/usr/bin/env python3
"""For each high-demand model: what must I bid to rank #1 / #3 among live rivals?

Score is quality / price, so price is the only lever a provider with no history
has. Cold start is real: successScore is 0 until the first session completes and
tpsScore floors at 0.24 because an absent mean sits far below the model mean.
"""
import json, os, sys
import score as S

ME = "0x2f144f3b192a2d2d2384de7007ee2cad943c601b"
MINSTAKE = int(0.2*1e18)
WSETS = {
  "even":    dict(tps=.2, ttft=.2, duration=.2, success=.2, stake=.2),
  "quality": dict(tps=.35,ttft=.35,duration=.1, success=.15,stake=.05),
  "success": dict(tps=.15,ttft=.15,duration=.1, success=.55,stake=.05),
}
CACHE = "target_cache.json"
C = json.load(open(CACHE)) if os.path.exists(CACHE) else {"prov":{}, "model":{}, "pm":{}, "bids":{}}
def save(): json.dump(C, open(CACHE,"w"))

def prov(a):
    if a not in C["prov"]:
        pw = S.W(S.rpc([S.call("0x55f21eb7"+a[2:].rjust(64,"0"))])[0]["result"])
        b = int(pw[0],16)//32
        C["prov"][a] = {"stake": int(pw[b+1],16), "head": int(pw[b+1],16)-int(pw[b+4],16)}
        save()
    return C["prov"][a]

def mstats(mid):
    if mid not in C["model"]:
        C["model"][mid] = S.model_stats(mid); save()
    return C["model"][mid]

def pmstats(mid, a):
    k = mid+"|"+a
    if k not in C["pm"]:
        C["pm"][k] = S.pm_stats(mid, a); save()
    return C["pm"][k]

def modelbids(mid):
    if mid not in C["bids"]:
        w = S.W(S.rpc([S.call("0x8a683b6e"+mid[2:]+"%064x"%0+"%064x"%200)])[0]["result"])
        off = int(w[0],16)//32; n = int(w[off],16)
        ids = ["0x"+w[off+1+i] for i in range(n)]
        out = []
        for i in range(0,len(ids),3):
            ch = ids[i:i+3]
            res = {x["id"]:x for x in S.rpc([S.call("0x91704e1e"+b[2:], j) for j,b in enumerate(ch)])}
            for j,b in enumerate(ch):
                r = S.W(res[j]["result"])
                out.append({"bid":b, "prov":"0x"+r[0][24:], "pps":int(r[2],16)})
        C["bids"][mid] = out; save()
    return C["bids"][mid]

def analyse(mid, name, sess10, wname="even"):
    w = WSETS[wname]
    ms = mstats(mid)
    live = []
    for b in modelbids(mid):
        p = prov(b["prov"])
        if p["head"] <= 0.5*1e18:      # choked: cannot be paid, so not a rival
            continue
        pm = pmstats(mid, b["prov"])
        sc, parts = S.score(pm, ms, b["pps"], p["stake"], MINSTAKE, w)
        live.append({"a": b["prov"], "day": b["pps"]*86400/1e18, "sc": sc, "q": parts["q"],
                     "mine": b["prov"].lower()==ME})
    live.sort(key=lambda x:-x["sc"])
    # my cold-start quality on this model (zero stats), computed the same way
    myq = S.score({"tps":0,"ttft":0,"dur":0,"succ":0,"total":0}, ms,
                  int(1e18), 700*10**18, MINSTAKE, w)[1]["q"]
    def price_to_beat(rank):
        rivals = [x for x in live if not x["mine"]]
        if len(rivals) < rank: return None
        return myq/rivals[rank-1]["sc"]*86400*0.98   # 2% under, in MOR/day
    return {"mid":mid, "name":name, "s10":sess10, "live":live, "myq":myq,
            "p1":price_to_beat(1), "p3":price_to_beat(3)}
