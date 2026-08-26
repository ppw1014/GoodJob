#!/usr/bin/env python3
"""Minimal, ABI-correct FwpmEngineOpen0 dynamic-session probe."""

from __future__ import annotations

import ctypes
from ctypes import wintypes as wt
import json
import platform
import sys


RPC_C_AUTHN_WINNT = 10
FWPM_SESSION_FLAG_DYNAMIC = 0x00000001


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class FWPM_DISPLAY_DATA0(ctypes.Structure):
    _fields_ = [("name", wt.LPWSTR), ("description", wt.LPWSTR)]


class FWPM_SESSION0(ctypes.Structure):
    _fields_ = [
        ("sessionKey", GUID),
        ("displayData", FWPM_DISPLAY_DATA0),
        ("flags", wt.DWORD),
        ("txnWaitTimeoutInMSec", wt.DWORD),
        ("processId", wt.DWORD),
        ("sid", ctypes.c_void_p),
        ("username", wt.LPWSTR),
        ("kernelMode", wt.BOOL),
    ]


def is_admin() -> bool:
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def main() -> int:
    fwpuclnt = ctypes.WinDLL("fwpuclnt.dll", use_last_error=True)
    fwpuclnt.FwpmEngineOpen0.argtypes = [
        wt.LPCWSTR,
        wt.DWORD,
        ctypes.c_void_p,
        ctypes.POINTER(FWPM_SESSION0),
        ctypes.POINTER(wt.HANDLE),
    ]
    fwpuclnt.FwpmEngineOpen0.restype = wt.DWORD
    fwpuclnt.FwpmEngineClose0.argtypes = [wt.HANDLE]
    fwpuclnt.FwpmEngineClose0.restype = wt.DWORD

    session = FWPM_SESSION0()
    session.displayData.name = "GoodJob WFP ABI retest"
    session.displayData.description = "Dynamic session open/close only"
    session.flags = FWPM_SESSION_FLAG_DYNAMIC

    handle = wt.HANDLE()
    status = fwpuclnt.FwpmEngineOpen0(
        None,
        RPC_C_AUTHN_WINNT,
        None,
        ctypes.byref(session),
        ctypes.byref(handle),
    )
    report = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "admin": is_admin(),
        "fwpm_session0_size": ctypes.sizeof(FWPM_SESSION0),
        "open_status": status,
        "handle_opened": bool(handle.value),
    }
    if status == 0:
        report["close_status"] = fwpuclnt.FwpmEngineClose0(handle)

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if status == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
