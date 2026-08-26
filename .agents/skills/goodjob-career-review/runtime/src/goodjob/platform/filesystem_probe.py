"""Read-only workspace filesystem capability probing."""

from __future__ import annotations

import ctypes
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

FilesystemProbeStatus = Literal["supported", "unsupported", "unknown", "error"]

_MNT_LOCAL = 0x00001000
_MAC_FILESYSTEM_TYPES = frozenset({"apfs", "hfs", "hfsx", "ufs", "devfs", "msdos", "exfat"})
_NETWORK_FILESYSTEM_TYPES = frozenset(
    {
        "afpfs",
        "cifs",
        "fuse",
        "fuseblk",
        "fuse.sshfs",
        "macfuse",
        "nfs",
        "nfs4",
        "osxfuse",
        "smbfs",
        "sshfs",
        "webdav",
    }
)


@dataclass(frozen=True)
class FilesystemInfo:
    """Minimal statfs result retained by the capability classifier."""

    filesystem_type: str | None
    flags: int | None
    is_local: bool | None


@dataclass(frozen=True)
class FilesystemProbeResult:
    """Stable result for one workspace filesystem capability probe."""

    status: FilesystemProbeStatus
    filesystem_type: str
    flags: int | None
    message: str
    remediation: str


class _MacStatFS(ctypes.Structure):
    _fields_ = [
        ("f_bsize", ctypes.c_uint32),
        ("f_iosize", ctypes.c_int32),
        ("f_blocks", ctypes.c_uint64),
        ("f_bfree", ctypes.c_uint64),
        ("f_bavail", ctypes.c_uint64),
        ("f_files", ctypes.c_uint64),
        ("f_ffree", ctypes.c_uint64),
        ("f_fsid", ctypes.c_int32 * 2),
        ("f_owner", ctypes.c_uint32),
        ("f_type", ctypes.c_uint32),
        ("f_flags", ctypes.c_uint32),
        ("f_fssubtype", ctypes.c_uint32),
        ("f_fstypename", ctypes.c_char * 16),
        ("f_mntonname", ctypes.c_char * 1024),
        ("f_mntfromname", ctypes.c_char * 1024),
        ("f_reserved", ctypes.c_uint32 * 8),
    ]


def _macos_statfs(path: Path) -> FilesystemInfo:
    libc = ctypes.CDLL(None, use_errno=True)
    statfs = getattr(libc, "statfs", None)
    if statfs is None:
        raise OSError("macOS statfs is unavailable")
    statfs.argtypes = [ctypes.c_char_p, ctypes.POINTER(_MacStatFS)]
    statfs.restype = ctypes.c_int
    result = _MacStatFS()
    encoded_path = os.fsencode(path)
    if statfs(encoded_path, ctypes.byref(result)) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    filesystem_type = (
        bytes(result.f_fstypename).split(b"\0", 1)[0].decode("ascii", errors="replace")
    )
    return FilesystemInfo(
        filesystem_type=filesystem_type or None,
        flags=int(result.f_flags),
        is_local=bool(result.f_flags & _MNT_LOCAL),
    )


def _default_statfs(path: Path) -> FilesystemInfo:
    if sys.platform == "darwin":
        return _macos_statfs(path)
    statvfs = getattr(os, "statvfs", None)
    if statvfs is not None:
        statvfs(path)
    else:
        path.stat()
    return FilesystemInfo(filesystem_type="platform-local", flags=None, is_local=True)


def _filesystem_type_is_unsupported(filesystem_type: str) -> bool:
    normalized = filesystem_type.casefold()
    return normalized in _NETWORK_FILESYSTEM_TYPES or normalized.startswith(
        ("fuse", "macfuse", "osxfuse", "sshfs")
    )


def _unsupported_result(info: FilesystemInfo, *, reason: str) -> FilesystemProbeResult:
    filesystem_type = info.filesystem_type or "unknown"
    return FilesystemProbeResult(
        status="unsupported",
        filesystem_type=filesystem_type,
        flags=info.flags,
        message=(
            f"The workspace filesystem is not supported for protected local scanning ({reason})."
        ),
        remediation="Move or reselect the workspace on a local APFS/HFS/Linux filesystem, "
        "then run preflight again; remote execution and automatic copying are not available.",
    )


def probe_workspace_filesystem(
    workspace: Path,
    *,
    statfs_probe: Callable[[Path], FilesystemInfo] = _default_statfs,
) -> FilesystemProbeResult:
    """Probe one workspace path without reading directory contents or source."""
    try:
        info = statfs_probe(workspace)
    except (OSError, ValueError):
        return FilesystemProbeResult(
            status="error",
            filesystem_type="unknown",
            flags=None,
            message="The workspace filesystem capability could not be probed safely.",
            remediation="Choose a readable local workspace whose filesystem type can be verified, "
            "then run preflight again.",
        )

    filesystem_type = info.filesystem_type or "unknown"
    if info.is_local is False or _filesystem_type_is_unsupported(filesystem_type):
        return _unsupported_result(info, reason=f"filesystem type {filesystem_type}")
    if not filesystem_type or filesystem_type == "unknown" or info.is_local is None:
        return FilesystemProbeResult(
            status="unknown",
            filesystem_type="unknown",
            flags=info.flags,
            message="The workspace filesystem type is unknown, so protected local scanning cannot "
            "prove its capability.",
            remediation="Choose a local filesystem with a verifiable statfs type, then run "
            "preflight again.",
        )
    if sys.platform == "darwin" and filesystem_type.casefold() not in _MAC_FILESYSTEM_TYPES:
        return _unsupported_result(
            info, reason=f"unrecognized local filesystem type {filesystem_type}"
        )
    return FilesystemProbeResult(
        status="supported",
        filesystem_type=filesystem_type,
        flags=info.flags,
        message=f"The workspace filesystem ({filesystem_type}) supports protected local scanning.",
        remediation="",
    )
