from __future__ import annotations

import ctypes
import inspect
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from windows_process_fakes import FakeProcessApi

import goodjob.git_metadata as git_metadata
import goodjob.process_identity as process_identity_helpers
from goodjob.errors import CapabilityError, InvalidInputError, UnsupportedPlatformError
from goodjob.git_metadata import GitMetadataReader, InternalGitBinding
from goodjob.platform import (
    capability_windows,
    fs_windows,
    handles_windows,
    launcher_windows,
    preflight_windows,
    process_windows,
    sandbox_windows,
)
from goodjob.platform.capability_windows import WindowsTransferPipe
from goodjob.platform.fs_windows import relative_components, validate_component
from goodjob.platform.handles_windows import OwnedHandle
from goodjob.platform.lock_windows import WindowsSharedFileLock
from goodjob.platform.process_windows import FILETIME


@pytest.fixture(autouse=True)
def _isolate_windows_cleanup_registries() -> Any:
    handles_windows._RETAINED_OWNERS.clear()
    launcher_windows._RETAINED_CLEANUP_GROUPS.clear()
    sandbox_windows._RETAINED_WFP_ENGINES.clear()
    yield
    handles_windows._RETAINED_OWNERS.clear()
    launcher_windows._RETAINED_CLEANUP_GROUPS.clear()
    sandbox_windows._RETAINED_WFP_ENGINES.clear()


def test_owned_windows_handle_closes_exactly_once_and_invalidates_before_close() -> None:
    closed: list[int] = []
    handle = OwnedHandle(41, closer=closed.append)

    handle.close()
    handle.close()

    assert closed == [41]
    assert handle.closed
    with pytest.raises(OSError, match="already moved or closed"):
        _ = handle.value


def test_owned_windows_handle_detach_moves_close_authority() -> None:
    closed: list[int] = []
    handle = OwnedHandle(73, closer=closed.append)

    assert handle.detach() == 73
    handle.close()

    assert closed == []


def test_owned_windows_handle_retains_ownership_when_close_fails() -> None:
    attempts: list[int] = []

    def close(value: int) -> None:
        attempts.append(value)
        if len(attempts) == 1:
            raise OSError("injected close failure")

    handle = OwnedHandle(91, closer=close)

    with pytest.raises(OSError, match="injected close failure"):
        handle.close()

    assert not handle.closed
    assert handle.value == 91
    handle.retry_close()
    assert handle.closed
    assert attempts == [91, 91]


def test_windows_owner_cleanup_continues_and_retries_the_failed_owner() -> None:
    attempts: list[int] = []
    closed: list[int] = []

    def fail_once(value: int) -> None:
        attempts.append(value)
        if len(attempts) == 1:
            raise OSError("injected first owner close failure")
        closed.append(value)

    first = OwnedHandle(92, closer=fail_once)
    second = OwnedHandle(93, closer=closed.append)

    with pytest.raises(OSError, match="first owner close failure"):
        handles_windows.close_owned_resources((first, second))

    assert not first.closed
    assert second.closed
    assert closed == [93]
    assert [first] == handles_windows._RETAINED_OWNERS

    handles_windows.retry_retained_owners()
    assert first.closed
    assert handles_windows._RETAINED_OWNERS == []
    assert closed == [93, 92]


def test_windows_process_exists_rejects_openable_signaled_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeProcessApi(0)
    monkeypatch.setattr(process_windows, "load_windows_dll", lambda _name: api)
    monkeypatch.setattr(
        process_windows,
        "OwnedHandle",
        lambda value: OwnedHandle(value, closer=api.closed.append),
    )

    assert process_windows.process_exists(321) is False
    assert api.wait_calls == [(701, 0)]
    assert api.closed == [701]


def test_windows_process_exists_keeps_openable_unsignaled_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeProcessApi(process_windows.WAIT_TIMEOUT)
    monkeypatch.setattr(process_windows, "load_windows_dll", lambda _name: api)
    monkeypatch.setattr(
        process_windows,
        "OwnedHandle",
        lambda value: OwnedHandle(value, closer=api.closed.append),
    )

    assert process_windows.process_exists(321) is True
    assert api.wait_calls == [(701, 0)]
    assert api.closed == [701]


def test_windows_process_exists_reports_wait_failure_and_closes_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeProcessApi(0xFFFFFFFF)
    monkeypatch.setattr(process_windows, "load_windows_dll", lambda _name: api)
    monkeypatch.setattr(process_windows, "last_error", lambda: 6)
    monkeypatch.setattr(
        process_windows,
        "OwnedHandle",
        lambda value: OwnedHandle(value, closer=api.closed.append),
    )

    with pytest.raises(OSError, match="WaitForSingleObject") as failure:
        process_windows.process_exists(321)

    assert failure.value.errno == 6
    assert api.wait_calls == [(701, 0)]
    assert api.closed == [701]


def test_windows_process_exists_rejects_unknown_wait_status_and_closes_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeProcessApi(7)
    monkeypatch.setattr(process_windows, "load_windows_dll", lambda _name: api)
    monkeypatch.setattr(
        process_windows,
        "OwnedHandle",
        lambda value: OwnedHandle(value, closer=api.closed.append),
    )

    with pytest.raises(OSError, match="unexpected status: 7"):
        process_windows.process_exists(321)

    assert api.wait_calls == [(701, 0)]
    assert api.closed == [701]


def test_windows_owner_process_stopped_fails_closed_on_access_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeProcessApi(process_windows.WAIT_TIMEOUT, open_result=0)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(process_windows, "load_windows_dll", lambda _name: api)
    monkeypatch.setattr(process_windows, "last_error", lambda: process_windows.ERROR_ACCESS_DENIED)

    assert not process_identity_helpers.owner_process_stopped("pid:321;started:100")
    assert len(api.open_calls) == 2
    assert api.wait_calls == []
    assert api.closed == []


def test_windows_owner_process_stopped_accepts_proven_absent_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeProcessApi(process_windows.WAIT_TIMEOUT, open_result=0)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(process_windows, "load_windows_dll", lambda _name: api)
    monkeypatch.setattr(
        process_windows,
        "last_error",
        lambda: process_windows.ERROR_INVALID_PARAMETER,
    )

    assert process_identity_helpers.owner_process_stopped("pid:321;started:100")
    assert len(api.open_calls) == 1
    assert api.wait_calls == []
    assert api.closed == []


@pytest.mark.parametrize(
    ("creation_marker", "expected_stopped"),
    [(100, False), (101, True)],
    ids=["same-owner-live", "pid-reused"],
)
def test_windows_owner_process_stopped_preserves_creation_marker_proof(
    monkeypatch: pytest.MonkeyPatch,
    creation_marker: int,
    expected_stopped: bool,
) -> None:
    api = FakeProcessApi(process_windows.WAIT_TIMEOUT, creation_marker=creation_marker)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(process_windows, "load_windows_dll", lambda _name: api)
    monkeypatch.setattr(
        process_windows,
        "OwnedHandle",
        lambda value: OwnedHandle(value, closer=api.closed.append),
    )

    assert process_identity_helpers.owner_process_stopped("pid:321;started:100") is expected_stopped
    assert len(api.open_calls) == 2
    assert api.wait_calls == [(701, 0)]
    assert api.closed == [701, 701]


def test_windows_owner_process_stopped_withholds_signaled_result_until_close_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeProcessApi(process_windows.WAIT_OBJECT_0)
    close_attempts: list[int] = []

    def close_fails_once(value: int) -> None:
        close_attempts.append(value)
        if len(close_attempts) == 1:
            raise OSError("injected process handle close failure")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(process_windows, "load_windows_dll", lambda _name: api)
    monkeypatch.setattr(
        process_windows,
        "OwnedHandle",
        lambda value: OwnedHandle(value, closer=close_fails_once),
    )

    with pytest.raises(OSError, match="process handle close failure"):
        process_identity_helpers.owner_process_stopped("pid:321;started:100")

    assert len(handles_windows._RETAINED_OWNERS) == 1
    handles_windows.retry_retained_owners()
    assert handles_windows._RETAINED_OWNERS == []
    assert process_identity_helpers.owner_process_stopped("pid:321;started:100")
    assert close_attempts == [701, 701, 701]


@pytest.mark.skipif(sys.platform != "win32", reason="requires native Windows process APIs")
def test_native_windows_process_signal_controls_owner_recovery() -> None:
    retained_before = tuple(handles_windows._RETAINED_OWNERS)
    child = subprocess.Popen(
        [sys.executable, "-I", "-B", "-c", "import sys; sys.stdin.buffer.read(1)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        marker = process_identity_helpers.process_start_marker(child.pid)
        assert marker is not None
        identity = f"pid:{child.pid};started:{marker}"

        assert not process_identity_helpers.owner_process_stopped(identity)
        assert tuple(handles_windows._RETAINED_OWNERS) == retained_before

        assert child.stdin is not None
        child.stdin.close()
        assert child.wait(timeout=10) == 0

        assert process_identity_helpers.owner_process_stopped(identity)
        assert tuple(handles_windows._RETAINED_OWNERS) == retained_before
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        if child.stdin is not None and not child.stdin.closed:
            child.stdin.close()
        if child.stderr is not None:
            child.stderr.close()


class _FakeBfeCall:
    argtypes: object
    restype: object

    def __init__(self, callback: Any) -> None:
        self._callback = callback

    def __call__(self, *args: Any) -> Any:
        return self._callback(*args)


class _FakeBfeApi:
    manager_handle = 101
    service_handle = 202

    def __init__(self, fail_close_once_for: int) -> None:
        self.close_attempts: list[int] = []
        self._fail_close_once_for = fail_close_once_for
        self._close_failed = False
        self.OpenSCManagerW = _FakeBfeCall(lambda *_args: self.manager_handle)
        self.OpenServiceW = _FakeBfeCall(lambda *_args: self.service_handle)
        self.QueryServiceStatusEx = _FakeBfeCall(self._query_service_status)
        self.CloseServiceHandle = _FakeBfeCall(self._close_service_handle)

    @staticmethod
    def _query_service_status(
        _service: Any,
        _info_level: int,
        status_buffer: Any,
        _status_size: int,
        _needed: Any,
    ) -> int:
        status = ctypes.cast(
            status_buffer, ctypes.POINTER(preflight_windows.SERVICE_STATUS_PROCESS)
        )
        status.contents.dwCurrentState = 4
        return 1

    def _close_service_handle(self, handle: Any) -> int:
        raw_value = getattr(handle, "value", handle)
        value = int(raw_value)
        self.close_attempts.append(value)
        if value == self._fail_close_once_for and not self._close_failed:
            self._close_failed = True
            return 0
        return 1


class _FakeAdministratorApi:
    def __init__(self) -> None:
        self.IsUserAnAdmin = _FakeBfeCall(lambda: 1)


@pytest.mark.parametrize(
    "failed_handle",
    [_FakeBfeApi.service_handle, _FakeBfeApi.manager_handle],
    ids=["service", "manager"],
)
def test_bfe_probe_retains_failed_close_and_retries_before_next_probe(
    monkeypatch: pytest.MonkeyPatch,
    failed_handle: int,
) -> None:
    api = _FakeBfeApi(failed_handle)
    administrator_api = _FakeAdministratorApi()
    loaded_dlls: list[str] = []

    def load_api(name: str) -> Any:
        loaded_dlls.append(name)
        return api if name == "advapi32.dll" else administrator_api

    monkeypatch.setattr(preflight_windows, "load_windows_dll", load_api)
    probes = preflight_windows.SystemWindowsPrerequisiteProbes()

    with pytest.raises(OSError, match="CloseServiceHandle"):
        probes.bfe_is_running()

    assert api.close_attempts == [api.service_handle, api.manager_handle]
    assert len(handles_windows._RETAINED_OWNERS) == 1
    retained = handles_windows._RETAINED_OWNERS[0]
    assert isinstance(retained, OwnedHandle)
    assert retained.value == failed_handle

    assert probes.is_elevated() is True
    assert handles_windows._RETAINED_OWNERS == []
    assert api.close_attempts[-1] == failed_handle
    assert api.close_attempts.count(failed_handle) == 2
    assert loaded_dlls == ["advapi32.dll", "shell32.dll"]


class _FailingMsvcrt:
    @staticmethod
    def open_osfhandle(_handle: int, _flags: int) -> int:
        raise OSError("injected CRT transfer failure")


def test_crt_transfer_retains_handle_when_primary_and_close_both_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    def fail_close_once(_value: int) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("injected original handle close failure")

    owner = OwnedHandle(600, closer=fail_close_once)
    monkeypatch.setitem(sys.modules, "msvcrt", _FailingMsvcrt)

    with pytest.raises(OSError, match="original handle close failure") as failure:
        handles_windows.transfer_handle_to_crt_descriptor(owner)

    assert failure.value.__cause__ is not None
    assert "CRT transfer failure" in str(failure.value.__cause__)
    assert [owner] == handles_windows._RETAINED_OWNERS

    handles_windows.retry_retained_owners()
    assert owner.closed
    assert attempts == 2


def test_fs_crt_transfer_failure_closes_the_original_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[int] = []

    class FakeDirectory:
        def __enter__(self) -> FakeDirectory:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        @staticmethod
        def open_regular(_relative: str) -> OwnedHandle:
            return OwnedHandle(601, closer=closed.append)

    monkeypatch.setitem(sys.modules, "msvcrt", _FailingMsvcrt)
    monkeypatch.setattr(fs_windows.WindowsDirectory, "open", lambda *_args: FakeDirectory())

    with pytest.raises(OSError, match="CRT transfer failure"):
        fs_windows.open_regular_file(tmp_path, "source.py")

    assert closed == [601]


def test_git_crt_transfer_failure_closes_the_original_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[int] = []
    directory = object.__new__(fs_windows.WindowsDirectory)
    monkeypatch.setattr(
        directory,
        "open_regular",
        lambda _relative: OwnedHandle(602, closer=closed.append),
    )
    monkeypatch.setitem(sys.modules, "msvcrt", _FailingMsvcrt)

    with pytest.raises(OSError, match="CRT transfer failure"):
        git_metadata._open_regular_file_at(directory, "source.py")

    assert closed == [602]


def test_capability_crt_transfer_failure_closes_the_original_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[int] = []
    monkeypatch.setitem(sys.modules, "msvcrt", _FailingMsvcrt)
    monkeypatch.setattr(
        capability_windows,
        "OwnedHandle",
        lambda value: OwnedHandle(value, closer=closed.append),
    )

    with pytest.raises(CapabilityError, match="take ownership"):
        capability_windows.read_bytes_from_handle(603, maximum_bytes=10)

    assert closed == [603]


def test_capability_retains_descriptor_when_final_close_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    reads = iter((b"secret", b""))

    def fail_close_once(_value: int) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("injected capability descriptor close failure")

    def transfer(owner: OwnedHandle, _flags: int) -> handles_windows.OwnedCrtDescriptor:
        owner.detach()
        return descriptor

    descriptor = handles_windows.OwnedCrtDescriptor(706, closer=fail_close_once)
    monkeypatch.setattr(
        capability_windows,
        "OwnedHandle",
        lambda value: OwnedHandle(value, closer=lambda _value: None),
    )
    monkeypatch.setattr(capability_windows, "transfer_handle_to_crt_descriptor", transfer)
    monkeypatch.setattr(os, "read", lambda _descriptor, _count: next(reads))

    with pytest.raises(CapabilityError, match="close protected input"):
        capability_windows.read_bytes_from_handle(606, maximum_bytes=10)

    assert [descriptor] == handles_windows._RETAINED_OWNERS
    handles_windows.retry_retained_owners()
    assert descriptor.closed
    assert attempts == 2


def test_fs_fstat_failure_retains_descriptor_when_close_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = 0

    class FakeDirectory:
        def __enter__(self) -> FakeDirectory:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        @staticmethod
        def open_regular(_relative: str) -> OwnedHandle:
            return OwnedHandle(604, closer=lambda _value: None)

    def fail_descriptor_close(_value: int) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("injected descriptor close failure")

    descriptor = handles_windows.OwnedCrtDescriptor(704, closer=fail_descriptor_close)
    monkeypatch.setattr(fs_windows.WindowsDirectory, "open", lambda *_args: FakeDirectory())
    monkeypatch.setattr(fs_windows, "transfer_handle_to_crt_descriptor", lambda *_args: descriptor)
    monkeypatch.setattr(
        os,
        "fstat",
        lambda _descriptor: (_ for _ in ()).throw(OSError("injected fstat failure")),
    )

    with pytest.raises(OSError, match="descriptor close failure") as failure:
        fs_windows.open_regular_file(tmp_path, "source.py")

    assert failure.value.__cause__ is not None
    assert "fstat failure" in str(failure.value.__cause__)
    assert [descriptor] == handles_windows._RETAINED_OWNERS

    handles_windows.retry_retained_owners()
    assert descriptor.closed
    assert attempts == 2


def test_open_regular_retains_parent_and_result_when_delivery_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: dict[int, int] = {}

    def fail_once(value: int) -> None:
        attempts[value] = attempts.get(value, 0) + 1
        if attempts[value] == 1:
            raise OSError(f"injected close failure for {value}")

    root = fs_windows.WindowsRoot(
        Path("/authorized-workspace"),
        OwnedHandle(710, closer=lambda _value: None),
        r"\\?\C:\authorized-workspace",
        fs_windows.WindowsFileIdentity(17, b"root".ljust(16, b"\0")),
    )
    directory = fs_windows.WindowsDirectory(
        root, OwnedHandle(711, closer=lambda _value: None), owns_root=False
    )
    parent = OwnedHandle(712, closer=fail_once)
    result = OwnedHandle(713, closer=fail_once)
    opened = iter((parent, result))
    monkeypatch.setattr(fs_windows, "_open_relative", lambda *_args, **_kwargs: next(opened))

    with pytest.raises(OSError, match="close failure for 713") as failure:
        directory.open_regular("nested/source.py")

    assert failure.value.__cause__ is not None
    assert "close failure for 712" in str(failure.value.__cause__)
    assert [parent, result] == handles_windows._RETAINED_OWNERS

    handles_windows.retry_retained_owners()
    assert parent.closed
    assert result.closed
    assert attempts == {712: 2, 713: 2}


def test_open_regular_file_retains_descriptor_when_directory_exit_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: dict[int, int] = {}

    def fail_once(value: int) -> None:
        attempts[value] = attempts.get(value, 0) + 1
        if attempts[value] == 1:
            raise OSError(f"injected close failure for {value}")

    directory_owner = OwnedHandle(714, closer=fail_once)
    descriptor = handles_windows.OwnedCrtDescriptor(715, closer=fail_once)

    class FakeDirectory:
        def __enter__(self) -> FakeDirectory:
            return self

        def __exit__(self, *_exc: object) -> None:
            handles_windows.close_owned_resources((directory_owner,))

        @staticmethod
        def open_regular(_relative: str) -> OwnedHandle:
            return OwnedHandle(716, closer=lambda _value: None)

    monkeypatch.setattr(fs_windows.WindowsDirectory, "open", lambda *_args: FakeDirectory())
    monkeypatch.setattr(fs_windows, "transfer_handle_to_crt_descriptor", lambda *_args: descriptor)
    monkeypatch.setattr(os, "fstat", lambda _descriptor: os.stat_result((0,) * 10))

    with pytest.raises(OSError, match="close failure for 715") as failure:
        fs_windows.open_regular_file(tmp_path, "source.py")

    assert failure.value.__cause__ is not None
    assert "close failure for 714" in str(failure.value.__cause__)
    assert [directory_owner, descriptor] == handles_windows._RETAINED_OWNERS

    handles_windows.retry_retained_owners()
    assert directory_owner.closed
    assert descriptor.closed
    assert attempts == {714: 2, 715: 2}


def test_git_fstat_failure_closes_descriptor_before_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[int] = []
    directory = object.__new__(fs_windows.WindowsDirectory)
    descriptor = handles_windows.OwnedCrtDescriptor(705, closer=closed.append)
    monkeypatch.setattr(
        directory,
        "open_regular",
        lambda _relative: OwnedHandle(605, closer=lambda _value: None),
    )
    monkeypatch.setattr(
        git_metadata, "transfer_handle_to_crt_descriptor", lambda *_args: descriptor
    )
    monkeypatch.setattr(
        os,
        "fstat",
        lambda _descriptor: (_ for _ in ()).throw(OSError("injected git fstat failure")),
    )

    with pytest.raises(OSError, match="git fstat failure"):
        git_metadata._open_regular_file_at(directory, "source.py")

    assert descriptor.closed
    assert closed == [705]


def test_windows_write_all_handle_retries_partial_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    written_chunks: list[bytes] = []

    class WriteFileCall:
        argtypes: object
        restype: object

        def __call__(
            self, _handle: Any, buffer: Any, length: int, written: Any, _overlapped: Any
        ) -> int:
            count = min(int(length), 2)
            written_chunks.append(ctypes.string_at(buffer, count))
            ctypes.cast(written, ctypes.POINTER(ctypes.c_uint32))[0] = count
            return 1

    class FakeWriteApi:
        def __init__(self) -> None:
            self.WriteFile = WriteFileCall()

    monkeypatch.setattr(handles_windows, "load_windows_dll", lambda _name: FakeWriteApi())

    handles_windows.write_all_handle(42, b"abcdef", chunk_size=4)

    assert b"".join(written_chunks) == b"abcdef"


def test_windows_shared_file_lock_uses_nonblocking_read_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operations: list[int] = []

    class FakeMsvcrt:
        LK_NBRLCK = 11
        LK_NBLCK = 12
        LK_UNLCK = 13

        @staticmethod
        def locking(_fd: int, mode: int, _length: int) -> None:
            operations.append(mode)

    monkeypatch.setitem(sys.modules, "msvcrt", FakeMsvcrt)
    lock = WindowsSharedFileLock(tmp_path / "shared.lock")

    lock.acquire()
    lock.release()

    assert operations == [FakeMsvcrt.LK_NBRLCK, FakeMsvcrt.LK_UNLCK]


@pytest.mark.parametrize(
    "component",
    [
        "",
        ".",
        "..",
        "C:",
        "C:escape",
        "child/name",
        "child\\name",
        "stream:secret",
        "\\\\server",
        "\\?\\device",
        "nul\0suffix",
        "a" * 256,
    ],
)
def test_windows_component_guard_rejects_every_forbidden_shape(component: str) -> None:
    with pytest.raises(InvalidInputError):
        validate_component(component)


def test_windows_component_guard_accepts_the_explicit_ntfs_boundary() -> None:
    assert validate_component("a" * 255) == "a" * 255
    assert validate_component("source.py") == "source.py"


def test_windows_rename_info_uses_fixed_16_bit_wchar_layout() -> None:
    pointer_size = ctypes.sizeof(ctypes.c_void_p)

    assert ctypes.sizeof(fs_windows.WINDOWS_WCHAR) == 2
    assert fs_windows.FILE_RENAME_INFO.RootDirectory.offset == (8 if pointer_size == 8 else 4)
    assert fs_windows.FILE_RENAME_INFO.FileNameLength.offset == (
        fs_windows.FILE_RENAME_INFO.RootDirectory.offset + pointer_size
    )
    assert fs_windows.FILE_RENAME_INFO.FileName.offset == (
        fs_windows.FILE_RENAME_INFO.FileNameLength.offset + ctypes.sizeof(ctypes.c_uint32)
    )
    assert ctypes.sizeof(fs_windows.FILE_RENAME_INFO) == (24 if pointer_size == 8 else 16)


@pytest.mark.parametrize("target_name", ["x", "a" * 255])
@pytest.mark.parametrize("replace", [False, True])
def test_windows_handle_relative_rename_emits_nt_class_10_abi(
    target_name: str,
    replace: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeApi:
        @staticmethod
        def NtSetInformationFile(
            source: ctypes.c_void_p,
            io_status: Any,
            buffer: Any,
            size: int,
            info_class: int,
        ) -> int:
            captured.update(
                source=source.value,
                info_class=info_class,
                io_status=bytes(
                    ctypes.string_at(io_status, ctypes.sizeof(fs_windows.IO_STATUS_BLOCK))
                ),
                raw=bytes(ctypes.string_at(buffer, size)),
                size=size,
            )
            return 0

        @staticmethod
        def RtlNtStatusToDosError(_status: ctypes.c_int32) -> int:
            return 0

    monkeypatch.setattr(fs_windows, "_ntdll", lambda: FakeApi())

    fs_windows._rename_handle(41, 73, target_name, replace=replace)

    encoded_name = target_name.encode("utf-16-le")
    raw = captured["raw"]
    root_offset = fs_windows.FILE_RENAME_INFO.RootDirectory.offset
    root_end = root_offset + ctypes.sizeof(ctypes.c_void_p)
    length_offset = fs_windows.FILE_RENAME_INFO.FileNameLength.offset
    name_offset = fs_windows.FILE_RENAME_INFO.FileName.offset
    assert captured["source"] == 41
    assert captured["info_class"] == 10
    assert captured["size"] == ctypes.sizeof(fs_windows.FILE_RENAME_INFO) + len(encoded_name)
    assert captured["io_status"] == bytes(ctypes.sizeof(fs_windows.IO_STATUS_BLOCK))
    assert int.from_bytes(raw[:4], "little") == int(replace)
    assert raw[4:root_offset] == bytes(root_offset - 4)
    assert int.from_bytes(raw[root_offset:root_end], "little") == 73
    assert int.from_bytes(raw[length_offset : length_offset + 4], "little") == len(encoded_name)
    assert raw[name_offset : name_offset + len(encoded_name)] == encoded_name
    assert raw[name_offset + len(encoded_name) :] == bytes(
        captured["size"] - name_offset - len(encoded_name)
    )
    assert handles_windows._RETAINED_OWNERS == []


@pytest.mark.parametrize("replace", [False, True])
@pytest.mark.parametrize(
    ("error", "exception_type"),
    [(fs_windows.ERROR_ALREADY_EXISTS, FileExistsError), (5, OSError)],
)
def test_windows_handle_relative_rename_maps_ntstatus_failures(
    replace: bool,
    error: int,
    exception_type: type[OSError],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeApi:
        @staticmethod
        def NtSetInformationFile(*_args: Any) -> int:
            return -1

        @staticmethod
        def RtlNtStatusToDosError(status: ctypes.c_int32) -> int:
            assert status.value == -1
            return error

    monkeypatch.setattr(fs_windows, "_ntdll", lambda: FakeApi())

    with pytest.raises(
        exception_type, match=r"NtSetInformationFile\(FileRenameInformation\)"
    ) as failure:
        fs_windows._rename_handle(41, 73, "target", replace=replace)

    assert failure.value.errno == error


def _write_windows_directory_page(buffer: Any, name: str) -> None:
    ctypes.memset(buffer, 0, ctypes.sizeof(buffer))
    record = fs_windows.FILE_ID_BOTH_DIR_INFO.from_buffer(buffer)
    encoded = name.encode("utf-16-le")
    record.FileNameLength = len(encoded)
    record.EndOfFile = len(encoded)
    ctypes.memmove(
        ctypes.addressof(buffer) + fs_windows.FILE_ID_BOTH_DIR_INFO.FileName.offset,
        encoded,
        len(encoded),
    )


def test_windows_directory_enumeration_restarts_each_independent_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeApi:
        def __init__(self) -> None:
            self.calls: list[int] = []
            self.cursor = 0
            self.error = 0

        def GetFileInformationByHandleEx(
            self, _handle: Any, info_class: int, buffer: Any, _size: int
        ) -> int:
            self.calls.append(info_class)
            if info_class == 11:
                self.cursor = 0
            if self.cursor == 2:
                self.error = fs_windows.ERROR_NO_MORE_FILES
                return 0
            _write_windows_directory_page(buffer, ("alpha", "beta")[self.cursor])
            self.cursor += 1
            return 1

    api = FakeApi()
    monkeypatch.setattr(fs_windows, "_kernel32", lambda: api)
    monkeypatch.setattr(fs_windows, "last_error", lambda: api.error)

    first = fs_windows._list_handle(41)
    second = fs_windows._list_handle(41)

    assert [entry.name for entry in first] == ["alpha", "beta"]
    assert second == first
    assert api.calls == [11, 10, 10, 11, 10, 10]


def test_windows_directory_enumeration_returns_empty_on_initial_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    class FakeApi:
        @staticmethod
        def GetFileInformationByHandleEx(
            _handle: Any, info_class: int, _buffer: Any, _size: int
        ) -> int:
            calls.append(info_class)
            return 0

    monkeypatch.setattr(fs_windows, "_kernel32", lambda: FakeApi())
    monkeypatch.setattr(fs_windows, "last_error", lambda: fs_windows.ERROR_NO_MORE_FILES)

    assert fs_windows._list_handle(41) == []
    assert calls == [11]


def test_windows_directory_enumeration_fails_closed_midstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    class FakeApi:
        @staticmethod
        def GetFileInformationByHandleEx(
            _handle: Any, info_class: int, buffer: Any, _size: int
        ) -> int:
            calls.append(info_class)
            if len(calls) == 1:
                _write_windows_directory_page(buffer, "alpha")
                return 1
            return 0

    monkeypatch.setattr(fs_windows, "_kernel32", lambda: FakeApi())
    monkeypatch.setattr(fs_windows, "last_error", lambda: 5)

    with pytest.raises(OSError, match="FileIdBothDirectoryInfo") as failure:
        fs_windows._list_handle(41)

    assert failure.value.errno == 5
    assert calls == [11, 10]


def test_windows_target_parent_open_requests_traverse_and_read_attributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_access: list[int] = []
    root_identity = fs_windows.WindowsFileIdentity(17, b"root".ljust(16, b"\0"))
    root = fs_windows.WindowsRoot(
        Path("/authorized-workspace"),
        OwnedHandle(710, closer=lambda _value: None),
        r"\\?\C:\authorized-workspace",
        root_identity,
    )

    def open_relative(
        _parent: int,
        _component: str,
        *,
        access: int,
        directory: bool | None,
    ) -> OwnedHandle:
        assert directory is True
        requested_access.append(access)
        return OwnedHandle(711, closer=lambda _value: None)

    monkeypatch.setattr(fs_windows, "_open_relative", open_relative)
    monkeypatch.setattr(fs_windows, "_identity", lambda _handle: root_identity)

    with root.open_parent(("artifacts", "target")) as (parent, name, owner):
        assert parent == 711
        assert name == "target"
        assert owner is not None

    assert requested_access == [
        fs_windows.FILE_LIST_DIRECTORY | fs_windows.FILE_TRAVERSE | fs_windows.FILE_READ_ATTRIBUTES
    ]


@pytest.mark.parametrize("relative", ["/rooted", "\\rooted", "a//b", "a/../b", "a/./b"])
def test_windows_relative_path_rejects_rooted_empty_and_dot_components(relative: str) -> None:
    with pytest.raises(InvalidInputError):
        relative_components(relative)


def test_windows_output_budget_is_one_atomic_budget_across_both_streams() -> None:
    budget = launcher_windows._OutputBudget(100)
    allowed: list[int] = []

    def reserve() -> None:
        allowed.append(budget.reserve(60))

    threads = [threading.Thread(target=reserve) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(allowed) == [40, 60]
    assert budget.exceeded.is_set()


class _BoundedReadApi:
    def __init__(self, content: bytes) -> None:
        self.remaining = content

    def ReadFile(self, _handle: Any, buffer: Any, length: int, count: Any, _overlapped: Any) -> int:
        chunk = self.remaining[: int(length)]
        self.remaining = self.remaining[len(chunk) :]
        if chunk:
            ctypes.memmove(buffer, chunk, len(chunk))
        ctypes.cast(count, ctypes.POINTER(ctypes.c_uint32))[0] = len(chunk)
        return 1


def test_windows_read_regular_accepts_the_exact_size_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fs_windows, "_kernel32", lambda: _BoundedReadApi(b"abcd"))

    assert fs_windows._read_handle_bounded(41, maximum_bytes=4) == b"abcd"


def test_windows_read_regular_fails_closed_above_the_size_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fs_windows, "_kernel32", lambda: _BoundedReadApi(b"abcde"))

    with pytest.raises(OSError, match="bounded read limit"):
        fs_windows._read_handle_bounded(41, maximum_bytes=4)


def test_windows_publish_verifies_through_the_fixed_temp_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[tuple[int, str, int]] = []
    renamed: list[tuple[int, int, str]] = []
    read_only_handles: list[int] = []
    outside_sentinel = {"touched": False}

    class FakeRoot:
        @contextmanager
        def open_parent(self, parts: tuple[str, ...]) -> Any:
            parent = 10 if parts[-1] == "candidate" else 20
            yield parent, parts[-1], None

    def open_relative(
        parent: int,
        component: str,
        *,
        access: int,
        disposition: int = fs_windows.FILE_OPEN,
        directory: bool | None,
        reject_reparse: bool = True,
    ) -> OwnedHandle:
        del access, directory, reject_reparse
        opened.append((parent, component, disposition))
        if parent == 10 and component == "candidate" and disposition == fs_windows.FILE_OPEN:
            raise FileNotFoundError
        if parent == 10 and component == "candidate":
            return OwnedHandle(100, closer=lambda _value: None)
        if parent == 100 and component == "manifest.json":
            return OwnedHandle(101, closer=lambda _value: None)
        outside_sentinel["touched"] = True
        raise AssertionError("verification escaped the fixed temporary-directory handle")

    def list_handle(handle: int) -> list[fs_windows.WindowsDirectoryEntry]:
        if handle != 100:
            outside_sentinel["touched"] = True
            pytest.fail("directory verification used an unexpected handle")
        return [fs_windows.WindowsDirectoryEntry("manifest.json", 0, 2)]

    def read_handle(handle: int, *, maximum_bytes: int) -> bytes:
        if (handle, maximum_bytes) != (101, 2):
            outside_sentinel["touched"] = True
            pytest.fail("file verification used an unexpected handle or bound")
        return b"{}"

    identity = fs_windows.WindowsFileIdentity(17, b"fixed".ljust(16, b"\0"))
    monkeypatch.setattr(fs_windows, "_open_relative", open_relative)
    monkeypatch.setattr(fs_windows, "_identity", lambda _handle: identity)
    monkeypatch.setattr(fs_windows, "write_new_file_at", lambda *_args: None)
    monkeypatch.setattr(fs_windows, "_list_handle", list_handle)
    monkeypatch.setattr(fs_windows, "_read_handle_bounded", read_handle)
    monkeypatch.setattr(fs_windows, "_mark_read_only", read_only_handles.append)
    monkeypatch.setattr(
        fs_windows,
        "_rename_handle",
        lambda source, parent, name, *, replace: renamed.append((source, parent, name)),
    )
    tree: Any = object.__new__(fs_windows.WindowsDataTree)
    tree._root = FakeRoot()
    tree._label = "test"

    def verify(directory: fs_windows.WindowsPublicationDirectory) -> None:
        assert directory.list_directory() == {"manifest.json"}
        assert directory.read_regular("manifest.json") == b"{}"

    tree.publish_directory(
        "artifacts/candidate",
        "artifacts/final",
        {"manifest.json": b"{}"},
        verify=verify,
        before_rename=None,
    )

    assert not outside_sentinel["touched"]
    assert renamed == [(100, 20, "final")]
    assert read_only_handles == [101, 100]
    assert all(parent in {10, 100} for parent, _name, _disposition in opened)


def test_windows_launcher_and_fs_do_not_contain_forbidden_fallbacks() -> None:
    launcher_source = inspect.getsource(launcher_windows)
    filesystem_source = inspect.getsource(fs_windows)

    assert "subprocess.Popen" not in launcher_source
    assert ".communicate(" not in launcher_source
    assert "DeleteFileW" not in filesystem_source
    assert "RemoveDirectoryW" not in filesystem_source
    assert "GetFileAttributesW" not in filesystem_source
    assert "CREATE_NO_WINDOW" in launcher_source
    assert "CREATE_SUSPENDED" in launcher_source


def test_windows_git_resolver_accepts_only_real_mingw_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_git = tmp_path / "Git" / "mingw64" / "bin" / "git.exe"
    shim = tmp_path / "Git" / "cmd" / "git.exe"
    real_git.parent.mkdir(parents=True)
    shim.parent.mkdir(parents=True)
    real_git.write_bytes(b"real")
    shim.write_bytes(b"shim")
    monkeypatch.setattr(sandbox_windows, "windows_git_candidates", lambda: (shim, real_git))

    assert sandbox_windows.resolve_windows_git_executable() == str(real_git.resolve())


def test_windows_git_resolver_rejects_cmd_shim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shim = tmp_path / "Git" / "cmd" / "git.exe"
    shim.parent.mkdir(parents=True)
    shim.write_bytes(b"shim")
    monkeypatch.setattr(sandbox_windows, "windows_git_candidates", lambda: (shim,))

    with pytest.raises(OSError, match=r"cmd\\git.exe"):
        sandbox_windows.resolve_windows_git_executable()


def test_windows_filetime_marker_preserves_all_64_bits() -> None:
    marker = FILETIME(0x89ABCDEF, 0x01234567)
    assert marker.as_uint64() == 0x0123456789ABCDEF


class _FakeWfpApi:
    def __init__(self, *, corrupt_readback: bool = False, sublayer_status: int = 0) -> None:
        self.blob_data = (ctypes.c_ubyte * 4)(1, 2, 3, 4)
        self.blob = sandbox_windows.FWP_BYTE_BLOB(4, self.blob_data)
        self.retrieved = sandbox_windows.FWPM_FILTER0()
        self.retrieved_condition = sandbox_windows.FWPM_FILTER_CONDITION0()
        self.layers: dict[int, sandbox_windows.GUID] = {}
        self.added_layers: list[str] = []
        self.readbacks: list[int] = []
        self.free_calls = 0
        self.close_calls = 0
        self.corrupt_readback = corrupt_readback
        self.close_statuses: list[int] = []
        self.sublayer_calls = 0
        self.sublayer_status = sublayer_status

    def FwpmEngineOpen0(self, *_args: Any) -> int:
        output = ctypes.cast(_args[-1], ctypes.POINTER(ctypes.c_void_p))
        output[0] = ctypes.c_void_p(901)
        return 0

    def FwpmSubLayerAdd0(self, *_args: Any) -> int:
        self.sublayer_calls += 1
        return self.sublayer_status

    def FwpmGetAppIdFromFileName0(self, _path: str, output: Any) -> int:
        pointer = ctypes.cast(output, ctypes.POINTER(ctypes.POINTER(sandbox_windows.FWP_BYTE_BLOB)))
        pointer[0] = ctypes.pointer(self.blob)
        return 0

    def FwpmFilterAdd0(self, _engine: Any, filter_pointer: Any, _security: Any, output: Any) -> int:
        filter_object = ctypes.cast(
            filter_pointer, ctypes.POINTER(sandbox_windows.FWPM_FILTER0)
        ).contents
        self.added_layers.append(str(filter_object.displayData.name))
        identifier = len(self.added_layers)
        self.layers[identifier] = filter_object.layerKey
        ctypes.cast(output, ctypes.POINTER(ctypes.c_uint64))[0] = identifier
        return 0

    def FwpmFilterGetById0(self, _engine: Any, identifier: int, output: Any) -> int:
        self.readbacks.append(int(identifier))
        self.retrieved.filterId = identifier
        self.retrieved.layerKey = self.layers[int(identifier)]
        self.retrieved.action.type = sandbox_windows.FWP_ACTION_BLOCK
        if self.corrupt_readback and int(identifier) == len(sandbox_windows.LAYER_KEYS):
            self.retrieved.action.type = 0
        self.retrieved.numFilterConditions = 1
        self.retrieved_condition.fieldKey = sandbox_windows.APP_ID_KEY
        self.retrieved_condition.matchType = sandbox_windows.FWP_MATCH_EQUAL
        self.retrieved_condition.conditionValue.type = sandbox_windows.FWP_BYTE_BLOB_TYPE
        self.retrieved_condition.conditionValue.value.byteBlob = ctypes.pointer(self.blob)
        self.retrieved.filterCondition = ctypes.pointer(self.retrieved_condition)
        pointer = ctypes.cast(output, ctypes.POINTER(ctypes.POINTER(sandbox_windows.FWPM_FILTER0)))
        pointer[0] = ctypes.pointer(self.retrieved)
        return 0

    def FwpmFreeMemory0(self, _pointer: Any) -> None:
        self.free_calls += 1

    def FwpmEngineClose0(self, _engine: Any) -> int:
        self.close_calls += 1
        return self.close_statuses.pop(0) if self.close_statuses else 0


def test_wfp_filter_structure_keeps_the_native_64_bit_union_offsets() -> None:
    assert ctypes.sizeof(sandbox_windows.FWPM_FILTER_CONTEXT0) == 16
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        assert sandbox_windows.FWPM_FILTER0.context.offset == 152
        assert sandbox_windows.FWPM_FILTER0.reserved.offset == 168
        assert sandbox_windows.FWPM_FILTER0.filterId.offset == 176
        assert ctypes.sizeof(sandbox_windows.FWPM_FILTER0) == 200


def test_wfp_policy_write_probe_adds_only_a_dynamic_sublayer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWfpApi()
    monkeypatch.setattr(sandbox_windows, "_wfp_api", lambda: api)

    sandbox_windows.probe_wfp_policy_write_access()

    assert api.sublayer_calls == 1
    assert api.added_layers == []
    assert api.readbacks == []
    assert api.close_calls == 1


def test_wfp_policy_write_probe_fails_closed_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWfpApi(sublayer_status=5)
    monkeypatch.setattr(sandbox_windows, "_wfp_api", lambda: api)

    with pytest.raises(PermissionError, match="FwpmSubLayerAdd0"):
        sandbox_windows.probe_wfp_policy_write_access()

    assert api.close_calls == 1


def test_wfp_session_requires_four_filter_additions_and_readbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWfpApi()
    monkeypatch.setattr(sandbox_windows, "_wfp_api", lambda: api)

    session = sandbox_windows.WfpSession.create(r"C:\Program Files\Git\mingw64\bin\git.exe")

    assert session.verified
    assert session.filter_ids == (1, 2, 3, 4)
    assert len(api.added_layers) == 4
    assert api.readbacks == [1, 2, 3, 4]
    session.close()
    session.close()
    assert api.close_calls == 1
    assert api.free_calls == 5


def test_wfp_session_fails_closed_when_filter_readback_differs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWfpApi(corrupt_readback=True)
    monkeypatch.setattr(sandbox_windows, "_wfp_api", lambda: api)

    with pytest.raises(OSError, match="readback did not match"):
        sandbox_windows.WfpSession.create(r"C:\Program Files\Git\mingw64\bin\git.exe")

    assert api.close_calls == 1
    assert api.free_calls == 5


def test_wfp_session_retains_engine_ownership_when_close_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWfpApi()
    api.close_statuses = [5, 0]
    monkeypatch.setattr(sandbox_windows, "_wfp_api", lambda: api)
    session = sandbox_windows.WfpSession.create(r"C:\Program Files\Git\mingw64\bin\git.exe")

    with pytest.raises(OSError, match="FwpmEngineClose0"):
        session.close()

    assert session._engine == 0
    assert [(api, 901)] == sandbox_windows._RETAINED_WFP_ENGINES
    sandbox_windows._retry_retained_wfp_engines()
    assert sandbox_windows._RETAINED_WFP_ENGINES == []
    assert api.close_calls == 2


def test_wfp_construction_retains_engine_when_cleanup_close_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWfpApi(corrupt_readback=True)
    api.close_statuses = [5, 0]
    monkeypatch.setattr(sandbox_windows, "_wfp_api", lambda: api)
    sandbox_windows._RETAINED_WFP_ENGINES.clear()

    with pytest.raises(OSError, match="construction cleanup") as failure:
        sandbox_windows.WfpSession.create(r"C:\Program Files\Git\mingw64\bin\git.exe")

    assert failure.value.__cause__ is not None
    assert "readback did not match" in str(failure.value.__cause__)
    assert [(api, 901)] == sandbox_windows._RETAINED_WFP_ENGINES

    sandbox_windows._retry_retained_wfp_engines()
    assert sandbox_windows._RETAINED_WFP_ENGINES == []
    assert api.close_calls == 2


def test_windows_directory_retains_each_handle_when_close_fails() -> None:
    directory_attempts: list[int] = []
    root_attempts: list[int] = []

    def fail_directory_once(value: int) -> None:
        directory_attempts.append(value)
        if len(directory_attempts) == 1:
            raise OSError("injected directory close failure")

    def fail_root_once(value: int) -> None:
        root_attempts.append(value)
        if len(root_attempts) == 1:
            raise OSError("injected root close failure")

    root_handle = OwnedHandle(811, closer=fail_root_once)
    root = fs_windows.WindowsRoot(
        Path("/authorized-workspace"),
        root_handle,
        r"\\?\C:\authorized-workspace",
        fs_windows.WindowsFileIdentity(17, b"root".ljust(16, b"\0")),
    )
    directory_handle = OwnedHandle(812, closer=fail_directory_once)
    directory = fs_windows.WindowsDirectory(root, directory_handle, owns_root=True)

    with pytest.raises(OSError, match="directory close failure"):
        directory.close()

    assert not directory_handle.closed
    assert not root_handle.closed
    assert directory._directory is directory_handle
    assert directory._owns_root

    handles_windows.retry_retained_owners()
    directory.close()
    assert directory_handle.closed
    assert root_handle.closed
    assert directory_attempts == [812, 812]
    assert root_attempts == [811, 811]


def test_windows_scan_scope_reuses_one_absolute_root_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_path = Path("/authorized-workspace")
    opened: list[Path] = []
    closed: list[int] = []
    child_closed: list[int] = []
    relative_opens: list[tuple[str, ...]] = []

    def open_root(_cls: type[fs_windows.WindowsRoot], path: Path) -> fs_windows.WindowsRoot:
        opened.append(path)
        return fs_windows.WindowsRoot(
            path,
            OwnedHandle(801, closer=closed.append),
            r"\\?\C:\authorized-workspace",
            fs_windows.WindowsFileIdentity(17, b"root".ljust(16, b"\0")),
        )

    monkeypatch.setattr(fs_windows.WindowsRoot, "open", classmethod(open_root))

    with fs_windows.bind_authorized_root(root_path) as boundary:

        def open_directory(parts: tuple[str, ...]) -> OwnedHandle:
            relative_opens.append(parts)
            return OwnedHandle(802, closer=child_closed.append)

        monkeypatch.setattr(boundary, "open_directory", open_directory)
        first = fs_windows.open_directory(root_path)
        second = fs_windows.open_directory(root_path)
        nested = fs_windows.open_directory(root_path / "nested-project", "src")
        first.close()
        second.close()
        nested.close()
        assert not boundary.handle.closed

    assert opened == [root_path]
    assert closed == [801]
    assert child_closed == [802]
    assert relative_opens == [("nested-project", "src")]


class _CreatePipeCall:
    argtypes: object
    restype: object

    def __init__(self, read_value: int, write_value: int) -> None:
        self.read_value = read_value
        self.write_value = write_value

    def __call__(self, read: Any, write: Any, _attributes: Any, _size: int) -> int:
        ctypes.cast(read, ctypes.POINTER(ctypes.c_void_p))[0] = self.read_value
        ctypes.cast(write, ctypes.POINTER(ctypes.c_void_p))[0] = self.write_value
        return 1


class _FailSetHandleInformationCall:
    argtypes: object
    restype: object

    def __call__(self, _handle: Any, _mask: int, _flags: int) -> int:
        return 0


class _PartialPipeApi:
    def __init__(self, read_value: int, write_value: int) -> None:
        self.CreatePipe = _CreatePipeCall(read_value, write_value)
        self.SetHandleInformation = _FailSetHandleInformationCall()


def test_output_pipe_partial_construction_closes_remaining_owner_after_close_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: dict[int, int] = {}
    closed: list[int] = []

    def close(value: int) -> None:
        attempts[value] = attempts.get(value, 0) + 1
        if value == 902 and attempts[value] == 1:
            raise OSError("injected output write close failure")
        closed.append(value)

    monkeypatch.setattr(
        launcher_windows, "OwnedHandle", lambda value: OwnedHandle(value, closer=close)
    )

    with pytest.raises(OSError, match="output write close failure") as failure:
        launcher_windows._create_output_pipe(_PartialPipeApi(901, 902))

    assert failure.value.__cause__ is not None
    assert "SetHandleInformation" in str(failure.value.__cause__)
    assert closed == [901]
    assert len(handles_windows._RETAINED_OWNERS) == 1

    handles_windows.retry_retained_owners()
    assert closed == [901, 902]


def test_output_pipe_invalid_pair_closes_the_valid_raw_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[int] = []
    monkeypatch.setattr(
        launcher_windows,
        "OwnedHandle",
        lambda value: OwnedHandle(value, closer=closed.append),
    )

    with pytest.raises(OSError, match="invalid handle pair"):
        launcher_windows._create_output_pipe(_PartialPipeApi(0, 906))

    assert closed == [906]


def test_job_partial_construction_retains_owner_when_configuration_and_close_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    class FakeJobApi:
        @staticmethod
        def CreateJobObjectW(_attributes: Any, _name: Any) -> int:
            return 907

        @staticmethod
        def SetInformationJobObject(*_args: Any) -> int:
            return 0

    def fail_close_once(_value: int) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("injected Job close failure")

    monkeypatch.setattr(
        launcher_windows,
        "OwnedHandle",
        lambda value: OwnedHandle(value, closer=fail_close_once),
    )

    with pytest.raises(OSError, match="Job close failure") as failure:
        launcher_windows._create_job(FakeJobApi(), 1)

    assert failure.value.__cause__ is not None
    assert "SetInformationJobObject" in str(failure.value.__cause__)
    assert len(handles_windows._RETAINED_OWNERS) == 1

    handles_windows.retry_retained_owners()
    assert attempts == 2


def test_transfer_pipe_partial_construction_closes_remaining_owner_after_close_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: dict[int, int] = {}
    closed: list[int] = []

    def close(value: int) -> None:
        attempts[value] = attempts.get(value, 0) + 1
        if value == 904 and attempts[value] == 1:
            raise OSError("injected transfer write close failure")
        closed.append(value)

    monkeypatch.setattr(
        capability_windows, "load_windows_dll", lambda _name: _PartialPipeApi(903, 904)
    )
    monkeypatch.setattr(
        capability_windows, "OwnedHandle", lambda value: OwnedHandle(value, closer=close)
    )

    with pytest.raises(OSError, match="transfer write close failure") as failure:
        WindowsTransferPipe.create()

    assert failure.value.__cause__ is not None
    assert "SetHandleInformation" in str(failure.value.__cause__)
    assert closed == [903]
    assert len(handles_windows._RETAINED_OWNERS) == 1

    handles_windows.retry_retained_owners()
    assert closed == [903, 904]


def test_nt_relative_open_retains_handle_when_validation_and_close_both_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    class FakeNtApi:
        @staticmethod
        def NtCreateFile(raw: Any, *_args: Any) -> int:
            ctypes.cast(raw, ctypes.POINTER(ctypes.c_void_p))[0] = 905
            return 0

    def fail_close_once(_value: int) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("injected NT handle close failure")

    monkeypatch.setattr(fs_windows, "_ntdll", lambda: FakeNtApi())
    monkeypatch.setattr(
        fs_windows,
        "_attributes",
        lambda _handle: (_ for _ in ()).throw(OSError("injected NT validation failure")),
    )
    monkeypatch.setattr(
        fs_windows, "OwnedHandle", lambda value: OwnedHandle(value, closer=fail_close_once)
    )

    with pytest.raises(OSError, match="NT handle close failure") as failure:
        fs_windows._open_relative(17, "source.py", access=1, directory=False)

    assert failure.value.__cause__ is not None
    assert "NT validation failure" in str(failure.value.__cause__)
    assert len(handles_windows._RETAINED_OWNERS) == 1

    handles_windows.retry_retained_owners()
    assert attempts == 2


class _FakeKernel32:
    def __init__(self, *, assign_ok: bool = True) -> None:
        self.assign_ok = assign_ok
        self.resumed = False
        self.terminated = False
        self.read_counts: dict[int, int] = {}

    def CreateProcessW(self, *_args: Any) -> int:
        process_info = ctypes.cast(
            _args[-1], ctypes.POINTER(launcher_windows.PROCESS_INFORMATION)
        ).contents
        process_info.hProcess = 501
        process_info.hThread = 502
        process_info.dwProcessId = 600
        process_info.dwThreadId = 601
        return 1

    def AssignProcessToJobObject(self, _job: Any, _process: Any) -> int:
        return int(self.assign_ok)

    def ResumeThread(self, _thread: Any) -> int:
        self.resumed = True
        return 1

    def WaitForSingleObject(self, _process: Any, _timeout: int) -> int:
        return launcher_windows.WAIT_OBJECT_0

    def GetExitCodeProcess(self, _process: Any, output: Any) -> int:
        ctypes.cast(output, ctypes.POINTER(ctypes.c_uint32))[0] = 0
        return 1

    def TerminateJobObject(self, _job: Any, _code: int) -> int:
        self.terminated = True
        return 1

    def TerminateProcess(self, _process: Any, _code: int) -> int:
        self.terminated = True
        return 1

    def ReadFile(self, handle: Any, buffer: Any, _length: int, count: Any, _overlapped: Any) -> int:
        value = int(handle.value)
        reads = self.read_counts.get(value, 0)
        self.read_counts[value] = reads + 1
        if reads == 0:
            content = b"stdout" if value == 101 else b"stderr"
            ctypes.memmove(buffer, content, len(content))
            ctypes.cast(count, ctypes.POINTER(ctypes.c_uint32))[0] = len(content)
            return 1
        ctypes.cast(count, ctypes.POINTER(ctypes.c_uint32))[0] = 0
        return 0


class _NeverStopsKernel32(_FakeKernel32):
    def WaitForSingleObject(self, _process: Any, _timeout: int) -> int:
        return launcher_windows.WAIT_TIMEOUT


class _FakeAttributeList:
    def __init__(self, handles: list[int]) -> None:
        self.handles = handles
        self.pointer = ctypes.c_void_p(701)

    def close(self) -> None:
        self.pointer = ctypes.c_void_p()


class _FakeGuard:
    verified = True

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.closed = False

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.events.append("guard_close_called")


class _FailingGuard(_FakeGuard):
    def __init__(self, events: list[str]) -> None:
        super().__init__(events)
        self.close_attempts = 0

    def close(self) -> None:
        self.close_attempts += 1
        if self.close_attempts == 1:
            raise OSError("injected guard close failure")
        super().close()


def _configure_fake_launcher(
    monkeypatch: pytest.MonkeyPatch, api: _FakeKernel32, closed: list[int]
) -> None:
    output_pairs = iter(((101, 102), (103, 104)))

    def handle(value: int) -> OwnedHandle:
        return OwnedHandle(value, closer=closed.append)

    monkeypatch.setattr(launcher_windows, "_kernel32", lambda: api)
    monkeypatch.setattr(launcher_windows, "OwnedHandle", lambda value: handle(value))
    monkeypatch.setattr(launcher_windows, "_create_job", lambda _api, _limit: handle(200))
    monkeypatch.setattr(
        launcher_windows,
        "_create_output_pipe",
        lambda _api: tuple(handle(value) for value in next(output_pairs)),
    )
    monkeypatch.setattr(launcher_windows, "_AttributeList", _FakeAttributeList)
    monkeypatch.setattr(
        WindowsTransferPipe,
        "create",
        classmethod(lambda _cls: WindowsTransferPipe(handle(301), handle(302))),
    )
    monkeypatch.setattr(launcher_windows, "last_error", lambda: launcher_windows.ERROR_BROKEN_PIPE)


def test_direct_launcher_assigns_before_resume_and_closes_wfp_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeKernel32()
    closed: list[int] = []
    _configure_fake_launcher(monkeypatch, api, closed)
    events: list[str] = []
    guard = _FakeGuard(events)

    result = launcher_windows.run_windows_process(
        launcher_windows.WindowsLaunchRequest(
            application=r"C:\Python312\python.exe",
            arguments=("-c", "pass"),
            cwd=r"C:\runtime",
            environment={"PATH": ""},
            maximum_output_bytes=1024,
            timeout_seconds=1.0,
            active_process_limit=1,
        ),
        network_guard=guard,
        observer=events.append,
    )

    assert result.stdout == b"stdout"
    assert result.stderr == b"stderr"
    assert api.resumed
    assert events[:6] == [
        "boundary_verified",
        "created_suspended",
        "assigned_job",
        "child_sides_closed",
        "resumed",
        "primary_thread_closed",
    ]
    assert events[-3:] == ["job_closed", "guard_close_called", "network_guard_closed"]
    assert closed.count(200) == 1
    assert closed.count(501) == 1
    assert closed.count(502) == 1


def test_direct_launcher_assign_failure_terminates_without_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeKernel32(assign_ok=False)
    closed: list[int] = []
    _configure_fake_launcher(monkeypatch, api, closed)
    events: list[str] = []
    guard = _FakeGuard(events)

    with pytest.raises(OSError, match="AssignProcessToJobObject"):
        launcher_windows.run_windows_process(
            launcher_windows.WindowsLaunchRequest(
                application=r"C:\Python312\python.exe",
                arguments=("-c", "pass"),
                cwd=r"C:\runtime",
                environment={},
                maximum_output_bytes=1024,
                timeout_seconds=1.0,
            ),
            network_guard=guard,
            observer=events.append,
        )

    assert api.terminated
    assert not api.resumed
    assert "resumed" not in events
    assert events[-3:] == ["job_closed", "guard_close_called", "network_guard_closed"]


def test_direct_launcher_closes_network_guard_when_api_loading_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    guard = _FakeGuard(events)

    def fail_api_load() -> None:
        raise OSError("kernel32 unavailable")

    monkeypatch.setattr(launcher_windows, "_kernel32", fail_api_load)

    with pytest.raises(OSError, match="kernel32 unavailable"):
        launcher_windows.run_windows_process(
            launcher_windows.WindowsLaunchRequest(
                application=r"C:\Python312\python.exe",
                arguments=("-c", "pass"),
                cwd=r"C:\runtime",
                environment={},
                maximum_output_bytes=1024,
                timeout_seconds=1.0,
            ),
            network_guard=guard,
            observer=events.append,
        )

    assert events == [
        "boundary_verified",
        "guard_close_called",
        "network_guard_closed",
    ]


def test_direct_launcher_reports_late_protected_input_writer_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeKernel32()
    closed: list[int] = []
    _configure_fake_launcher(monkeypatch, api, closed)

    def fail_write(_handle: int, _content: bytes) -> None:
        time.sleep(0.05)
        raise OSError("protected input write failed")

    monkeypatch.setattr(launcher_windows, "write_handle", fail_write)

    with pytest.raises(OSError, match="protected input write failed"):
        launcher_windows.run_windows_process(
            launcher_windows.WindowsLaunchRequest(
                application=r"C:\Python312\python.exe",
                arguments=("-c", "pass"),
                cwd=r"C:\runtime",
                environment={},
                maximum_output_bytes=1024,
                timeout_seconds=1.0,
            ),
            inputs=(launcher_windows.ProtectedInput("--capability-handle", b"secret"),),
        )


def test_direct_launcher_retains_wfp_when_child_exit_cannot_be_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _NeverStopsKernel32()
    closed: list[int] = []
    _configure_fake_launcher(monkeypatch, api, closed)
    events: list[str] = []
    guard = _FakeGuard(events)

    with pytest.raises(subprocess.TimeoutExpired):
        launcher_windows.run_windows_process(
            launcher_windows.WindowsLaunchRequest(
                application=r"C:\Python312\python.exe",
                arguments=("-c", "pass"),
                cwd=r"C:\runtime",
                environment={},
                maximum_output_bytes=1024,
                timeout_seconds=0.001,
            ),
            network_guard=guard,
            observer=events.append,
        )

    assert not guard.closed
    assert "network_guard_closed" not in events


def test_direct_launcher_continues_cleanup_after_attribute_close_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeKernel32()
    closed: list[int] = []
    _configure_fake_launcher(monkeypatch, api, closed)
    events: list[str] = []
    guard = _FakeGuard(events)

    class FailingAttributeList(_FakeAttributeList):
        def close(self) -> None:
            super().close()
            raise OSError("attribute close failed")

    monkeypatch.setattr(launcher_windows, "_AttributeList", FailingAttributeList)

    with pytest.raises(OSError, match="attribute close failed"):
        launcher_windows.run_windows_process(
            launcher_windows.WindowsLaunchRequest(
                application=r"C:\Python312\python.exe",
                arguments=("-c", "pass"),
                cwd=r"C:\runtime",
                environment={},
                maximum_output_bytes=1024,
                timeout_seconds=1.0,
            ),
            network_guard=guard,
            observer=events.append,
        )

    assert guard.closed
    assert events[-3:] == ["job_closed", "guard_close_called", "network_guard_closed"]


def test_direct_launcher_retains_complete_group_when_guard_close_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeKernel32()
    closed: list[int] = []
    _configure_fake_launcher(monkeypatch, api, closed)
    events: list[str] = []
    guard = _FailingGuard(events)

    with pytest.raises(OSError, match="guard close failure"):
        launcher_windows.run_windows_process(
            launcher_windows.WindowsLaunchRequest(
                application=r"C:\Python312\python.exe",
                arguments=("-c", "pass"),
                cwd=r"C:\runtime",
                environment={},
                maximum_output_bytes=1024,
                timeout_seconds=1.0,
            ),
            network_guard=guard,
            observer=events.append,
        )

    assert len(launcher_windows._RETAINED_CLEANUP_GROUPS) == 1
    assert "network_guard_closed" not in events
    launcher_windows._retry_retained_cleanup_groups()
    assert launcher_windows._RETAINED_CLEANUP_GROUPS == []
    assert guard.closed


def test_direct_launcher_reports_job_closed_only_after_retry_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeKernel32()
    closed: list[int] = []
    _configure_fake_launcher(monkeypatch, api, closed)
    job_attempts = 0

    def close_job(value: int) -> None:
        nonlocal job_attempts
        job_attempts += 1
        if job_attempts == 1:
            raise OSError("injected job close failure")
        closed.append(value)

    monkeypatch.setattr(
        launcher_windows,
        "_create_job",
        lambda _api, _limit: OwnedHandle(200, closer=close_job),
    )
    events: list[str] = []
    guard = _FakeGuard(events)

    with pytest.raises(OSError, match="job close failure"):
        launcher_windows.run_windows_process(
            launcher_windows.WindowsLaunchRequest(
                application=r"C:\Python312\python.exe",
                arguments=("-c", "pass"),
                cwd=r"C:\runtime",
                environment={},
                maximum_output_bytes=1024,
                timeout_seconds=1.0,
            ),
            network_guard=guard,
            observer=events.append,
        )

    assert "job_closed" not in events
    assert not guard.closed
    assert len(launcher_windows._RETAINED_CLEANUP_GROUPS) == 1
    assert handles_windows._RETAINED_OWNERS == []
    launcher_windows._retry_retained_cleanup_groups()
    assert launcher_windows._RETAINED_CLEANUP_GROUPS == []
    assert closed.count(200) == 1
    assert guard.closed


def test_direct_launcher_retries_helper_owner_before_closing_network_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeKernel32()
    closed: list[int] = []
    _configure_fake_launcher(monkeypatch, api, closed)
    events: list[str] = []
    guard = _FakeGuard(events)
    attempts = 0

    def close_helper(_value: int) -> None:
        nonlocal attempts
        attempts += 1
        events.append(f"helper_close_{attempts}")
        if attempts < 3:
            raise OSError("injected helper owner close failure")

    helper_owner = OwnedHandle(908, closer=close_helper)

    def fail_job(_api: Any, _limit: int | None) -> OwnedHandle:
        configuration_error = OSError("injected helper configuration failure")
        handles_windows.close_owned_resources((helper_owner,), cause=configuration_error)
        raise AssertionError("helper cleanup unexpectedly succeeded")

    monkeypatch.setattr(launcher_windows, "_create_job", fail_job)

    with pytest.raises(OSError, match="helper owner close failure"):
        launcher_windows.run_windows_process(
            launcher_windows.WindowsLaunchRequest(
                application=r"C:\Python312\python.exe",
                arguments=("-c", "pass"),
                cwd=r"C:\runtime",
                environment={},
                maximum_output_bytes=1024,
                timeout_seconds=1.0,
            ),
            network_guard=guard,
            observer=events.append,
        )

    assert attempts == 2
    assert not guard.closed
    assert [helper_owner] == handles_windows._RETAINED_OWNERS
    assert len(launcher_windows._RETAINED_CLEANUP_GROUPS) == 1

    launcher_windows._retry_retained_cleanup_groups()
    assert handles_windows._RETAINED_OWNERS == []
    assert launcher_windows._RETAINED_CLEANUP_GROUPS == []
    assert guard.closed
    assert events[-2:] == ["helper_close_3", "guard_close_called"]


def test_direct_launcher_retains_pipe_owner_until_close_retry_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeKernel32()
    closed: list[int] = []
    _configure_fake_launcher(monkeypatch, api, closed)
    pairs = iter(((101, 102), (103, 104)))
    read_attempts = 0

    def close_read(value: int) -> None:
        nonlocal read_attempts
        read_attempts += 1
        if value == 101 and read_attempts == 1:
            raise OSError("injected pipe close failure")
        closed.append(value)

    def output_pipe(_api: Any) -> tuple[OwnedHandle, OwnedHandle]:
        read, write = next(pairs)
        return OwnedHandle(read, closer=close_read), OwnedHandle(write, closer=closed.append)

    monkeypatch.setattr(launcher_windows, "_create_output_pipe", output_pipe)

    with pytest.raises(OSError, match="pipe close failure"):
        launcher_windows.run_windows_process(
            launcher_windows.WindowsLaunchRequest(
                application=r"C:\Python312\python.exe",
                arguments=("-c", "pass"),
                cwd=r"C:\runtime",
                environment={},
                maximum_output_bytes=1024,
                timeout_seconds=1.0,
            )
        )

    assert len(launcher_windows._RETAINED_CLEANUP_GROUPS) == 1
    assert handles_windows._RETAINED_OWNERS == []
    launcher_windows._retry_retained_cleanup_groups()
    assert launcher_windows._RETAINED_CLEANUP_GROUPS == []
    assert closed.count(101) == 1


windows_only = pytest.mark.skipif(
    __import__("sys").platform != "win32", reason="native Windows API smoke test"
)


@windows_only
def test_nt_handle_relative_data_tree_smoke(
    tmp_path: Path,
    exclusive_outside_sentinel: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import goodjob.platform.detect as platform_detect
    from goodjob.platform.fs_windows import WindowsDataTree

    monkeypatch.setattr(platform_detect, "NATIVE_WINDOWS_RELEASE_ENABLED", True)
    assert handles_windows._RETAINED_OWNERS == []
    tree_root = tmp_path / "data"
    publication_parent = tree_root / "artifacts"
    publication_parent.mkdir(parents=True)
    with WindowsDataTree(tree_root, "test") as tree:
        tree.write_new("artifacts/source.tmp", b"source")
        tree.write_new("artifacts/source.txt", b"stale")
        assert tree.read_regular("artifacts/source.tmp") == b"source"
        tree.replace_file("artifacts/source.tmp", "artifacts/source.txt")
        assert tree.list_directory("artifacts") == {"source.txt"}
        assert tree.read_regular("artifacts/source.txt") == b"source"
        tree.publish_directory(
            "artifacts/candidate",
            "artifacts/final",
            {"report.txt": b"report"},
            verify=lambda _relative: None,
            before_rename=None,
        )
        assert tree.read_regular("artifacts/final/report.txt") == b"report"
        tree.remove("artifacts/final")
        assert tree.list_directory("artifacts") == {"source.txt"}
    assert handles_windows._RETAINED_OWNERS == []


def test_windows_git_runner_scopes_wfp_and_job_to_the_exact_application(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import goodjob.git_metadata as git_metadata

    git = tmp_path / "Git" / "mingw64" / "bin" / "git.exe"
    git.parent.mkdir(parents=True)
    git.write_bytes(b"git")
    workspace = tmp_path / "workspace"
    git_dir = workspace / ".git"
    git_dir.mkdir(parents=True)
    binding = InternalGitBinding(
        workspace_root=workspace,
        worktree_root=workspace,
        git_dir=git_dir,
        common_dir=git_dir,
        worktree_identity=(1, 2),
        git_dir_identity=(1, 3),
        common_dir_identity=(1, 3),
    )
    reader = GitMetadataReader(
        git_executable=str(git),
        issue_factory=lambda *_args: pytest.fail("issue factory should not run"),
        safe_history_path=lambda _path: True,
        git_command_timeout_seconds=lambda: 7.0,
        workspace_git_command=lambda _binding, arguments: [str(git), *arguments],
    )
    monkeypatch.setattr(reader, "_verify_git_binding", lambda _binding: None)
    sandbox = sandbox_windows.WfpGitSandbox(str(git))
    guard_events: list[str] = []
    guard = _FakeGuard(guard_events)
    monkeypatch.setattr(sandbox, "open_network_guard", lambda: guard)
    monkeypatch.setattr(git_metadata, "select_git_sandbox", lambda _executable: sandbox)
    captured: list[launcher_windows.WindowsLaunchRequest] = []

    def run(
        request: launcher_windows.WindowsLaunchRequest,
        **_kwargs: object,
    ) -> launcher_windows.WindowsLaunchResult:
        captured.append(request)
        launch_guard = _kwargs["network_guard"]
        assert isinstance(launch_guard, _FakeGuard)
        launch_guard.close()
        return launcher_windows.WindowsLaunchResult(request.arguments, 0, b"head\n", b"")

    monkeypatch.setattr(launcher_windows, "run_windows_process", run)

    returncode, stdout, stderr = reader._git_bounded_bytes_windows(
        binding, "rev-parse", "HEAD", maximum_output_bytes=4096
    )

    assert (returncode, stdout, stderr) == (0, b"head\n", b"")
    assert len(captured) == 1
    request = captured[0]
    assert request.application == str(git)
    assert request.arguments == ("rev-parse", "HEAD")
    assert request.active_process_limit == 1
    assert request.maximum_output_bytes == 4096
    assert request.environment["GIT_CONFIG_GLOBAL"] == "NUL"
    assert request.environment["GIT_ALLOW_PROTOCOL"] == "file"
    assert guard.closed


def test_cli_uses_numeric_handle_arguments_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    import goodjob.cli as cli

    monkeypatch.setattr(sys, "platform", "win32")
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "authorize",
            "--receipt-kind",
            "source_analysis",
            "--scope-json",
            "{}",
            "--notice-version",
            "v1",
            "--capability-handle",
            "91",
            "--confirmed",
        ]
    )

    assert args.capability_handle == 91
    assert not hasattr(args, "capability_fd")


def test_cli_fails_closed_while_native_windows_release_gate_is_disabled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import goodjob.cli as cli
    import goodjob.platform.detect as platform_detect

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(platform_detect, "NATIVE_WINDOWS_RELEASE_ENABLED", False)

    assert cli.run(["data-status"]) == 2
    response = capsys.readouterr()
    assert "unsupported_platform" in response.err
    assert "use WSL2" in response.err


def test_native_windows_release_gate_blocks_selectors_and_direct_backends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import goodjob.platform.detect as platform_detect

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(platform_detect, "NATIVE_WINDOWS_RELEASE_ENABLED", False)

    blocked_calls = (
        lambda: platform_detect.select_git_sandbox(r"C:\Git\mingw64\bin\git.exe"),
        platform_detect.resolve_git_executable,
        lambda: sandbox_windows.WfpSession.create(r"C:\Git\mingw64\bin\git.exe"),
        lambda: fs_windows.WindowsRoot.open(Path(r"C:\workspace")),
        lambda: launcher_windows.run_windows_process(
            launcher_windows.WindowsLaunchRequest(
                application=r"C:\Python312\python.exe",
                arguments=("-c", "pass"),
                cwd=r"C:\runtime",
                environment={},
                maximum_output_bytes=1024,
                timeout_seconds=1.0,
            )
        ),
    )
    for call in blocked_calls:
        with pytest.raises(UnsupportedPlatformError, match="use WSL2"):
            call()
