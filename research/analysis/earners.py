import json,time,subprocess,datetime,collections,statistics
EP="https://base.drpc.org"; D="0x6aBE1d282f72B474E54527D93b979A4f64d3030a"
def rpc(batch):
    last=None
    for a in range(8):
        try:
            p=subprocess.run(["curl","-s","--max-time","60","-X","POST",EP,
                "-H","content-type: application/json","--data-binary","@-"],
                input=json.dumps(batch).encode(),capture_output=True)
            r=json.loads(p.stdout)
            if not isinstance(r,list): raise ValueError(str(r)[:150])
            if any('result' not in x for x in r): raise ValueError('partial '+str(r)[:150])
            return r
        except Exception as e:
            last=e; time.sleep(min(2*(a+1),15))
    raise RuntimeError(repr(last))
def call(d,i=1): return {"jsonrpc":"2.0","id":i,"method":"eth_call","params":[{"to":D,"data":d},"latest"]}
def W(h):
    h=h[2:] if h.startswith("0x") else h
    return [h[i:i+64] for i in range(0,len(h),64)]
u=lambda x:int(x,16)

dc=json.load(open("biddet_cache.json"))
live=[b for b in dc.values() if b["deletedAt"]==0]
provs=sorted({b["provider"] for b in live})
print("bidding providers:",len(provs))

out={}
for s in range(0,len(provs),3):
    ch=provs[s:s+3]
    res={x["id"]:x for x in rpc([call("0x55f21eb7"+p[2:].rjust(64,"0"),i) for i,p in enumerate(ch)])}
    for i,p in enumerate(ch):
        w=W(res[i]["result"]); b=u(w[0])//32
        eb=b+u(w[b])//32; n=u(w[eb])
        ep=bytes.fromhex("".join(w[eb+1:eb+1+(n+31)//32])[:n*2]).decode('utf8','replace') if n else ""
        out[p]=dict(addr=p,endpoint=ep,stake=u(w[b+1]),createdAt=u(w[b+2]),
                    limitPeriodEnd=u(w[b+3]),earned=u(w[b+4]),isDeleted=bool(u(w[b+5])))
json.dump(out,open("providers.json","w"),indent=1)

now=int(time.time())
rows=[]
for p,d in out.items():
    days=max((now-d["createdAt"])/86400.0,0.01)
    ps=[b["pps"] for b in live if b["provider"]==p]
    rows.append(dict(addr=p,stake=d["stake"]/1e18,earned=d["earned"]/1e18,
        days=days, perday=(d["earned"]/1e18)/days, nbids=len(ps),
        medprice=statistics.median(ps)*86400/1e18,
        headroom=(d["stake"]-d["earned"])/1e18,
        pct=100*d["earned"]/d["stake"] if d["stake"] else 0))
rows.sort(key=lambda r:-r["earned"])
print("\n%-14s %9s %11s %8s %8s %6s %10s %7s"%("provider","stake","EARNED","MOR/day","%ofcap","bids","medprice","age_d"))
for r in rows:
    print("%-14s %9.1f %11.2f %8.3f %7.1f%% %6d %10.2f %7.0f"%(
        r["addr"][:12],r["stake"],r["earned"],r["perday"],r["pct"],r["nbids"],r["medprice"],r["days"]))
tot=sum(r["earned"] for r in rows)
print("\ntotal earned by bidding providers: %.1f MOR"%tot)
print("providers with ZERO earnings: %d / %d"%(sum(1 for r in rows if r["earned"]==0),len(rows)))
print("providers at 100%% of cap    : %d"%sum(1 for r in rows if r["pct"]>=99.9))
json.dump(rows,open("earners.json","w"),indent=1)
