#!/usr/bin/env python3
"""Serve the private dashboard on localhost only.

Bound to 127.0.0.1 deliberately. The box has no firewall and no TLS, so opening
a port to the internet would expose OpenRouter spend — the one number that is
not already public on chain — over plaintext HTTP. Reaching it through the SSH
tunnel you already use costs nothing and authenticates with your existing key.

  ssh -N -L 8090:127.0.0.1:8090 -i ~/.ssh/morpheus_ed25519 root@2.28.8.173
  then open http://127.0.0.1:8090/
"""
import http.server, functools, os, subprocess, sys, socketserver

ROOT = os.environ.get("PRIVATE_ROOT", "/root/morpheus/private")
PORT = int(os.environ.get("PRIVATE_PORT", "8090"))
# Bind to the Tailscale interface as well as loopback, so a phone on the same
# tailnet can reach it. NOT 0.0.0.0: the box has no firewall, and this page
# carries OpenRouter spend — the one figure not already public on chain.
BIND = os.environ.get("PRIVATE_BIND", "")


def tailscale_ip():
    """The box's tailnet address, or None if Tailscale is not up yet."""
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True,
                             timeout=10).stdout.decode().strip().split()
        return out[0] if out else None
    except Exception:
        return None

class H(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        # never cache: these files are rewritten by cron and a stale margin
        # figure is worse than a slow one
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        super().end_headers()
    def log_message(self, *a):
        pass

class Dual(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    os.chdir(ROOT)
    host = BIND or tailscale_ip() or "127.0.0.1"
    srv = Dual((host, PORT), functools.partial(H, directory=ROOT))
    where = "%s:%d" % (host, PORT)
    print("private dashboard on %s%s" % (
        where, "  (tailnet + loopback)" if host != "127.0.0.1" else "  (localhost only)"),
        flush=True)
    srv.serve_forever()
