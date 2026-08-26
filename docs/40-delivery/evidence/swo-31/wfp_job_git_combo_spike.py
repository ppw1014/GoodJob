#!/usr/bin/env python3
"""Exercise the complete GoodJob Git containment model on Windows.

The candidate defence is intentionally layered:
CreateProcessW(CREATE_SUSPENDED) -> assign to a one-process Job -> resume,
while dynamic WFP ALE filters for git.exe are installed.  Local metadata
commands must work; a remote command must be unable to create its helper.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes as wt
import json
from pathlib import Path
import time
import uuid

import wfp_filter_spike as wfp


CREATE_SUSPENDED = 0x00000004
CREATE_UNICODE_ENVIRONMENT = 0x00000400
CREATE_NO_WINDOW = 0x08000000
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
STARTF_USESTDHANDLES = 0x00000100
HANDLE_FLAG_INHERIT = 0x00000001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JobObjectExtendedLimitInformation = 9
JobObjectBasicAccountingInformation = 1
JobObjectAssociateCompletionPortInformation = 7
JOB_OBJECT_MSG_ACTIVE_PROCESS_LIMIT = 3
JOB_OBJECT_MSG_ACTIVE_PROCESS_ZERO = 4
JOB_OBJECT_MSG_NEW_PROCESS = 6
JOB_OBJECT_MSG_EXIT_PROCESS = 7


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD),
        ("lpReserved", wt.LPWSTR),
        ("lpDesktop", wt.LPWSTR),
        ("lpTitle", wt.LPWSTR),
        ("dwX", wt.DWORD), ("dwY", wt.DWORD),
        ("dwXSize", wt.DWORD), ("dwYSize", wt.DWORD),
        ("dwXCountChars", wt.DWORD), ("dwYCountChars", wt.DWORD),
        ("dwFillAttribute", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("wShowWindow", wt.WORD),
        ("cbReserved2", wt.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
        ("hStdInput", wt.HANDLE),
        ("hStdOutput", wt.HANDLE),
        ("hStdError", wt.HANDLE),
    ]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wt.HANDLE), ("hThread", wt.HANDLE),
        ("dwProcessId", wt.DWORD), ("dwThreadId", wt.DWORD),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wt.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wt.DWORD),
        ("Affinity", ctypes.c_void_p),
        ("PriorityClass", wt.DWORD),
        ("SchedulingClass", wt.DWORD),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_int64),
        ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64),
        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", wt.DWORD),
        ("TotalProcesses", wt.DWORD),
        ("ActiveProcesses", wt.DWORD),
        ("TotalTerminatedProcesses", wt.DWORD),
    ]


class JOBOBJECT_ASSOCIATE_COMPLETION_PORT(ctypes.Structure):
    _fields_ = [("CompletionKey", ctypes.c_void_p), ("CompletionPort", wt.HANDLE)]


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wt.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wt.BOOL),
    ]


kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
kernel32.CreateJobObjectW.restype = wt.HANDLE
kernel32.SetInformationJobObject.argtypes = [wt.HANDLE, wt.DWORD, ctypes.c_void_p, wt.DWORD]
kernel32.SetInformationJobObject.restype = wt.BOOL
kernel32.QueryInformationJobObject.argtypes = [wt.HANDLE, wt.DWORD, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p]
kernel32.QueryInformationJobObject.restype = wt.BOOL
kernel32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
kernel32.AssignProcessToJobObject.restype = wt.BOOL
kernel32.CreateProcessW.argtypes = [
    wt.LPCWSTR, wt.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wt.BOOL, wt.DWORD,
    ctypes.c_void_p, wt.LPCWSTR, ctypes.POINTER(STARTUPINFOEXW),
    ctypes.POINTER(PROCESS_INFORMATION),
]
kernel32.CreateProcessW.restype = wt.BOOL
kernel32.ResumeThread.argtypes = [wt.HANDLE]
kernel32.ResumeThread.restype = wt.DWORD
kernel32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
kernel32.WaitForSingleObject.restype = wt.DWORD
kernel32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
kernel32.GetExitCodeProcess.restype = wt.BOOL
kernel32.IsProcessInJob.argtypes = [wt.HANDLE, wt.HANDLE, ctypes.POINTER(wt.BOOL)]
kernel32.IsProcessInJob.restype = wt.BOOL
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.CloseHandle.restype = wt.BOOL
kernel32.CreateIoCompletionPort.argtypes = [wt.HANDLE, wt.HANDLE, ctypes.c_size_t, wt.DWORD]
kernel32.CreateIoCompletionPort.restype = wt.HANDLE
kernel32.GetQueuedCompletionStatus.argtypes = [
    wt.HANDLE, ctypes.POINTER(wt.DWORD), ctypes.POINTER(ctypes.c_size_t),
    ctypes.POINTER(ctypes.c_void_p), wt.DWORD,
]
kernel32.GetQueuedCompletionStatus.restype = wt.BOOL
kernel32.CreatePipe.argtypes = [ctypes.POINTER(wt.HANDLE), ctypes.POINTER(wt.HANDLE), ctypes.POINTER(SECURITY_ATTRIBUTES), wt.DWORD]
kernel32.CreatePipe.restype = wt.BOOL
kernel32.SetHandleInformation.argtypes = [wt.HANDLE, wt.DWORD, wt.DWORD]
kernel32.SetHandleInformation.restype = wt.BOOL
kernel32.ReadFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
kernel32.ReadFile.restype = wt.BOOL
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = wt.BOOL


JOB_MESSAGES = {
    JOB_OBJECT_MSG_ACTIVE_PROCESS_LIMIT: "ACTIVE_PROCESS_LIMIT",
    JOB_OBJECT_MSG_ACTIVE_PROCESS_ZERO: "ACTIVE_PROCESS_ZERO",
    JOB_OBJECT_MSG_NEW_PROCESS: "NEW_PROCESS",
    JOB_OBJECT_MSG_EXIT_PROCESS: "EXIT_PROCESS",
}


def create_one_process_job() -> tuple[wt.HANDLE, wt.HANDLE, dict[str, object]]:
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return job, wt.HANDLE(), {"status": "CreateJobObjectW failed", "winerror": ctypes.get_last_error()}
    limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    limits.BasicLimitInformation.LimitFlags = (
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
    )
    limits.BasicLimitInformation.ActiveProcessLimit = 1
    ok = kernel32.SetInformationJobObject(
        job, JobObjectExtendedLimitInformation, ctypes.byref(limits), ctypes.sizeof(limits)
    )
    result: dict[str, object] = {
        "created": True,
        "set_limits": bool(ok),
        "winerror": ctypes.get_last_error() if not ok else 0,
        "active_process_limit": 1,
    }
    port = kernel32.CreateIoCompletionPort(wt.HANDLE(INVALID_HANDLE_VALUE), None, 0x474A4F42, 1)
    associate = JOBOBJECT_ASSOCIATE_COMPLETION_PORT(ctypes.c_void_p(0x474A4F42), port)
    port_ok = bool(port) and bool(kernel32.SetInformationJobObject(
        job, JobObjectAssociateCompletionPortInformation, ctypes.byref(associate), ctypes.sizeof(associate)
    ))
    result["completion_port"] = {"created": bool(port), "associated": port_ok, "winerror": ctypes.get_last_error() if not port_ok else 0}
    return job, port, result


def job_accounting(job: wt.HANDLE) -> dict[str, object]:
    info = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
    ok = kernel32.QueryInformationJobObject(
        job, JobObjectBasicAccountingInformation, ctypes.byref(info), ctypes.sizeof(info), None
    )
    if not ok:
        return {"query_ok": False, "winerror": ctypes.get_last_error()}
    return {
        "query_ok": True,
        "total_processes": info.TotalProcesses,
        "active_processes": info.ActiveProcesses,
        "total_terminated_processes": info.TotalTerminatedProcesses,
    }


def process_image(process_id: int | None) -> str | None:
    if not process_id:
        return None
    process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, process_id)
    if not process:
        return None
    try:
        size = wt.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return None
    finally:
        kernel32.CloseHandle(process)


def next_job_event(port: wt.HANDLE, timeout_ms: int) -> dict[str, object] | None:
    message = wt.DWORD()
    key = ctypes.c_size_t()
    process_id = ctypes.c_void_p()
    ok = kernel32.GetQueuedCompletionStatus(
        port, ctypes.byref(message), ctypes.byref(key), ctypes.byref(process_id), timeout_ms
    )
    if not ok:
        if ctypes.get_last_error() == WAIT_TIMEOUT:
            return None
        return {"message": "GET_QUEUED_COMPLETION_STATUS_ERROR", "winerror": ctypes.get_last_error()}
    pid = ctypes.cast(process_id, ctypes.c_void_p).value
    return {
        "message": JOB_MESSAGES.get(message.value, str(message.value)),
        "process_id": pid,
        "image": process_image(pid),
    }


def make_capture_pipe() -> tuple[wt.HANDLE, wt.HANDLE]:
    read_handle = wt.HANDLE()
    write_handle = wt.HANDLE()
    attributes = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), None, True)
    if not kernel32.CreatePipe(ctypes.byref(read_handle), ctypes.byref(write_handle), ctypes.byref(attributes), 0):
        raise OSError(ctypes.get_last_error(), "CreatePipe")
    if not kernel32.SetHandleInformation(read_handle, HANDLE_FLAG_INHERIT, 0):
        raise OSError(ctypes.get_last_error(), "SetHandleInformation")
    return read_handle, write_handle


def read_pipe(handle: wt.HANDLE) -> str:
    chunks: list[bytes] = []
    buffer = ctypes.create_string_buffer(4096)
    while True:
        count = wt.DWORD()
        ok = kernel32.ReadFile(handle, buffer, len(buffer), ctypes.byref(count), None)
        if count.value:
            chunks.append(buffer.raw[:count.value])
        if not ok:
            break
    return b"".join(chunks).decode("utf-8", "replace").strip()


def run_git_in_job(git: Path, cwd: Path, args: list[str]) -> dict[str, object]:
    job, port, job_result = create_one_process_job()
    report: dict[str, object] = {"args": args, "job": job_result}
    if not job_result.get("set_limits"):
        if job:
            kernel32.CloseHandle(job)
        if port:
            kernel32.CloseHandle(port)
        return report

    startup = STARTUPINFOEXW()
    startup.StartupInfo.cb = ctypes.sizeof(startup)
    stdout_read, stdout_write = make_capture_pipe()
    stderr_read, stderr_write = make_capture_pipe()
    startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES
    startup.StartupInfo.hStdOutput = stdout_write
    startup.StartupInfo.hStdError = stderr_write
    process = PROCESS_INFORMATION()
    command = " ".join([f'"{git}"', *args])
    command_buffer = ctypes.create_unicode_buffer(command)
    flags = CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW | EXTENDED_STARTUPINFO_PRESENT
    created = kernel32.CreateProcessW(
        None, command_buffer, None, None, True, flags, None, str(cwd), ctypes.byref(startup), ctypes.byref(process)
    )
    kernel32.CloseHandle(stdout_write)
    kernel32.CloseHandle(stderr_write)
    report["create_suspended"] = {"ok": bool(created), "winerror": ctypes.get_last_error() if not created else 0}
    try:
        if not created:
            return report
        in_job = wt.BOOL()
        membership = kernel32.IsProcessInJob(process.hProcess, job, ctypes.byref(in_job))
        report["assigned_before_resume"] = {
            "ok": bool(kernel32.AssignProcessToJobObject(job, process.hProcess)),
            "winerror": ctypes.get_last_error(),
        }
        membership = kernel32.IsProcessInJob(process.hProcess, job, ctypes.byref(in_job))
        report["in_job_before_resume"] = {"api_ok": bool(membership), "in_job": bool(in_job.value)}
        resumed = kernel32.ResumeThread(process.hThread)
        report["resume"] = {"ok": resumed != 0xFFFFFFFF, "previous_suspend_count": resumed}
        events: list[dict[str, object]] = []
        deadline = time.monotonic() + 30
        wait = WAIT_TIMEOUT
        while time.monotonic() < deadline:
            event = next_job_event(port, 25)
            if event:
                events.append(event)
            wait = kernel32.WaitForSingleObject(process.hProcess, 0)
            if wait == WAIT_OBJECT_0:
                # Capture completion notifications queued immediately after parent exit.
                while True:
                    event = next_job_event(port, 50)
                    if not event:
                        break
                    events.append(event)
                break
        exit_code = wt.DWORD()
        kernel32.GetExitCodeProcess(process.hProcess, ctypes.byref(exit_code))
        report["completed"] = {"wait": wait, "timed_out": wait == WAIT_TIMEOUT, "exit_code": exit_code.value}
        report["stdout"] = read_pipe(stdout_read)
        report["stderr"] = read_pipe(stderr_read)
        report["accounting"] = job_accounting(job)
        report["job_events"] = events
    finally:
        if process.hThread:
            kernel32.CloseHandle(process.hThread)
        if process.hProcess:
            kernel32.CloseHandle(process.hProcess)
        kernel32.CloseHandle(stdout_read)
        kernel32.CloseHandle(stderr_read)
        kernel32.CloseHandle(job)
        kernel32.CloseHandle(port)
    return report


def add_git_filters(dll: ctypes.WinDLL, engine: wt.HANDLE, git: Path) -> tuple[dict[str, object], list[int], ctypes.POINTER(wfp.FWP_BYTE_BLOB)]:
    report: dict[str, object] = {}
    filter_ids: list[int] = []
    blob = ctypes.POINTER(wfp.FWP_BYTE_BLOB)()
    sublayer_key = wfp.GUID.parse(str(uuid.uuid4()))
    sublayer = wfp.FWPM_SUBLAYER0()
    sublayer.subLayerKey = sublayer_key
    sublayer.displayData.name = "GoodJob Git WFP + Job combo spike"
    sublayer.weight = 0xFFFF
    status = dll.FwpmSubLayerAdd0(engine, ctypes.byref(sublayer), None)
    report["sublayer"] = {"status": status, "name": wfp.status_name(status)}
    if status != 0:
        return report, filter_ids, blob
    status = dll.FwpmGetAppIdFromFileName0(str(git), ctypes.byref(blob))
    report["app_id"] = {"status": status, "name": wfp.status_name(status)}
    if status != 0:
        return report, filter_ids, blob
    filters: dict[str, object] = {}
    for label in ("connect_v4", "connect_v6"):
        filter_obj, _condition = wfp.make_filter(
            sublayer_key, wfp.GUID.parse(wfp.LAYER_KEYS[label]), blob, f"GoodJob Git combo {label}"
        )
        filter_id = ctypes.c_uint64()
        status = dll.FwpmFilterAdd0(engine, ctypes.byref(filter_obj), None, ctypes.byref(filter_id))
        readback = None
        if status == 0:
            filter_ids.append(filter_id.value)
            retrieved = ctypes.POINTER(wfp.FWPM_FILTER0)()
            readback = dll.FwpmFilterGetById0(engine, filter_id, ctypes.byref(retrieved))
            if readback == 0:
                free_ptr = ctypes.cast(retrieved, ctypes.c_void_p)
                dll.FwpmFreeMemory0(ctypes.byref(free_ptr))
        filters[label] = {"status": status, "name": wfp.status_name(status), "filter_id": filter_id.value if status == 0 else None, "readback": readback}
    report["filters"] = filters
    return report, filter_ids, blob


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--git", required=True, type=Path)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    parsed = parser.parse_args()

    report: dict[str, object] = {"admin": bool(ctypes.windll.shell32.IsUserAnAdmin()), "git": str(parsed.git), "repo": str(parsed.repo)}
    if not report["admin"]:
        report["error"] = "administrator token required to add WFP filters"
        parsed.result.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return 1

    dll = wfp.api()
    open_status, engine = wfp.open_dynamic_session(dll)
    report["wfp_open"] = {"status": open_status, "name": wfp.status_name(open_status)}
    filter_ids: list[int] = []
    blob = ctypes.POINTER(wfp.FWP_BYTE_BLOB)()
    try:
        if open_status != 0:
            return 1
        report["wfp"], filter_ids, blob = add_git_filters(dll, engine, parsed.git)
        if len(filter_ids) != 2:
            return 1
        report["local_rev_parse"] = run_git_in_job(parsed.git, parsed.repo, ["rev-parse", "--is-inside-work-tree"])
        report["local_log"] = run_git_in_job(parsed.git, parsed.repo, ["--no-pager", "log", "-1", "--format=%H"])
        report["remote_helper_attempt"] = run_git_in_job(
            parsed.git, parsed.repo, ["ls-remote", "--exit-code", "https://github.com/git/git", "HEAD"]
        )
    finally:
        if blob:
            free_blob = ctypes.cast(blob, ctypes.c_void_p)
            dll.FwpmFreeMemory0(ctypes.byref(free_blob))
        if engine:
            report["wfp_close"] = dll.FwpmEngineClose0(engine)

    post_status, post_engine = wfp.open_dynamic_session(dll)
    report["post_cleanup_open"] = {"status": post_status, "name": wfp.status_name(post_status)}
    if post_status == 0:
        cleanup: dict[str, object] = {}
        for filter_id in filter_ids:
            retrieved = ctypes.POINTER(wfp.FWPM_FILTER0)()
            status = dll.FwpmFilterGetById0(post_engine, filter_id, ctypes.byref(retrieved))
            cleanup[str(filter_id)] = {"status": status, "name": wfp.status_name(status)}
        report["dynamic_cleanup"] = cleanup
        report["post_cleanup_close"] = dll.FwpmEngineClose0(post_engine)

    parsed.result.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
