#!/usr/bin/env python3
"""Append newly-closed sessions to history.db.

getProviderSessions is append-only, so only entries past each provider's stored
index need fetching. Without this the census would keep reporting a 10-day window
that silently drifts backwards as the database ages.
"""
import json, os, sqlite3, subprocess, sys, time

DIAMOND="0x6aBE1d282f72B474E54527D93b979A4f64d3030a"
DB=os.environ.get("HISTORY_DB","history.db")
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
try:
    from rpc_endpoints import endpoints; RPCS=endpoints()
except Exception:
    RPCS=["https://base-rpc.publicnode.com","https://mainnet.base.org"]
_rr=[0]

def rpc(batch):
    for a in range(len(RPCS)*5):
        url=RPCS[_rr[0]%len(RPCS)]; _rr[0]+=1
        try:
            p=subprocess.run(["curl","-s","--max-time","50","-X","POST",url,
                "-H","content-type: application/json","--data-binary","@-"],
                input=json.dumps(batch).encode(),capture_output=True)
            r=json.loads(p.stdout)
            if isinstance(r,list) and all("result" in x for x in r): return r
        except Exception: pass
        time.sleep(min(0.4*(a+1),4))
    raise RuntimeError("rpc failed")
def call(d,i=1): return {"jsonrpc":"2.0","id":i,"method":"eth_call","params":[{"to":DIAMOND,"data":d},"latest"]}
def W(h):
    h=h[2:] if h.startswith("0x") else h
    return [h[i:i+64] for i in range(0,len(h),64)]

con=sqlite3.connect(DB); c=con.cursor()
c.execute("CREATE TABLE IF NOT EXISTS cursor(p INTEGER PRIMARY KEY, idx INTEGER)")
prov={a.lower():i for i,a in c.execute("SELECT id,addr FROM provider")}
model={m.lower():i for i,m in c.execute("SELECT id,mid FROM model")}
buyer={a.lower():i for i,a in c.execute("SELECT id,addr FROM buyer")}
bid2model={}
added=0
w=W(rpc([call("0xd5472642"+"%064x"%0+"%064x"%500)])[0]["result"])
o=int(w[0],16)//32; n=int(w[o],16)
for i in range(n):
    a="0x"+w[o+1+i][24:]
    pid=prov.get(a.lower())
    if pid is None:
        c.execute("INSERT INTO provider(addr) VALUES(?)",(a,)); pid=c.lastrowid; prov[a.lower()]=pid
    have=c.execute("SELECT idx FROM cursor WHERE p=?",(pid,)).fetchone()
    have=have[0] if have else c.execute("SELECT COUNT(*) FROM session WHERE p=?",(pid,)).fetchone()[0]
    try:
        sw=W(rpc([call("0x87bced7d"+a[2:].rjust(64,"0")+"%064x"%have+"%064x"%5000)])[0]["result"])
        so=int(sw[0],16)//32; sn=int(sw[so],16)
        ids=["0x"+sw[so+1+k] for k in range(sn)]
    except Exception:
        continue
    for j in range(0,len(ids),3):
        ch=ids[j:j+3]
        try: res={x["id"]:x for x in rpc([call("0x39b240bd"+s[2:],k) for k,s in enumerate(ch)])}
        except Exception: continue
        for k in range(len(ch)):
            try:
                r=W(res[k]["result"])
                cl=int(r[9],16) or int(r[8],16)
                op=int(r[7],16)
                if not cl or cl<=op: continue          # still running: no earnings yet
                bid="0x"+r[2]
                if bid not in bid2model:
                    bb=W(rpc([call("0x91704e1e"+bid[2:])])[0]["result"])
                    bid2model[bid]=("0x"+bb[1], int(bb[2],16))
                mid,pps=bid2model[bid]
                mi=model.get(mid.lower())
                if mi is None:
                    c.execute("INSERT INTO model(mid,name) VALUES(?,?)",(mid,mid[:14])); mi=c.lastrowid; model[mid.lower()]=mi
                ua="0x"+r[1][24:]
                ui=buyer.get(ua.lower())
                if ui is None:
                    c.execute("INSERT INTO buyer(addr) VALUES(?)",(ua,)); ui=c.lastrowid; buyer[ua.lower()]=ui
                c.execute("INSERT INTO session(p,m,u,t,dur,mor) VALUES(?,?,?,?,?,?)",
                          (pid,mi,ui,op,cl-op,int(pps*(cl-op)/1e9)))
                added+=1
            except Exception: pass
    c.execute("INSERT OR REPLACE INTO cursor(p,idx) VALUES(?,?)",(pid,have+len(ids)))
    con.commit()
print("appended %d sessions; total %d"%(added, c.execute("SELECT COUNT(*) FROM session").fetchone()[0]))
