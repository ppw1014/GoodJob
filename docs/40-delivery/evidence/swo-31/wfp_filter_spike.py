#!/usr/bin/env python3
"""ABI-correct dynamic WFP application-filter spike for Windows."""

from __future__ import annotations

import ctypes
from ctypes import wintypes as wt
import json
from pathlib import Path
import platform
import subprocess
import sys
import uuid


RPC_C_AUTHN_WINNT = 10
FWPM_SESSION_FLAG_DYNAMIC = 0x00000001
FWP_EMPTY = 0
FWP_BYTE_BLOB_TYPE = 12
FWP_MATCH_EQUAL = 0
FWP_ACTION_BLOCK = 0x00001001  # FWP_ACTION_FLAG_TERMINATING | 1


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def parse(cls, value: str) -> "GUID":
        parsed = uuid.UUID(value)
        return cls(
            parsed.time_low,
            parsed.time_mid,
            parsed.time_hi_version,
            (ctypes.c_ubyte * 8).from_buffer_copy(parsed.bytes[8:]),
        )


class FWP_BYTE_BLOB(ctypes.Structure):
    _fields_ = [("size", wt.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


class FWP_VALUE_UNION(ctypes.Union):
    _fields_ = [
        ("uint8", ctypes.c_uint8),
        ("uint16", ctypes.c_uint16),
        ("uint32", ctypes.c_uint32),
        ("uint64", ctypes.POINTER(ctypes.c_uint64)),
        ("byteBlob", ctypes.POINTER(FWP_BYTE_BLOB)),
        ("pointer", ctypes.c_void_p),
    ]


class FWP_VALUE0(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("value", FWP_VALUE_UNION)]


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


class FWPM_SUBLAYER0(ctypes.Structure):
    _fields_ = [
        ("subLayerKey", GUID),
        ("displayData", FWPM_DISPLAY_DATA0),
        ("flags", wt.WORD),
        ("providerKey", ctypes.POINTER(GUID)),
        ("providerData", FWP_BYTE_BLOB),
        ("weight", wt.WORD),
    ]


class FWPM_FILTER_CONDITION0(ctypes.Structure):
    _fields_ = [
        ("fieldKey", GUID),
        ("matchType", wt.DWORD),
        ("conditionValue", FWP_VALUE0),
    ]


class FWPM_ACTION0(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("filterType", GUID)]


class FWPM_FILTER0(ctypes.Structure):
    _fields_ = [
        ("filterKey", GUID),
        ("displayData", FWPM_DISPLAY_DATA0),
        ("flags", wt.DWORD),
        ("providerKey", ctypes.POINTER(GUID)),
        ("providerData", FWP_BYTE_BLOB),
        ("layerKey", GUID),
        ("subLayerKey", GUID),
        ("weight", FWP_VALUE0),
        ("numFilterConditions", wt.DWORD),
        ("filterCondition", ctypes.POINTER(FWPM_FILTER_CONDITION0)),
        ("action", FWPM_ACTION0),
        ("rawContext", ctypes.c_uint64),
        ("reserved", ctypes.POINTER(GUID)),
        ("filterId", ctypes.c_uint64),
        ("effectiveWeight", FWP_VALUE0),
    ]


LAYER_KEYS = {
    "connect_v4": "c38d57d1-05a7-4c33-904f-7fbceee60e82",
    "connect_v6": "4a72393b-319f-44bc-84c3-ba54dcb3b6b4",
    "recv_accept_v4": "e1cd9fe7-f4b5-4273-96c0-592e487b8650",
    "recv_accept_v6": "a3b42c97-9f04-4672-b87e-cee9c483257f",
}
APP_ID_KEY = GUID.parse("d78e1e87-8644-4ea5-9437-d809ecefc971")


def status_name(status: int) -> str:
    known = {
        0: "ERROR_SUCCESS",
        5: "ERROR_ACCESS_DENIED",
        50: "ERROR_NOT_SUPPORTED",
        1722: "RPC_S_SERVER_UNAVAILABLE",
        0x80320003: "FWP_E_FILTER_NOT_FOUND",
    }
    return known.get(status, f"0x{status:08X}")


def run_target(target: Path) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, str(target)],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    try:
        payload: object = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = {"stdout": result.stdout, "stderr": result.stderr}
    return {"returncode": result.returncode, "probe": payload}


def api() -> ctypes.WinDLL:
    dll = ctypes.WinDLL("fwpuclnt.dll", use_last_error=True)
    dll.FwpmEngineOpen0.argtypes = [
        wt.LPCWSTR,
        wt.DWORD,
        ctypes.c_void_p,
        ctypes.POINTER(FWPM_SESSION0),
        ctypes.POINTER(wt.HANDLE),
    ]
    dll.FwpmEngineOpen0.restype = wt.DWORD
    dll.FwpmEngineClose0.argtypes = [wt.HANDLE]
    dll.FwpmEngineClose0.restype = wt.DWORD
    dll.FwpmSubLayerAdd0.argtypes = [
        wt.HANDLE,
        ctypes.POINTER(FWPM_SUBLAYER0),
        ctypes.c_void_p,
    ]
    dll.FwpmSubLayerAdd0.restype = wt.DWORD
    dll.FwpmFilterAdd0.argtypes = [
        wt.HANDLE,
        ctypes.POINTER(FWPM_FILTER0),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint64),
    ]
    dll.FwpmFilterAdd0.restype = wt.DWORD
    dll.FwpmFilterGetById0.argtypes = [
        wt.HANDLE,
        ctypes.c_uint64,
        ctypes.POINTER(ctypes.POINTER(FWPM_FILTER0)),
    ]
    dll.FwpmFilterGetById0.restype = wt.DWORD
    dll.FwpmGetAppIdFromFileName0.argtypes = [
        wt.LPCWSTR,
        ctypes.POINTER(ctypes.POINTER(FWP_BYTE_BLOB)),
    ]
    dll.FwpmGetAppIdFromFileName0.restype = wt.DWORD
    dll.FwpmFreeMemory0.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    dll.FwpmFreeMemory0.restype = None
    return dll


def open_dynamic_session(dll: ctypes.WinDLL) -> tuple[int, wt.HANDLE]:
    session = FWPM_SESSION0()
    session.displayData.name = "GoodJob WFP application-filter spike"
    session.displayData.description = "Dynamic, application-scoped validation"
    session.flags = FWPM_SESSION_FLAG_DYNAMIC
    handle = wt.HANDLE()
    status = dll.FwpmEngineOpen0(
        None,
        RPC_C_AUTHN_WINNT,
        None,
        ctypes.byref(session),
        ctypes.byref(handle),
    )
    return status, handle


def make_filter(
    sublayer_key: GUID,
    layer_key: GUID,
    app_blob: ctypes.POINTER(FWP_BYTE_BLOB),
    name: str,
) -> tuple[FWPM_FILTER0, FWPM_FILTER_CONDITION0]:
    condition = FWPM_FILTER_CONDITION0()
    condition.fieldKey = APP_ID_KEY
    condition.matchType = FWP_MATCH_EQUAL
    condition.conditionValue.type = FWP_BYTE_BLOB_TYPE
    condition.conditionValue.value.byteBlob = app_blob

    filter_obj = FWPM_FILTER0()
    filter_obj.displayData.name = name
    filter_obj.displayData.description = "GoodJob dynamic WFP spike"
    filter_obj.layerKey = layer_key
    filter_obj.subLayerKey = sublayer_key
    filter_obj.weight.type = FWP_EMPTY
    filter_obj.numFilterConditions = 1
    filter_obj.filterCondition = ctypes.pointer(condition)
    filter_obj.action.type = FWP_ACTION_BLOCK
    return filter_obj, condition


def main() -> int:
    target = Path(__file__).with_name("wfp_network_target.py")
    report: dict[str, object] = {
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "is_admin": bool(ctypes.windll.shell32.IsUserAnAdmin()),
        "sizes": {
            "FWPM_SESSION0": ctypes.sizeof(FWPM_SESSION0),
            "FWPM_FILTER0": ctypes.sizeof(FWPM_FILTER0),
            "FWPM_FILTER_CONDITION0": ctypes.sizeof(FWPM_FILTER_CONDITION0),
        },
        "app_path": sys.executable,
        "positive_control": run_target(target),
    }
    dll = api()
    open_status, handle = open_dynamic_session(dll)
    report["open"] = {"status": open_status, "name": status_name(open_status)}
    if open_status != 0:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 1

    filter_ids: list[int] = []
    app_blob = ctypes.POINTER(FWP_BYTE_BLOB)()
    try:
        sublayer_key = GUID.parse(str(uuid.uuid4()))
        sublayer = FWPM_SUBLAYER0()
        sublayer.subLayerKey = sublayer_key
        sublayer.displayData.name = "GoodJob WFP spike sublayer"
        sublayer.displayData.description = "Dynamic application-scoped test"
        sublayer.weight = 0xFFFF
        add_sublayer = dll.FwpmSubLayerAdd0(handle, ctypes.byref(sublayer), None)
        report["add_sublayer"] = {"status": add_sublayer, "name": status_name(add_sublayer)}
        if add_sublayer != 0:
            return 1

        app_status = dll.FwpmGetAppIdFromFileName0(sys.executable, ctypes.byref(app_blob))
        report["app_id"] = {
            "status": app_status,
            "name": status_name(app_status),
            "size": app_blob.contents.size if app_status == 0 else None,
        }
        if app_status != 0:
            return 1

        additions: dict[str, object] = {}
        for label, guid_text in LAYER_KEYS.items():
            filter_obj, _condition = make_filter(
                sublayer_key,
                GUID.parse(guid_text),
                app_blob,
                f"GoodJob WFP spike {label}",
            )
            filter_id = ctypes.c_uint64()
            status = dll.FwpmFilterAdd0(
                handle,
                ctypes.byref(filter_obj),
                None,
                ctypes.byref(filter_id),
            )
            entry: dict[str, object] = {"status": status, "name": status_name(status)}
            if status == 0:
                filter_ids.append(filter_id.value)
                retrieved = ctypes.POINTER(FWPM_FILTER0)()
                get_status = dll.FwpmFilterGetById0(handle, filter_id, ctypes.byref(retrieved))
                entry["filter_id"] = filter_id.value
                entry["get_status"] = get_status
                entry["get_name"] = status_name(get_status)
                if get_status == 0:
                    free_ptr = ctypes.cast(retrieved, ctypes.c_void_p)
                    dll.FwpmFreeMemory0(ctypes.byref(free_ptr))
            additions[label] = entry
        report["filters"] = additions
        if len(filter_ids) != len(LAYER_KEYS):
            return 1

        report["blocked_probe"] = run_target(target)
    finally:
        if app_blob:
            free_blob = ctypes.cast(app_blob, ctypes.c_void_p)
            dll.FwpmFreeMemory0(ctypes.byref(free_blob))
        report["close"] = {
            "status": dll.FwpmEngineClose0(handle),
            "name": "ERROR_SUCCESS",
        }
        Path(__file__).with_name("wfp_filter_result.json").write_text(
            json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
        )

    post_open_status, post_handle = open_dynamic_session(dll)
    report["post_cleanup_open"] = {
        "status": post_open_status,
        "name": status_name(post_open_status),
    }
    if post_open_status == 0:
        cleanup_results: dict[str, object] = {}
        for filter_id in filter_ids:
            retrieved = ctypes.POINTER(FWPM_FILTER0)()
            status = dll.FwpmFilterGetById0(post_handle, filter_id, ctypes.byref(retrieved))
            cleanup_results[str(filter_id)] = {"status": status, "name": status_name(status)}
            if status == 0:
                free_ptr = ctypes.cast(retrieved, ctypes.c_void_p)
                dll.FwpmFreeMemory0(ctypes.byref(free_ptr))
        report["dynamic_cleanup"] = cleanup_results
        report["post_cleanup_close"] = dll.FwpmEngineClose0(post_handle)
    report["post_cleanup_probe"] = run_target(target)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
