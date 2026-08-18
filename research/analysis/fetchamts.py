import json,os,sys,time,traceback
import reglogs as R
ADDRS=['0xd01c1b0eedbe341c409369177478f2eabbeee848',
       '0x5a373b605eb61693a54f1d471bfc048e05757baf',
       '0x249f75006cf302089f2f6c3a632580fc58ebe465',
       '0x6f93f1cbd3247e73d10ff1ad582a11bea327a865',
       '0x010208ec11f3a844dd2f5003a2807dde982ebb03']
out={}
for a in ADDRS:
    try:
        l=R.logs(a)
    except Exception as e:
        print('LOGS FAIL',a,e,flush=True); continue
    recs=[]
    for i,lg in enumerate(l):
        h=lg["transactionHash"]
        try:
            amt,call=R.amount_of(h)
        except Exception as e:
            amt,call=None,"ERR:%s"%e
        recs.append({"block":int(lg["blockNumber"],16),
                     "ts":int(lg["timeStamp"],16) if lg.get("timeStamp") else None,
                     "tx":h,"amount":amt,"call":call})
        if i%40==0: print("  %s %d/%d"%(a[:10],i,len(l)),flush=True)
    recs.sort(key=lambda r:r["block"])
    out[a]=recs
    json.dump(out,open("stake_history.json","w"),indent=1)
    print("done",a[:10],len(recs),flush=True)
print("ALL DONE", {k:len(v) for k,v in out.items()})
