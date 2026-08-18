#!/usr/bin/env python3
"""Independently check the MOR figures in history.db against the chain.

`mor` is computed as `pricePerSecond * duration`, both of which come from the
bid. That makes it a derived quantity, and a derived quantity that nothing
contradicts is not the same as a correct one. `providerWithdrawnAmount` lives on
the session record rather than the bid, so it is a genuinely independent number
to check against.

This re-reads a random sample of sessions spanning the whole history and
compares. Expect:

  * where a withdrawal exists, agreement to ~0.01% (median ratio exactly 1.0)
  * roughly 44% of sessions with no withdrawal at all — earned but never
    claimed. Those are why the naive aggregate ratio looks like ~1.35 and why
    `mor` must be read as earned, not received.

It is also what catches the duration trap: billing `closedAt - openedAt` instead
of capping at `endsAt` inflates the total by ~3.6x, and this check fails loudly
if that regresses.

    python3 research/foundation/history_validate.py [sample_size]
"""
import json
import os
import random
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import history_build as H  # noqa: E402

N = int(sys.argv[1]) if len(sys.argv) > 1 else 2500
CACHE = H.CACHE


def main():
    ids = json.load(open(os.path.join(CACHE, "ids.json")))
    bids = json.load(open(os.path.join(CACHE, "bids.json")))
    provs = sorted(ids)

    rows = {}
    with open(os.path.join(CACHE, "sess.jsonl")) as f:
        for line in f:
            line = line.strip()
            if line:
                r = json.loads(line)
                rows[(r[0], r[1])] = r[2:]
    print("cache holds %d sessions" % len(rows))

    random.seed(7)
    sample = random.sample(sorted(rows), min(N, len(rows)))
    sids = {k: ids[provs[k[0]]][k[1]] for k in sample}

    def decode(_it, res):
        w = H.W(res)
        base = int(w[0], 16) // 32
        return int(w[base + 5], 16)      # providerWithdrawnAmount

    got = {}
    H.drive(sample, encode=lambda k: H.SEL_GET_SESSION + sids[k][2:],
            decode=decode, sink=lambda k, v: got.__setitem__(k, v),
            label="check", eps=H.make_endpoints(), report_every=1000)

    paid_c = paid_w = unpaid_c = 0.0
    npaid = nunpaid = 0
    ratios = []
    for k, wd in got.items():
        _user, bid, o, e, c = rows[k]
        b = bids.get(bid)
        if not c or not b:
            continue
        dur = (min(c, e) if e else c) - o
        if dur <= 0:
            continue
        calc = int(b[1]) * dur / 1e18
        if wd > 0:
            paid_c += calc
            paid_w += wd / 1e18
            npaid += 1
            ratios.append(calc / (wd / 1e18))
        else:
            unpaid_c += calc
            nunpaid += 1

    print("\nsample of %d sessions across the full history" % len(got))
    print("  withdrawn > 0 : %5d sessions  computed %9.2f MOR  withdrawn %9.2f MOR"
          % (npaid, paid_c, paid_w))
    print("  withdrawn = 0 : %5d sessions  computed %9.2f MOR  (never claimed)"
          % (nunpaid, unpaid_c))
    if npaid:
        err = abs(paid_c / paid_w - 1) * 100
        print("\n  formula vs chain, where a withdrawal exists:")
        print("    aggregate %.5f  (%.4f%% error)   median %.5f"
              % (paid_c / paid_w, err, statistics.median(ratios)))
        print("    within 2%% of 1.0: %.1f%%"
              % (100 * sum(1 for x in ratios if abs(x - 1) < 0.02) / len(ratios)))
        print("\n  %.1f%% of sessions and %.1f%% of earned MOR were never withdrawn"
              % (100 * nunpaid / (npaid + nunpaid),
                 100 * unpaid_c / (paid_c + unpaid_c)))
        print("\n  %s" % ("PASS" if err < 1.0 else
                          "FAIL — earnings no longer match the chain"))
        return 0 if err < 1.0 else 1
    print("  no withdrawals in sample — inconclusive")
    return 1


if __name__ == "__main__":
    sys.exit(main())
