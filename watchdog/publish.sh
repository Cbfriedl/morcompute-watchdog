#!/usr/bin/env bash
# Publish the freshly built census to the public repo.
#
# Deliberately discards local git state every run. This checkout exists only to
# carry one generated file upstream; it never holds work worth keeping, so the
# safe move is to reset onto origin/main rather than try to merge. The previous
# version used `git pull --rebase || true`, which silently swallowed a failure
# (the clone was in detached HEAD) and then pushed a non-fast-forward that was
# rightly rejected.
set -euo pipefail

REPO=/root/morpheus/census/repo
SRC=/root/morpheus/census/census-full.json

[ -s "$SRC" ] || { echo "no census-full.json to publish"; exit 1; }
cd "$REPO"

git config user.name  "morcompute-census"
git config user.email "census@users.noreply.github.com"

git fetch -q origin main
git checkout -q -B main origin/main     # force local main onto the remote tip

cp "$SRC" census-full.json
git add census-full.json
if git diff --staged --quiet; then
  echo "census unchanged — nothing to publish"
  exit 0
fi
git commit -q -m "census $(date -u '+%F %H:%MZ')"
git push -q origin main
echo "published $(git rev-parse --short HEAD)"
