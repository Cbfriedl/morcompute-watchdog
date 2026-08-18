#!/usr/bin/env python3
"""
Morpheus provider monitor — READ ONLY.

Collects provider state from chain + the local router API, evaluates alert
thresholds, pushes breaches to ntfy, and writes a metrics snapshot + heartbeat.

Design constraints (see DESIGN_dashboard-and-alerting.md):
  * This script NEVER touches the wallet private key and never sends a tx.
  * It talks to a *different* RPC endpoint than the router, so a monitor
    poll can never rate-limit the node it is watching.
  * Alerts are edge-triggered with a re-alert interval, so a sustained
    problem does not spam the phone every cycle.
  * RPC failure is itself an alert condition, but only after N consecutive
    failures — a single flaky poll must not cry wolf.

Exit code is always 0 unless the script itself is broken; alert conditions
are signalled by notification, not exit status, so the systemd timer does
not enter a failed state for an expected business condition.
"""

import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "state.json")
METRICS_PATH = os.path.join(HERE, "metrics.json")

DIAMOND = "0x6aBE1d282f72B474E54527D93b979A4f64d3030a"
MOR_TOKEN = "0x7431aDa8a591C955a994a21710752EF9b882b8e3"

SEL_GET_PROVIDER = "0x55f21eb7"          # getProvider(address)
SEL_IS_ACTIVE = "0x63ef175d"             # getIsProviderActive(address)
SEL_PROVIDER_ACTIVE_BIDS = "0xaf5b77ca"  # getProviderActiveBids(address,uint256,uint256)
SEL_BALANCE_OF = "0x70a08231"            # balanceOf(address)

WEI = 10 ** 18


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def env(key, default=None, required=False):
    v = os.environ.get(key, default)
    if required and not v:
        sys.stderr.write("FATAL: %s is not set\n" % key)
        sys.exit(2)
    return v


CFG = {}


def load_config():
    CFG["provider"] = env("PROVIDER_ADDRESS", required=True)
    # Deliberately NOT the router's endpoint. See module docstring.
    CFG["rpc"] = env("MONITOR_RPC", "https://base.drpc.org")
    CFG["ntfy_server"] = env("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    CFG["ntfy_topic"] = env("NTFY_TOPIC", required=True)
    CFG["router_url"] = env("ROUTER_URL", "http://localhost:8082").rstrip("/")
    CFG["router_auth"] = env("ROUTER_AUTH", "")  # "user:pass" from .cookie
    CFG["container"] = env("CONTAINER_NAME", "morpheus-router")

    CFG["headroom_warn_pct"] = float(env("HEADROOM_WARN_PCT", "30"))
    CFG["eth_min"] = float(env("ETH_MIN", "0.005"))
    CFG["expect_bids"] = env("EXPECT_BIDS", "false").lower() == "true"
    CFG["realert_hours"] = float(env("REALERT_HOURS", "6"))
    CFG["rpc_fail_threshold"] = int(env("RPC_FAIL_THRESHOLD", "3"))
    CFG["heartbeat_url"] = env("HEARTBEAT_URL", "")  # optional external DMS ping
    CFG["models_config"] = env("MODELS_CONFIG",
                               "/root/morpheus/morpheus-data/models-config.json")
    CFG["or_min_credits"] = float(env("OPENROUTER_MIN_CREDITS", "5"))


# --------------------------------------------------------------------------
# low-level helpers
# --------------------------------------------------------------------------

def http(url, data=None, headers=None, timeout=30, auth=None):
    """Minimal HTTP via curl. curl is used rather than urllib because some
    public RPC providers reject urllib's default User-Agent outright."""
    cmd = ["curl", "-s", "-S", "--max-time", str(timeout)]
    if auth:
        cmd += ["-u", auth]
    for k, v in (headers or {}).items():
        cmd += ["-H", "%s: %s" % (k, v)]
    if data is not None:
        cmd += ["-X", "POST", "--data-binary", "@-"]
    cmd.append(url)
    p = subprocess.run(
        cmd,
        input=(data.encode() if isinstance(data, str) else data),
        capture_output=True,
    )
    if p.returncode != 0:
        raise RuntimeError("curl failed (%d): %s" % (p.returncode, p.stderr.decode()[:200]))
    return p.stdout.decode()


def rpc_call(to, data):
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"to": to, "data": data}, "latest"],
    })
    r = json.loads(http(CFG["rpc"], data=payload,
                        headers={"content-type": "application/json"}))
    if "error" in r:
        raise RuntimeError("rpc error: %s" % str(r["error"])[:200])
    return r["result"]


def rpc_balance(addr):
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "eth_getBalance",
        "params": [addr, "latest"],
    })
    r = json.loads(http(CFG["rpc"], data=payload,
                        headers={"content-type": "application/json"}))
    if "error" in r:
        raise RuntimeError("rpc error: %s" % str(r["error"])[:200])
    return int(r["result"], 16)


def words(hexstr):
    h = hexstr[2:] if hexstr.startswith("0x") else hexstr
    return [h[i:i + 64] for i in range(0, len(h), 64)]


def pad_addr(a):
    return a.lower().replace("0x", "").rjust(64, "0")


def decode_string(w, base):
    n = int(w[base], 16)
    if n == 0:
        return ""
    raw = "".join(w[base + 1: base + 1 + (n + 31) // 32])
    return bytes.fromhex(raw[: n * 2]).decode("utf8", "replace")


# --------------------------------------------------------------------------
# collectors
# --------------------------------------------------------------------------

def get_provider():
    """getProvider -> {endpoint, stake, createdAt, limitPeriodEnd,
    limitPeriodEarned, isDeleted}"""
    w = words(rpc_call(DIAMOND, SEL_GET_PROVIDER + pad_addr(CFG["provider"])))
    b = int(w[0], 16) // 32
    return {
        "endpoint": decode_string(w, b + int(w[b], 16) // 32),
        "stake": int(w[b + 1], 16),
        "createdAt": int(w[b + 2], 16),
        "limitPeriodEnd": int(w[b + 3], 16),
        "limitPeriodEarned": int(w[b + 4], 16),
        "isDeleted": bool(int(w[b + 5], 16)),
    }


def get_is_active():
    return bool(int(rpc_call(DIAMOND, SEL_IS_ACTIVE + pad_addr(CFG["provider"])), 16))


def get_active_bid_count():
    data = (SEL_PROVIDER_ACTIVE_BIDS + pad_addr(CFG["provider"])
            + "%064x" % 0 + "%064x" % 100)
    w = words(rpc_call(DIAMOND, data))
    off = int(w[0], 16) // 32
    return int(w[off], 16)


def get_mor_balance():
    return int(rpc_call(MOR_TOKEN, SEL_BALANCE_OF + pad_addr(CFG["provider"])), 16)


def get_provider_ping(endpoint):
    """Protocol-level self-ping via the router: dials our own public provider
    endpoint and returns per-model health. This is the closest thing to
    'what a paying customer experiences' that we can check locally.

    status values seen so far: "no_bid" (model has no active bid).
    """
    body = json.dumps({"providerAddr": CFG["provider"], "providerUrl": endpoint})
    try:
        r = json.loads(http(CFG["router_url"] + "/proxy/provider/ping",
                            data=body,
                            headers={"content-type": "application/json"},
                            timeout=30, auth=CFG["router_auth"] or None))
        return {"ok": True, "version": r.get("version"),
                "ping_ms": r.get("ping"),
                "models": r.get("models") or []}
    except Exception as e:
        return {"ok": False, "error": str(e)[:150]}


def get_openrouter():
    """Read the OpenRouter key from models-config.json (single source of truth —
    it is NOT duplicated into monitor.env) and check the account balance.

    The key never leaves this box. The public dashboard deliberately does not
    show this: a static page cannot hold a secret.

    Note: /api/v1/credits `total_usage` lags real spend by some minutes, so
    `remaining` is a slow-moving figure. That is fine for a low-balance alert
    and useless for real-time spend tracking.
    """
    out = {"ok": False}
    try:
        with open(CFG["models_config"]) as f:
            models = json.load(f).get("models") or []
        key = (models[0].get("apiKey") or "") if models else ""
    except Exception as e:
        out["error"] = "cannot read models config: %s" % str(e)[:100]
        return out

    if not key or "<" in key:
        out["error"] = "no real OpenRouter key configured (placeholder)"
        out["placeholder"] = True
        return out

    try:
        raw = http("https://openrouter.ai/api/v1/credits", timeout=25,
                   headers={"Authorization": "Bearer " + key})
        d = json.loads(raw).get("data") or {}
        total = float(d.get("total_credits") or 0)
        used = float(d.get("total_usage") or 0)
        out.update({"ok": True, "total_credits": total, "total_usage": used,
                    "remaining": total - used})
    except Exception as e:
        out["error"] = str(e)[:150]
    return out


def get_container_state():
    try:
        p = subprocess.run(
            ["docker", "inspect", "-f",
             "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}"
             "{{else}}none{{end}}|{{.RestartCount}}", CFG["container"]],
            capture_output=True, timeout=30)
        if p.returncode != 0:
            return {"ok": False, "error": p.stderr.decode()[:120].strip()}
        status, health, restarts = p.stdout.decode().strip().split("|")
        return {"ok": True, "status": status, "health": health,
                "restarts": int(restarts)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


def get_router_health():
    out = {"reachable": False}
    try:
        http(CFG["router_url"] + "/healthcheck", timeout=10, auth=CFG["router_auth"] or None)
        out["reachable"] = True
    except Exception as e:
        out["error"] = str(e)[:120]
        return out
    try:
        models = json.loads(http(CFG["router_url"] + "/v1/models", timeout=15,
                                 auth=CFG["router_auth"] or None))
        out["model_count"] = len(models)
        # A model whose on-chain id is all-zeros is an unconfigured placeholder.
        out["placeholder_models"] = sum(
            1 for m in models if int(m.get("Id", "0x0"), 16) == 0)
        out["models"] = [
            {"id": m.get("Id"), "name": m.get("Name"), "slots": m.get("Slots")}
            for m in models
        ]
    except Exception as e:
        out["models_error"] = str(e)[:120]
    return out


# --------------------------------------------------------------------------
# state / alerting
# --------------------------------------------------------------------------

def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {"alerts": {}, "rpc_failures": 0}


def save_state(s):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=1)
    os.replace(tmp, STATE_PATH)


def notify(title, message, priority="default", tags=""):
    url = "%s/%s" % (CFG["ntfy_server"], CFG["ntfy_topic"])
    headers = {"Title": title, "Priority": priority}
    if tags:
        headers["Tags"] = tags
    try:
        http(url, data=message, headers=headers, timeout=20)
        return True
    except Exception as e:
        sys.stderr.write("notify failed: %s\n" % e)
        return False


def should_fire(state, key, active, now):
    """Edge-trigger with re-alert. Returns (fire, is_recovery)."""
    rec = state["alerts"].get(key)
    if active:
        if rec is None:
            state["alerts"][key] = {"since": now, "last": now}
            return True, False
        if now - rec["last"] >= CFG["realert_hours"] * 3600:
            rec["last"] = now
            return True, False
        return False, False
    else:
        if rec is not None:
            del state["alerts"][key]
            return True, True
        return False, False


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    load_config()
    now = int(time.time())
    state = load_state()
    metrics = {"ts": now,
               "iso": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    findings = []   # (key, active, severity, title, message)

    # ---- chain reads -----------------------------------------------------
    chain_ok = True
    try:
        prov = get_provider()
        active = get_is_active()
        bids = get_active_bid_count()
        mor = get_mor_balance()
        eth = rpc_balance(CFG["provider"])
        state["rpc_failures"] = 0
    except Exception as e:
        chain_ok = False
        state["rpc_failures"] = state.get("rpc_failures", 0) + 1
        metrics["chain_error"] = str(e)[:200]

    # Always evaluate rpc_down, in both directions. If this were only appended
    # in the except branch, a recovered RPC would never clear the alert record
    # and no RESOLVED would ever fire.
    fails = state.get("rpc_failures", 0)
    findings.append((
        "rpc_down", (not chain_ok) and fails >= CFG["rpc_fail_threshold"], "high",
        "Monitor cannot read chain",
        "%d consecutive RPC failures via %s\n%s"
        % (fails, CFG["rpc"], metrics.get("chain_error", ""))))

    if chain_ok:
        headroom = prov["stake"] - prov["limitPeriodEarned"]
        headroom_pct = (100.0 * headroom / prov["stake"]) if prov["stake"] else 0.0
        metrics.update({
            "stake_mor": prov["stake"] / WEI,
            "earned_mor": prov["limitPeriodEarned"] / WEI,
            "headroom_mor": headroom / WEI,
            "headroom_pct": round(headroom_pct, 2),
            "endpoint": prov["endpoint"],
            "is_active": active,
            "is_deleted": prov["isDeleted"],
            "active_bids": bids,
            "wallet_mor": mor / WEI,
            "wallet_eth": eth / 1e18,
            "limit_period_end": prov["limitPeriodEnd"],
            "days_to_anniversary": round(
                (prov["limitPeriodEnd"] - now) / 86400.0, 1),
        })

        # --- theft / integrity signatures (highest severity) ---
        last_stake = state.get("last_stake")
        stake_dropped = last_stake is not None and prov["stake"] < last_stake
        findings.append((
            "stake_drop", stake_dropped, "urgent",
            "STAKE DECREASED",
            "Stake fell from %.4f to %.4f MOR.\nIf you did not deregister, treat "
            "as key compromise." % ((last_stake or 0) / WEI, prov["stake"] / WEI)))

        findings.append((
            "deregistered", (prov["isDeleted"] or not active), "urgent",
            "PROVIDER NOT ACTIVE",
            "isDeleted=%s isActive=%s. Provider is not registered on-chain."
            % (prov["isDeleted"], active)))

        last_ep = state.get("last_endpoint")
        findings.append((
            "endpoint_changed", last_ep is not None and prov["endpoint"] != last_ep,
            "urgent", "ENDPOINT CHANGED ON-CHAIN",
            "Endpoint changed from %r to %r. If unintended, treat as compromise."
            % (last_ep, prov["endpoint"])))

        # --- economics ---
        findings.append((
            "headroom_low", headroom_pct < CFG["headroom_warn_pct"], "urgent",
            "Headroom %.1f%%" % headroom_pct,
            "Headroom %.2f / %.2f MOR (%.1f%%). Below %.0f%%. Re-stake before it "
            "hits zero — you stop being paid, silently. Claim and re-stake to reopen it."
            % (headroom / WEI, prov["stake"] / WEI, headroom_pct,
               CFG["headroom_warn_pct"])))

        findings.append((
            "eth_low", (eth / 1e18) < CFG["eth_min"], "high",
            "Gas low: %.5f ETH" % (eth / 1e18),
            "Wallet ETH %.6f is below %.5f. Cannot transact — re-stake would fail."
            % (eth / 1e18, CFG["eth_min"])))

        # Evaluated in both directions so that turning EXPECT_BIDS off, or
        # posting a bid, clears a previously-raised alert.
        findings.append((
            "no_bids", CFG["expect_bids"] and bids == 0, "high", "No active bids",
            "Provider has 0 active bids but EXPECT_BIDS=true. Not earning."))

        state["last_stake"] = prov["stake"]
        state["last_endpoint"] = prov["endpoint"]

    # ---- container -------------------------------------------------------
    cont = get_container_state()
    metrics["container"] = cont
    cont_bad = (not cont.get("ok")) or cont.get("status") != "running" \
        or cont.get("health") not in ("healthy", "none")
    findings.append((
        "container", cont_bad, "high", "Router container unhealthy",
        "docker inspect: %s" % json.dumps(cont)))

    # ---- router API ------------------------------------------------------
    rh = get_router_health()
    metrics["router"] = rh
    findings.append((
        "router_unreachable", not rh["reachable"], "high",
        "Router API unreachable",
        "GET %s/healthcheck failed: %s" % (CFG["router_url"], rh.get("error"))))

    # ---- provider endpoint self-ping -------------------------------------
    # Only meaningful once we know the on-chain endpoint (needs a chain read).
    if chain_ok:
        ping = get_provider_ping(prov["endpoint"])
        metrics["provider_ping"] = ping
        findings.append((
            "endpoint_unreachable", not ping["ok"], "urgent",
            "Provider endpoint not answering",
            "Self-ping to %s failed: %s\nCustomers cannot open sessions."
            % (prov["endpoint"], ping.get("error"))))

        # Model-level health. "no_bid" is expected before Step 9, so it is only
        # treated as a fault once we actually expect to be selling.
        # "skipped" = the model registry entry has no LLM tag, so the router
        # reports modelType UNKNOWN and never probes it. That is a property of
        # how the model was listed on chain, not a fault of this node.
        bad = [m for m in ping.get("models", [])
               if m.get("status") not in ("healthy", "no_bid", "skipped")]
        findings.append((
            "model_unhealthy", bool(bad), "high", "Model unhealthy",
            "Unhealthy models reported by provider ping:\n%s"
            % json.dumps(bad)[:400]))

        no_bid_models = [m for m in ping.get("models", [])
                         if m.get("status") == "no_bid"]
        findings.append((
            "models_no_bid", CFG["expect_bids"] and bool(no_bid_models), "high",
            "Model has no active bid",
            "EXPECT_BIDS=true but these models report no_bid:\n%s"
            % json.dumps(no_bid_models)[:400]))

    if rh.get("placeholder_models"):
        findings.append((
            "placeholder_model", True, "default",
            "Model config is a placeholder",
            "%d model(s) have an all-zero on-chain id — models-config.json has "
            "not been filled in. The node cannot serve inference."
            % rh["placeholder_models"]))
    else:
        findings.append(("placeholder_model", False, "default", "", ""))

    # ---- OpenRouter balance ----------------------------------------------
    orr = get_openrouter()
    # never let the key reach metrics.json — that file is world-readable in spirit
    metrics["openrouter"] = {k: v for k, v in orr.items() if k != "key"}

    findings.append((
        "or_unreachable", (not orr["ok"]) and not orr.get("placeholder"), "high",
        "OpenRouter check failed",
        "Could not read the OpenRouter balance: %s\nIf the key is rejected, every "
        "session will fail while still costing you headroom." % orr.get("error")))

    findings.append((
        "or_low_credits",
        orr["ok"] and orr["remaining"] < CFG["or_min_credits"], "high",
        "OpenRouter credit low: $%.2f" % (orr.get("remaining") or 0),
        "Remaining $%.2f is below $%.2f. At zero the node accepts sessions and "
        "fails every request — you keep burning headroom and earn nothing."
        % (orr.get("remaining") or 0, CFG["or_min_credits"])))

    # ---- emit ------------------------------------------------------------
    fired = []
    for key, act, sev, title, msg in findings:
        fire, recovered = should_fire(state, key, act, now)
        if not fire:
            continue
        if recovered:
            notify("RESOLVED: %s" % key, "Condition cleared.", "low", "white_check_mark")
            fired.append("resolved:" + key)
        else:
            notify(title, msg, sev, "rotating_light" if sev == "urgent" else "warning")
            fired.append(key)

    metrics["alerts_active"] = sorted(state["alerts"].keys())
    metrics["alerts_fired_this_run"] = fired

    tmp = METRICS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(metrics, f, indent=1)
    os.replace(tmp, METRICS_PATH)

    state["last_run"] = now
    save_state(state)

    # ---- heartbeat (dead-man's switch) -----------------------------------
    # Only ping when the run itself was healthy enough to be meaningful.
    # A monitor that cannot read the chain must NOT report "all well".
    if CFG["heartbeat_url"] and chain_ok:
        try:
            http(CFG["heartbeat_url"], timeout=15)
        except Exception as e:
            sys.stderr.write("heartbeat failed: %s\n" % e)

    print(json.dumps(metrics, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
