#!/usr/bin/env python3
"""Build the bid-market census JSON the public page reads.

Sessions come from history.db (built once, 318k sessions, 244 days) rather than
being rescanned — rescanning is what made the old census time out and publish
half-formed data. Everything else is read live so prices and headroom are current.

Writes census-full.json. The page fetches it; nothing is embedded in the HTML,
so a daily refresh is a data change, not a code change.
"""
import json, os, sqlite3, statistics, subprocess, sys, time, collections
from datetime import datetime, timezone

DIAMOND="0x6aBE1d282f72B474E54527D93b979A4f64d3030a"
ME=os.environ.get("PROVIDER_ADDRESS", "").lower()
if not ME:
    raise SystemExit("PROVIDER_ADDRESS is not set. Export it, or add it to the EnvironmentFile named by the systemd unit.")
HIST=os.environ.get("HISTORY_DB","/home/cbfriedl/Documents/Projects/morcompute-watchdog/history.db")
OUT=os.environ.get("CENSUS_FULL","census-full.json")
WINDOW=int(os.environ.get("WINDOW_DAYS","10"))

sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
try:
    from rpc_endpoints import endpoints
    RPCS=endpoints()
except Exception:
    RPCS=["https://base-rpc.publicnode.com","https://mainnet.base.org",
          "https://base.drpc.org","https://base.lava.build"]
_rr=[0]

def rpc(batch):
    last=None
    for a in range(len(RPCS)*5):
        url=RPCS[_rr[0]%len(RPCS)]; _rr[0]+=1
        try:
            p=subprocess.run(["curl","-s","--max-time","50","-X","POST",url,
                "-H","content-type: application/json","--data-binary","@-"],
                input=json.dumps(batch).encode(),capture_output=True)
            r=json.loads(p.stdout)
            if not isinstance(r,list): raise ValueError(str(r)[:120])
            if any("result" not in x for x in r): raise ValueError("partial")
            return r
        except Exception as e:
            last=e; time.sleep(min(0.4*(a+1),4))
    raise RuntimeError("rpc failed: %r"%last)

def call(d,i=1): return {"jsonrpc":"2.0","id":i,"method":"eth_call","params":[{"to":DIAMOND,"data":d},"latest"]}
def W(h):
    h=h[2:] if h.startswith("0x") else h
    return [h[i:i+64] for i in range(0,len(h),64)]
def dstr(w,b):
    n=int(w[b],16)
    if not n: return ""
    return bytes.fromhex("".join(w[b+1:b+1+(n+31)//32])[:n*2]).decode("utf-8","replace")
def chunks(s,n):
    for i in range(0,len(s),n): yield s[i:i+n]

B=6
log=lambda m: print(m,flush=True)

# ---- models ----
mids=[]
off=0
while True:
    w=W(rpc([call("0x3839d3dc"+"%064x"%off+"%064x"%100)])[0]["result"])
    o=int(w[0],16)//32; n=int(w[o],16)
    got=["0x"+w[o+1+i] for i in range(n)]
    mids+=got
    if n<100: break
    off+=100
log("models: %d"%len(mids))

meta={}
for ch in chunks(mids,B):
    try: res={x["id"]:x for x in rpc([call("0x21e7c498"+m[2:],i) for i,m in enumerate(ch)])}
    except Exception: continue
    for i,m in enumerate(ch):
        try:
            w=W(res[i]["result"]); b=int(w[0],16)//32
            meta[m]={"n":dstr(w,b+int(w[b+4],16)//32) or "(unnamed)",
                     "t":"", "r":1}
        except Exception: meta[m]={"n":m[:10],"t":"","r":1}
log("model names: %d"%len(meta))

# ---- bids per model ----
bids=collections.defaultdict(list)
allbids={}
for ch in chunks(mids,B):
    try: res={x["id"]:x for x in rpc([call("0x8a683b6e"+m[2:]+"%064x"%0+"%064x"%200,i) for i,m in enumerate(ch)])}
    except Exception: continue
    for i,m in enumerate(ch):
        try:
            w=W(res[i]["result"]); o=int(w[0],16)//32; n=int(w[o],16)
            for k in range(n): allbids["0x"+w[o+1+k]]=m
        except Exception: pass
log("bid ids: %d"%len(allbids))
bl=list(allbids)
for ch in chunks(bl,B):
    try: res={x["id"]:x for x in rpc([call("0x91704e1e"+b[2:],i) for i,b in enumerate(ch)])}
    except Exception: continue
    for i,b in enumerate(ch):
        try:
            r=W(res[i]["result"])
            bids[allbids[b]].append({"p":"0x"+r[0][24:],"day":int(r[2],16)*86400/1e18})
        except Exception: pass
log("bids resolved: %d"%sum(len(v) for v in bids.values()))

# ---- providers ----
w=W(rpc([call("0xd5472642"+"%064x"%0+"%064x"%500)])[0]["result"])
o=int(w[0],16)//32; n=int(w[o],16)
provs=["0x"+w[o+1+i][24:] for i in range(n)]
psnap={}
for ch in chunks(provs,B):
    try: res={x["id"]:x for x in rpc([call("0x55f21eb7"+a[2:].rjust(64,"0"),i) for i,a in enumerate(ch)])}
    except Exception: continue
    for i,a in enumerate(ch):
        try:
            pw=W(res[i]["result"]); b=int(pw[0],16)//32
            st=int(pw[b+1],16)/1e18; ea=int(pw[b+4],16)/1e18
            psnap[a]={"stake":round(st,4),"earned":round(ea,4),"head":round(st-ea,4),
                      "pct":round(100*ea/st,2) if st else 0.0,
                      "createdAt":int(pw[b+2],16),"deleted":int(pw[b+5],16)==1}
        except Exception: pass
log("providers: %d"%len(psnap))

# ---- sessions from history.db ----
con=sqlite3.connect("file:%s?mode=ro"%HIST,uri=True); c=con.cursor()
hi=c.execute("SELECT MAX(t) FROM session").fetchone()[0]
cut=hi-WINDOW*86400
s_model=collections.Counter(); s_prov=collections.Counter()
sp_model=collections.defaultdict(collections.Counter)
for mid,pa,cnt,mo in c.execute("""SELECT m.mid,p.addr,COUNT(*),SUM(s.mor)/1e9 FROM session s
    JOIN model m ON m.id=s.m JOIN provider p ON p.id=s.p WHERE s.t>=? GROUP BY s.m,s.p""",(cut,)):
    s_model[mid]+=cnt; s_prov[pa.lower()]+=cnt; sp_model[pa.lower()][mid]=cnt
sAll=dict(c.execute("SELECT p.addr,COUNT(*) FROM session s JOIN provider p ON p.id=s.p GROUP BY s.p"))
sAll={k.lower():v for k,v in sAll.items()}
netAll=c.execute("SELECT COUNT(*) FROM session").fetchone()[0]
net10=sum(s_model.values())
log("sessions: %d in %dd window, %d all-time"%(net10,WINDOW,netAll))

# ---- assemble ----
models=[]
for m in mids:
    bl2=bids.get(m,[])
    if not bl2: continue
    px=sorted(x["day"] for x in bl2)
    models.append({"id":m,"n":meta.get(m,{}).get("n",m[:10]),
        "b":len(bl2),"p":len({x["p"] for x in bl2}),
        "mn":round(px[0],4),"md":round(statistics.median(px),4),"mx":round(px[-1],4),
        "t":meta.get(m,{}).get("t",""),"r":1,
        "px":[round(x,4) for x in px],
        "pv":sorted([[x["p"],round(x["day"],4)] for x in bl2],key=lambda y:-y[1]),
        "s10":s_model.get(m,0),
        "live":1 if any(psnap.get(x["p"],{}).get("head",0)>0.5 for x in bl2) else 0})
models.sort(key=lambda r:-r["s10"])

pdetail={}
for a,s in psnap.items():
    mybids=[(mid,x["day"]) for mid,lst in bids.items() for x in lst if x["p"]==a]
    px=sorted(p for _,p in mybids)
    pdetail[a]=dict(s,
        bids=len(mybids), models=len({m for m,_ in mybids}),
        mn=round(px[0],4) if px else None, md=round(statistics.median(px),4) if px else None,
        mx=round(px[-1],4) if px else None,
        s10=s_prov.get(a,0), sAll=sAll.get(a,0),
        rows=sorted([{"n":meta.get(mid,{}).get("n",mid[:10]),"p":round(pr,4),
                      "s":sp_model.get(a,{}).get(mid,0)} for mid,pr in mybids],
                    key=lambda r:-r["s"]))

now=datetime.now(timezone.utc)
allpx=sorted(x for m in models for x in m["px"])
q=lambda f: round(allpx[min(len(allpx)-1,int(len(allpx)*f))],4) if allpx else None
doc={
 "generated": now.isoformat(timespec="seconds"),
 "asOf": now.strftime("%Y-%m-%d"), "asOfTime": now.strftime("%H:%M UTC"),
 "sessionsAsOf": datetime.fromtimestamp(hi,timezone.utc).isoformat(timespec="seconds"),
 "windowDays": WINDOW,
 "youAre": ME,
 "registeredModels": len(mids), "modelsWithBids": len(models),
 "totalBids": sum(len(v) for v in bids.values()),
 "biddingAddresses": len({x["p"] for v in bids.values() for x in v}),
 "registeredProviders": len(psnap),
 "activeProviders": len([p for p in psnap.values() if not p["deleted"]]),
 "contenders": len([p for p in psnap.values() if not p["deleted"] and p["head"]>0.5]),
 "chokedAddresses": len([p for p in psnap.values() if not p["deleted"] and p["head"]<=0.5]),
 "sessionsWindow": net10, "sessionsNetwork": netAll,
 "p10":q(.10),"p25":q(.25),"p50":q(.50),"p75":q(.75),"p90":q(.90),
 "atCeiling": sum(1 for v in allpx if v>=863.9),
 "models": models, "pdetail": pdetail,
}
json.dump(doc,open(OUT,"w"),separators=(",",":"))
log("wrote %s  (%.1f MB)  models=%d providers=%d"%(OUT,os.path.getsize(OUT)/1e6,len(models),len(pdetail)))
