import json,subprocess,time,os
BS="https://base.blockscout.com/api/v2"
DIA="0x6abe1d282f72b474e54527d93b979a4f64d3030a"
CACHE="outside_cache.json"

def get(u,tries=12):
    """Blockscout 500s intermittently; retry hard rather than accept a blank."""
    for a in range(tries):
        r=subprocess.run(["curl","-s","-L","--max-time","45","-H","accept: application/json",u],
                         capture_output=True)
        try:
            d=json.loads(r.stdout)
            if isinstance(d,dict) and "items" in d: return d
        except Exception: pass
        time.sleep(min(1.5*(a+1),10))
    return None

def outside(addr):
    tot_in=from_dia=0.0; pages=0; params="?type=ERC-20"
    url="%s/addresses/%s/token-transfers"%(BS,addr)
    while pages<50:
        d=get(url+params)
        if d is None: return None,None,None
        for t in d.get("items") or []:
            if ((t.get("token") or {}).get("symbol") or "").upper()!="MOR": continue
            if (t.get("to") or {}).get("hash","").lower()!=addr.lower(): continue
            v=int(((t.get("total") or {}).get("value")) or 0)/1e18
            tot_in+=v
            if (t.get("from") or {}).get("hash","").lower()==DIA: from_dia+=v
        np=d.get("next_page_params")
        if not np: break
        params="?type=ERC-20&"+"&".join("%s=%s"%(k,v) for k,v in np.items())
        pages+=1; time.sleep(0.2)
    return round(tot_in,4), round(from_dia,4), round(tot_in-from_dia,4)

bm=json.load(open("bymodel2.json"))
cache=json.load(open(CACHE)) if os.path.exists(CACHE) else {}
addrs=list(bm["pdetail"].keys())
todo=[a for a in addrs if a not in cache]
print("providers: %d | cached %d | to scan %d"%(len(addrs),len(cache),len(todo)),flush=True)
for i,a in enumerate(todo,1):
    ti,fd,ext=outside(a)
    if ext is None:
        print("  %2d/%d %s FAILED"%(i,len(todo),a[:12]),flush=True); continue
    cache[a]={"in":ti,"fromDiamond":fd,"outside":ext}
    json.dump(cache,open(CACHE,"w"))
    print("  %2d/%d %s  in %10.1f | from Diamond %10.1f | OUTSIDE %9.1f"%(i,len(todo),a[:12],ti,fd,ext),flush=True)
print("done: %d/%d resolved"%(len(cache),len(addrs)))
