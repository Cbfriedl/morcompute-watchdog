import json,subprocess,time,os,collections
MOR="0x7431aDa8a591C955a994a21710752EF9b882b8e3"
TR="0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
A="0x010208ec11f3a844dd2f5003a2807dde982ebb03"
PAD="0x"+A[2:].rjust(64,"0")
EPS=["https://base.drpc.org","https://mainnet.base.org"]
i=[0]
def rpc(method,params):
    last=None
    for a in range(10):
        url=EPS[i[0]%len(EPS)]; i[0]+=1
        try:
            p=subprocess.run(["curl","-s","--max-time","50","-X","POST",url,
                "-H","content-type: application/json","--data-binary","@-"],
                input=json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params}).encode(),
                capture_output=True)
            r=json.loads(p.stdout)
            if "error" in r: raise RuntimeError(str(r["error"])[:80])
            return r["result"]
        except Exception as e:
            last=e; time.sleep(min(1.0*(a+1),6))
    raise RuntimeError(last)

CACHE="flows_cache.json"
st=json.load(open(CACHE)) if os.path.exists(CACHE) else {"cursor":None,"in":{},"out":{}}
head=int(rpc("eth_blockNumber",[]),16)
# registered 2026-06-25; ~2s blocks over 54 days
start = st["cursor"] or (head - int(54*86400/2))
STEP=10000
inn=collections.Counter(st["in"]); out=collections.Counter(st["out"])
blk=start; n=0
t0=time.time()
while blk < head and time.time()-t0 < 500:
    to_blk=min(blk+STEP-1, head)
    for topic_pos,bucket in ((2,inn),(1,out)):
        f={"address":MOR,"fromBlock":hex(blk),"toBlock":hex(to_blk),
           "topics":[TR,None,None]}
        f["topics"][topic_pos]=PAD
        try:
            logs=rpc("eth_getLogs",[f])
        except Exception:
            continue
        for lg in logs:
            other="0x"+lg["topics"][1 if topic_pos==2 else 2][26:]
            bucket[other]+=int(lg["data"],16)/1e18
    blk=to_blk+1; n+=1
    if n%25==0:
        st={"cursor":blk,"in":dict(inn),"out":dict(out)}
        json.dump(st,open(CACHE,"w"))
        print("  scanned to %d (%.0f%%)"%(blk,100*(blk-start)/(head-start)),flush=True)
st={"cursor":blk,"in":dict(inn),"out":dict(out)}
json.dump(st,open(CACHE,"w"))
print("cursor %d / head %d  (%.0f%% of window)"%(blk,head,100*(blk-start)/(head-start)))
print("  MOR IN  total %.1f"%sum(inn.values()))
for k,v in inn.most_common(4): print("     from %s %10.1f"%(k[:14],v))
print("  MOR OUT total %.1f"%sum(out.values()))
for k,v in out.most_common(4): print("     to   %s %10.1f"%(k[:14],v))
