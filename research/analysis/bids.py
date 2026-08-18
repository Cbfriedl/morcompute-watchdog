import json,time,os,subprocess,datetime,sys
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
def call(data,i=1): return {"jsonrpc":"2.0","id":i,"method":"eth_call","params":[{"to":D,"data":data},"latest"]}
def W(h):
    h=h[2:] if h.startswith("0x") else h
    return [h[i:i+64] for i in range(0,len(h),64)]
u=lambda x:int(x,16)

ids=json.load(open("model_ids.json"))
bc=json.load(open("bidids_cache.json")) if os.path.exists("bidids_cache.json") else {}
todo=[m for m in ids if m not in bc]
print("scanning active bids for %d models (%d cached)"%(len(todo),len(bc)),flush=True)
for s in range(0,len(todo),3):
    ch=todo[s:s+3]
    res={x["id"]:x for x in rpc([call("0x8a683b6e"+m[2:]+"%064x"%0+"%064x"%50,i) for i,m in enumerate(ch)])}
    for i,m in enumerate(ch):
        w=W(res[i]["result"]); off=u(w[0])//32; n=u(w[off])
        bc[m]=["0x"+w[off+1+j] for j in range(n)]
    json.dump(bc,open("bidids_cache.json","w"))
    if (s//3)%20==0: print("  %d/%d"%(min(s+3,len(todo)),len(todo)),flush=True)

allb=sorted({b for v in bc.values() for b in v})
print("\nTOTAL ACTIVE BIDS ACROSS ALL %d MODELS: %d"%(len(ids),len(allb)),flush=True)
json.dump(bc,open("bidids_cache.json","w"))

# fetch bid details
dc=json.load(open("biddet_cache.json")) if os.path.exists("biddet_cache.json") else {}
todo=[b for b in allb if b not in dc]
for s in range(0,len(todo),3):
    ch=todo[s:s+3]
    res={x["id"]:x for x in rpc([call("0x91704e1e"+b[2:],i) for i,b in enumerate(ch)])}
    for i,b in enumerate(ch):
        w=W(res[i]["result"])
        dc[b]=dict(bidId=b,provider="0x"+w[0][24:],modelId="0x"+w[1],
                   pps=u(w[2]),nonce=u(w[3]),createdAt=u(w[4]),deletedAt=u(w[5]))
    json.dump(dc,open("biddet_cache.json","w"))
print("bid details fetched:",len(dc))
