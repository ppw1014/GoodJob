#!/usr/bin/env python3
"""V6 loopback WFP verification using the ABI-correct base spike helpers."""

from __future__ import annotations

import ctypes
from ctypes import wintypes as wt
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import uuid

import wfp_filter_spike as wfp


class LoopbackServers:
    def __enter__(self) -> "LoopbackServers":
        self.tcp = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        self.tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.tcp.bind(("::1", 0))
        self.tcp.listen(1)
        self.tcp.settimeout(4)
        self.udp = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        self.udp.bind(("::1", 0))
        self.udp.settimeout(4)
        self.tcp_thread = threading.Thread(target=self._serve_tcp, daemon=True)
        self.udp_thread = threading.Thread(target=self._serve_udp, daemon=True)
        self.tcp_thread.start()
        self.udp_thread.start()
        return self

    def _serve_tcp(self) -> None:
        try:
            client, _ = self.tcp.accept()
            with client:
                client.recv(64)
                client.sendall(b"ok")
        except OSError:
            pass

    def _serve_udp(self) -> None:
        try:
            data, peer = self.udp.recvfrom(64)
            self.udp.sendto(b"ok" if data else b"", peer)
        except OSError:
            pass

    def probe(self) -> dict[str, object]:
        env = os.environ | {
            "WFP_TCP_PORT": str(self.tcp.getsockname()[1]),
            "WFP_UDP_PORT": str(self.udp.getsockname()[1]),
        }
        target = Path(__file__).with_name("wfp_loopback_v6_target.py")
        completed = subprocess.run(
            [sys.executable, str(target)],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
            env=env,
        )
        return {
            "returncode": completed.returncode,
            "probe": json.loads(completed.stdout),
        }

    def __exit__(self, *_: object) -> None:
        self.tcp.close()
        self.udp.close()
        self.tcp_thread.join(timeout=1)
        self.udp_thread.join(timeout=1)


def run_probe() -> dict[str, object]:
    with LoopbackServers() as servers:
        return servers.probe()


def main() -> int:
    report: dict[str, object] = {
        "admin": bool(ctypes.windll.shell32.IsUserAnAdmin()),
        "positive_control": run_probe(),
    }
    dll = wfp.api()
    open_status, handle = wfp.open_dynamic_session(dll)
    report["open"] = {"status": open_status, "name": wfp.status_name(open_status)}
    if open_status != 0:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 1

    ids: list[int] = []
    app_blob = ctypes.POINTER(wfp.FWP_BYTE_BLOB)()
    try:
        key = wfp.GUID.parse(str(uuid.uuid4()))
        sublayer = wfp.FWPM_SUBLAYER0()
        sublayer.subLayerKey = key
        sublayer.displayData.name = "GoodJob V6 loopback spike sublayer"
        sublayer.weight = 0xFFFF
        status = dll.FwpmSubLayerAdd0(handle, ctypes.byref(sublayer), None)
        report["add_sublayer"] = {"status": status, "name": wfp.status_name(status)}
        if status != 0:
            return 1
        status = dll.FwpmGetAppIdFromFileName0(sys.executable, ctypes.byref(app_blob))
        report["app_id"] = {"status": status, "name": wfp.status_name(status)}
        if status != 0:
            return 1
        filters: dict[str, object] = {}
        for label in ("connect_v6", "recv_accept_v6"):
            filter_obj, _condition = wfp.make_filter(
                key,
                wfp.GUID.parse(wfp.LAYER_KEYS[label]),
                app_blob,
                f"GoodJob V6 loopback spike {label}",
            )
            filter_id = ctypes.c_uint64()
            status = dll.FwpmFilterAdd0(
                handle, ctypes.byref(filter_obj), None, ctypes.byref(filter_id)
            )
            filters[label] = {
                "status": status,
                "name": wfp.status_name(status),
                "filter_id": filter_id.value if status == 0 else None,
            }
            if status == 0:
                ids.append(filter_id.value)
        report["filters"] = filters
        if len(ids) != 2:
            return 1
        report["blocked_probe"] = run_probe()
    finally:
        if app_blob:
            blob_ptr = ctypes.cast(app_blob, ctypes.c_void_p)
            dll.FwpmFreeMemory0(ctypes.byref(blob_ptr))
        report["close"] = dll.FwpmEngineClose0(handle)

    post_open, post_handle = wfp.open_dynamic_session(dll)
    report["post_cleanup_open"] = post_open
    if post_open == 0:
        absent: dict[str, object] = {}
        for filter_id in ids:
            result = ctypes.POINTER(wfp.FWPM_FILTER0)()
            status = dll.FwpmFilterGetById0(post_handle, filter_id, ctypes.byref(result))
            absent[str(filter_id)] = {"status": status, "name": wfp.status_name(status)}
        report["dynamic_cleanup"] = absent
        report["post_cleanup_close"] = dll.FwpmEngineClose0(post_handle)
    report["post_cleanup_probe"] = run_probe()
    rendered = json.dumps(report, indent=2, sort_keys=True)
    Path(__file__).with_name("wfp_v6_loopback_result.json").write_text(rendered, encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
