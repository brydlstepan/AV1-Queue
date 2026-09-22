"""
AV1 Queue server entrypoint.

Binds uvicorn to one or more *explicit* addresses instead of uvicorn's single
``--host`` CLI flag, so the server can listen on loopback (always, for the
tray's local control) plus one specific interface — e.g. a Tailscale
``100.x.x.x`` address — WITHOUT also exposing it on the LAN.

That three-way split (Tailscale: yes, localhost: yes, other LAN machines: no)
isn't reachable through a single ``--host``: a non-wildcard host excludes
loopback, and ``0.0.0.0``/``::`` includes every interface, LAN included.
Binding two real sockets gets exactly loopback + Tailscale and nothing else.

Env vars (see README "Network access"):
  AV1QUEUE_PORT  — default 8765
  AV1QUEUE_HOST  — comma-separated extra bind addresses, e.g. a Tailscale IP,
                   or 0.0.0.0 for every interface (LAN included — the
                   README's exposure warning applies). Loopback (127.0.0.1)
                   is always included too, unless a wildcard address is
                   already present (which already covers loopback, and
                   binding both would just double-bind the same traffic).
"""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import uvicorn  # noqa: E402

_WILDCARDS = {"0.0.0.0", "::"}


def _bind_hosts() -> list:
    hosts: list = []
    extra = os.environ.get("AV1QUEUE_HOST", "").strip()
    for tok in extra.split(","):
        tok = tok.strip()
        if tok and tok not in hosts:
            hosts.append(tok)
    if "127.0.0.1" not in hosts and not any(h in _WILDCARDS for h in hosts):
        hosts.insert(0, "127.0.0.1")
    return hosts or ["127.0.0.1"]


def _port() -> int:
    raw = os.environ.get("AV1QUEUE_PORT", "").strip()
    if raw:
        try:
            p = int(raw)
            if 1 <= p <= 65535:
                return p
        except ValueError:
            pass
    return 8765


def main() -> None:
    port = _port()
    hosts = _bind_hosts()

    sockets = []
    try:
        for host in hosts:
            family = socket.AF_INET6 if ":" in host else socket.AF_INET
            sock = socket.socket(family, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
            sock.listen(2048)
            sockets.append(sock)
    except OSError as e:
        for s in sockets:
            s.close()
        print(f"[!] Could not bind {hosts} on port {port}: {e}", file=sys.stderr)
        raise SystemExit(1)

    print(f"[av1queue] Listening on: {', '.join(f'{h}:{port}' for h in hosts)}")
    if any(h not in ("127.0.0.1", "::1") for h in hosts):
        print(
            "[av1queue] Reachable from other machines — there is no login on this "
            "server; see README 'Network access'."
        )

    config = uvicorn.Config("server.app:app", log_level="info")
    server = uvicorn.Server(config)
    server.run(sockets=sockets)


if __name__ == "__main__":
    main()
