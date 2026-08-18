import json,subprocess,time,os,itertools
D="0x6aBE1d282f72B474E54527D93b979A4f64d3030a"
EPS=[("https://mainnet.base.org",10),("https://base.drpc.org",3)]
cyc=itertools.cycle(range(len(EPS)))
def rpc(calls):
    last=None
    for a in range(12):
        i=next(cyc); url,lim=EPS[i]
        if len(calls)>lim:            # split to fit this endpoint
            mid=len(calls)//2
            return rpc(calls[:mid])+rpc(calls[mid:])
        try:
            p=subprocess.run(["curl","-s","--max-time","60","-X","POST",url,
                "-H","content-type: application/json","--data-binary","@-"],
                input=json.dumps(calls).encode(),capture_output=True)
            r=json.loads(p.stdout)
            if not isinstance(r,list): raise ValueError(str(r)[:100])
            if any("result" not in x for x in r): raise ValueError("partial")
            return r
        except Exception as e:
            last=e; time.sleep(min(1.2*(a+1),8))
    raise RuntimeError(last)
def mk(data,i): return {"jsonrpc":"2.0","id":i,"method":"eth_call","params":[{"to":D,"data":data},"latest"]}
def W(h):
    h=h[2:] if h.startswith("0x") else h
    return [h[i:i+64] for i in range(0,len(h),64)]

bounds=json.load(open("session_bounds.json")); bounds["0xd01c1b0eedbe341c409369177478f2eabbeee848"]=json.load(open("d01c_bounds.json"))
cache=json.load(open("sess_cache.json"))
def sess_ids(addr,off,lim):
    w=W(rpc([mk("0x87bced7d"+addr[2:].rjust(64,"0")+"%064x"%off+"%064x"%lim,0)])[0]["result"])
    o=int(w[0],16)//32; n=int(w[o],16)
    return ["0x"+w[o+1+i] for i in range(n)]

todo=[]
for a,b in bounds.items():
    if not b or not b.get("recent"): continue
    for sid in sess_ids(a,b["start"],b["recent"]):
        if sid not in cache: todo.append((a,sid))
print("sessions to fetch: %d (cached %d)"%(len(todo),len(cache)),flush=True)
B=10
done=0
for i in range(0,len(todo),B):
    ch=todo[i:i+B]
    try:
        res={x["id"]:x for x in rpc([mk("0x39b240bd"+sid[2:],j) for j,(_a,sid) in enumerate(ch)])}
    except Exception as e:
        print("  batch fail",str(e)[:50],flush=True); continue
    for j,(a,sid) in enumerate(ch):
        w=W(res[j]["result"])
        cache[sid]={"user":"0x"+w[1][24:],"bidId":"0x"+w[2],"stake":int(w[3],16)/1e18,
                    "withdrawn":int(w[6],16)/1e18,"openedAt":int(w[7],16),
                    "endsAt":int(w[8],16),"closedAt":int(w[9],16),"provider":a}
    done+=len(ch)
    if (i//B)%40==0:
        json.dump(cache,open("sess_cache.json","w"))
        print("  %d/%d"%(done,len(todo)),flush=True)
json.dump(cache,open("sess_cache.json","w"))
print("DONE cached=%d"%len(cache))
