import json,collections,datetime,re,sys
sc=json.load(open('sess_cache.json'))
bd=json.load(open('biddet_full.json'))
h=open('bidmarket.html').read()
bm=json.loads(re.search(r'const BM = (\{.*?\});\n',h,re.S).group(1))
mid_by_name={m['n']:m['id'] for m in bm['models']}
def show(name):
    MID=mid_by_name[name]
    day=lambda t: datetime.datetime.fromtimestamp(t,datetime.UTC).strftime('%m-%d')
    tab=collections.defaultdict(collections.Counter); price={}
    for v in sc.values():
        b=bd.get(v['bidId'])
        if not b or b['modelId']!=MID: continue
        p=b['provider'][:8]; price[p]=round(b['pps']*86400/1e18,3)
        tab[day(v['openedAt'])][p]+=1
    provs=sorted(price,key=lambda p:price[p])
    print("\n=== %s"%name)
    print("%-6s"%"day"+"".join("%15s"%f"{p}@{price[p]}" for p in provs))
    for d in sorted(tab):
        print("%-6s"%d+"".join("%15d"%tab[d][p] for p in provs))
for n in sys.argv[1:]: show(n)
