"""NT handle-relative filesystem guard for native Windows."""

from __future__ import annotations

import ctypes
import os
import stat
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from goodjob.errors import InvalidInputError
from goodjob.platform.handles_windows import (
    OwnedCrtDescriptor,
    OwnedHandle,
    close_owned_resources,
    last_error,
    load_windows_dll,
    retry_retained_owners,
    transfer_handle_to_crt_descriptor,
    write_all_handle,
)

FILE_READ_DATA = 0x0001
FILE_LIST_DIRECTORY = 0x0001
FILE_WRITE_DATA = 0x0002
FILE_APPEND_DATA = 0x0004
FILE_TRAVERSE = 0x0020
FILE_READ_ATTRIBUTES = 0x0080
FILE_WRITE_ATTRIBUTES = 0x0100
DELETE = 0x00010000
SYNCHRONIZE = 0x00100000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
FILE_OPEN = 0x00000001
FILE_CREATE = 0x00000002
FILE_DIRECTORY_FILE = 0x00000001
FILE_WRITE_THROUGH = 0x00000002
FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
FILE_NON_DIRECTORY_FILE = 0x00000040
FILE_OPEN_REPARSE_POINT = 0x00200000
OBJ_CASE_INSENSITIVE = 0x00000040
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
FILE_ATTRIBUTE_READONLY = 0x00000001
FILE_ATTRIBUTE_DIRECTORY = 0x00000010
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
FILE_ATTRIBUTE_NORMAL = 0x00000080
FILE_BASIC_INFO_CLASS = 0
FILE_ID_INFO_CLASS = 18
FILE_ATTRIBUTE_TAG_INFO_CLASS = 9
FILE_NAME_INFO_CLASS = 2
FILE_STANDARD_INFO_CLASS = 1
FILE_ID_BOTH_DIRECTORY_INFO_CLASS = 10
FILE_ID_BOTH_DIRECTORY_RESTART_INFO_CLASS = 11
FILE_RENAME_INFORMATION_CLASS = 10
FILE_DISPOSITION_INFO_EX_CLASS = 21
FILE_DISPOSITION_INFO_CLASS = 4
FILE_DISPOSITION_DELETE = 0x00000001
FILE_DISPOSITION_POSIX_SEMANTICS = 0x00000002
FILE_DISPOSITION_IGNORE_READONLY_ATTRIBUTE = 0x00000010
FSCTL_GET_REPARSE_POINT = 0x000900A8
IO_REPARSE_TAG_SYMLINK = 0xA000000C
IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
ERROR_FILE_NOT_FOUND = 2
ERROR_PATH_NOT_FOUND = 3
ERROR_NO_MORE_FILES = 18
ERROR_FILE_EXISTS = 80
ERROR_ALREADY_EXISTS = 183
MAX_COMPONENT_UTF16_UNITS = 255
MAX_READ_BYTES = 2 * 1024 * 1024
TARGET_PARENT_ACCESS = FILE_LIST_DIRECTORY | FILE_TRAVERSE | FILE_READ_ATTRIBUTES


class FILE_ID_128(ctypes.Structure):
    _fields_ = [("Identifier", ctypes.c_ubyte * 16)]


class FILE_ID_INFO(ctypes.Structure):
    _fields_ = [("VolumeSerialNumber", ctypes.c_uint64), ("FileId", FILE_ID_128)]


class FILE_ATTRIBUTE_TAG_INFO(ctypes.Structure):
    _fields_ = [("FileAttributes", ctypes.c_uint32), ("ReparseTag", ctypes.c_uint32)]


class FILE_BASIC_INFO(ctypes.Structure):
    _fields_ = [
        ("CreationTime", ctypes.c_int64),
        ("LastAccessTime", ctypes.c_int64),
        ("LastWriteTime", ctypes.c_int64),
        ("ChangeTime", ctypes.c_int64),
        ("FileAttributes", ctypes.c_uint32),
    ]


class FILE_STANDARD_INFO(ctypes.Structure):
    _fields_ = [
        ("AllocationSize", ctypes.c_int64),
        ("EndOfFile", ctypes.c_int64),
        ("NumberOfLinks", ctypes.c_uint32),
        ("DeletePending", ctypes.c_int),
        ("Directory", ctypes.c_int),
    ]


class UNICODE_STRING(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_uint16),
        ("MaximumLength", ctypes.c_uint16),
        ("Buffer", ctypes.c_wchar_p),
    ]


class OBJECT_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_uint32),
        ("RootDirectory", ctypes.c_void_p),
        ("ObjectName", ctypes.POINTER(UNICODE_STRING)),
        ("Attributes", ctypes.c_uint32),
        ("SecurityDescriptor", ctypes.c_void_p),
        ("SecurityQualityOfService", ctypes.c_void_p),
    ]


class IO_STATUS_BLOCK_UNION(ctypes.Union):
    _fields_ = [("Status", ctypes.c_int32), ("Pointer", ctypes.c_void_p)]


class IO_STATUS_BLOCK(ctypes.Structure):
    _fields_ = [("result", IO_STATUS_BLOCK_UNION), ("Information", ctypes.c_size_t)]


class FILE_ID_BOTH_DIR_INFO(ctypes.Structure):
    _fields_ = [
        ("NextEntryOffset", ctypes.c_uint32),
        ("FileIndex", ctypes.c_uint32),
        ("CreationTime", ctypes.c_int64),
        ("LastAccessTime", ctypes.c_int64),
        ("LastWriteTime", ctypes.c_int64),
        ("ChangeTime", ctypes.c_int64),
        ("EndOfFile", ctypes.c_int64),
        ("AllocationSize", ctypes.c_int64),
        ("FileAttributes", ctypes.c_uint32),
        ("FileNameLength", ctypes.c_uint32),
        ("EaSize", ctypes.c_uint32),
        ("ShortNameLength", ctypes.c_ubyte),
        ("Reserved", ctypes.c_ubyte),
        ("ShortName", ctypes.c_wchar * 12),
        ("FileId", ctypes.c_int64),
        ("FileName", ctypes.c_wchar * 1),
    ]


class WINDOWS_WCHAR(ctypes.c_uint16):
    pass


class FILE_RENAME_INFO(ctypes.Structure):
    _fields_ = [
        ("ReplaceIfExists", ctypes.c_ubyte),
        ("RootDirectory", ctypes.c_void_p),
        ("FileNameLength", ctypes.c_uint32),
        ("FileName", WINDOWS_WCHAR * 1),
    ]


class FILE_DISPOSITION_INFO_EX(ctypes.Structure):
    _fields_ = [("Flags", ctypes.c_uint32)]


class FILE_DISPOSITION_INFO(ctypes.Structure):
    _fields_ = [("DeleteFile", ctypes.c_int)]


@dataclass(frozen=True)
class WindowsFileIdentity:
    volume_serial: int
    file_id: bytes


@dataclass(frozen=True)
class WindowsDirectoryEntry:
    name: str
    file_attributes: int
    size: int

    @property
    def is_reparse(self) -> bool:
        return bool(self.file_attributes & FILE_ATTRIBUTE_REPARSE_POINT)

    @property
    def is_directory(self) -> bool:
        return bool(self.file_attributes & FILE_ATTRIBUTE_DIRECTORY)

    @property
    def mode(self) -> int:
        if self.is_reparse:
            return stat.S_IFLNK | 0o777
        if self.is_directory:
            return stat.S_IFDIR | 0o700
        return stat.S_IFREG | 0o600

    def stat_result(self) -> os.stat_result:
        return os.stat_result((self.mode, 0, 0, 1, 0, 0, self.size, 0, 0, 0))

    def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
        del follow_symlinks
        return self.stat_result()


def validate_component(component: str) -> str:
    """Validate the only string shape accepted below an authorized parent handle."""
    if (
        not component
        or component in {".", ".."}
        or "/" in component
        or "\\" in component
        or ":" in component
        or "\0" in component
        or component.startswith(("\\?\\", "\\.\\", "\\\\"))
    ):
        raise InvalidInputError("Windows path component is not a safe relative name")
    try:
        encoded = component.encode("utf-16-le")
    except UnicodeEncodeError as exc:
        raise InvalidInputError("Windows path component is not valid UTF-16") from exc
    units = len(encoded) // 2
    if units > MAX_COMPONENT_UTF16_UNITS:
        raise InvalidInputError("Windows path component exceeds the NTFS component limit")
    return component


def relative_components(relative: str, *, allow_root: bool = False) -> tuple[str, ...]:
    if relative == "." and allow_root:
        return ()
    if not relative or relative.startswith(("/", "\\")):
        raise InvalidInputError("Windows path must be relative to its authorized root")
    raw_parts = relative.split("/")
    if any(not part for part in raw_parts):
        raise InvalidInputError("Windows relative path contains an empty component")
    return tuple(validate_component(part) for part in raw_parts)


def _kernel32() -> Any:
    api = load_windows_dll("kernel32.dll")
    api.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    api.CreateFileW.restype = ctypes.c_void_p
    api.GetFinalPathNameByHandleW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
    ]
    api.GetFinalPathNameByHandleW.restype = ctypes.c_uint32
    api.GetFileInformationByHandleEx.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    api.GetFileInformationByHandleEx.restype = ctypes.c_int
    api.SetFileInformationByHandle.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    api.SetFileInformationByHandle.restype = ctypes.c_int
    api.GetVolumeInformationByHandleW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_wchar_p,
        ctypes.c_uint32,
    ]
    api.GetVolumeInformationByHandleW.restype = ctypes.c_int
    api.ReadFile.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_void_p,
    ]
    api.ReadFile.restype = ctypes.c_int
    api.FlushFileBuffers.argtypes = [ctypes.c_void_p]
    api.FlushFileBuffers.restype = ctypes.c_int
    api.DeviceIoControl.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_void_p,
    ]
    api.DeviceIoControl.restype = ctypes.c_int
    return api


def _ntdll() -> Any:
    api = load_windows_dll("ntdll.dll")
    api.NtCreateFile.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_uint32,
        ctypes.POINTER(OBJECT_ATTRIBUTES),
        ctypes.POINTER(IO_STATUS_BLOCK),
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    api.NtCreateFile.restype = ctypes.c_int32
    api.NtSetInformationFile.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(IO_STATUS_BLOCK),
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
    ]
    api.NtSetInformationFile.restype = ctypes.c_int32
    api.RtlNtStatusToDosError.argtypes = [ctypes.c_int32]
    api.RtlNtStatusToDosError.restype = ctypes.c_uint32
    return api


def _raise_nt(api: Any, status: int, operation: str) -> None:
    error = int(api.RtlNtStatusToDosError(ctypes.c_int32(status)))
    if error in {ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND}:
        raise FileNotFoundError(error, operation)
    if error in {ERROR_FILE_EXISTS, ERROR_ALREADY_EXISTS}:
        raise FileExistsError(error, operation)
    raise OSError(error, operation)


def _identity(handle: int) -> WindowsFileIdentity:
    info = FILE_ID_INFO()
    api = _kernel32()
    if not api.GetFileInformationByHandleEx(
        ctypes.c_void_p(handle), FILE_ID_INFO_CLASS, ctypes.byref(info), ctypes.sizeof(info)
    ):
        raise OSError(last_error(), "GetFileInformationByHandleEx(FileIdInfo)")
    return WindowsFileIdentity(int(info.VolumeSerialNumber), bytes(info.FileId.Identifier))


def _attributes(handle: int) -> FILE_ATTRIBUTE_TAG_INFO:
    info = FILE_ATTRIBUTE_TAG_INFO()
    api = _kernel32()
    if not api.GetFileInformationByHandleEx(
        ctypes.c_void_p(handle),
        FILE_ATTRIBUTE_TAG_INFO_CLASS,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        raise OSError(last_error(), "GetFileInformationByHandleEx(FileAttributeTagInfo)")
    return info


def _mark_read_only(handle: int) -> None:
    info = FILE_BASIC_INFO()
    api = _kernel32()
    if not api.GetFileInformationByHandleEx(
        ctypes.c_void_p(handle), FILE_BASIC_INFO_CLASS, ctypes.byref(info), ctypes.sizeof(info)
    ):
        raise OSError(last_error(), "GetFileInformationByHandleEx(FileBasicInfo)")
    info.FileAttributes |= FILE_ATTRIBUTE_READONLY
    if not api.SetFileInformationByHandle(
        ctypes.c_void_p(handle), FILE_BASIC_INFO_CLASS, ctypes.byref(info), ctypes.sizeof(info)
    ):
        raise OSError(last_error(), "SetFileInformationByHandle(FileBasicInfo)")


def _standard_info(handle: int) -> FILE_STANDARD_INFO:
    info = FILE_STANDARD_INFO()
    api = _kernel32()
    if not api.GetFileInformationByHandleEx(
        ctypes.c_void_p(handle),
        FILE_STANDARD_INFO_CLASS,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        raise OSError(last_error(), "GetFileInformationByHandleEx(FileStandardInfo)")
    return info


def _exact_final_component(handle: int) -> str:
    api = _kernel32()
    capacity = 32768
    buffer = ctypes.create_unicode_buffer(capacity)
    if not api.GetFileInformationByHandleEx(
        ctypes.c_void_p(handle), FILE_NAME_INFO_CLASS, buffer, ctypes.sizeof(buffer)
    ):
        raise OSError(last_error(), "GetFileInformationByHandleEx(FileNameInfo)")
    length = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_uint32)).contents.value
    raw = ctypes.string_at(ctypes.addressof(buffer) + 4, length)
    full_name = raw.decode("utf-16-le")
    return full_name.rstrip("\\").rsplit("\\", 1)[-1]


def _open_relative(
    parent_handle: int,
    component: str,
    *,
    access: int,
    disposition: int = FILE_OPEN,
    directory: bool | None,
    reject_reparse: bool = True,
) -> OwnedHandle:
    retry_retained_owners()
    name = validate_component(component)
    name_buffer = ctypes.create_unicode_buffer(name)
    encoded_length = len(name.encode("utf-16-le"))
    unicode_name = UNICODE_STRING(
        encoded_length, encoded_length, ctypes.cast(name_buffer, ctypes.c_wchar_p)
    )
    attributes = OBJECT_ATTRIBUTES(
        ctypes.sizeof(OBJECT_ATTRIBUTES),
        ctypes.c_void_p(parent_handle),
        ctypes.pointer(unicode_name),
        OBJ_CASE_INSENSITIVE,
        None,
        None,
    )
    io_status = IO_STATUS_BLOCK()
    raw = ctypes.c_void_p()
    options = FILE_OPEN_REPARSE_POINT | FILE_SYNCHRONOUS_IO_NONALERT
    if directory is True:
        options |= FILE_DIRECTORY_FILE
    elif directory is False:
        options |= FILE_NON_DIRECTORY_FILE
    api = _ntdll()
    status = int(
        api.NtCreateFile(
            ctypes.byref(raw),
            access | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
            ctypes.byref(attributes),
            ctypes.byref(io_status),
            None,
            FILE_ATTRIBUTE_NORMAL,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            disposition,
            options,
            None,
            0,
        )
    )
    if status < 0 or not raw.value:
        _raise_nt(api, status, "NtCreateFile")
    assert raw.value is not None
    handle = OwnedHandle(int(raw.value))
    try:
        tag = _attributes(handle.value)
        if reject_reparse and tag.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
            raise OSError("refusing to follow a Windows reparse point")
        if disposition == FILE_OPEN and _exact_final_component(handle.value) != name:
            raise OSError("Windows path component differs by a case alias")
        return handle
    except BaseException as primary_error:
        close_owned_resources((handle,), cause=primary_error)
        raise


class WindowsRoot:
    """One absolute root open followed only by verified handle-relative operations."""

    def __init__(
        self, path: Path, handle: OwnedHandle, display_path: str, identity: WindowsFileIdentity
    ):
        self.path = path
        self.handle = handle
        self.display_path = display_path
        self.identity = identity
        self.request_key = os.path.normcase(os.path.abspath(str(path)))

    @classmethod
    def open(cls, path: Path) -> WindowsRoot:
        from goodjob.platform.detect import require_released_runtime

        require_released_runtime()
        retry_retained_owners()
        if not path.is_absolute():
            raise OSError("Windows authorized root must be absolute")
        raw_path = str(path)
        if raw_path.startswith("\\\\"):
            raise OSError("UNC roots remain fail-closed pending IMP-31C evidence")
        api = _kernel32()
        raw = api.CreateFileW(
            raw_path,
            TARGET_PARENT_ACCESS | SYNCHRONIZE,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        invalid = ctypes.c_void_p(-1).value
        if not raw or int(raw) == invalid:
            raise OSError(last_error(), "CreateFileW(authorized root)")
        handle = OwnedHandle(int(raw))
        try:
            tag = _attributes(handle.value)
            if not tag.FileAttributes & FILE_ATTRIBUTE_DIRECTORY:
                raise OSError("Windows authorized root is not a directory")
            if tag.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
                raise OSError("Windows authorized root must not be a reparse point")
            filesystem = ctypes.create_unicode_buffer(32)
            if not api.GetVolumeInformationByHandleW(
                ctypes.c_void_p(handle.value),
                None,
                0,
                None,
                None,
                None,
                filesystem,
                len(filesystem),
            ):
                raise OSError(last_error(), "GetVolumeInformationByHandleW")
            if filesystem.value.upper() != "NTFS":
                raise OSError("native Windows scanning requires NTFS")
            capacity = 32768
            display = ctypes.create_unicode_buffer(capacity)
            length = int(
                api.GetFinalPathNameByHandleW(ctypes.c_void_p(handle.value), display, capacity, 0)
            )
            if length == 0 or length >= capacity:
                raise OSError(last_error(), "GetFinalPathNameByHandleW")
            return cls(path, handle, display.value, _identity(handle.value))
        except BaseException as primary_error:
            close_owned_resources((handle,), cause=primary_error)
            raise

    def close(self) -> None:
        close_owned_resources((self.handle,))

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def open_directory(self, parts: Sequence[str]) -> OwnedHandle:
        current_value = self.handle.value
        owned: OwnedHandle | None = None
        try:
            if not parts:
                raise InvalidInputError("a child directory component is required")
            for part in parts:
                next_handle = _open_relative(
                    current_value, part, access=TARGET_PARENT_ACCESS, directory=True
                )
                if owned is not None:
                    try:
                        close_owned_resources((owned,))
                    except BaseException as primary_error:
                        close_owned_resources((next_handle,), cause=primary_error)
                        raise
                owned = next_handle
                current_value = next_handle.value
            assert owned is not None
            if _identity(owned.value).volume_serial != self.identity.volume_serial:
                raise OSError("Windows child directory crossed the authorized volume")
            return owned
        except BaseException as primary_error:
            if owned is not None:
                close_owned_resources((owned,), cause=primary_error)
            raise

    @contextmanager
    def open_parent(self, parts: Sequence[str]) -> Iterator[tuple[int, str, OwnedHandle | None]]:
        if not parts:
            raise InvalidInputError("Windows relative path requires a child name")
        parent: OwnedHandle | None = None
        try:
            if len(parts) == 1:
                yield self.handle.value, parts[0], None
            else:
                parent = self.open_directory(parts[:-1])
                yield parent.value, parts[-1], parent
        finally:
            if parent is not None:
                close_owned_resources((parent,))


_ACTIVE_AUTHORIZED_ROOT: ContextVar[WindowsRoot | None] = ContextVar(
    "goodjob_windows_authorized_root", default=None
)


def _matching_active_root(path: Path) -> WindowsRoot | None:
    active = _ACTIVE_AUTHORIZED_ROOT.get()
    request_key = os.path.normcase(os.path.abspath(str(path)))
    return active if active is not None and active.request_key == request_key else None


def _active_root_prefix(path: Path) -> tuple[WindowsRoot, tuple[str, ...]] | None:
    active = _ACTIVE_AUTHORIZED_ROOT.get()
    if active is None:
        return None
    active_path = os.path.abspath(str(active.path))
    candidate_path = os.path.abspath(str(path))
    try:
        common = os.path.commonpath((active_path, candidate_path))
    except ValueError:
        return None
    if os.path.normcase(common) != os.path.normcase(active_path):
        return None
    relative = os.path.relpath(candidate_path, active_path)
    parts = () if relative == "." else relative_components(relative.replace("\\", "/"))
    return active, parts


@contextmanager
def bind_authorized_root(path: Path) -> Iterator[WindowsRoot]:
    """Keep one absolute workspace root handle for an entire scan/refresh operation."""
    active = _matching_active_root(path)
    if active is not None:
        yield active
        return
    root = WindowsRoot.open(path)
    token = _ACTIVE_AUTHORIZED_ROOT.set(root)
    try:
        yield root
    finally:
        _ACTIVE_AUTHORIZED_ROOT.reset(token)
        root.close()


class WindowsDirectory:
    """A directory handle kept together with the root handle that authorized it."""

    def __init__(
        self, root: WindowsRoot, directory: OwnedHandle | None, *, owns_root: bool
    ) -> None:
        self._root = root
        self._directory = directory
        self._owns_root = owns_root

    @property
    def value(self) -> int:
        return self._directory.value if self._directory is not None else self._root.handle.value

    @property
    def identity(self) -> WindowsFileIdentity:
        return _identity(self.value)

    @classmethod
    def open(cls, root_path: Path, relative: str = ".") -> WindowsDirectory:
        retry_retained_owners()
        active = _active_root_prefix(root_path)
        owns_root = active is None
        if active is None:
            root = WindowsRoot.open(root_path)
            prefix: tuple[str, ...] = ()
        else:
            root, prefix = active
        try:
            parts = (*prefix, *relative_components(relative, allow_root=True))
            directory = root.open_directory(parts) if parts else None
            return cls(root, directory, owns_root=owns_root)
        except BaseException as primary_error:
            if owns_root:
                close_owned_resources((root.handle,), cause=primary_error)
            raise

    def close(self) -> None:
        close_owned_resources((self._directory, self._root.handle if self._owns_root else None))
        if self._directory is not None and self._directory.closed:
            self._directory = None
        if self._owns_root and self._root.handle.closed:
            self._owns_root = False

    def __enter__(self) -> Self:
        _ = self.value
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def open_regular(self, relative: str) -> OwnedHandle:
        parts = relative_components(relative)
        current = self.value
        parents: list[OwnedHandle] = []
        try:
            for component in parts[:-1]:
                parent = _open_relative(
                    current, component, access=FILE_LIST_DIRECTORY, directory=True
                )
                parents.append(parent)
                current = parent.value
            result = _open_relative(current, parts[-1], access=FILE_READ_DATA, directory=False)
        except BaseException as primary_error:
            close_owned_resources(reversed(parents), cause=primary_error)
            raise
        try:
            close_owned_resources(reversed(parents))
        except BaseException as cleanup_error:
            close_owned_resources((result,), cause=cleanup_error)
            raise
        return result

    def stat(self, component: str) -> os.stat_result:
        with _open_relative(
            self.value,
            component,
            access=FILE_READ_ATTRIBUTES,
            directory=None,
            reject_reparse=False,
        ) as handle:
            return _entry_from_handle(component, handle.value).stat_result()

    def list_entries(self) -> list[WindowsDirectoryEntry]:
        return _list_handle(self.value)

    def readlink(self, component: str) -> str:
        return _readlink_at(self.value, component)


def open_directory(root: Path, relative: str = ".") -> WindowsDirectory:
    return WindowsDirectory.open(root, relative)


def open_absolute_directory(path: Path) -> WindowsDirectory:
    return WindowsDirectory.open(path)


def read_text_at(directory: WindowsDirectory, relative: str, *, maximum_bytes: int = 4096) -> str:
    handle = directory.open_regular(relative)
    with handle:
        api = _kernel32()
        chunks: list[bytes] = []
        total = 0
        while True:
            buffer = ctypes.create_string_buffer(min(4096, maximum_bytes + 1 - total))
            count = ctypes.c_uint32()
            if not api.ReadFile(
                ctypes.c_void_p(handle.value), buffer, len(buffer), ctypes.byref(count), None
            ):
                raise OSError(last_error(), "ReadFile")
            if count.value == 0:
                return b"".join(chunks).decode("utf-8")
            total += count.value
            if total > maximum_bytes:
                raise OSError("Windows file exceeded its bounded read limit")
            chunks.append(bytes(buffer.raw[: count.value]))


def _entry_from_handle(name: str, handle: int) -> WindowsDirectoryEntry:
    tag = _attributes(handle)
    standard = _standard_info(handle)
    return WindowsDirectoryEntry(name, int(tag.FileAttributes), int(standard.EndOfFile))


def _list_handle(directory_handle: int) -> list[WindowsDirectoryEntry]:
    api = _kernel32()
    entries: list[WindowsDirectoryEntry] = []
    info_class = FILE_ID_BOTH_DIRECTORY_RESTART_INFO_CLASS
    while True:
        buffer = ctypes.create_string_buffer(64 * 1024)
        ok = api.GetFileInformationByHandleEx(
            ctypes.c_void_p(directory_handle),
            info_class,
            buffer,
            ctypes.sizeof(buffer),
        )
        if not ok:
            error = last_error()
            if error == ERROR_NO_MORE_FILES:
                break
            raise OSError(error, "GetFileInformationByHandleEx(FileIdBothDirectoryInfo)")
        info_class = FILE_ID_BOTH_DIRECTORY_INFO_CLASS
        offset = 0
        while True:
            record = FILE_ID_BOTH_DIR_INFO.from_buffer(buffer, offset)
            name_address = ctypes.addressof(buffer) + offset + FILE_ID_BOTH_DIR_INFO.FileName.offset
            name = ctypes.string_at(name_address, record.FileNameLength).decode("utf-16-le")
            if name not in {".", ".."}:
                entries.append(
                    WindowsDirectoryEntry(name, int(record.FileAttributes), int(record.EndOfFile))
                )
            if record.NextEntryOffset == 0:
                break
            offset += int(record.NextEntryOffset)
    return entries


def list_directory(root: Path, relative: str = ".") -> list[WindowsDirectoryEntry]:
    with WindowsDirectory.open(root, relative) as directory:
        return directory.list_entries()


def stat_relative(root: Path, relative: str) -> os.stat_result:
    parts = relative_components(relative)
    parent_relative = "." if len(parts) == 1 else "/".join(parts[:-1])
    with WindowsDirectory.open(root, parent_relative) as parent:
        return parent.stat(parts[-1])


def directory_identity(path: Path) -> tuple[int, int]:
    with WindowsDirectory.open(path) as directory:
        identity = directory.identity
        return identity.volume_serial, int.from_bytes(identity.file_id, "little")


def open_regular_file(root: Path, relative: str) -> tuple[OwnedCrtDescriptor, os.stat_result]:
    descriptor = None
    file_stat: os.stat_result
    try:
        with WindowsDirectory.open(root) as directory:
            handle = directory.open_regular(relative)
            descriptor = transfer_handle_to_crt_descriptor(handle, os.O_RDONLY)
            file_stat = os.fstat(descriptor.value)
    except BaseException as primary_error:
        if descriptor is not None:
            close_owned_resources((descriptor,), cause=primary_error)
        raise
    assert descriptor is not None
    return descriptor, file_stat


def open_absolute_regular_file(path: Path) -> tuple[OwnedCrtDescriptor, os.stat_result]:
    if not path.is_absolute() or not path.name:
        raise OSError("absolute Windows file path is required")
    return open_regular_file(path.parent, path.name)


def read_regular(root: Path, relative: str, *, maximum_bytes: int = MAX_READ_BYTES) -> bytes:
    descriptor, _file_stat = open_regular_file(root, relative)
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            chunk = os.read(descriptor.value, min(64 * 1024, maximum_bytes + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > maximum_bytes:
                raise OSError("Windows file exceeded its bounded read limit")
            chunks.append(chunk)
    except BaseException as primary_error:
        close_owned_resources((descriptor,), cause=primary_error)
        raise
    close_owned_resources((descriptor,))
    return b"".join(chunks)


def _read_handle_bounded(handle: int, *, maximum_bytes: int) -> bytes:
    if maximum_bytes < 0:
        raise ValueError("Windows bounded read limit must not be negative")
    chunks: list[bytes] = []
    total = 0
    api = _kernel32()
    while True:
        buffer = ctypes.create_string_buffer(min(64 * 1024, maximum_bytes + 1 - total))
        count = ctypes.c_uint32()
        if not api.ReadFile(
            ctypes.c_void_p(handle), buffer, len(buffer), ctypes.byref(count), None
        ):
            raise OSError(last_error(), "ReadFile")
        if count.value == 0:
            return b"".join(chunks)
        total += count.value
        if total > maximum_bytes:
            raise OSError("Windows file exceeded its bounded read limit")
        chunks.append(bytes(buffer.raw[: count.value]))


@dataclass(frozen=True)
class WindowsPublicationDirectory:
    """Borrow the exact temporary-directory HANDLE during publication verification."""

    _handle: int
    _expected_sizes: Mapping[str, int]

    def list_directory(self) -> set[str]:
        return {entry.name for entry in _list_handle(self._handle)}

    def read_regular(self, name: str) -> bytes:
        component = validate_component(name)
        maximum_bytes = self._expected_sizes.get(component)
        if maximum_bytes is None:
            raise InvalidInputError("publication verification requested an unexpected file")
        with _open_relative(
            self._handle, component, access=FILE_READ_DATA, directory=False
        ) as handle:
            return _read_handle_bounded(handle.value, maximum_bytes=maximum_bytes)


def _readlink_at(parent: int, name: str) -> str:
    handle = _open_relative(
        parent,
        name,
        access=FILE_READ_ATTRIBUTES,
        directory=None,
        reject_reparse=False,
    )
    with handle:
        tag_info = _attributes(handle.value)
        if not tag_info.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
            raise OSError("Windows entry is not a reparse point")
        api = _kernel32()
        buffer = ctypes.create_string_buffer(16 * 1024)
        returned = ctypes.c_uint32()
        if not api.DeviceIoControl(
            ctypes.c_void_p(handle.value),
            FSCTL_GET_REPARSE_POINT,
            None,
            0,
            buffer,
            len(buffer),
            ctypes.byref(returned),
            None,
        ):
            raise OSError(last_error(), "FSCTL_GET_REPARSE_POINT")
        tag = int.from_bytes(buffer.raw[0:4], "little")
        if tag not in {IO_REPARSE_TAG_SYMLINK, IO_REPARSE_TAG_MOUNT_POINT}:
            raise OSError("unsupported Windows reparse tag")
        base = 20 if tag == IO_REPARSE_TAG_SYMLINK else 16
        substitute_offset = int.from_bytes(buffer.raw[8:10], "little")
        substitute_length = int.from_bytes(buffer.raw[10:12], "little")
        print_offset = int.from_bytes(buffer.raw[12:14], "little")
        print_length = int.from_bytes(buffer.raw[14:16], "little")
        offset, length = (
            (print_offset, print_length) if print_length else (substitute_offset, substitute_length)
        )
        return buffer.raw[base + offset : base + offset + length].decode("utf-16-le")


def readlink(root: Path, relative: str) -> str:
    parts = relative_components(relative)
    parent_relative = "." if len(parts) == 1 else "/".join(parts[:-1])
    with WindowsDirectory.open(root, parent_relative) as parent:
        return parent.readlink(parts[-1])


def _write_all(handle: int, content: bytes) -> None:
    api = _kernel32()
    write_all_handle(handle, content)
    if not api.FlushFileBuffers(ctypes.c_void_p(handle)):
        raise OSError(last_error(), "FlushFileBuffers")


def write_new_file_at(parent_handle: int, name: str, content: bytes) -> None:
    from goodjob.platform.detect import require_released_runtime

    require_released_runtime()
    with _open_relative(
        parent_handle,
        name,
        access=FILE_WRITE_DATA | FILE_WRITE_ATTRIBUTES,
        disposition=FILE_CREATE,
        directory=False,
    ) as handle:
        _write_all(handle.value, content)


def _rename_handle(source: int, target_parent: int, target_name: str, *, replace: bool) -> None:
    name = validate_component(target_name)
    encoded = name.encode("utf-16-le")
    size = ctypes.sizeof(FILE_RENAME_INFO) + len(encoded)
    storage = ctypes.create_string_buffer(size)
    info = FILE_RENAME_INFO.from_buffer(storage)
    info.ReplaceIfExists = replace
    info.RootDirectory = ctypes.c_void_p(target_parent)
    info.FileNameLength = len(encoded)
    ctypes.memmove(
        ctypes.addressof(storage) + FILE_RENAME_INFO.FileName.offset, encoded, len(encoded)
    )
    io_status = IO_STATUS_BLOCK()
    api = _ntdll()
    status = int(
        api.NtSetInformationFile(
            ctypes.c_void_p(source),
            ctypes.byref(io_status),
            ctypes.byref(storage),
            size,
            FILE_RENAME_INFORMATION_CLASS,
        )
    )
    if status < 0:
        _raise_nt(api, status, "NtSetInformationFile(FileRenameInformation)")


def _dispose_handle(handle: int) -> None:
    api = _kernel32()
    extended = FILE_DISPOSITION_INFO_EX(
        FILE_DISPOSITION_DELETE
        | FILE_DISPOSITION_POSIX_SEMANTICS
        | FILE_DISPOSITION_IGNORE_READONLY_ATTRIBUTE
    )
    if api.SetFileInformationByHandle(
        ctypes.c_void_p(handle),
        FILE_DISPOSITION_INFO_EX_CLASS,
        ctypes.byref(extended),
        ctypes.sizeof(extended),
    ):
        return
    fallback = FILE_DISPOSITION_INFO(True)
    if not api.SetFileInformationByHandle(
        ctypes.c_void_p(handle),
        FILE_DISPOSITION_INFO_CLASS,
        ctypes.byref(fallback),
        ctypes.sizeof(fallback),
    ):
        raise OSError(last_error(), "SetFileInformationByHandle(FileDispositionInfo)")


class WindowsDataTree:
    def __init__(self, root: Path, label: str) -> None:
        self._root = WindowsRoot.open(root)
        self._label = label

    def close(self) -> None:
        self._root.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @contextmanager
    def open_parent(self, relative: str) -> Iterator[tuple[int, str]]:
        parts = relative_components(relative)
        with self._root.open_parent(parts) as (parent, name, _owner):
            yield parent, name

    def read_regular(self, relative: str, *, maximum_bytes: int = MAX_READ_BYTES) -> bytes:
        parts = relative_components(relative)
        with (
            self._root.open_parent(parts) as (parent, name, _owner),
            _open_relative(parent, name, access=FILE_READ_DATA, directory=False) as handle,
        ):
            return _read_handle_bounded(handle.value, maximum_bytes=maximum_bytes)

    def list_directory(self, relative: str) -> set[str]:
        parts = relative_components(relative)
        with self._root.open_directory(parts) as directory:
            return {entry.name for entry in _list_handle(directory.value)}

    def write_new(self, relative: str, content: bytes) -> None:
        with self.open_parent(relative) as (parent, name):
            write_new_file_at(parent, name, content)

    def replace_file(self, source: str, destination: str) -> None:
        source_parts = relative_components(source)
        destination_parts = relative_components(destination)
        with (
            self._root.open_parent(source_parts) as (source_parent, source_name, _source_owner),
            self._root.open_parent(destination_parts) as (
                destination_parent,
                destination_name,
                _destination_owner,
            ),
            _open_relative(
                source_parent,
                source_name,
                access=DELETE | FILE_READ_ATTRIBUTES,
                directory=False,
            ) as source_handle,
        ):
            source_identity = _identity(source_handle.value)
            destination_identity = _identity(destination_parent)
            if source_identity.volume_serial != destination_identity.volume_serial:
                raise OSError("Windows atomic rename cannot cross volumes")
            try:
                existing = _open_relative(
                    destination_parent,
                    destination_name,
                    access=FILE_READ_ATTRIBUTES,
                    directory=None,
                )
            except FileNotFoundError:
                existing = None
            if existing is not None:
                with existing:
                    if _attributes(existing.value).FileAttributes & (
                        FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT
                    ):
                        raise OSError("Windows replacement target is not a regular file")
            _rename_handle(source_handle.value, destination_parent, destination_name, replace=True)
            if _identity(source_handle.value) != source_identity:
                raise OSError("Windows replacement source identity changed during rename")

    def publish_directory(
        self,
        temp_relative: str,
        final_relative: str,
        files: dict[str, bytes],
        *,
        verify: Callable[[WindowsPublicationDirectory], None],
        before_rename: Callable[[], None] | None,
    ) -> None:
        if not files:
            raise InvalidInputError(f"{self._label} publication file set is invalid")
        for name in files:
            validate_component(name)
        temp_parts = relative_components(temp_relative)
        final_parts = relative_components(final_relative)
        with (
            self._root.open_parent(temp_parts) as (temp_parent, temp_name, _temp_owner),
            self._root.open_parent(final_parts) as (final_parent, final_name, _final_owner),
        ):
            try:
                existing = _open_relative(temp_parent, temp_name, access=0, directory=None)
            except FileNotFoundError:
                existing = None
            if existing is not None:
                close_owned_resources((existing,))
                raise InvalidInputError(f"{self._label} publication path already exists")
            temp = _open_relative(
                temp_parent,
                temp_name,
                access=DELETE | FILE_LIST_DIRECTORY | FILE_WRITE_ATTRIBUTES,
                disposition=FILE_CREATE,
                directory=True,
            )
            with temp:
                temp_identity = _identity(temp.value)
                for name, content in files.items():
                    write_new_file_at(temp.value, name, content)
                verify(
                    WindowsPublicationDirectory(
                        temp.value, {name: len(content) for name, content in files.items()}
                    )
                )
                for name in files:
                    with _open_relative(
                        temp.value,
                        name,
                        access=FILE_READ_ATTRIBUTES | FILE_WRITE_ATTRIBUTES,
                        directory=False,
                    ) as file_handle:
                        _mark_read_only(file_handle.value)
                _mark_read_only(temp.value)
                if before_rename is not None:
                    before_rename()
                if _identity(temp.value).volume_serial != _identity(final_parent).volume_serial:
                    raise OSError("Windows directory publication cannot cross volumes")
                _rename_handle(temp.value, final_parent, final_name, replace=False)
                if _identity(temp.value) != temp_identity:
                    raise OSError("Windows publication identity changed during rename")

    def remove(self, relative: str) -> None:
        parts = relative_components(relative)
        with self._root.open_parent(parts) as (parent, name, _parent_owner):
            self._remove_entry(parent, name)

    def _remove_entry(self, parent: int, name: str) -> None:
        try:
            handle = _open_relative(
                parent,
                name,
                access=DELETE | FILE_LIST_DIRECTORY | FILE_WRITE_ATTRIBUTES,
                directory=None,
                reject_reparse=False,
            )
        except FileNotFoundError:
            return
        with handle:
            attributes = _attributes(handle.value).FileAttributes
            if attributes & FILE_ATTRIBUTE_REPARSE_POINT:
                _dispose_handle(handle.value)
                return
            if attributes & FILE_ATTRIBUTE_DIRECTORY:
                for entry in _list_handle(handle.value):
                    self._remove_entry(handle.value, entry.name)
            _dispose_handle(handle.value)
