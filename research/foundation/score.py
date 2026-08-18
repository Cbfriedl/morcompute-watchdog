#!/usr/bin/env python3
"""Replicate proxy-router's bid scoring for a model, from chain state.

score = (w_tps*tps + w_ttft*ttft + w_dur*dur + w_succ*succ + w_stake*stake) / price

The quality terms are z-scores of the provider's stats against the model's
cross-provider stats; price is a straight DIVISOR, which is why it dominates.
Source: proxy-router/internal/rating/{scorer_default,common}.go
"""
import json, math, subprocess, sys, time

D = "0x6aBE1d282f72B474E54527D93b979A4f64d3030a"
RPCS = ["https://mainnet.base.org", "https://base.drpc.org"]
_i = [0]

def rpc(batch):
    last = None
    for a in range(len(RPCS)*6):
        url = RPCS[_i[0] % len(RPCS)]; _i[0] += 1
        try:
            p = subprocess.run(["curl","-s","--max-time","50","-X","POST",url,
                "-H","content-type: application/json","--data-binary","@-"],
                input=json.dumps(batch).encode(), capture_output=True)
            r = json.loads(p.stdout)
            if not isinstance(r, list): raise ValueError(str(r)[:120])
            if any("result" not in x for x in r): raise ValueError("partial "+str(r)[:120])
            return r
        except Exception as e:
            last = e; time.sleep(min(a+1, 5))
    raise RuntimeError(last)

def call(data, i=1):
    return {"jsonrpc":"2.0","id":i,"method":"eth_call","params":[{"to":D,"data":data},"latest"]}

def W(h):
    h = h[2:] if h.startswith("0x") else h
    return [h[i:i+64] for i in range(0,len(h),64)]

def i64(w):
    v = int(w,16)
    return v - (1<<256) if v >= (1<<255) else v

def model_stats(mid):
    w = W(rpc([call("0xce535723"+mid[2:])])[0]["result"])
    # ModelStats{ SD tps{mean,sqSum}; SD ttft; SD dur; uint32 count } -> 7 words
    return {"tps":(i64(w[0]),i64(w[1])), "ttft":(i64(w[2]),i64(w[3])),
            "dur":(i64(w[4]),i64(w[5])), "count":int(w[6],16)}

def pm_stats(mid, addr):
    w = W(rpc([call("0x1b26c116"+mid[2:]+addr[2:].lower().rjust(64,"0"))])[0]["result"])
    # ProviderModelStats{ SD tps; SD ttft; uint32 totalDuration; uint32 successCount; uint32 totalCount }
    return {"tps":i64(w[0]), "ttft":i64(w[2]), "dur":int(w[4],16),
            "succ":int(w[5],16), "total":int(w[6],16)}

def sd(sqsum, n):
    if n <= 1: return 0.0
    v = sqsum/(n-1)
    return math.sqrt(v) if v > 0 else 0.0

def cut01(x): return max(0.0, min(1.0, x))
def norm_range(z, r=3.0): return cut01((z + r)/(2*r))
def zidx(pm_mean, m_mean, m_sqsum, n):
    s = sd(m_sqsum, n)
    return 0.0 if s == 0 else (pm_mean - m_mean)/s
def minmax(v, lo, hi): return 0.0 if hi==lo else cut01((v-lo)/(hi-lo))

def score(pm, ms, price_wei, stake_wei, minstake_wei, w):
    tps  = norm_range(zidx(pm["tps"],  ms["tps"][0],  ms["tps"][1],  ms["count"]))
    ttft = norm_range(-zidx(pm["ttft"], ms["ttft"][0], ms["ttft"][1], ms["count"]))
    dur  = norm_range(zidx(pm["dur"],  ms["dur"][0],  ms["dur"][1],  ms["count"]))
    succ = (pm["succ"]/pm["total"])**2 if pm["total"] else 0.0
    stk  = minmax(stake_wei, minstake_wei, 10*minstake_wei)
    q = w["tps"]*tps + w["ttft"]*ttft + w["duration"]*dur + w["success"]*succ + w["stake"]*stk
    return q/(price_wei/1e18), dict(tps=tps, ttft=ttft, dur=dur, succ=succ, stake=stk, q=q)
