import json,collections,datetime,re,statistics
sc=json.load(open('sess_cache.json'))
bd=json.load(open('biddet_full.json'))
h=open('bidmarket.html').read()
bm=json.loads(re.search(r'const BM = (\{.*?\});\n',h,re.S).group(1))
name={m['id']:m['n'] for m in bm['models']}

CUT=max(v['openedAt'] for v in sc.values())-4*86400   # last 4 days = current regime
per=collections.defaultdict(lambda: {"n":0,"dur":[],"mor":0.0,"provs":collections.Counter()})
for v in sc.values():
    b=bd.get(v['bidId'])
    if not b: continue
    if v['openedAt']<CUT: continue
    end=v.get('closedAt') or v.get('endsAt')
    d=max(0,(end or v['openedAt'])-v['openedAt'])
    p=per[b['modelId']]
    p["n"]+=1; p["dur"].append(d); p["mor"]+=b['pps']*d/1e18
    p["provs"][b['provider'][:8]]+=1
rows=[]
for mid,p in per.items():
    if p["n"]<8: continue
    days=4.0
    rows.append({"m":name.get(mid,mid[:10]),"mid":mid,"sd":p["n"]/days,
                 "med":statistics.median(p["dur"])/60,
                 "mord":p["mor"]/days, "per":p["mor"]/p["n"],
                 "who":p["provs"].most_common(3)})
rows.sort(key=lambda r:-r["mord"])
print("Last 4 days (the current pricing regime). MOR/day = what the winner actually took.")
print("%-26s %7s %8s %9s %9s  %s"%("model","sess/d","med min","MOR/sess","MOR/day","who is taking them"))
for r in rows:
    print("%-26s %7.1f %8.1f %9.4f %9.2f  %s"%(r["m"],r["sd"],r["med"],r["per"],r["mord"],
      " ".join(f"{a}:{c}" for a,c in r["who"])))
print("\nTOTAL MOR/day across these models: %.1f"%sum(r["mord"] for r in rows))
json.dump(rows,open('oppo.json','w'))
