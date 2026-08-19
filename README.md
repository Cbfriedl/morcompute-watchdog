# morcompute-watchdog

External dead-man's switch for a Morpheus compute provider node.

It exists to detect the one failure the node cannot report about itself:
**the node being dead.** A monitor that lives only on the box cannot tell you
the box has stopped.

Every 15 minutes, GitHub Actions:

1. reads the provider's state from Base mainnet via public RPC
   (stake, `limitPeriodEarned`, headroom, active flag, on-chain endpoint), and
2. opens a TCP connection to the provider's public port the way a paying
   customer's client would.

If the provider is inactive, headroom falls below threshold, or the endpoint is
unreachable, it pushes an alert to [ntfy](https://ntfy.sh) and fails the run.

## Why headroom matters

Morpheus caps a provider's annual earnings at its stake. Once
`limitPeriodEarned` reaches `stake`, the node **keeps serving inference and
keeps paying its upstream provider while earning nothing** — no error, no log
line, no on-chain event. That failure is invisible by construction, which is
why it is monitored externally.

## The public census site

Published from this repo with GitHub Pages:
**https://cbfriedl.github.io/morcompute-watchdog/**

Four views over the Morpheus provider market on Base mainnet, all client-side —
no server, no build step, no external requests:

| Page | Question it answers |
|---|---|
| `index.html` | Market overview — who earns, who pays, what prices look like |
| `models.html` | Bids by model — demand and value paid per model, with the price spread |
| `addresses.html` | Bids by address — every bidder's stake, headroom and realised earnings |
| `provider.html` | Provider lookup — one address in full, deep-linkable as `provider.html#0x…` |

### Data files the pages read

| File | Built by | Contents |
|---|---|---|
| `census-full.json` | `watchdog/gen_census.py` | live bids, per-model prices and demand, per-provider stake/headroom |
| `providers-history.json` | `research/foundation/gen_provider_history.py` | per-provider lifetime and windowed totals, per-model breakdown, buyer concentration |
| `history-daily.json` | the history build | one row per day: sessions, MOR, providers earning |
| `history.db` | `research/foundation/` | the complete session history, 318k rows — the source the two aggregates are derived from |

`history.db` is committed for reproducibility but is **not** fetched by the
browser. Everything the site displays comes from the two aggregates, which
together are under 250 KB.

### Two conventions the numbers depend on

**Self-dealt volume is excluded from value.** One address is both the provider
and the buyer on every session it books, at the protocol's maximum price. Left
in, it is 91% of the last ten days' MOR and swamps every other figure. It is
still shown — in the earnings chart, in the address table, and in its own
provider page — but it is subtracted from anything labelled *value paid*, and
the amount removed is displayed beside the total rather than hidden.

**MOR earned is gross, not received.** It is `pricePerSecond × billable
duration` summed over closed sessions. About 44% of sessions were never
withdrawn, so this is what providers earned, not what landed in their wallet.
Sessions still open are excluded entirely rather than counted as zero.

## Operator identity is not in this repo

The scripts here run against one specific provider, but that address is read
from the `PROVIDER_ADDRESS` environment variable and is deliberately absent from
every committed file. On the node it lives in `/root/morpheus/provider.env`,
referenced by the systemd units through `EnvironmentFile=`.

`watchdog/publish.sh` strips the census's `youAre` field at the publishing
boundary, so a census built locally with the variable set cannot carry it
upstream even if a future caller forgets. The operator's address still appears
inside `census-full.json` and `providers-history.json` as *one bidder among
all the others* — that is public chain state and removing it would make the
census wrong.


## Configuration

Repository **secret**:

| Name | Value |
|---|---|
| `NTFY_TOPIC` | ntfy topic to alert to |

Repository **variables**:

| Name | Value |
|---|---|
| `PROVIDER_ADDRESS` | provider wallet address |
| `PROVIDER_ENDPOINT` | `host:port`, used only as a fallback if the chain read fails |

This repo holds **no credential belonging to the provider host**. The provider
address and endpoint are already permanently public in on-chain history, so
publishing them here costs no additional exposure.

## Caveats

- GitHub delays scheduled workflows by several minutes, and **disables cron
  after 60 days of repository inactivity**. Budget for both.
- This is the *external* layer only. The push layer (thresholds, container
  health, model health) runs on the box itself.

## The operator's own dashboard is private

The node's own status — stake, headroom, wallet gas, per-model rank and
reputation, capture rate, inference cost and margin — is **not** part of the
public site and is not served from this repo.

It runs on the box itself, bound to a Tailscale tailnet address and loopback,
never to `0.0.0.0`, so it is reachable from the operator's own devices and from
nowhere else. `watchdog/private_serve.py` and `watchdog/private-dash.service`
are committed here because they are part of the system's shape, but the pages
they serve and the feeds they read (`margin.json`, `reputation.json`) are
gitignored and stay on the node.

That separation is deliberate: the census is a public good and is more useful
the more people read it, while a single operator's cost basis, margin and
capture rate are not. Nothing on the public site says which of the addresses in
the census belongs to the operator of this repo.

The dashboard failing is **not** the same as the node failing — it is a viewer,
not a monitor. Alerting is what tells you something is wrong.

## Documentation

- **[Pricing Strategy](docs/PRICING_STRATEGY.md)** — how a listing wins work, what it
  costs to serve, and how to price it from first bid to steady state. Start here.
- **[Operations Handoff](docs/HANDOFF.md)** — what runs where, the runbook, and the
  failure modes worth knowing before you touch anything.
- **[research/](research/)** — the scripts behind the analysis. `foundation/` builds the
  dataset; `analysis/` queries it.
