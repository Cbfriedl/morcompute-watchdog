/* Shared chain-read + formatting helpers.
   Reads Base mainnet directly from the browser — no server, no key, nothing stored. */

export const ADDR    = "0x2f144F3b192A2d2D2384de7007EE2cAd943C601b";
export const DIAMOND = "0x6aBE1d282f72B474E54527D93b979A4f64d3030a";
export const MOR     = "0x7431aDa8a591C955a994a21710752EF9b882b8e3";

/* Rotate: every free Base endpoint throttles under light bursts, so a single
   URL fails intermittently. Same reason the router keeps a pool. */
const RPCS = ["https://base-rpc.publicnode.com","https://mainnet.base.org",
              "https://base.drpc.org","https://base.lava.build"];
let rr = 0;

export const pad = a => a.toLowerCase().replace("0x","").padStart(64,"0");
export const W = h => { h = h.startsWith("0x") ? h.slice(2) : h;
  return Array.from({length:Math.ceil(h.length/64)},(_,i)=>h.slice(i*64,(i+1)*64)); };

export async function rpc(method, params){
  let last;
  for (let i=0; i<RPCS.length*2; i++){
    const url = RPCS[rr++ % RPCS.length];
    try{
      const r = await fetch(url,{method:"POST",headers:{"content-type":"application/json"},
        body:JSON.stringify({jsonrpc:"2.0",id:1,method,params})});
      const j = await r.json();
      if (j.error) throw new Error(j.error.message||"rpc error");
      return j.result;
    }catch(e){ last = e; }
  }
  throw last || new Error("all RPCs failed");
}
export const call = (to,data) => rpc("eth_call",[{to,data},"latest"]);

export function decodeStr(w, base){
  const n = parseInt(w[base],16); if(!n) return "";
  const hex = w.slice(base+1, base+1+Math.ceil(n/32)).join("").slice(0,n*2);
  let s=""; for(let i=0;i<hex.length;i+=2) s+=String.fromCharCode(parseInt(hex.substr(i,2),16));
  return s;
}

/* ---------- formatting ---------- */
export const fmt = (n,d=2) => (n==null||!isFinite(n)) ? "—"
  : n.toLocaleString(undefined,{minimumFractionDigits:d,maximumFractionDigits:d});
export const int = n => (n==null||!isFinite(n)) ? "—" : Math.round(n).toLocaleString();
/* every MOR amount to 1dp; bid prices keep 2 — they are compared closely */
export const mor   = n => fmt(n,1);
export const price = n => fmt(n,2);
export const pct   = (n,d=1) => (n==null||!isFinite(n)) ? "—" : fmt(n,d)+"%";
export const short = a => !a ? "—" : a.slice(0,6)+"…"+a.slice(-4);
export const ago = ts => {
  if(!ts) return "—";
  const s = Date.now()/1000 - ts;
  if (s < 3600) return Math.round(s/60)+"m ago";
  if (s < 86400) return Math.round(s/3600)+"h ago";
  return Math.round(s/86400)+"d ago";
};
export const day = ts => !ts ? "—"
  : new Date(ts*1000).toLocaleDateString(undefined,{month:"2-digit",day:"2-digit",year:"2-digit"});

/* ---------- shared chrome ---------- */
export function nav(current){
  const pages = [["index.html","Market"],["models.html","Models"],["trends.html","Trends"]];
  return `<nav>` + pages.map(([h,t]) =>
    `<a href="${h}"${h===current?' aria-current="page"':''}>${t}</a>`).join("") + `</nav>`;
}

export function card(k,v,s,cat){
  return `<div class="card ${cat||''}"><span class="k">${k}</span>
    <span class="v num">${v}</span><span class="s">${s||""}</span></div>`;
}

/* Sortable table: click a th[data-k] to sort. Returns a render function so the
   caller keeps ownership of the row markup. */
export function sortable(tableEl, rows, renderRow, initialKey, initialDir){
  let key = initialKey, dir = initialDir ?? -1;
  const body = tableEl.querySelector("tbody");
  const draw = () => {
    const r = rows.slice().sort((a,b)=>{
      const x=a[key], y=b[key];
      if (x==null) return 1; if (y==null) return -1;
      return (typeof x === "string") ? dir*x.localeCompare(y) : dir*(x-y);
    });
    body.innerHTML = r.map(renderRow).join("");
    tableEl.querySelectorAll("th[data-k]").forEach(th=>{
      const on = th.dataset.k===key;
      th.style.color = on ? "var(--ink)" : "";
      th.dataset.arrow = on ? (dir<0?"▼":"▲") : "";
      if (!th.dataset.label) th.dataset.label = th.textContent.replace(/[▼▲]\s*$/,"").trim();
      th.textContent = th.dataset.label + (on ? (dir<0?" ▼":" ▲") : "");
    });
  };
  tableEl.querySelectorAll("th[data-k]").forEach(th=>{
    th.addEventListener("click",()=>{
      if (key===th.dataset.k) dir = -dir; else { key = th.dataset.k; dir = -1; }
      draw();
    });
  });
  draw();
  return draw;
}

/* MOR price, for the dollar columns. Decoration — never block a page on it. */
export async function morUsd(){
  try{
    const r = await fetch("https://api.dexscreener.com/latest/dex/tokens/"+MOR);
    const j = await r.json();
    const ps = (j.pairs||[]).filter(p=>(p.baseToken?.symbol||"").toUpperCase()==="MOR")
      .sort((a,b)=>(b.liquidity?.usd||0)-(a.liquidity?.usd||0));
    return ps.length ? {usd:parseFloat(ps[0].priceUsd), dex:ps[0].dexId,
                        liq:Math.round(ps[0].liquidity?.usd||0)} : null;
  }catch(e){ return null; }
}
