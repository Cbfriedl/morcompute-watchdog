import json,subprocess,time,os,sys
DIAMOND="0x6abe1d282f72b474e54527d93b979a4f64d3030a"
BS="https://base.blockscout.com/api/v2"
def get(url,params=None):
    for a in range(6):
        try:
            u=url
            if params: 
                import urllib.parse
                u=url+"?"+urllib.parse.urlencode(params)
            p=subprocess.run(["curl","-s","--max-time","45",u],capture_output=True)
            return json.loads(p.stdout)
        except Exception as e:
            time.sleep(2*(a+1))
    raise RuntimeError("blockscout failed")

def transfers(addr):
    """All ERC-20 MOR transfers touching addr, paginated."""
    out=[]; params={"type":"ERC-20"}
    url="%s/addresses/%s/token-transfers"%(BS,addr)
    for page in range(60):
        d=get(url,params)
        items=d.get("items") or []
        out+=items
        np=d.get("next_page_params")
        if not np: break
        params={"type":"ERC-20",**np}
        time.sleep(0.25)
    return out

def summarize(addr,label=""):
    cache="tx_%s.json"%addr[:10]
    if os.path.exists(cache):
        ts=json.load(open(cache))
    else:
        ts=transfers(addr); json.dump(ts,open(cache,"w"))
    a=addr.lower()
    ev=[]
    for t in ts:
        tok=(t.get("token") or {}).get("symbol")
        if tok!="MOR": continue
        f=t["from"]["hash"].lower(); to=t["to"]["hash"].lower()
        dec=int((t.get("total") or {}).get("decimals") or 18)
        v=int((t.get("total") or {}).get("value") or 0)/10**dec
        if f==a and to==DIAMOND: ev.append((t["timestamp"],"STAKE_IN",v))
        elif f==DIAMOND and to==a: ev.append((t["timestamp"],"FROM_DIAMOND",v))
    ev.sort()
    return ev,len(ts)
