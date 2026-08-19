#!/usr/bin/env python3
import os
import json, sys
import score as S

MID = sys.argv[1] if len(sys.argv)>1 else "0xc2c4b037ff12e0aa81178deac52aeed902b36189b9e6feae22b72324c9221130"
ME  = os.environ.get("PROVIDER_ADDRESS","").lower()
MINSTAKE = int(0.2*1e18)

# weight sets: the buyer's config is not public, so test a spread and check the
# ranking is not an artefact of one guess
WSETS = {
  "even":        dict(tps=.2, ttft=.2, duration=.2, success=.2, stake=.2),
  "quality":     dict(tps=.35,ttft=.35,duration=.1, success=.15,stake=.05),
  "success":     dict(tps=.15,ttft=.15,duration=.1, success=.55,stake=.05),
  "tps-only":    dict(tps=1.0,ttft=0,  duration=0,  success=0,  stake=0),
}

w = S.W(S.rpc([S.call("0x8a683b6e"+MID[2:]+"%064x"%0+"%064x"%200)])[0]["result"])
off = int(w[0],16)//32; n = int(w[off],16)
ids = ["0x"+w[off+1+i] for i in range(n)]
bids = []
for i in range(0,len(ids),3):
    ch = ids[i:i+3]
    res = {x["id"]:x for x in S.rpc([S.call("0x91704e1e"+b[2:], j) for j,b in enumerate(ch)])}
    for j,b in enumerate(ch):
        r = S.W(res[j]["result"])
        bids.append({"bid":b, "prov":"0x"+r[0][24:], "pps":int(r[2],16)})

ms = S.model_stats(MID)
for b in bids:
    b["pm"] = S.pm_stats(MID, b["prov"])
    pw = S.W(S.rpc([S.call("0x55f21eb7"+b["prov"][2:].rjust(64,"0"))])[0]["result"])
    base = int(pw[0],16)//32
    b["stake"] = int(pw[base+1],16)
    b["head"]  = b["stake"] - int(pw[base+4],16)

print("model count=%d  tps mean=%d  ttft mean=%d" % (ms["count"], ms["tps"][0], ms["ttft"][0]))
print()
for wn, wv in WSETS.items():
    for b in bids:
        b["sc"], b["parts"] = S.score(b["pm"], ms, b["pps"], b["stake"], MINSTAKE, wv)
    bids.sort(key=lambda x:-x["sc"])
    print("=== weights: %s" % wn)
    print("%-4s %-12s %9s %8s %7s %7s %7s %7s %7s %12s %s" %
          ("#","provider","MOR/day","sess","tps","ttft","dur","succ","qual","score","headroom"))
    for i,b in enumerate(bids,1):
        mark = "  <== YOU" if b["prov"].lower()==ME else ("" if b["head"]>0.5 else "  (choked)")
        p=b["parts"]
        print("%-4d %-12s %9.3f %8d %7.2f %7.2f %7.2f %7.2f %7.3f %12.1f %s" %
              (i, b["prov"][:12], b["pps"]*86400/1e18, b["pm"]["total"],
               p["tps"],p["ttft"],p["dur"],p["succ"],p["q"], b["sc"], mark))
    print()
