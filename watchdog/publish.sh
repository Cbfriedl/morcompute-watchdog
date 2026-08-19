#!/usr/bin/env bash
# Commit the freshly built census to the public repo.
# Only census-full.json moves — the page itself is code and changes rarely.
set -e
cd /root/morpheus/census/repo || exit 0
cp ../census-full.json census-full.json
git config user.name  "morcompute-census"
git config user.email "census@users.noreply.github.com"
git add census-full.json
git diff --staged --quiet && exit 0
git commit -q -m "census $(date -u +%F\ %H:%MZ)"
git pull --rebase --autostash -q origin main || true
git push -q origin main
