#!/usr/bin/env python3
"""Bring sess_cache.json up to the current block.

getProviderSessions is append-only and ordered oldest->newest, so for each
provider we only need the ids past what we already hold. That turns a
multi-hour rescan into a few hundred calls.
"""
import json, os, collections
import score as S

SEL_PS="0x87bced7d"; SEL_GS="0x39b240bd"
sc=json.load(open('sess_cache.json'))
alltime=json.load(open('sess_alltime.json'))
have=collections.Counter(v['provider'] for v in sc.values())
added=0
for a,total in sorted(alltime.items(), key=lambda kv:-(kv[1] or 0)):
    if not total: continue
    # only walk the tail: everything after what we already have for this provider
    start=max(0, total-400)          # last 400 ids is >> 3 days for every provider here
    try:
        w=S.W(S.rpc([S.call(SEL_PS+a[2:].lower().rjust(64,"0")+"%064x"%start+"%064x"%400)])[0]["result"])
        off=int(w[0],16)//32; n=int(w[off],16)
        ids=["0x"+w[off+1+i] for i in range(n)]
    except Exception as e:
        print("  ids fail",a[:10],str(e)[:50]); continue
    new=[s for s in ids if s not in sc]
    got=0
    for i in range(0,len(new),3):
        ch=new[i:i+3]
        try:
            res={x["id"]:x for x in S.rpc([S.call(SEL_GS+s[2:],j) for j,s in enumerate(ch)])}
        except Exception: continue
        for j,sid in enumerate(ch):
            try:
                r=S.W(res[j]["result"])
                sc[sid]={"user":"0x"+r[1][24:], "bidId":"0x"+r[2],
                         "stake":int(r[3],16)/1e18, "withdrawn":int(r[6],16)/1e18,
                         "openedAt":int(r[7],16), "endsAt":int(r[8],16),
                         "closedAt":int(r[9],16), "provider":a.lower()}
                got+=1; added+=1
            except Exception: pass
    if got: print("  %s +%d"%(a[:10],got), flush=True)
json.dump(sc,open('sess_cache.json','w'))
print("added %d, cache now %d"%(added,len(sc)))
