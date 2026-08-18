import json,time,os,subprocess,datetime
EP="https://base.drpc.org"
D="0x6aBE1d282f72B474E54527D93b979A4f64d3030a"
def rpc(batch):
    last=None
    for attempt in range(8):
        try:
            p=subprocess.run(["curl","-s","--max-time","60","-X","POST",EP,
                "-H","content-type: application/json","--data-binary","@-"],
                input=json.dumps(batch).encode(),capture_output=True)
            r=json.loads(p.stdout)
            if not isinstance(r,list): raise ValueError(str(r)[:160])
            if any('result' not in x for x in r): raise ValueError('partial '+str(r)[:160])
            return r
        except Exception as e:
            last=e; time.sleep(min(2*(attempt+1),15))
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

ids=json.load(open("model_ids.json"))
cache=json.load(open("models_cache.json")) if os.path.exists("models_cache.json") else {}
todo=[m for m in ids if m not in cache]
print("total %d, cached %d, todo %d"%(len(ids),len(cache),len(todo)),flush=True)
B=3
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
    if (s//B)%5==0: print("  %d/%d"%(min(s+B,len(todo)),len(todo)),flush=True)

models=[cache[m] for m in ids]
real=[m for m in models if m["tags"] and int(m["ipfs"],16)!=0]
print("\n=== %d of %d models have BOTH tags and a non-zero ipfsCID ==="%(len(real),len(models)))
for m in sorted(real,key=lambda x:-x["createdAt"]):
    d=datetime.datetime.fromtimestamp(m["createdAt"],datetime.UTC).strftime("%Y-%m-%d")
    print(f"{m['name'][:30]:30} {d} own={m['owner'][:10]} stk={m['stake']/1e18:>7.2f} {m['tags']}")
    print(f"   {m['id']}")
json.dump(real,open("models_real.json","w"),indent=1)
