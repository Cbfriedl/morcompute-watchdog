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

## Dashboard

A live status page is published at **https://cbfriedl.github.io/morcompute-watchdog/**

It reads Base mainnet **directly from your browser** — no server, no backend, no
key. It shows stake, headroom against the annual cap, wallet gas, active bids and
their prices, and the anniversary countdown.

**It deliberately does not show the OpenRouter balance.** A static public page
cannot hold a secret, and the OpenRouter key is required to read it. That figure
is checked every 10 minutes by the on-box monitor instead, which alerts to ntfy
if it drops below threshold.

The dashboard failing is **not** the same as the node failing — it is a viewer,
not a monitor. Alerting is what tells you something is wrong.
