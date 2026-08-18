import json,urllib.request,time,os,itertools
# deliberately excludes mainnet.base.org: the router depends on it, don't compete
EPS=["https://base.drpc.org"]
D="0x6aBE1d282f72B474E54527D93b979A4f64d3030a"
cyc=itertools.cycle(EPS)
def rpc(batch):
    last=None
    for attempt in range(10):
        ep=next(cyc)
        try:
            req=urllib.request.Request(ep,data=json.dumps(batch).encode(),
                headers={'content-type':'application/json',
                         'user-agent':'Mozilla/5.0 (X11; Linux x86_64) morcompute/1.0',
                         'accept':'application/json'})
            r=json.loads(urllib.request.urlopen(req,timeout=60).read())
            if not isinstance(r,list): raise ValueError(str(r)[:200])
            if any('result' not in x for x in r): raise ValueError('partial: '+str(r)[:200])
            return r
        except Exception as e:
            last=e; time.sleep(min(3*(attempt+1),20))
    raise RuntimeError("rpc failed: %r"%last)
def call(data,i=1): return {"jsonrpc":"2.0","id":i,"method":"eth_call","params":[{"to":D,"data":data},"latest"]}
def words(h):
    h=h[2:] if h.startswith("0x") else h
    return [h[i:i+64] for i in range(0,len(h),64)]
def u(w): return int(w,16)
def dec_str(w,base):
    ln=u(w[base])
    if ln==0: return ""
    raw="".join(w[base+1:base+1+(ln+31)//32])
    return bytes.fromhex(raw[:ln*2]).decode('utf8','replace')

ids=json.load(open("model_ids.json")) if os.path.exists("model_ids.json") else None
if not ids:
    w=words(rpc([call("0x3839d3dc"+"%064x"%0+"%064x"%500)])[0]["result"])
    off=u(w[0])//32; n=u(w[off]); ids=["0x"+w[off+1+i] for i in range(n)]
    json.dump(ids,open("model_ids.json","w"))
print("active model ids:",len(ids))

cache=json.load(open("models_cache.json")) if os.path.exists("models_cache.json") else {}
todo=[m for m in ids if m not in cache]
B=8
for s in range(0,len(todo),B):
    chunk=todo[s:s+B]
    res={x["id"]:x for x in rpc([call("0x21e7c498"+m[2:],i) for i,m in enumerate(chunk)])}
    for i,m in enumerate(chunk):
        w=words(res[i]["result"]); b=u(w[0])//32
        tb=b+u(w[b+5])//32; tn=u(w[tb])
        cache[m]=dict(id=m,ipfs=w[b],fee=u(w[b+1]),stake=u(w[b+2]),owner="0x"+w[b+3][24:],
            name=dec_str(w,b+u(w[b+4])//32),
            tags=[dec_str(w,tb+1+u(w[tb+1+j])//32) for j in range(tn)],
            createdAt=u(w[b+6]),isDeleted=bool(u(w[b+7])))
    json.dump(cache,open("models_cache.json","w"))
    print("  %d/%d"%(min(s+B,len(todo)),len(todo)),flush=True)

models=[cache[m] for m in ids]
real=[m for m in models if m["tags"] and int(m["ipfs"],16)!=0]
print("\n=== %d/%d models have BOTH tags and a non-zero ipfsCID ==="%(len(real),len(models)))
for m in sorted(real,key=lambda x:-x["createdAt"]):
    import datetime
    d=datetime.datetime.fromtimestamp(m["createdAt"],datetime.UTC).strftime("%Y-%m-%d")
    print(f"{m['name'][:32]:32} {d} owner={m['owner'][:12]} stake={m['stake']/1e18:>8.2f} {m['tags']}")
    print(f"   id={m['id']}")
json.dump(real,open("models_real.json","w"),indent=1)
