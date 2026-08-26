"""Detect the host platform and select the matching Git sandbox backend."""

from __future__ import annotations

import enum
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from goodjob.errors import GoodJobError, UnsupportedPlatformError

NATIVE_WINDOWS_RELEASE_ENABLED = True

if TYPE_CHECKING:
    from goodjob.git_metadata import InternalGitBinding


class GitSandbox(Protocol):
    """Build the sandboxed Git command for the current platform."""

    def build_command(
        self,
        git_executable: str,
        binding: InternalGitBinding,
        git_command: list[str],
    ) -> list[str]: ...


class GitSandboxUnavailableError(GoodJobError, OSError):
    """The selected Git sandbox is missing or could not establish its boundary."""


class Platform(enum.Enum):
    """Enumeration of supported host platforms."""

    MACOS = "macos"
    LINUX = "linux"
    WINDOWS = "windows"

    @classmethod
    def from_sys_platform(cls, value: str) -> Platform:
        if value == "darwin":
            return cls.MACOS
        if value.startswith("linux"):
            return cls.LINUX
        if value == "win32":
            return cls.WINDOWS
        raise OSError(f"unsupported host platform: {value}")


def detect_platform() -> Platform:
    """Return the current host platform."""
    return Platform.from_sys_platform(sys.platform)


def require_released_runtime() -> None:
    """Reject native Windows when the persistent release gate is disabled."""
    if detect_platform() == Platform.WINDOWS and not NATIVE_WINDOWS_RELEASE_ENABLED:
        raise UnsupportedPlatformError(
            "native Windows remains unsupported until IMP-31A-G pass on real hardware; use WSL2"
        )


def _macos_sandbox() -> GitSandbox:
    from goodjob.platform.sandbox_macos import SeatbeltSandbox

    return SeatbeltSandbox()


def _linux_sandbox() -> GitSandbox:
    from goodjob.platform.sandbox_linux import BwrapSandbox

    return BwrapSandbox()


def _windows_sandbox(git_executable: str) -> GitSandbox:
    from goodjob.platform.sandbox_windows import WfpGitSandbox

    return WfpGitSandbox(git_executable)


def select_git_sandbox(git_executable: str) -> GitSandbox:
    """Return the Git sandbox backend for the current platform.

    Raises OSError when no supported sandbox backend is available on the host.
    Fail-closed: never returns a no-op backend.
    """
    platform = detect_platform()
    if platform == Platform.MACOS:
        return _macos_sandbox()
    if platform == Platform.LINUX:
        return _linux_sandbox()
    if platform == Platform.WINDOWS:
        require_released_runtime()
        return _windows_sandbox(git_executable)
    raise OSError("a supported local Git filesystem sandbox is unavailable on this platform")


def git_executable_candidates() -> tuple[Path, ...]:
    """Return platform-aware Git executable candidate paths."""
    platform = detect_platform()
    if platform == Platform.MACOS:
        return (
            Path("/Applications/Xcode.app/Contents/Developer/usr/bin/git"),
            Path("/Library/Developer/CommandLineTools/usr/bin/git"),
            Path("/usr/bin/git"),
            Path("/bin/git"),
        )
    if platform == Platform.LINUX:
        return (Path("/usr/bin/git"),)
    require_released_runtime()
    from goodjob.platform.sandbox_windows import windows_git_candidates

    return windows_git_candidates()


def resolve_git_executable() -> str:
    """Resolve Git only from the platform's trusted absolute candidates."""
    if detect_platform() == Platform.WINDOWS:
        require_released_runtime()
        from goodjob.platform.sandbox_windows import resolve_windows_git_executable

        return resolve_windows_git_executable()
    for candidate in git_executable_candidates():
        if candidate.is_file():
            return str(candidate)
    raise GitSandboxUnavailableError(
        "Git is unavailable at a trusted system path; install Git through the platform "
        "toolchain before running GoodJob"
    )


def sandbox_failure_reason(command: list[str], returncode: int, stderr: bytes) -> str | None:
    """Classify a launcher failure without treating ordinary Git errors as sandbox failures."""
    if returncode == 0 or not command:
        return None
    launcher = Path(command[0]).name
    first_line = stderr.splitlines()[0].decode("utf-8", errors="replace") if stderr else ""
    if launcher == "bwrap" and first_line.startswith("bwrap:"):
        return (
            "bubblewrap could not establish the Linux Git sandbox; install bwrap and enable "
            "unprivileged user namespaces/AppArmor access (WSL requires WSL2)"
        )
    if launcher == "sandbox-exec" and first_line.startswith("sandbox-exec:"):
        return "macOS sandbox-exec could not establish the Git sandbox"
    return None
