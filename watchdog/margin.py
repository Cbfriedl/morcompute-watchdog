#!/usr/bin/env python3
"""Compute session margin: what a session earns minus what it cost to serve.

This is the one genuinely private number in the whole system. Everything else on
the dashboard — stake, headroom, bids, sessions, MOR earned — is public on Base
mainnet and anyone can read it. OpenRouter spend is not on chain, and it is what
reveals margin. A competitor who knows your cost per session knows exactly how
far they can push you in a price war.

So this runs on the provider box and its output stays there.

CAVEAT on attribution: OpenRouter's key endpoint reports account-level spend
only. There is no per-model breakdown available to a plain API key, so per-model
margin cannot be computed without issuing a separate key per model. Blended
margin is honest; per-model figures here would be invented.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

MODELS_CONFIG = os.environ.get("MODELS_CONFIG",
                               "/root/morpheus/morpheus-data/models-config.json")
OUT = os.environ.get("MARGIN_FILE", "margin.json")
STATE = os.environ.get("MARGIN_STATE", "margin-state.json")
DIAMOND = "0x6aBE1d282f72B474E54527D93b979A4f64d3030a"
ME = os.environ.get("PROVIDER_ADDRESS", "").lower()
if not ME:
    raise SystemExit("PROVIDER_ADDRESS is not set. Export it, or add it to the EnvironmentFile named by the systemd unit.")
RPCS = [u.strip() for u in os.environ.get(
    "MARGIN_RPC", "https://base-rpc.publicnode.com,https://mainnet.base.org"
).split(",") if u.strip()]
_rr = [0]


def api_key():
    with open(MODELS_CONFIG) as f:
        return json.load(f)["models"][0]["apiKey"]


def openrouter():
    p = subprocess.run(["curl", "-s", "--max-time", "25",
                        "https://openrouter.ai/api/v1/key",
                        "-H", "Authorization: Bearer " + api_key()],
                       capture_output=True)
    d = json.loads(p.stdout)["data"]
    return {"usage": d.get("usage"), "usageDaily": d.get("usage_daily"),
            "limit": d.get("limit"), "remaining": d.get("limit_remaining")}


def eth_call(data):
    body = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
            "params": [{"to": DIAMOND, "data": data}, "latest"]}
    for a in range(len(RPCS) * 4):
        url = RPCS[_rr[0] % len(RPCS)]
        _rr[0] += 1
        try:
            p = subprocess.run(["curl", "-s", "--max-time", "30", "-X", "POST", url,
                                "-H", "content-type: application/json",
                                "--data-binary", json.dumps(body)], capture_output=True)
            h = json.loads(p.stdout)["result"][2:]
            return [h[i:i + 64] for i in range(0, len(h), 64)]
        except Exception:
            time.sleep(min(0.4 * (a + 1), 3))
    raise RuntimeError("rpc failed")


def mor_usd():
    try:
        p = subprocess.run(["curl", "-s", "--max-time", "20",
            "https://api.dexscreener.com/latest/dex/tokens/0x7431aDa8a591C955a994a21710752EF9b882b8e3"],
            capture_output=True)
        ps = [x for x in json.loads(p.stdout).get("pairs", [])
              if (x.get("baseToken", {}).get("symbol", "") or "").upper() == "MOR"]
        ps.sort(key=lambda x: -(x.get("liquidity", {}).get("usd") or 0))
        return float(ps[0]["priceUsd"]) if ps else None
    except Exception:
        return None


def main():
    pad = ME[2:].rjust(64, "0")
    pw = eth_call("0x55f21eb7" + pad)
    b = int(pw[0], 16) // 32
    stake = int(pw[b + 1], 16) / 1e18
    earned = int(pw[b + 4], 16) / 1e18
    ws = eth_call("0x87bced7d" + pad + "%064x" % 0 + "%064x" % 5000)
    o = int(ws[0], 16) // 32
    sessions = int(ws[o], 16)

    orr = openrouter()
    usd = mor_usd()
    spend = orr["usage"] or 0.0

    # Marginal figures matter more than lifetime ones: early sessions were served
    # at entry prices that no longer apply. Deltas since the last run give the
    # rate as it is now, not as it was.
    prev = {}
    try:
        with open(STATE) as f:
            prev = json.load(f)
    except Exception:
        pass

    dS = sessions - prev.get("sessions", 0) if prev else None
    dE = earned - prev.get("earned", 0.0) if prev else None
    dC = spend - prev.get("spend", 0.0) if prev else None

    doc = {
        "asOf": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "morUsd": usd,
        "sessions": sessions,
        "earnedMor": round(earned, 6),
        "earnedUsd": round(earned * usd, 4) if usd else None,
        "spendUsd": round(spend, 4),
        "openrouter": orr,
        "lifetime": {
            "perSessionEarnedUsd": round(earned * usd / sessions, 5) if usd and sessions else None,
            "perSessionCostUsd": round(spend / sessions, 5) if sessions else None,
            "perSessionMarginUsd": round((earned * usd - spend) / sessions, 5) if usd and sessions else None,
            "marginPct": round(100 * (earned * usd - spend) / (earned * usd), 1) if usd and earned else None,
            "netUsd": round(earned * usd - spend, 4) if usd else None,
        },
        "sinceLastRun": None,
        "note": "OpenRouter reports account-level spend only; per-model margin is "
                "not computable without a separate key per model.",
    }
    if dS and dS > 0 and usd:
        doc["sinceLastRun"] = {
            "since": prev.get("asOf"),
            "sessions": dS,
            "earnedUsd": round(dE * usd, 4),
            "spendUsd": round(dC, 4),
            "perSessionEarnedUsd": round(dE * usd / dS, 5),
            "perSessionCostUsd": round(dC / dS, 5),
            "perSessionMarginUsd": round((dE * usd - dC) / dS, 5),
            "marginPct": round(100 * (dE * usd - dC) / (dE * usd), 1) if dE * usd else None,
        }

    with open(OUT, "w") as f:
        json.dump(doc, f, indent=1)
    with open(STATE, "w") as f:
        json.dump({"asOf": doc["asOf"], "sessions": sessions,
                   "earned": earned, "spend": spend}, f)

    L = doc["lifetime"]
    print("sessions %d   earned %.4f MOR ($%.2f)   spend $%.2f   net $%+.2f"
          % (sessions, earned, doc["earnedUsd"] or 0, spend, L["netUsd"] or 0))
    print("per session: earned $%.5f  cost $%.5f  MARGIN $%+.5f  (%s%%)"
          % (L["perSessionEarnedUsd"] or 0, L["perSessionCostUsd"] or 0,
             L["perSessionMarginUsd"] or 0, L["marginPct"]))
    if doc["sinceLastRun"]:
        M = doc["sinceLastRun"]
        print("since last run (%d sessions): margin $%+.5f/session (%s%%)"
              % (M["sessions"], M["perSessionMarginUsd"], M["marginPct"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
