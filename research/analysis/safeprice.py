#!/usr/bin/env python3
"""Safe rank-#1 price per model, using only what is measured.

From the router's own /bids/rated we know two things exactly:
  * on glm-5.2 every stat SD is 0, so tps=ttft=dur=0.5 for everyone. A
    zero-history provider there scored q=0.280  ->  w_tps+w_ttft+w_dur = 0.560
  * a zero-history provider gets success=0

We do NOT know how that 0.560 splits three ways, so take the worst case: assume
the whole 0.560 sits on whichever of our three features is lowest, and give
ourselves no credit for stake. That is a floor on our quality, so a price
derived from it wins under every possible weight split.
"""
import json, sys
import score as S, target as T

W3 = 0.560          # w_tps + w_ttft + w_dur, measured on glm-5.2
MARGIN = 0.97       # sit 3% under the price that exactly ties

def my_features(mid):
    ms = T.mstats(mid)
    tps  = S.norm_range( S.zidx(0, ms["tps"][0],  ms["tps"][1],  ms["count"]))
    ttft = S.norm_range(-S.zidx(0, ms["ttft"][0], ms["ttft"][1], ms["count"]))
    dur  = S.norm_range( S.zidx(0, ms["dur"][0],  ms["dur"][1],  ms["count"]))
    return tps, ttft, dur

def safe_price(mid, top_rival_score):
    tps, ttft, dur = my_features(mid)
    q_floor = W3 * min(tps, ttft, dur)
    if top_rival_score <= 0: return None, q_floor, (tps,ttft,dur)
    tie = q_floor / top_rival_score * 86400
    return tie * MARGIN, q_floor, (tps, ttft, dur)

if __name__ == "__main__":
    for mid, name, top in json.load(open(sys.argv[1])):
        p, q, f = safe_price(mid, top)
        print("%-24s q_floor=%.4f  feats=(%.2f,%.2f,%.2f)  topRival=%8.1f  -> bid %.4f MOR/day"
              % (name, q, f[0], f[1], f[2], top, p))
