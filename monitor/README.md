# Provider monitoring — deployed 2026-08-16

Two independent layers, as required by `DESIGN_dashboard-and-alerting.md`.
Both are **live**.

| Layer | Runs on | Catches | Cadence |
|---|---|---|---|
| **Push** — `morpheus_monitor.py` | the Hetzner box (systemd timer) | headroom, stake/theft, gas, container, router API, model + endpoint health | 10 min |
| **Dead-man's switch** — [`morcompute-watchdog`](https://github.com/Cbfriedl/morcompute-watchdog) | GitHub Actions | **the box being dead** | 15 min |

The second layer exists because *a dead box cannot report its own death*. It
holds no credential belonging to the box: it reads public chain state and dials
the public provider port the way a customer would.

Alerts go to **ntfy**. Subscribe to the topic in the ntfy app (Android/iOS/web).
The topic is a capability — anyone who knows it can read your alerts and post
fake ones. It lives in `monitor.env` (chmod 600) on the box and as the
`NTFY_TOPIC` Actions secret. It is **not** in any repo.

## Alert conditions

| Key | Severity | Fires when |
|---|---|---|
| `stake_drop` | urgent | stake fell vs. last observation — **theft signature** |
| `deregistered` | urgent | `isDeleted` true or `getIsProviderActive` false |
| `endpoint_changed` | urgent | on-chain endpoint changed — **theft signature** |
| `headroom_low` | urgent | headroom < `HEADROOM_WARN_PCT` (default 30%) |
| `endpoint_unreachable` | urgent | provider self-ping fails |
| `eth_low` | high | wallet ETH < `ETH_MIN` — cannot pay gas, re-stake would fail |
| `container` | high | container not running/healthy |
| `router_unreachable` | high | `GET /healthcheck` fails |
| `model_unhealthy` | high | provider ping reports a model not healthy/no_bid |
| `no_bids`, `models_no_bid` | high | 0 active bids **while `EXPECT_BIDS=true`** |
| `rpc_down` | high | 3 consecutive RPC failures |
| `placeholder_model` | default | `models-config.json` still unconfigured |

Alerts are **edge-triggered** with a re-alert every `REALERT_HOURS` (default 6),
and emit an explicit `RESOLVED:` notification when the condition clears — so a
sustained problem does not spam, and a recovery is never silent.

`rpc_down` requires 3 consecutive failures so a single flaky poll cannot page
you. Conversely, the heartbeat is only sent on a run that successfully read the
chain: **a monitor that cannot see the chain must not report "all well."**

## Flip these when a bid goes live

`EXPECT_BIDS=false` today, because 0 bids is currently the correct state. Set it
to `true` in `/root/morpheus/monitor/monitor.env` the moment Step 9 completes,
or the "not earning" alerts stay disarmed.

Re-tune `HEADROOM_WARN_PCT` from the *measured* capture rate once sessions are
real. At a p25 bid (4 MOR/day) a 700 MOR headroom lasts ~175 days; at the
protocol ceiling it lasts ~19 hours. The threshold that is right depends
entirely on which of those you bid.

## Operating

```bash
# on the box
systemctl status morpheus-monitor.timer
systemctl list-timers morpheus-monitor.timer
journalctl -u morpheus-monitor.service -n 50 --no-pager

# run once by hand
cd /root/morpheus/monitor && set -a && . ./monitor.env && set +a \
  && python3 morpheus_monitor.py

# current snapshot / alert state
cat /root/morpheus/monitor/metrics.json
cat /root/morpheus/monitor/state.json
```

Files on the box live in `/root/morpheus/monitor/` (mode 700); `monitor.env` is
mode 600. `state.json` holds last-seen stake and endpoint — deleting it will
re-arm every alert and suppress the next `stake_drop`/`endpoint_changed`
comparison for one cycle.

## Deliberate non-goals

- **The monitor never holds the wallet key and never sends a transaction.** It
  is read-only by construction. The future claim→re-stake automation is a
  separate component with different privileges.
- It uses a *different* RPC endpoint than the router (`base.drpc.org` vs. the
  router's), so a monitor poll can never rate-limit the node it watches.
- Note: **dRPC's free plan rejects JSON-RPC batches larger than 3.** The monitor
  makes unbatched sequential calls, so this does not affect it, but any future
  poller must account for it.
