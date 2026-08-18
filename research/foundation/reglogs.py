import json,subprocess,time,os
T_REG="0x70abce74777b3838ae60a33a6b9a87d9d25532668fe4fea548554c55868579c0"
T_DEREG="0xf04091b4a187e321a42001e46961e45b6a75b203fc6fb766b7e05505f6080abb"
D="0x6aBE1d282f72B474E54527D93b979A4f64d3030a"
def curl(u):
    for a in range(6):
        try:
            p=subprocess.run(["curl","-s","--max-time","60",u],capture_output=True)
            return json.loads(p.stdout)
        except Exception: time.sleep(2*(a+1))
    raise RuntimeError("fail")
def logs(provider,topic=T_REG,frm=0):
    t1="0x"+provider[2:].lower().rjust(64,"0")
    u=("https://base.blockscout.com/api?module=logs&action=getLogs"
       "&fromBlock=%d&toBlock=latest&address=%s&topic0=%s&topic1=%s&topic0_1_opr=and"%(frm,D,topic,t1))
    d=curl(u)
    r=d.get("result")
    return r if isinstance(r,list) else []
def tx(h):
    c="txc"; os.makedirs(c,exist_ok=True)
    f=os.path.join(c,h+".json")
    if os.path.exists(f): return json.load(open(f))
    d=curl("https://base.blockscout.com/api/v2/transactions/"+h)
    json.dump(d,open(f,"w")); time.sleep(0.05); return d
def amount_of(h):
    d=tx(h)
    di=d.get("decoded_input") or {}
    if not di: return None,d.get("method")
    for p in di.get("parameters") or []:
        if p["name"]=="amount_": return int(p["value"])/1e18, di.get("method_call","")
    return None,di.get("method_call","")
