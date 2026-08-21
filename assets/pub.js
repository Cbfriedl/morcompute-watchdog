/* Shared helpers for the public census site.
   Deliberately holds no operator identity: this bundle ships to a public origin,
   so it must not name any particular provider address as "mine". Every address
   here is read out of the census as one row among many. */

export const DIAMOND = "0x6aBE1d282f72B474E54527D93b979A4f64d3030a";

/* MORDIEM Venice Models — one model, one bidder, at the 864 MOR/day ceiling.
   It is included in every headline total; these ids exist so its contribution
   can also be reported on its own. */
export const MORDIEM_ADDR  = "0xd01c1b0eedbe341c409369177478f2eabbeee848";
export const MORDIEM_MODEL = "0x847be4fc18c6f498bacda1c4ec85d4845e8e4d65718fc488d09b306c422e0f85";

export const CHOKE = 0.5;          /* MOR of headroom below which a provider cannot be paid */

/* ---------- formatting ---------- */
export const fmt = (v, d = 2) =>
  v == null || !isFinite(v) ? "—"
  : v.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
export const int = v => v == null || !isFinite(v) ? "—" : Math.round(v).toLocaleString("en-US");
export const pct = (v, d = 0) => v == null || !isFinite(v) ? "—" : fmt(v, d) + "%";
/* Addresses render as 0xHHHH...HHHH — first four and last four hex digits.
   A leading-prefix-only form collides across addresses that share a prefix. */
export const short = a => {
  const h = String(a || "").replace(/^0x/i, "");
  return h ? "0x" + h.slice(0, 4) + "..." + h.slice(-4) : "—";
};

/* MOR amounts span six orders of magnitude across this market, so a fixed
   precision is either noise at the top or all zeroes at the bottom. */
export const mor = v => v == null || !isFinite(v) ? "—"
  : Math.abs(v) >= 100000 ? int(v)
  : Math.abs(v) >= 100 ? fmt(v, 1)
  : Math.abs(v) >= 1 ? fmt(v, 2)
  : fmt(v, 4);
export const price = v => v == null || !isFinite(v) ? "—"
  : v >= 100 ? fmt(v, 1) : v >= 1 ? fmt(v, 2) : fmt(v, 4);

export const day = ts => !ts ? "—" : new Date(ts * 1000).toISOString().slice(0, 10);

/* ---------- chrome ---------- */
const PAGES = [
  ["index.html",     "Market overview"],
  ["models.html",    "Bids by model"],
  ["addresses.html", "Bids by address"],
  ["provider.html",  "Provider lookup"],
];
export function nav(current) {
  return `<nav class="tabs">` + PAGES.map(([h, t]) =>
    `<a href="${h}"${h === current ? ' aria-current="page"' : ""}>${t}</a>`).join("") + `</nav>`;
}
export const kpi = (k, v, sub, cat) =>
  `<div class="kpi ${cat || ""}"><span class="k">${k}</span>` +
  `<span class="v num">${v}</span><span class="sub">${sub || ""}</span></div>`;

export const esc = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ---------- census loader ---------- */
export async function census() {
  const r = await fetch("census-full.json?cb=" + Date.now());
  if (!r.ok) throw new Error("census-full.json " + r.status);
  return r.json();
}

/* ---------- tooltip ---------- */
let tipEl = null;
export function tip() {
  if (!tipEl) {
    tipEl = document.getElementById("tip")
      || document.body.appendChild(Object.assign(document.createElement("div"), { id: "tip" }));
  }
  return tipEl;
}
export function wireTip(root) {
  const el = tip();
  root.addEventListener("mousemove", e => {
    const t = e.target.closest("[data-tip]");
    if (!t) { el.style.opacity = 0; return; }
    el.innerHTML = t.getAttribute("data-tip");
    el.style.opacity = 1;
    const pad = 14, w = el.offsetWidth, h = el.offsetHeight;
    let x = e.clientX + pad, y = e.clientY + pad;
    if (x + w > innerWidth - 8) x = e.clientX - w - pad;
    if (y + h > innerHeight - 8) y = e.clientY - h - pad;
    el.style.left = x + "px"; el.style.top = y + "px";
  });
  root.addEventListener("mouseleave", () => { el.style.opacity = 0; });
}

/* ---------- sortable tables ---------- */
/* Sorts the caller's array in place and re-renders through its own draw fn, so
   the DOM is never the source of truth for ordering. */
export function sortable(table, rows, draw, initial, initialDir) {
  let key = initial, dir = initialDir || -1;
  const apply = () => {
    rows.sort((a, b) => {
      const x = a[key], y = b[key];
      if (x == null && y == null) return 0;
      if (x == null) return 1;            /* nulls always sink, either direction */
      if (y == null) return -1;
      if (typeof x === "string" || typeof y === "string")
        return dir * String(x).localeCompare(String(y), "en", { numeric: true });
      return dir * (x - y);
    });
    table.querySelectorAll("thead th[data-k]").forEach(th => {
      if (th.dataset.k === key) { th.dataset.dir = dir > 0 ? "asc" : "desc";
        const ar = th.querySelector(".ar"); if (ar) ar.textContent = dir > 0 ? "▲" : "▼"; }
      else { delete th.dataset.dir; const ar = th.querySelector(".ar"); if (ar) ar.textContent = "▲"; }
    });
    draw();
  };
  table.querySelectorAll("thead th[data-k]").forEach(th => {
    th.addEventListener("click", () => {
      const k = th.dataset.k;
      /* First click on a new column sorts descending for numbers (the
         interesting end) but ascending for names, which read alphabetically. */
      if (k === key) dir = -dir;
      else { key = k; dir = (k === "n" || k === "a") ? 1 : -1; }
      apply();
    });
  });
  apply();
  return { get key() { return key; }, resort: apply };
}

/* ---------- horizontal bar chart ---------- */
/* items: [{label, value, color, tipHtml, id}] — SVG, no library, so it inherits
   the page's tokens and needs no external request. */
export function hbar(items, opts = {}) {
  const w = opts.width || 900, rowH = opts.rowH || 26, padL = opts.padL || 132,
        padR = 74, padT = 8, padB = 26;
  const h = padT + items.length * rowH + padB;
  const max = Math.max(...items.map(d => d.value), 0) || 1;
  const x = v => padL + (v / max) * (w - padL - padR);
  const ticks = [0, .25, .5, .75, 1].map(f => f * max);
  let s = `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="${esc(opts.aria || "bar chart")}">`;
  ticks.forEach(t => {
    s += `<line class="gridline" x1="${x(t).toFixed(1)}" y1="${padT}" x2="${x(t).toFixed(1)}" y2="${padT + items.length * rowH}"/>`;
    s += `<text class="tick" x="${x(t).toFixed(1)}" y="${h - 8}" text-anchor="middle">${opts.fmtTick ? opts.fmtTick(t) : mor(t)}</text>`;
  });
  items.forEach((d, i) => {
    const y = padT + i * rowH, bh = rowH - 9;
    const bw = Math.max(x(d.value) - padL, d.value > 0 ? 2 : 0);
    s += `<text class="blab mono" x="${padL - 8}" y="${y + bh - 1}" text-anchor="end"` +
         (d.choked ? ` fill="var(--choke-ink)"` : ``) + `>${esc(d.label)}</text>`;
    s += `<rect class="bar" x="${padL}" y="${y}" width="${bw.toFixed(1)}" height="${bh}" rx="4"` +
         ` fill="${d.color}"${d.id ? ` data-prov="${esc(d.id)}" tabindex="0"` : ""}` +
         (d.tipHtml ? ` data-tip="${esc(d.tipHtml)}"` : "") + `/>`;
    s += `<text class="tick mono" x="${(padL + bw + 6).toFixed(1)}" y="${y + bh - 1}">${esc(d.right ?? mor(d.value))}</text>`;
  });
  s += `<line class="axis" x1="${padL}" y1="${padT + items.length * rowH}" x2="${w - padR}" y2="${padT + items.length * rowH}"/>`;
  return s + `</svg>`;
}

/* ---------- log-binned price histogram ---------- */
export function loghist(values, opts = {}) {
  const v = values.filter(x => x > 0);
  if (!v.length) return { svg: "", bins: [] };
  const lo = Math.log10(Math.min(...v)), hi = Math.log10(Math.max(...v));
  const NB = opts.bins || 16;
  const bins = Array.from({ length: NB }, (_, i) => ({
    lo: Math.pow(10, lo + (hi - lo) * i / NB),
    hi: Math.pow(10, lo + (hi - lo) * (i + 1) / NB), c: 0 }));
  v.forEach(x => {
    let i = Math.floor((Math.log10(x) - lo) / (hi - lo) * NB);
    if (i >= NB) i = NB - 1; if (i < 0) i = 0;
    bins[i].c++;
  });
  const w = 900, h = 260, padL = 44, padR = 12, padT = 10, padB = 40;
  const max = Math.max(...bins.map(b => b.c)) || 1;
  const bw = (w - padL - padR) / NB;
  const y = c => padT + (1 - c / max) * (h - padT - padB);
  let s = `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="Distribution of active bid prices">`;
  for (let i = 0; i <= 4; i++) {
    const yy = padT + (i / 4) * (h - padT - padB);
    s += `<line class="gridline" x1="${padL}" y1="${yy.toFixed(1)}" x2="${w - padR}" y2="${yy.toFixed(1)}"/>`;
    s += `<text class="tick" x="${padL - 6}" y="${(yy + 4).toFixed(1)}" text-anchor="end">${Math.round(max * (1 - i / 4))}</text>`;
  }
  bins.forEach((b, i) => {
    /* Band by price, not by index: the colour is a fact about the bucket. */
    const mid = Math.sqrt(b.lo * b.hi);
    const col = mid < 10 ? "var(--band-low)" : mid >= 100 ? "var(--band-high)" : "var(--band-mid)";
    const bh = padT + (h - padT - padB) - y(b.c);
    s += `<rect class="bar" x="${(padL + i * bw + 1).toFixed(1)}" y="${y(b.c).toFixed(1)}"` +
         ` width="${(bw - 2).toFixed(1)}" height="${Math.max(bh, b.c ? 1 : 0).toFixed(1)}" rx="3" fill="${col}"` +
         ` data-tip="<span class='t'>${price(b.lo)} – ${price(b.hi)} MOR/day</span><br>${b.c} bid${b.c === 1 ? "" : "s"}"/>`;
    if (i % 3 === 0)
      s += `<text class="tick" x="${(padL + i * bw + bw / 2).toFixed(1)}" y="${h - 22}" text-anchor="middle">${price(b.lo)}</text>`;
  });
  s += `<line class="axis" x1="${padL}" y1="${(padT + h - padT - padB).toFixed(1)}" x2="${w - padR}" y2="${(padT + h - padT - padB).toFixed(1)}"/>`;
  s += `<text class="tick" x="${(padL + (w - padL - padR) / 2).toFixed(1)}" y="${h - 4}" text-anchor="middle">bid price, MOR / day (log scale)</text>`;
  return { svg: s + `</svg>`, bins };
}
