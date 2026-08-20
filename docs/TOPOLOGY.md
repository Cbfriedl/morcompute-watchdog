# What runs where

Three places hold this system, and the split is deliberate: **the market census
is public, the operator's own position is not, and the node itself is neither.**
Anything that names which address belongs to this operator stays on the VPS.

```
                 VPS (Fedora, "MORProvider")
                 ├── morpheus-router  (Docker)  ← the actual provider node
                 ├── /root/morpheus/private/    ← private dashboard, tailnet only
                 └── /root/morpheus/census/     ← builds the public data, pushes it
                                │
                                │  publish.sh  (strips identity, stamps assets)
                                ▼
                 GitHub  Cbfriedl/morcompute-watchdog
                 └── Pages → cbfriedl.github.io/morcompute-watchdog  ← public census
                                ▲
                                │  status.json, ntfy alerts
                 GitHub Actions: watchdog.yml  ← external dead-man's switch
```

## 1. The VPS — the node and everything private

Reachable over SSH and over Tailscale. Nothing here is world-reachable except
the router's own provider port.

| Path | What it is |
|---|---|
| `morpheus-router` (Docker, `morpheus-router:7.5.0`) | The provider node. **Not a systemd service** — `systemctl restart` silently matches nothing; use `docker restart morpheus-router`. |
| `/root/morpheus/morpheus-data/models-config.json` | The models the router will serve, with the OpenRouter key. A bid on a model absent from this file wins sessions and fails every one of them. |
| `/root/morpheus/morpheus-data/.cookie` | Router API basic-auth credentials, used by every local script. |
| `/root/morpheus/provider.env` | `PROVIDER_ADDRESS`, chmod 600. The single place the operator's address lives. Referenced by units via `EnvironmentFile=`. |
| `/root/morpheus/private/` | The private dashboard's pages and feeds. |
| `/root/morpheus/census/` | Census and history build: `gen_census.py`, `refresh_history.py`, `rpc_endpoints.py`, `history.db`, `publish.sh`. |
| `/root/morpheus/census/repo/` | The git clone that pushes to GitHub. Disposable — `publish.sh` resets it onto `origin/main` every run. |
| `/root/morpheus/*.py` | `reputation.py`, `margin.py`, `reprice.py`, `snapshot.py`, `private_serve.py`. |

### Timers on the box

| Unit | Cadence | Does |
|---|---|---|
| `gen-census.timer` | daily, 05:18 UTC | `refresh_history.py` → `gen_census.py` → `publish.sh` |
| `private-refresh.timer` | every 15 min | `reputation.py`, `margin.py`, copies `census-full.json` into `private/`, pulls `status.json` from origin |
| `private-snapshot.timer` | hourly at :41 | `snapshot.py` → `private/snapshots.jsonl` (public RPC, costs nothing on the keyed plan) |
| `morpheus-monitor.timer` | — | on-box health and OpenRouter balance checks, alerts to ntfy |

### The keyed RPC

`rpc_endpoints.py` reads `HISTORY_RPC` or `.rpc-key`. **Only `gen_census.py` and
`refresh_history.py` use it.** The router must never point at it: the node alone
makes 5–16M requests a month, 130–400M CU, more than ten times the 30M free
allowance. `ETH_NODE_ADDRESS` stays unset. `snapshot.py` deliberately uses public
endpoints for the same reason.

## 2. GitHub — the public census

Repo `Cbfriedl/morcompute-watchdog`, served by Pages at
`cbfriedl.github.io/morcompute-watchdog`.

| Published | Built by |
|---|---|
| `index.html` `models.html` `addresses.html` `provider.html` | hand-written, four views |
| `assets/pub.css` `assets/pub.js` | shared; URLs content-hashed by `stamp.sh` |
| `census-full.json` | `gen_census.py` on the box, identity stripped by `publish.sh` |
| `providers-history.json` | `gen_provider_history.py`, collapsed from `history.db` |
| `history-daily.json` | the history build |
| `history.db` | committed for reproducibility; **never fetched by the browser** |
| `status.json` | `watchdog.yml` |

### The only workflow left

`watchdog.yml` — the external dead-man's switch. It is the one thing that can
detect the node being dead, which the node cannot report about itself. Runs in
~13s.

Two workflows were retired on 2026-08-20:

- **`snapshot.yml`** — appended position history and committed `snapshots.jsonl`.
  Once that file was gitignored as private, `git add` refused it and the job
  failed hourly. It was publishing personal position history to a public repo.
  Replaced by `private-snapshot.timer` on the box.
- **`census.yml`** — ran `census.py` against public RPC with
  `timeout-minutes: 110`, hit that ceiling and was cancelled on every run, ~2h of
  Actions time daily producing `census.json`, which nothing reads any more.

## 3. Private — the tailnet dashboard

`private_serve.py` binds to **`127.0.0.1:8090` and the tailnet address only,
never `0.0.0.0`**. Tabs: My node, Compare, Trends, Market, Models, Providers.

Gitignored and never published: `private/`, `margin.json`, `reputation.json`,
`snapshots.jsonl`.

## The identity rule

`gen_census.py` emits a `youAre` field naming this operator, because the private
dashboard needs it. **`publish.sh` strips that field at the publishing
boundary** — not by asking callers to unset a variable, because a future caller
will forget. Scripts read `PROVIDER_ADDRESS` from the environment with no
committed default.

The operator's address *does* appear inside `census-full.json`,
`providers-history.json` and `history.db` as one bidder among all the others.
That is public chain state; removing it would make the census wrong. What is
withheld is which of those addresses is this one.

## Asset cache-busting

The pages import `./assets/pub.js` as a static specifier. Pages serves it with
`cache-control: max-age=600`, and browsers additionally pin resolved ES modules
in a per-document module map, so a redeploy **does not reach an open tab** — the
HTML is new while the module inside it is the previous build, and nothing errors.
`stamp.sh` appends each asset's content hash to its URL, and `publish.sh` runs it
before staging. Verifying the server is not verifying what a browser runs.
