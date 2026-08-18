import re,json,collections
import target as T
h=open('bidmarket.html').read()
bm=json.loads(re.search(r'const BM = (\{.*?\});\n',h,re.S).group(1))
# every model that actually saw a session in the 10-day window
dem=collections.Counter()
for a,d in bm['pdetail'].items():
    for r in d.get('rows',[]):
        if r.get('s'): dem[r['n']]+=r['s']
byname={m['n']:m for m in bm['models']}
out=[]
for n,s in dem.most_common():
    m=byname.get(n)
    if not m: continue
    try:
        out.append(T.analyse(m['id'], n, s)); print("ok",n,s,flush=True)
    except Exception as e: print("fail",n,str(e)[:60],flush=True)
json.dump(out, open('all_out.json','w'))
print("DONE",len(out))
