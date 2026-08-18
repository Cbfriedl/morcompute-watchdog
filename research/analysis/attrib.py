import json,os,datetime,sesslib as S
CACHE="sess_cache.json"
cache=json.load(open(CACHE)) if os.path.exists(CACHE) else {}
bounds=json.load(open("session_bounds.json"))
d01=json.load(open("d01c_bounds.json"))
bounds["0xd01c1b0eedbe341c409369177478f2eabbeee848"]=d01
todo=[]
for a,b in bounds.items():
    if not b or not b.get("recent"): continue
    ids=S.sess_ids(a,b["start"],b["recent"])
    for s in ids:
        if s not in cache: todo.append((a,s))
print("sessions to fetch: %d (cached %d)"%(len(todo),len(cache)),flush=True)
B=3
for i in range(0,len(todo),B):
    ch=todo[i:i+B]
    try:
        res=S.sessions([s for _,s in ch])
    except Exception as e:
        print("batch fail",str(e)[:60],flush=True); continue
    for (a,sid),(sid2,parsed) in zip(ch,res):
        parsed["provider"]=a; cache[sid]=parsed
    if (i//B)%100==0:
        json.dump(cache,open(CACHE,"w")); print("  %d/%d"%(i,len(todo)),flush=True)
json.dump(cache,open(CACHE,"w"))
print("DONE cached=%d"%len(cache))
