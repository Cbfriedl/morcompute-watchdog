#!/usr/bin/env python3
"""All-time session counts per provider.

getProviderSessions returns the full append-only id list, so the count is one
call per provider — no need to read the sessions themselves. That is what makes
an all-time session card affordable at all; a per-model all-time breakdown
would mean reading every session and is not.
"""
import json, subprocess, sys, time

DIAMOND = "0x6aBE1d282f72B474E54527D93b979A4f64d3030a"
SEL_PROV_SESS = "0x87bced7d"
SEL_ACTIVE_PROVIDERS = "0xd5472642"
RPCS = ["https://mainnet.base.org", "https://base.drpc.org"]
_i = [0]

def rpc(batch):
    last = None
    for a in range(len(RPCS)*5):
        url = RPCS[_i[0] % len(RPCS)]; _i[0] += 1
        try:
            p = subprocess.run(["curl","-s","--max-time","60","-X","POST",url,
                "-H","content-type: application/json","--data-binary","@-"],
                input=json.dumps(batch).encode(), capture_output=True)
            r = json.loads(p.stdout)
            if not isinstance(r, list): raise ValueError(str(r)[:150])
            if any("result" not in x for x in r): raise ValueError("partial "+str(r)[:150])
            return r
        except Exception as e:
            last = e; time.sleep(min(1.0*(a+1), 6))
    raise RuntimeError("rpc failed: %r" % last)

def call(data, i=1):
    return {"jsonrpc":"2.0","id":i,"method":"eth_call",
            "params":[{"to":DIAMOND,"data":data},"latest"]}

def W(h):
    h = h[2:] if h.startswith("0x") else h
    return [h[i:i+64] for i in range(0,len(h),64)]

def sess_ids(addr, start=0, limit=100000):
    w = W(rpc([call(SEL_PROV_SESS + addr[2:].lower().rjust(64,"0")
                    + "%064x"%start + "%064x"%limit)])[0]["result"])
    off = int(w[0],16)//32
    n = int(w[off],16)
    return ["0x"+w[off+1+i] for i in range(n)]

if __name__ == "__main__":
    provs = json.load(open(sys.argv[1])) if len(sys.argv)>1 else None
    if not provs:
        w = W(rpc([call(SEL_ACTIVE_PROVIDERS + "%064x"%0 + "%064x"%500)])[0]["result"])
        off = int(w[0],16)//32; n = int(w[off],16)
        provs = ["0x"+w[off+1+i][24:] for i in range(n)]
    out = {}
    for a in provs:
        try:
            ids = sess_ids(a)
            out[a] = len(ids)
        except Exception as e:
            print("  fail %s %s" % (a, str(e)[:60]), flush=True)
            out[a] = None
        print("%s %s" % (a, out[a]), flush=True)
    json.dump(out, open("sess_alltime.json","w"), indent=1)
    tot = sum(v for v in out.values() if v)
    print("providers %d  all-time sessions %d" % (len(out), tot))
