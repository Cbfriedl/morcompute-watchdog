import json,subprocess,time,os,datetime
D="0x6aBE1d282f72B474E54527D93b979A4f64d3030a"
T_REG="0x70abce74777b3838ae60a33a6b9a87d9d25532668fe4fea548554c55868579c0"
BS="https://base.blockscout.com"
RPC="https://base.drpc.org"

def curl(u,tries=8):
    for a in range(tries):
        p=subprocess.run(["curl","-s","-L","--max-time","45","-H","accept: application/json",u],capture_output=True)
        try:
            d=json.loads(p.stdout)
            if isinstance(d,dict) and d.get("status")=="0": raise ValueError(d.get("message"))
            return d
        except Exception: time.sleep(1.2*(a+1))
    return None

def rpc_call(data,blk):
    for a in range(8):
        p=subprocess.run(["curl","-s","--max-time","45","-X","POST",RPC,
            "-H","content-type: application/json","--data-binary","@-"],
            input=json.dumps({"jsonrpc":"2.0","id":1,"method":"eth_call",
              "params":[{"to":D,"data":data},hex(blk)]}).encode(),capture_output=True)
        try:
            r=json.loads(p.stdout)
            if "result" in r: return r["result"]
        except Exception: pass
        time.sleep(1.2*(a+1))
    return None

def W(h):
    h=h[2:] if h.startswith("0x") else h
    return [h[i:i+64] for i in range(0,len(h),64)]

def earned_at(addr,blk):
    r=rpc_call("0x55f21eb7"+addr[2:].rjust(64,"0"),blk)
    if not r: return None
    w=W(r); b=int(w[0],16)//32
    return int(w[b+1],16)/1e18, int(w[b+4],16)/1e18   # stake, earned

def regs(addr):
    t1="0x"+addr[2:].lower().rjust(64,"0")
    d=curl("%s/api?module=logs&action=getLogs&fromBlock=0&toBlock=latest"
           "&address=%s&topic0=%s&topic1=%s&topic0_1_opr=and"%(BS,D,T_REG,t1))
    r=(d or {}).get("result")
    if not isinstance(r,list): return []
    return sorted([(int(x["blockNumber"],16), x["transactionHash"]) for x in r])

def outside(addr, log=print):
    """Outside capital >= max over time of (stake_at_block - earned_at_block).

    You cannot re-stake more than you have earned, so at every registration the
    excess of stake over cumulative earnings must have come from outside. The
    maximum of that difference is therefore a lower bound on contributed capital,
    and is exact whenever the operator re-stakes promptly.
    """
    rg=regs(addr)
    if not rg: return None,None,0
    if len(rg) > 45:
        head = rg[:25]
        tail = rg[25:][::max(1,(len(rg)-25)//20)]
        rg = head + tail
    best=0.0; best_blk=None
    for blk,_tx in rg:
        got=earned_at(addr,blk)
        if not got: continue
        stake,earned=got
        diff=stake-earned
        if diff>best: best, best_blk = diff, blk
    return round(best,4), best_blk, len(rg)
