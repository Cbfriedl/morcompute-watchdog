import json,subprocess,time,os,sys,urllib.parse,collections
BS="https://base.blockscout.com/api/v2"
def get(url,params):
    for a in range(6):
        try:
            u=url+"?"+urllib.parse.urlencode(params)
            p=subprocess.run(["curl","-s","--max-time","45",u],capture_output=True)
            return json.loads(p.stdout)
        except Exception:
            time.sleep(2*(a+1))
    raise RuntimeError("blockscout failed")

def all_txs(addr,maxpages=80):
    cache="txs_%s.json"%addr[:10]
    if os.path.exists(cache): return json.load(open(cache))
    out=[]; params={"filter":"from"}
    url="%s/addresses/%s/transactions"%(BS,addr)
    truncated=False
    for page in range(maxpages):
        d=get(url,params)
        items=d.get("items") or []
        out+=items
        np=d.get("next_page_params")
        if not np: break
        params={"filter":"from",**np}
        time.sleep(0.2)
    else:
        truncated=True
    json.dump({"txs":out,"truncated":truncated},open(cache,"w"))
    return {"txs":out,"truncated":truncated}

def history(addr):
    d=all_txs(addr); txs=d["txs"]
    regs=[]; claims=0; methods=collections.Counter()
    for t in txs:
        m=t.get("method") or "?"
        methods[m]+=1
        if m=="providerRegister":
            di=t.get("decoded_input") or {}
            amt=None
            for p in di.get("parameters") or []:
                if p["name"]=="amount_": amt=int(p["value"])/1e18
            regs.append((t["timestamp"][:19],amt,t.get("status"),t["hash"]))
        if m in ("claim","providerClaim"): claims+=1
    regs.sort()
    return regs,claims,methods,d["truncated"],len(txs)
