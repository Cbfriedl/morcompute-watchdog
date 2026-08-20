#!/usr/bin/env bash
# Refresh every feed the private dashboard reads.
#
# reputation and margin are generated here. census-full.json is produced by the
# daily census job in a different directory, and status.json is written by GitHub Actions;
# snapshots.jsonl is written locally by private-snapshot.timer — so those are copied or pulled rather than
# regenerated. Without this the private page silently served hours-old market
# data while its own tiles were current, which is worse than showing nothing.
set -uo pipefail
P=/root/morpheus/private

python3 /root/morpheus/reputation.py >/dev/null 2>&1 || echo "reputation refresh failed" >&2
python3 /root/morpheus/margin.py     >/dev/null 2>&1 || echo "margin refresh failed" >&2

# census produced by gen-census.timer (daily)
[ -s /root/morpheus/census/census-full.json ] && \
  cp /root/morpheus/census/census-full.json "$P/census-full.json"

# written by GitHub Actions; fetch whatever is current
cd /root/morpheus/census/repo 2>/dev/null && git fetch -q origin main 2>/dev/null && \
  git show origin/main:status.json > "$P/status.json" 2>/dev/null
# snapshots.jsonl is NO LONGER pulled from origin. It is personal position
# history and does not belong in the public repo; private-snapshot.timer writes
# it locally instead. The old line here was also destructive: the shell truncates
# the target before `git show` runs, so every refresh emptied the local file even
# when the fetch failed.
exit 0
