#!/usr/bin/env bash
# Publish the freshly built census to the public repo.
#
# Deliberately discards local git state every run. This checkout exists only to
# carry generated files upstream; it never holds work worth keeping, so the safe
# move is to reset onto origin/main rather than try to merge. An earlier version
# used `git pull --rebase || true`, which silently swallowed a failure (the clone
# was in detached HEAD) and then pushed a non-fast-forward that was rightly
# rejected.
#
# The census is built with PROVIDER_ADDRESS set, so it carries a `youAre` field
# naming this operator. That field is what turns a neutral market census into a
# personal disclosure, and it must never reach the public origin — so it is
# stripped here, at the boundary, rather than trusting every future caller to
# remember to unset an env var.
set -euo pipefail

REPO=/root/morpheus/census/repo
SRC=/root/morpheus/census/census-full.json
HDB="$REPO/history.db"

[ -s "$SRC" ] || { echo "no census-full.json to publish"; exit 1; }
cd "$REPO"

git config user.name  "morcompute-census"
git config user.email "census@users.noreply.github.com"

git fetch -q origin main
git checkout -q -B main origin/main     # force local main onto the remote tip

python3 - "$SRC" census-full.json <<'PY'
import json, sys
doc = json.load(open(sys.argv[1]))
removed = [k for k in ("youAre",) if doc.pop(k, None) is not None]
json.dump(doc, open(sys.argv[2], "w"), separators=(",", ":"))
print("census: stripped %s" % (", ".join(removed) or "nothing"))
PY

# Provider aggregates are derived from history.db, which is only rebuilt when the
# history job runs. Regenerating is cheap (a few seconds over 318k rows) and the
# alternative — a stale aggregate beside a fresh census — is silently wrong.
if [ -s "$HDB" ]; then
  python3 research/foundation/gen_provider_history.py "$HDB" providers-history.json
fi

git add census-full.json providers-history.json
if git diff --staged --quiet; then
  echo "census unchanged — nothing to publish"
  exit 0
fi
git commit -q -m "census $(date -u '+%F %H:%MZ')"
git push -q origin main
echo "published $(git rev-parse --short HEAD)"
