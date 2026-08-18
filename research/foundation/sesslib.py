import json,subprocess,time,datetime
EP="https://base.drpc.org"; D="0x6aBE1d282f72B474E54527D93b979A4f64d3030a"
def rpc(batch):
    last=None
    for a in range(8):
        try:
            p=subprocess.run(["curl","-s","--max-time","60","-X","POST",EP,
                "-H","content-type: application/json","--data-binary","@-"],
                input=json.dumps(batch).encode(),capture_output=True)
            r=json.loads(p.stdout)
            if not isinstance(r,list): raise ValueError(str(r)[:120])
            if any("result" not in x for x in r): raise ValueError("partial "+str(r)[:120])
            return r
        except Exception as e:
            last=e; time.sleep(min(2*(a+1),12))
    raise RuntimeError(last)
def mk(data,i=1): return {"jsonrpc":"2.0","id":i,"method":"eth_call","params":[{"to":D,"data":data},"latest"]}
def W(r):
    h=r[2:] if r.startswith("0x") else r
    return [h[i:i+64] for i in range(0,len(h),64)]
def sess_ids(addr,off,lim):
    w=W(rpc([mk("0x87bced7d"+addr[2:].rjust(64,"0")+"%064x"%off+"%064x"%lim)])[0]["result"])
    o=int(w[0],16)//32; n=int(w[o],16)
    return ["0x"+w[o+1+i] for i in range(n)]
def parse(res):
    w=W(res)
    return {"user":"0x"+w[1][24:],"bidId":"0x"+w[2],"stake":int(w[3],16)/1e18,
            "withdrawn":int(w[6],16)/1e18,"openedAt":int(w[7],16),
            "endsAt":int(w[8],16),"closedAt":int(w[9],16)}
def sessions(sids):
    out=[]
    for i in range(0,len(sids),3):
        ch=sids[i:i+3]
        res={x["id"]:x for x in rpc([mk("0x39b240bd"+s[2:],j) for j,s in enumerate(ch)])}
        for j,s in enumerate(ch): out.append((s,parse(res[j]["result"])))
    return out
def opened_at(addr,idx):
    sid=sess_ids(addr,idx,1)
    if not sid: return None
    return sessions(sid)[0][1]["openedAt"]
def boundary(addr,total,cutoff):
    """first index whose openedAt >= cutoff (sessions are oldest->newest)"""
    lo,hi=0,total
    while lo<hi:
        mid=(lo+hi)//2
        t=opened_at(addr,mid)
        if t is None or t<cutoff: lo=mid+1
        else: hi=mid
    return lo
