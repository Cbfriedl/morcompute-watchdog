#!/usr/bin/env bash
# Stamp the shared module import with a content hash.
#
# The pages import ./assets/pub.js as a static specifier. GitHub Pages serves it
# with cache-control: max-age=600 and browsers additionally keep resolved ES
# modules in a per-document module map, so a redeploy that changes pub.js does
# NOT reach an open tab — it keeps executing the previous bundle while the HTML
# around it is new. That is invisible: the page renders, just with stale code.
#
# Appending the file's own hash to the specifier makes the URL change whenever
# the contents change, which is the only thing a browser reliably keys on.
set -euo pipefail
cd "$(dirname "$0")"
H=$(sha256sum assets/pub.js  | cut -c1-12)
C=$(sha256sum assets/pub.css | cut -c1-12)
for f in index.html models.html addresses.html provider.html; do
  [ -f "$f" ] || continue
  sed -i -E 's#(from "\./assets/pub\.js)(\?v=[0-9a-f]+)?"#\1?v='"$H"'"#' "$f"
  sed -i -E 's#(href="assets/pub\.css)(\?v=[0-9a-f]+)?"#\1?v='"$C"'"#' "$f"
done
echo "stamped pub.js?v=$H  pub.css?v=$C"
