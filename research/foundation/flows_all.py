"""Outside capital for every provider, in one pass.

Claims arrive from the treasury multisig; anything else inbound is capital the
operator supplied. eth_getLogs accepts an array in a topic position (an OR), so
all providers can be captured in a single scan rather than one scan each.

Replaces the earlier max(stake - earned) heuristic, which was only a LOWER bound
and undershot whenever fresh capital arrived after earnings had begun.
"""
import json,subprocess,time,os,collections
MOR="0x7431aDa8a591C955a994a21710752EF9b882b8e3"
TR="0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
TREASURY="0x5160c0311a95e0a1072fa85df23712a7ba1cd4b1"
EPS=["https://base.drpc.org","https://mainnet.base.org"]
_i=[0]
def rpc(method,params):
    last=None
    for a in range(10):
        url=EPS[_i[0]%len(EPS)]; _i[0]+=1
        try:
            p=subprocess.run(["curl","-s","--max-time","55","-X","POST",url,
                "-H","content-type: application/json","--data-binary","@-"],
                input=json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params}).encode(),
                capture_output=True)
            r=json.loads(p.stdout)
            if "error" in r: raise RuntimeError(str(r["error"])[:70])
            return r["result"]
        except Exception as e:
            last=e; time.sleep(min(0.8*(a+1),5))
    raise RuntimeError(last)

bm=json.load(open("bymodel2.json"))
provs=[a.lower() for a in bm["pdetail"].keys()]
pad={("0x"+a[2:].rjust(64,"0")):a for a in provs}
topics_in=list(pad.keys())

CACHE="flows_all.json"
st=json.load(open(CACHE)) if os.path.exists(CACHE) else {"cursor":None,"in":{},"claims":{}}
head=int(rpc("eth_blockNumber",[]),16)
start=st["cursor"] or (head-int(160*86400/2))    # 160 days covers the oldest provider
STEP=9000
inflow=collections.defaultdict(lambda: collections.Counter())
for a,d in (st["in"] or {}).items():
    for k,v in d.items(): inflow[a][k]=v
claims=collections.Counter(st["claims"] or {})

blk=start; n=0; t0=time.time()
while blk<head and time.time()-t0<520:
    to_blk=min(blk+STEP-1,head)
    try:
        logs=rpc("eth_getLogs",[{"address":MOR,"fromBlock":hex(blk),"toBlock":hex(to_blk),
                                 "topics":[TR,None,topics_in]}])
    except Exception:
        blk=to_blk+1; continue
    for lg in logs:
        to_a=pad.get(lg["topics"][2]); frm="0x"+lg["topics"][1][26:]
        if not to_a: continue
        v=int(lg["data"],16)/1e18
        if frm==TREASURY: claims[to_a]+=v
        else: inflow[to_a][frm]+=v
    blk=to_blk+1; n+=1
    if n%25==0:
        json.dump({"cursor":blk,"in":{k:dict(v) for k,v in inflow.items()},"claims":dict(claims)},open(CACHE,"w"))
        print("  %.0f%%"%(100*(blk-start)/(head-start)),flush=True)
json.dump({"cursor":blk,"in":{k:dict(v) for k,v in inflow.items()},"claims":dict(claims)},open(CACHE,"w"))
done = blk>=head
print("scanned %.0f%% of window%s"%(100*(blk-start)/(head-start), "  COMPLETE" if done else "  (resumable)"))
print("providers with external inflow: %d"%len(inflow))
for a in sorted(inflow, key=lambda x:-sum(inflow[x].values()))[:6]:
    print("   %s  outside %9.1f   claimed %9.1f"%(a[:12],sum(inflow[a].values()),claims.get(a,0)))
