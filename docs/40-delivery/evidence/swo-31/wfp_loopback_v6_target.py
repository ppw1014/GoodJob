#!/usr/bin/env python3
"""IPv6 loopback TCP/UDP client for the WFP V6 layer spike."""

from __future__ import annotations

import json
import os
import socket


def tcp(port: int) -> dict[str, object]:
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
            sock.settimeout(2)
            sock.connect(("::1", port))
            sock.sendall(b"goodjob")
            response = sock.recv(64)
        return {"ok": response == b"ok"}
    except OSError as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def udp(port: int) -> dict[str, object]:
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as sock:
            sock.settimeout(2)
            sock.sendto(b"goodjob", ("::1", port))
            response, _ = sock.recvfrom(64)
        return {"ok": response == b"ok"}
    except OSError as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


print(
    json.dumps(
        {
            "tcp_v6_loopback": tcp(int(os.environ["WFP_TCP_PORT"])),
            "udp_v6_loopback": udp(int(os.environ["WFP_UDP_PORT"])),
        },
        sort_keys=True,
    )
)
