import re,json,collections,sys
import target as T
h=open('bidmarket.html').read()
bm=json.loads(re.search(r'const BM = (\{.*?\});\n',h,re.S).group(1))
byname={m['n']:m for m in bm['models']}
WANT=["deepseek-v4-flash","deepseek-v4-pro","Gemma-4-31b","glm-5.2","deepseek-v4-pro:web",
      "Claude Opus 4.7","venice-uncensored","deepseek-v4-flash:web","Claude Sonnet 4.6",
      "Kimi K3","DeepSeek V4 Flash 0731","gpt-oss-120b","llama-3.3-70b","MiniMax-M2.5",
      "text-embedding-bge-m3"]
out=[]
for n in WANT:
    m=byname.get(n)
    if not m: print("MISSING",n); continue
    try:
        r=T.analyse(m['id'], n, m.get('s10',0))
        out.append(r); print("done",n,flush=True)
    except Exception as e:
        print("FAIL",n,str(e)[:90],flush=True)
json.dump([{k:v for k,v in r.items()} for r in out], open('target_out.json','w'), indent=1)
