#!/usr/bin/env python3
"""Child process used by the app-scoped WFP spike."""

from __future__ import annotations

import json
import socket


DNS_QUERY = (
    b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
    b"\x07example\x03com\x00\x00\x01\x00\x01"
)


def tcp(family: int, host: str, port: int) -> dict[str, object]:
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.settimeout(3)
            sock.connect((host, port))
        return {"ok": True}
    except OSError as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def udp_dns(family: int, host: str) -> dict[str, object]:
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as sock:
            sock.settimeout(3)
            sock.sendto(DNS_QUERY, (host, 53))
            sock.recvfrom(1024)
        return {"ok": True}
    except OSError as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


print(
    json.dumps(
        {
            "tcp_v4": tcp(socket.AF_INET, "8.8.8.8", 53),
            "udp_dns_v4": udp_dns(socket.AF_INET, "8.8.8.8"),
            "tcp_v6": tcp(socket.AF_INET6, "2001:4860:4860::8888", 53),
            "udp_dns_v6": udp_dns(socket.AF_INET6, "2001:4860:4860::8888"),
        },
        sort_keys=True,
    )
)
