import json,urllib.request,time
RPC="https://1rpc.io/base"
D="0x6aBE1d282f72B474E54527D93b979A4f64d3030a"
_id=[0]
def rpc(batch):
    _id[0]+=1
    req=urllib.request.Request(RPC,data=json.dumps(batch).encode(),
        headers={'content-type':'application/json',
                 'user-agent':'Mozilla/5.0 (X11; Linux x86_64) morcompute-scan/1.0',
                 'accept':'application/json'})
    for attempt in range(5):
        try:
            return json.loads(urllib.request.urlopen(req,timeout=45).read())
        except Exception as e:
            last=e
            time.sleep(2*(attempt+1))
    raise RuntimeError("rpc failed: %r"%last)
def call(data,i=1):
    return {"jsonrpc":"2.0","id":i,"method":"eth_call","params":[{"to":D,"data":data},"latest"]}
def words(h):
    h=h[2:] if h.startswith("0x") else h
    return [h[i:i+64] for i in range(0,len(h),64)]
def u(w): return int(w,16)
def dec_str(w,base):
    ln=u(w[base]); 
    if ln==0: return ""
    raw="".join(w[base+1:base+1+(ln+31)//32])
    return bytes.fromhex(raw[:ln*2]).decode('utf8','replace')

# 1. active model ids
r=rpc([call("0x3839d3dc"+"%064x"%0+"%064x"%500)])
w=words(r[0]["result"])
off=u(w[0])//32; n=u(w[off]); ids=["0x"+w[off+1+i] for i in range(n)]
total=u(w[1]) if len(w)>1 else n
print(f"active models returned: {n}  (total reported: {total})")
open("model_ids.json","w").write(json.dumps(ids))

# 2. getModel for each, in batches
models=[]
B=25
for s in range(0,len(ids),B):
    chunk=ids[s:s+B]
    batch=[call("0x21e7c498"+m[2:],i) for i,m in enumerate(chunk)]
    res=rpc(batch)
    res={x["id"]:x for x in res}
    for i,m in enumerate(chunk):
        x=res.get(i,{})
        if "result" not in x or not x["result"]: continue
        w=words(x["result"])
        b=u(w[0])//32
        ipfs=w[b]; fee=u(w[b+1]); stake=u(w[b+2]); owner="0x"+w[b+3][24:]
        name=dec_str(w,b+u(w[b+4])//32)
        tb=b+u(w[b+5])//32
        tn=u(w[tb]); tags=[dec_str(w,tb+1+u(w[tb+1+j])//32) for j in range(tn)]
        created=u(w[b+6]); deleted=u(w[b+7])
        models.append(dict(id=m,ipfs=ipfs,fee=fee,stake=stake,owner=owner,
                           name=name,tags=tags,createdAt=created,isDeleted=bool(deleted)))
    print(f"  fetched {min(s+B,len(ids))}/{len(ids)}",flush=True)
json.dump(models,open("models.json","w"),indent=1)
real=[m for m in models if m["tags"] and int(m["ipfs"],16)!=0]
print(f"\nmodels with tags AND non-zero ipfsCID: {len(real)} / {len(models)}")
for m in real:
    print(f"  {m['name'][:34]:34} owner={m['owner'][:10]} stake={m['stake']/1e18:>7.2f} tags={m['tags']}")
