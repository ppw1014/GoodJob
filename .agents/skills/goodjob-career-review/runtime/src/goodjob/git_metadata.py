"""Own only Git subprocesses, timeouts, output limits, authorization, and path validation."""

from __future__ import annotations

import os
import selectors
import signal
import stat
import subprocess
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, BinaryIO, Literal, Protocol, cast

from goodjob.errors import InvalidInputError
from goodjob.platform import GitSandboxUnavailableError, select_git_sandbox
from goodjob.platform.detect import Platform, detect_platform, sandbox_failure_reason
from goodjob.platform.fs_windows import WindowsDirectory
from goodjob.platform.handles_windows import (
    close_owned_resources,
    transfer_handle_to_crt_descriptor,
)
from goodjob.source_io import (
    MAX_SOURCE_FILE_BYTES,
    FileDescriptor,
    close_file_descriptor,
    open_regular_file,
    read_open_file,
)

if TYPE_CHECKING:
    from goodjob.scanner import ScanIssueDraft


MAX_FILE_BYTES = MAX_SOURCE_FILE_BYTES

HISTORY_WINDOW_DAYS = 180

MAX_HISTORY_COMMITS = 250

MAX_HISTORY_PATHS_PER_COMMIT = 200

MAX_HISTORY_METADATA_BYTES = 512 * 1024

MAX_HISTORY_PATH_BYTES = 256 * 1024

MAX_REMOTE_HEAD_BYTES = 512 * 1024

MAX_GIT_COMMAND_BYTES = 8 * 1024 * 1024

MAX_HISTORY_FIELD_BYTES = 512


@dataclass(frozen=True)
class GitFailureDiagnosis:
    """Stable classification for a completed Git command that did not succeed."""

    kind: Literal[
        "git_executable_unusable",
        "git_repository_boundary_violation",
        "broken_repository",
    ]
    message: str
    remediation: str


def classify_git_command_failure(
    *,
    returncode: int,
    stdout: str,
    stderr: str,
) -> GitFailureDiagnosis:
    """Classify known Git entry-point and boundary failures without exposing stderr.

    Git's diagnostic text is untrusted and may be localized.  Only stable markers
    that identify the launcher/toolchain itself are used for the executable class;
    permission failures remain repository-boundary failures.  Everything else is
    an ordinary repository failure.
    """
    del stdout
    normalized = stderr.casefold()
    executable_markers = (
        "xcrun: error:",
        "xcode-select:",
        "cannot execute",
        "failed to execute",
        "gitconfig",
        "git executable",
    )
    if returncode in {126, 127} or any(marker in normalized for marker in executable_markers):
        return GitFailureDiagnosis(
            "git_executable_unusable",
            "The selected Git executable or its required system configuration is unusable.",
            "Select an installed trusted Git executable with readable system configuration, "
            "then run refresh.",
        )
    if any(marker in stderr for marker in ("Operation not permitted", "Permission denied")):
        return GitFailureDiagnosis(
            "git_repository_boundary_violation",
            "Repository metadata requested a path outside the authorized Git sandbox.",
            "Remove root-external Git config or object indirection, then run refresh.",
        )
    return GitFailureDiagnosis(
        "broken_repository",
        "Local Git metadata could not be read through its bound repository identity.",
        "Repair the repository metadata and run refresh.",
    )


# Resolution is delayed until a scanner is constructed so missing system tools
# can cross the CLI boundary as a stable GoodJobError.
GIT_EXECUTABLE = "/usr/bin/git"

GIT_ENV = {
    "PATH": "/usr/bin:/bin",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_NO_LAZY_FETCH": "1",
    "GIT_PAGER": "cat",
    "GIT_LITERAL_PATHSPECS": "1",
    "GIT_NOGLOB_PATHSPECS": "1",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_TERMINAL_PROMPT": "0",
    "LC_ALL": "C",
}


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _relative_path(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _posix_directory_flags() -> int:
    """Return link-resistant POSIX directory flags or fail closed."""
    directory_flag = cast(int | None, getattr(os, "O_DIRECTORY", None))
    if directory_flag is None:
        raise OSError("POSIX directory descriptors are unavailable on this platform")
    return os.O_RDONLY | directory_flag | getattr(os, "O_NOFOLLOW", 0)


def _posix_fchdir(file_descriptor: int) -> None:
    fchdir = cast(Callable[[int], None] | None, getattr(os, "fchdir", None))
    if fchdir is None:
        raise OSError("descriptor-relative working directories are unavailable on this platform")
    fchdir(file_descriptor)


def _kill_posix_process_group(process_id: int) -> None:
    killpg = cast(Callable[[int, int], None] | None, getattr(os, "killpg", None))
    sigkill = cast(int | None, getattr(signal, "SIGKILL", None))
    if killpg is None or sigkill is None:
        raise OSError("POSIX process-group termination is unavailable on this platform")
    killpg(process_id, sigkill)


def _open_regular_file(root: Path, relative_path: str) -> tuple[FileDescriptor, os.stat_result]:
    return open_regular_file(root, relative_path)


def _read_open_file(file_fd: FileDescriptor, *, maximum_bytes: int = MAX_FILE_BYTES) -> bytes:
    return read_open_file(file_fd, maximum_bytes=maximum_bytes)


type DirectoryDescriptor = int | WindowsDirectory


def _open_directory(root: Path, relative_path: str = ".") -> DirectoryDescriptor:
    """Open a directory below the authorized root without following links."""
    if detect_platform() == Platform.WINDOWS:
        from goodjob.platform.fs_windows import open_directory

        return open_directory(root, relative_path)
    parts = PurePosixPath(relative_path).parts
    if relative_path == ".":
        parts = ()
    if any(part in {"", ".", ".."} for part in parts):
        raise OSError("relative directory path is not safe to open")
    flags = _posix_directory_flags()
    root_fd = os.open(root, flags)
    directory_fd = root_fd
    try:
        for part in parts:
            next_fd = os.open(part, flags, dir_fd=directory_fd)
            _close_directory(directory_fd)
            directory_fd = next_fd
        return directory_fd
    except Exception:
        _close_directory(directory_fd)
        raise


def _open_absolute_directory(path: Path) -> DirectoryDescriptor:
    """Open one exact absolute directory without following any component symlink."""
    if detect_platform() == Platform.WINDOWS:
        from goodjob.platform.fs_windows import open_absolute_directory

        return open_absolute_directory(path)
    if not path.is_absolute():
        raise OSError("directory path must be absolute")
    parts = path.parts
    if not parts or any(part in {"", ".", ".."} for part in parts[1:]):
        raise OSError("absolute directory path is not safe to open")
    flags = _posix_directory_flags()
    directory_fd = os.open(parts[0], flags)
    try:
        for part in parts[1:]:
            next_fd = os.open(part, flags, dir_fd=directory_fd)
            _close_directory(directory_fd)
            directory_fd = next_fd
        return directory_fd
    except Exception:
        _close_directory(directory_fd)
        raise


def _open_regular_file_at(
    directory_fd: DirectoryDescriptor, relative_path: str
) -> tuple[FileDescriptor, os.stat_result]:
    """Open a regular file below an already-bound directory descriptor."""
    if isinstance(directory_fd, WindowsDirectory):
        handle = directory_fd.open_regular(relative_path)
        descriptor = transfer_handle_to_crt_descriptor(handle, os.O_RDONLY)
        try:
            file_stat = os.fstat(descriptor.value)
        except BaseException as primary_error:
            close_owned_resources((descriptor,), cause=primary_error)
            raise
        return descriptor, file_stat
    parts = PurePosixPath(relative_path).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise OSError("relative path is not safe to open")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flags = _posix_directory_flags()
    parent_fd = os.dup(directory_fd)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        file_fd = os.open(parts[-1], os.O_RDONLY | nofollow, dir_fd=parent_fd)
        file_stat = os.fstat(file_fd)
        if not stat.S_ISREG(file_stat.st_mode):
            os.close(file_fd)
            raise OSError("path is not a regular file")
        return file_fd, file_stat
    finally:
        os.close(parent_fd)


def _read_text_at(
    directory_fd: DirectoryDescriptor, relative_path: str, *, maximum_bytes: int = 4096
) -> str:
    if isinstance(directory_fd, WindowsDirectory):
        from goodjob.platform.fs_windows import read_text_at

        return read_text_at(directory_fd, relative_path, maximum_bytes=maximum_bytes)
    file_fd, _ = _open_regular_file_at(directory_fd, relative_path)
    try:
        return _read_open_file(file_fd, maximum_bytes=maximum_bytes).decode("utf-8")
    finally:
        close_file_descriptor(file_fd)


def _directory_identity(directory_fd: DirectoryDescriptor) -> tuple[int, int]:
    if isinstance(directory_fd, WindowsDirectory):
        identity = directory_fd.identity
        return identity.volume_serial, int.from_bytes(identity.file_id, "little")
    directory_stat = os.fstat(directory_fd)
    if not stat.S_ISDIR(directory_stat.st_mode):
        raise OSError("bound path is not a directory")
    return directory_stat.st_dev, directory_stat.st_ino


def _safe_lstat(root: Path, relative_path: str) -> os.stat_result:
    """Stat a child through an already-authorized directory descriptor."""
    if detect_platform() == Platform.WINDOWS:
        from goodjob.platform.fs_windows import stat_relative

        return stat_relative(root, relative_path)
    relative = PurePosixPath(relative_path)
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise OSError("relative path is not safe to stat")
    parent = relative.parent
    parent_relative = "." if str(parent) == "." else parent.as_posix()
    directory_fd = _open_directory(root, parent_relative)
    try:
        return _stat_at(directory_fd, relative.name)
    finally:
        _close_directory(directory_fd)


def _close_directory(directory: DirectoryDescriptor) -> None:
    if isinstance(directory, WindowsDirectory):
        directory.close()
    else:
        os.close(directory)


def _stat_at(directory: DirectoryDescriptor, component: str) -> os.stat_result:
    if isinstance(directory, WindowsDirectory):
        return directory.stat(component)
    return os.stat(component, dir_fd=directory, follow_symlinks=False)


def _child_relative(directory_relative: str, child_name: str) -> str:
    return child_name if directory_relative == "." else f"{directory_relative}/{child_name}"


def _relative_to_root(path: Path, root: Path) -> str:
    relative = path.relative_to(root).as_posix()
    return "." if relative == "." else relative


def _valid_git_commit(value: str) -> bool:
    return len(value) in {40, 64} and all(character in "0123456789abcdef" for character in value)


def _history_timestamp(raw_seconds: bytes) -> str:
    try:
        seconds = int(raw_seconds.decode("ascii"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("Git history returned an invalid commit timestamp") from exc
    if seconds < 0:
        raise ValueError("Git history returned a negative commit timestamp")
    return datetime.fromtimestamp(seconds, UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class GitHistoryEntry:
    commit: str
    committed_at: str
    author_name: str
    author_email: str
    subject: str
    changed_paths: tuple[str, ...]
    paths_truncated: bool


@dataclass(frozen=True)
class GitState:
    git_dir: Path
    common_dir: Path
    branch: str | None
    head_commit: str | None
    dirty_state: str
    path_states: dict[str, str]
    history_basis: str
    history_entries: tuple[GitHistoryEntry, ...]
    external_metadata_only: bool
    external_git_grant: ExternalGitGrant | None
    external_metadata_read_fields: tuple[str, ...]
    history_issues: tuple[ScanIssueDraft, ...]


@dataclass(frozen=True)
class InternalGitBinding:
    """Descriptor identities that bind every Git command to one authorized repository."""

    workspace_root: Path
    worktree_root: Path
    git_dir: Path
    common_dir: Path
    worktree_identity: tuple[int, int]
    git_dir_identity: tuple[int, int]
    common_dir_identity: tuple[int, int]


@dataclass(frozen=True)
class ExternalGitCandidate:
    """Root-internal marker data that may be shown before any external read."""

    git_pointer_path: Path
    marker_kind: Literal["file", "directory"]
    git_dir_candidate: Path
    common_dir_candidate: Path | None

    def as_json(self) -> dict[str, object]:
        return {
            "git_pointer_path": str(self.git_pointer_path),
            "marker_kind": self.marker_kind,
            "git_dir_candidate": str(self.git_dir_candidate),
            "common_dir_candidate": (
                str(self.common_dir_candidate) if self.common_dir_candidate is not None else None
            ),
            "read_fields": ["git_marker", "gitdir_candidate", "commondir_candidate"],
        }


@dataclass(frozen=True)
class ExternalGitGrant:
    """A metadata-only authorization for one root-external linked worktree relation."""

    git_pointer_path: Path
    marker_kind: Literal["file", "directory"]
    git_dir: Path
    common_dir: Path
    git_dir_device: int
    git_dir_inode: int
    common_dir_device: int
    common_dir_inode: int
    authorization_receipt_id: str
    confirmed_at: str


def inspect_external_git_candidate(
    workspace_root: Path, git_pointer_path: Path
) -> dict[str, object]:
    """Inspect only a root-internal Git marker before relation authorization."""
    workspace = workspace_root.resolve(strict=True)
    raw_pointer = (
        git_pointer_path if git_pointer_path.is_absolute() else workspace / git_pointer_path
    )
    pointer = Path(os.path.normpath(str(raw_pointer)))
    if pointer.name != ".git" or not _is_within(pointer, workspace):
        raise InvalidInputError("git_pointer must name a .git file inside the authorized workspace")
    try:
        pointer_relative = _relative_to_root(pointer, workspace)
        marker_stat = _safe_lstat(workspace, pointer_relative)
    except OSError as exc:
        raise InvalidInputError("git_pointer is not readable") from exc
    if stat.S_ISREG(marker_stat.st_mode):
        lexical_git_dir = GitMetadataReader._git_pointer_target_at(workspace, pointer_relative)
        if lexical_git_dir is None:
            raise InvalidInputError("git_pointer is not a valid linked-worktree pointer")
        common_candidate: Path | None = None
        if _is_within(lexical_git_dir, workspace):
            git_dir_relative = _relative_to_root(lexical_git_dir, workspace)
            common_candidate = GitMetadataReader._relation_target_at(
                workspace, git_dir_relative, "commondir"
            )
            if common_candidate is None or _is_within(common_candidate, workspace):
                raise InvalidInputError("git_pointer does not expose a root-external relation")
        candidate = ExternalGitCandidate(pointer, "file", lexical_git_dir, common_candidate)
        return candidate.as_json()
    if stat.S_ISDIR(marker_stat.st_mode):
        common_candidate = GitMetadataReader._relation_target_at(
            workspace, pointer_relative, "commondir"
        )
        if common_candidate is None or _is_within(common_candidate, workspace):
            raise InvalidInputError("git_pointer does not expose a root-external relation")
        return ExternalGitCandidate(pointer, "directory", pointer, common_candidate).as_json()
    raise InvalidInputError("git_pointer must be a regular file or directory .git marker")


def probe_external_git_relation(
    workspace_root: Path,
    git_pointer_path: Path,
    *,
    marker_kind: Literal["file", "directory"],
    git_dir_candidate: Path,
    common_dir_candidate: Path | None,
) -> dict[str, object]:
    """Resolve one candidate-bound relation; never invoke Git or read project source."""
    inspected = inspect_external_git_candidate(workspace_root, git_pointer_path)
    expected_common = str(common_dir_candidate) if common_dir_candidate is not None else None
    if (
        inspected.get("marker_kind") != marker_kind
        or inspected.get("git_dir_candidate") != str(git_dir_candidate)
        or inspected.get("common_dir_candidate") != expected_common
    ):
        raise InvalidInputError("Git marker candidate changed after relation authorization")
    pointer = Path(str(inspected["git_pointer_path"]))
    try:
        git_dir_fd = _open_absolute_directory(git_dir_candidate)
    except OSError as exc:
        raise InvalidInputError("linked-worktree gitdir is not readable") from exc
    try:
        lexical_common_dir = GitMetadataReader._relation_target_from_fd(
            git_dir_fd, git_dir_candidate, "commondir"
        )
        if lexical_common_dir is None:
            raise InvalidInputError("linked-worktree commondir is not valid")
        if common_dir_candidate is not None and lexical_common_dir != common_dir_candidate:
            raise InvalidInputError("linked-worktree commondir candidate changed")
        if marker_kind == "file":
            lexical_back_pointer = GitMetadataReader._relation_target_from_fd(
                git_dir_fd, git_dir_candidate, "gitdir"
            )
            if lexical_back_pointer != pointer:
                raise InvalidInputError(
                    "linked-worktree relation does not bind back to the authorized pointer"
                )
        try:
            common_dir_fd = _open_absolute_directory(lexical_common_dir)
        except OSError as exc:
            raise InvalidInputError("linked-worktree commondir is not readable") from exc
        try:
            git_identity = _directory_identity(git_dir_fd)
            common_identity = _directory_identity(common_dir_fd)
        finally:
            _close_directory(common_dir_fd)
    finally:
        _close_directory(git_dir_fd)
    return {
        "git_pointer_path": str(pointer),
        "marker_kind": marker_kind,
        "git_dir": str(git_dir_candidate),
        "common_dir": str(lexical_common_dir),
        "git_dir_device": git_identity[0],
        "git_dir_inode": git_identity[1],
        "common_dir_device": common_identity[0],
        "common_dir_inode": common_identity[1],
        "read_fields": ["gitdir", "commondir", "directory_identity"],
    }


class IssueFactory(Protocol):
    def __call__(
        self,
        kind: str,
        severity: Literal["info", "warning", "error"],
        message: str,
        remediation: str,
        relative_path: str | None = None,
    ) -> ScanIssueDraft: ...


class GitMetadataReader:
    def __init__(
        self,
        *,
        git_executable: str,
        issue_factory: IssueFactory,
        safe_history_path: Callable[[str], bool],
        git_command_timeout_seconds: Callable[[], float],
        workspace_git_command: Callable[[InternalGitBinding, tuple[str, ...]], list[str]],
    ) -> None:
        self._git_executable = git_executable
        self._issue = issue_factory
        self._safe_history_path = safe_history_path
        self._timeout = git_command_timeout_seconds
        self._workspace_git_command = workspace_git_command

    @staticmethod
    def _linked_worktree_relation_state(
        workspace_root: Path, marker: Path, git_dir: Path
    ) -> Literal["trusted", "external", "invalid"]:
        if not _is_within(git_dir, workspace_root):
            return "external"
        try:
            git_dir_relative = _relative_to_root(git_dir, workspace_root)
            git_dir_fd = _open_directory(workspace_root, git_dir_relative)
        except OSError:
            return "invalid"
        else:
            _close_directory(git_dir_fd)
        common_dir = GitMetadataReader._relation_target_at(
            workspace_root, git_dir_relative, "commondir"
        )
        back_pointer = GitMetadataReader._relation_target_at(
            workspace_root, git_dir_relative, "gitdir"
        )
        if common_dir is None or back_pointer != marker:
            return "invalid"
        if not _is_within(common_dir, workspace_root):
            return "external"
        try:
            common_dir_fd = _open_directory(
                workspace_root, _relative_to_root(common_dir, workspace_root)
            )
        except OSError:
            return "invalid"
        else:
            _close_directory(common_dir_fd)
        return "trusted"

    @staticmethod
    def _git_directory_relation_state(
        workspace_root: Path, marker_relative: str
    ) -> Literal["trusted", "external", "invalid"]:
        commondir_relative = _child_relative(marker_relative, "commondir")
        try:
            commondir_stat = _safe_lstat(workspace_root, commondir_relative)
        except FileNotFoundError:
            return "trusted"
        except OSError:
            return "invalid"
        if not stat.S_ISREG(commondir_stat.st_mode):
            return "invalid"
        common_dir = GitMetadataReader._relation_target_at(
            workspace_root, marker_relative, "commondir"
        )
        if common_dir is None:
            return "invalid"
        if not _is_within(common_dir, workspace_root):
            return "external"
        try:
            common_fd = _open_directory(
                workspace_root, _relative_to_root(common_dir, workspace_root)
            )
        except OSError:
            return "invalid"
        else:
            _close_directory(common_fd)
        return "trusted"

    @staticmethod
    def _bind_internal_git(worktree_root: Path, workspace_root: Path) -> InternalGitBinding | None:
        """Bind an internal repository without letting Git rediscover a mutable marker."""
        marker = worktree_root / ".git"
        marker_relative = _child_relative(_relative_to_root(worktree_root, workspace_root), ".git")
        try:
            marker_stat = _safe_lstat(workspace_root, marker_relative)
        except OSError:
            return None

        if stat.S_ISREG(marker_stat.st_mode):
            git_dir = GitMetadataReader._git_pointer_target_at(workspace_root, marker_relative)
            if git_dir is None or not _is_within(git_dir, workspace_root):
                return None
        elif stat.S_ISDIR(marker_stat.st_mode):
            git_dir = marker
        else:
            return None

        try:
            worktree_fd = _open_directory(
                workspace_root, _relative_to_root(worktree_root, workspace_root)
            )
        except OSError:
            return None
        try:
            git_dir_fd = _open_directory(workspace_root, _relative_to_root(git_dir, workspace_root))
        except OSError:
            _close_directory(worktree_fd)
            return None
        try:
            if stat.S_ISREG(marker_stat.st_mode):
                common_dir = GitMetadataReader._relation_target_from_fd(
                    git_dir_fd, git_dir, "commondir"
                )
                back_pointer = GitMetadataReader._relation_target_from_fd(
                    git_dir_fd, git_dir, "gitdir"
                )
                if common_dir is None or back_pointer != marker:
                    return None
            else:
                try:
                    common_stat = _stat_at(git_dir_fd, "commondir")
                except FileNotFoundError:
                    common_dir = git_dir
                except OSError:
                    return None
                else:
                    if not stat.S_ISREG(common_stat.st_mode):
                        return None
                    common_dir = GitMetadataReader._relation_target_from_fd(
                        git_dir_fd, git_dir, "commondir"
                    )
                    if common_dir is None:
                        return None
            if not _is_within(common_dir, workspace_root):
                return None
            try:
                common_dir_fd = _open_directory(
                    workspace_root, _relative_to_root(common_dir, workspace_root)
                )
            except OSError:
                return None
            try:
                return InternalGitBinding(
                    workspace_root=workspace_root,
                    worktree_root=worktree_root,
                    git_dir=git_dir,
                    common_dir=common_dir,
                    worktree_identity=_directory_identity(worktree_fd),
                    git_dir_identity=_directory_identity(git_dir_fd),
                    common_dir_identity=_directory_identity(common_dir_fd),
                )
            finally:
                _close_directory(common_dir_fd)
        finally:
            _close_directory(git_dir_fd)
            _close_directory(worktree_fd)

    def _external_git_state(
        self,
        root: Path,
        workspace_root: Path,
        marker: Path,
        grant: ExternalGitGrant,
    ) -> tuple[GitState | None, ScanIssueDraft | None]:
        relative_root = _relative_path(root, workspace_root)
        try:
            inspected = inspect_external_git_candidate(workspace_root, marker)
            expected_common_candidate = (
                None if not _is_within(grant.git_dir, workspace_root) else str(grant.common_dir)
            )
            if (
                inspected.get("marker_kind") != grant.marker_kind
                or inspected.get("git_dir_candidate") != str(grant.git_dir)
                or inspected.get("common_dir_candidate") != expected_common_candidate
            ):
                raise ValueError("the root-internal Git candidate changed")
            git_dir_fd = _open_absolute_directory(grant.git_dir)
            try:
                common_dir_fd = _open_absolute_directory(grant.common_dir)
                try:
                    if _directory_identity(git_dir_fd) != (
                        grant.git_dir_device,
                        grant.git_dir_inode,
                    ) or _directory_identity(common_dir_fd) != (
                        grant.common_dir_device,
                        grant.common_dir_inode,
                    ):
                        raise ValueError("an authorized external Git directory changed identity")
                    common_target = self._relation_target_from_fd(
                        git_dir_fd, grant.git_dir, "commondir"
                    )
                    if common_target != grant.common_dir:
                        raise ValueError("the authorized commondir relation changed")
                    if grant.marker_kind == "file":
                        root_target = self._relation_target_from_fd(
                            git_dir_fd, grant.git_dir, "gitdir"
                        )
                        if root_target != marker:
                            raise ValueError("the authorized gitdir back-pointer changed")
                    branch, head, read_fields = self._external_head_state(git_dir_fd, common_dir_fd)
                    if not self._external_directory_identity_matches(
                        grant.git_dir,
                        (grant.git_dir_device, grant.git_dir_inode),
                    ) or not self._external_directory_identity_matches(
                        grant.common_dir,
                        (grant.common_dir_device, grant.common_dir_inode),
                    ):
                        raise ValueError("an authorized external Git directory path changed")
                finally:
                    _close_directory(common_dir_fd)
            finally:
                _close_directory(git_dir_fd)
            inspected_after = inspect_external_git_candidate(workspace_root, marker)
            if inspected_after != inspected:
                raise ValueError("the root-internal Git marker changed during metadata reading")
        except (InvalidInputError, OSError, UnicodeError, ValueError):
            return None, self._issue(
                "external_git_relation_mismatch",
                "warning",
                "The authorized Git relation changed or contains invalid bounded metadata.",
                "Run a new candidate inspection and confirm the current exact Git relation.",
                relative_root,
            )
        return (
            GitState(
                grant.git_dir,
                grant.common_dir,
                branch,
                head,
                "not_applicable",
                {},
                "external_metadata_only",
                (),
                True,
                grant,
                read_fields,
                (
                    self._issue(
                        "external_git_history_unavailable",
                        "warning",
                        "External Git metadata authorization excludes history and object reads.",
                        "Expand the workspace explicitly before requesting Git history analysis.",
                        relative_root,
                    ),
                    self._issue(
                        "external_git_worktree_state_unavailable",
                        "warning",
                        "External metadata-only mode does not infer index or dirty-state details.",
                        "Treat source commit state as unknown unless the workspace scope expands.",
                        relative_root,
                    ),
                ),
            ),
            None,
        )

    @staticmethod
    def _external_head_state(
        git_dir_fd: DirectoryDescriptor, common_dir_fd: DirectoryDescriptor
    ) -> tuple[str | None, str | None, tuple[str, ...]]:
        raw_head = _read_text_at(git_dir_fd, "HEAD", maximum_bytes=4096)
        lines = raw_head.splitlines()
        if len(lines) != 1:
            raise ValueError("external Git HEAD must contain one line")
        head_value = lines[0].strip()
        if head_value.startswith("ref: "):
            reference = head_value.removeprefix("ref: ").strip()
            if not GitMetadataReader._safe_git_reference(reference):
                raise ValueError("external Git HEAD contains an unsafe reference")
            head = GitMetadataReader._external_ref_commit(common_dir_fd, reference)
            branch = (
                reference.removeprefix("refs/heads/")
                if reference.startswith("refs/heads/")
                else None
            )
            return branch, head, ("gitdir", "commondir", "head", "ref")
        if not _valid_git_commit(head_value):
            raise ValueError("external Git HEAD contains an invalid commit identity")
        return None, head_value, ("gitdir", "commondir", "head")

    @staticmethod
    def _safe_git_reference(reference: str) -> bool:
        if not reference.startswith("refs/") or len(reference) > 1024:
            return False
        if reference.endswith(("/", ".", ".lock")) or ".." in reference or "@{" in reference:
            return False
        forbidden = set(" ~^:?*[\\")
        return all(
            32 < ord(character) < 127 and character not in forbidden for character in reference
        ) and all(part not in {"", ".", ".."} for part in reference.split("/"))

    @staticmethod
    def _external_ref_commit(common_dir_fd: DirectoryDescriptor, reference: str) -> str | None:
        try:
            raw_reference = _read_text_at(common_dir_fd, reference, maximum_bytes=4096)
        except FileNotFoundError:
            raw_reference = ""
        if raw_reference:
            lines = raw_reference.splitlines()
            if len(lines) != 1 or not _valid_git_commit(lines[0].strip()):
                raise ValueError("external Git loose reference is invalid")
            return lines[0].strip()
        try:
            packed = _read_text_at(common_dir_fd, "packed-refs", maximum_bytes=MAX_FILE_BYTES)
        except FileNotFoundError:
            return None
        match: str | None = None
        for line in packed.splitlines():
            if not line or line.startswith(("#", "^")):
                continue
            commit, separator, name = line.partition(" ")
            if not separator or not _valid_git_commit(commit) or not name:
                raise ValueError("external Git packed-refs contains invalid data")
            if name == reference:
                if match is not None and match != commit:
                    raise ValueError("external Git packed reference is ambiguous")
                match = commit
        return match

    @staticmethod
    def _external_directory_identity_matches(path: Path, expected: tuple[int, int]) -> bool:
        try:
            directory_fd = _open_absolute_directory(path)
        except OSError:
            return False
        try:
            return _directory_identity(directory_fd) == expected
        finally:
            _close_directory(directory_fd)

    @staticmethod
    def _git_pointer_target(root: Path) -> Path | None:
        return GitMetadataReader._git_pointer_target_at(root, ".git")

    @staticmethod
    def _git_pointer_target_at(root: Path, relative_pointer: str) -> Path | None:
        try:
            file_fd, _ = _open_regular_file(root, relative_pointer)
            try:
                payload = _read_open_file(file_fd, maximum_bytes=4096).decode("utf-8")
            finally:
                close_file_descriptor(file_fd)
        except (OSError, UnicodeError):
            return None
        lines = payload.splitlines()
        if len(lines) != 1 or not lines[0].startswith("gitdir: "):
            return None
        raw_target = lines[0].removeprefix("gitdir: ").strip()
        if not raw_target or "\x00" in raw_target:
            return None
        target = Path(raw_target)
        if not target.is_absolute():
            target = root / PurePosixPath(relative_pointer).parent / target
        return Path(os.path.normpath(str(target)))

    @staticmethod
    def _relation_target_from_fd(
        git_dir_fd: DirectoryDescriptor, git_dir: Path, filename: str
    ) -> Path | None:
        try:
            raw_target = _read_text_at(git_dir_fd, filename, maximum_bytes=4096).strip()
        except (OSError, UnicodeError):
            return None
        if not raw_target or "\x00" in raw_target:
            return None
        target = Path(raw_target)
        if not target.is_absolute():
            target = git_dir / target
        return Path(os.path.normpath(str(target)))

    @staticmethod
    def _relation_target_at(root: Path, relative_git_dir: str, filename: str) -> Path | None:
        try:
            relative_file = _child_relative(relative_git_dir, filename)
            file_fd, _ = _open_regular_file(root, relative_file)
            try:
                raw_target = _read_open_file(file_fd, maximum_bytes=4096).decode("utf-8").strip()
            finally:
                close_file_descriptor(file_fd)
        except (OSError, UnicodeError):
            return None
        if not raw_target or "\x00" in raw_target:
            return None
        target = Path(raw_target)
        if not target.is_absolute():
            target = root / relative_git_dir / target
        return Path(os.path.normpath(str(target)))

    def _git_state(
        self,
        root: Path,
        workspace_root: Path,
        *,
        scan_started_at: str,
    ) -> tuple[GitState | None, ScanIssueDraft | None]:
        binding = self._bind_internal_git(root, workspace_root)
        if binding is None:
            return None, self._issue(
                "broken_repository",
                "warning",
                "Local Git metadata changed or could not be bound to the authorized workspace.",
                "Repair the repository metadata and run refresh.",
                _relative_path(root, workspace_root),
            )
        try:
            include_result = self._git(
                binding,
                "config",
                "--no-includes",
                "--null",
                "--show-origin",
                "--get-regexp",
                r"^include.*\.path$",
            )
            if include_result.returncode == 0 and self._has_root_external_include(
                binding, include_result.stdout
            ):
                return None, self._issue(
                    "git_repository_boundary_violation",
                    "warning",
                    "Repository metadata requested a path outside the authorized Git sandbox.",
                    "Remove root-external Git config or object indirection, then run refresh.",
                    _relative_path(root, workspace_root),
                )
            result = self._git(binding, "rev-parse", "--is-inside-work-tree")
            if result.returncode != 0 or result.stdout.strip() != "true":
                diagnosis = classify_git_command_failure(
                    returncode=result.returncode,
                    stdout=result.stdout,
                    stderr=result.stderr,
                )
                return None, self._issue(
                    diagnosis.kind,
                    "warning",
                    diagnosis.message,
                    diagnosis.remediation,
                    _relative_path(root, workspace_root),
                )
            branch_result = self._git(binding, "symbolic-ref", "--quiet", "--short", "HEAD")
            branch = branch_result.stdout.strip() if branch_result.returncode == 0 else None
            head_result = self._git(binding, "rev-parse", "--verify", "HEAD")
            head = head_result.stdout.strip() if head_result.returncode == 0 else None
            if head is not None and not _valid_git_commit(head):
                return None, self._issue(
                    "broken_repository",
                    "warning",
                    "Local Git metadata returned an invalid HEAD commit identity.",
                    "Repair the repository metadata and run refresh.",
                    _relative_path(root, workspace_root),
                )
            status_result = self._git(
                binding,
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                maximum_output_bytes=MAX_GIT_COMMAND_BYTES,
            )
        except GitSandboxUnavailableError as exc:
            return None, self._issue(
                "git_sandbox_unavailable",
                "error",
                "The platform Git sandbox is unavailable or could not establish its boundary.",
                str(exc),
                _relative_path(root, workspace_root),
            )
        except subprocess.TimeoutExpired:
            return None, self._issue(
                "git_command_timeout",
                "warning",
                "A bounded local Git command exceeded the scan time limit.",
                "Reduce repository pressure or repair Git integrations, then run refresh.",
                _relative_path(root, workspace_root),
            )
        except OSError:
            return None, self._issue(
                "git_command_resource_limit",
                "warning",
                "A local Git command exceeded an output limit or lost its bound repository.",
                "Repair the repository metadata or reduce generated files, then run refresh.",
                _relative_path(root, workspace_root),
            )
        if status_result.returncode != 0:
            diagnosis = classify_git_command_failure(
                returncode=status_result.returncode,
                stdout=status_result.stdout,
                stderr=status_result.stderr,
            )
            if diagnosis.kind != "broken_repository":
                return None, self._issue(
                    diagnosis.kind,
                    "warning",
                    diagnosis.message,
                    diagnosis.remediation,
                    _relative_path(root, workspace_root),
                )
            return None, self._issue(
                "git_status_unavailable",
                "warning",
                "Git working-tree state could not be read.",
                "Repair local Git metadata and run refresh.",
                _relative_path(root, workspace_root),
            )
        states = self._parse_git_status(status_result.stdout)
        state_values = set(states.values())
        dirty_state = (
            "mixed"
            if "modified" in state_values and "untracked" in state_values
            else "modified"
            if "modified" in state_values
            else "untracked"
            if "untracked" in state_values
            else "clean"
        )
        try:
            history_basis, history_entries, history_issues = self._recent_git_history(
                binding, head, branch, scan_started_at
            )
        except GitSandboxUnavailableError as exc:
            return None, self._issue(
                "git_sandbox_unavailable",
                "error",
                "The platform Git sandbox became unavailable while reading repository history.",
                str(exc),
                _relative_path(root, workspace_root),
            )
        return (
            GitState(
                binding.git_dir,
                binding.common_dir,
                branch,
                head,
                dirty_state,
                states,
                history_basis,
                history_entries,
                False,
                None,
                (),
                history_issues,
            ),
            None,
        )

    @staticmethod
    def _has_root_external_include(binding: InternalGitBinding, output: str) -> bool:
        fields = output.split("\0")
        if fields and not fields[-1]:
            fields.pop()
        if len(fields) % 2 != 0:
            return True
        for origin, record in zip(fields[::2], fields[1::2], strict=True):
            key, separator, raw_path = record.partition("\n")
            normalized_key = key.casefold()
            if not separator or not (
                normalized_key == "include.path"
                or normalized_key.startswith("includeif.")
                and normalized_key.endswith(".path")
            ):
                continue
            if not origin.startswith("file:") or not raw_path or raw_path.startswith(("~", "%")):
                return True
            origin_path = Path(origin.removeprefix("file:"))
            if not origin_path.is_absolute():
                origin_path = binding.worktree_root / origin_path
            origin_path = Path(os.path.normpath(str(origin_path)))
            if not _is_within(origin_path, binding.workspace_root):
                return True
            include_path = Path(raw_path)
            if not include_path.is_absolute():
                include_path = origin_path.parent / include_path
            lexical_path = Path(os.path.normpath(str(include_path)))
            if not _is_within(lexical_path, binding.workspace_root):
                return True
        return False

    def _recent_git_history(
        self,
        binding: InternalGitBinding,
        head: str | None,
        branch: str | None,
        scan_started_at: str,
    ) -> tuple[str, tuple[GitHistoryEntry, ...], tuple[ScanIssueDraft, ...]]:
        relative_root = _relative_path(binding.worktree_root, binding.workspace_root)
        revisions: tuple[str, ...]
        if head is None:
            return "head_only_unborn", (), ()
        try:
            if branch is None:
                history_basis = "head_only_detached"
                revisions = (head,)
            else:
                default_commit, history_basis = self._default_history_commit(binding)
                revisions = (
                    (head,)
                    if default_commit is None or default_commit == head
                    else (
                        head,
                        default_commit,
                    )
                )
            entries, truncated = self._read_recent_history(
                binding, revisions, head, scan_started_at
            )
        except GitSandboxUnavailableError:
            raise
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return (
                "history_unavailable",
                (),
                (
                    self._issue(
                        "git_history_unavailable",
                        "warning",
                        "Recent local Git history could not be read with the fixed bounded "
                        "commands.",
                        "Repair local Git metadata and run refresh; no network fetch was "
                        "attempted.",
                        relative_root,
                    ),
                ),
            )
        if not truncated:
            return history_basis, entries, ()
        return (
            history_basis,
            entries,
            (
                self._issue(
                    "git_history_truncated",
                    "warning",
                    "Recent Git history exceeded a local metadata safety limit.",
                    "Use a narrower later evidence query when the omitted history is needed.",
                    relative_root,
                ),
            ),
        )

    def _default_history_commit(self, binding: InternalGitBinding) -> tuple[str | None, str]:
        return_code, raw_targets, _ = self._git_bounded_bytes(
            binding,
            "for-each-ref",
            "--format=%(symref)",
            "refs/remotes/*/HEAD",
            maximum_output_bytes=MAX_REMOTE_HEAD_BYTES,
        )
        if return_code != 0:
            raise ValueError("could not list local remote HEAD references")
        try:
            decoded_targets = raw_targets.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("local remote HEAD references are not UTF-8") from exc
        targets = {line.strip() for line in decoded_targets.splitlines() if line.strip()}
        if len(targets) > 1:
            return None, "head_only_ambiguous_remote_head"
        if len(targets) == 1:
            target = next(iter(targets))
            if not target.startswith("refs/remotes/"):
                return None, "head_only_invalid_remote_head"
            commit = self._verified_git_commit(binding, target)
            if commit is None:
                return None, "head_only_invalid_remote_head"
            return commit, "head_plus_remote_head"
        main = self._verified_git_commit(binding, "refs/heads/main")
        if main is not None:
            return main, "head_plus_main"
        master = self._verified_git_commit(binding, "refs/heads/master")
        if master is not None:
            return master, "head_plus_master"
        return None, "head_only_no_default_ref"

    def _verified_git_commit(self, binding: InternalGitBinding, reference: str) -> str | None:
        result = self._git(
            binding,
            "rev-parse",
            "--verify",
            "--quiet",
            "--end-of-options",
            f"{reference}^{{commit}}",
        )
        candidate = result.stdout.strip()
        if result.returncode != 0 or not _valid_git_commit(candidate):
            return None
        return candidate

    def _read_recent_history(
        self,
        binding: InternalGitBinding,
        revisions: tuple[str, ...],
        head: str,
        scan_started_at: str,
    ) -> tuple[tuple[GitHistoryEntry, ...], bool]:
        try:
            started_at = datetime.fromisoformat(scan_started_at.removesuffix("Z") + "+00:00")
        except ValueError as exc:
            raise ValueError("scan start time is invalid") from exc
        cutoff = (
            (started_at - timedelta(days=HISTORY_WINDOW_DAYS)).isoformat().replace("+00:00", "Z")
        )
        metadata = self._git_bounded_bytes(
            binding,
            "log",
            "--no-ext-diff",
            "--no-textconv",
            "--date-order",
            f"--max-count={MAX_HISTORY_COMMITS + 1}",
            f"--since={cutoff}",
            "-z",
            "--format=%H%x00%ct%x00%an%x00%ae%x00%s",
            *revisions,
            "--",
            maximum_output_bytes=MAX_HISTORY_METADATA_BYTES,
        )
        if metadata[0] != 0:
            raise ValueError("Git history metadata command failed")
        records = self._parse_history_metadata(metadata[1])
        if head not in {record[0] for record in records}:
            head_metadata = self._git_bounded_bytes(
                binding,
                "log",
                "--no-ext-diff",
                "--no-textconv",
                "--no-walk",
                "-z",
                "--format=%H%x00%ct%x00%an%x00%ae%x00%s",
                head,
                "--",
                maximum_output_bytes=MAX_HISTORY_METADATA_BYTES,
            )
            if head_metadata[0] != 0:
                raise ValueError("Git HEAD metadata command failed")
            records = self._parse_history_metadata(head_metadata[1]) + records
        ordered: list[tuple[str, str, str, str, str]] = []
        seen_commits: set[str] = set()
        for record in records:
            if record[0] in seen_commits:
                continue
            seen_commits.add(record[0])
            ordered.append(record)
        truncated = len(ordered) > MAX_HISTORY_COMMITS
        selected = ordered[:MAX_HISTORY_COMMITS]
        entries: list[GitHistoryEntry] = []
        for commit, committed_at, author_name, author_email, subject in selected:
            paths, paths_truncated = self._history_paths(binding, commit)
            entries.append(
                GitHistoryEntry(
                    commit=commit,
                    committed_at=committed_at,
                    author_name=author_name,
                    author_email=author_email,
                    subject=subject,
                    changed_paths=paths,
                    paths_truncated=paths_truncated,
                )
            )
            truncated = truncated or paths_truncated
        return tuple(entries), truncated

    @staticmethod
    def _parse_history_metadata(raw: bytes) -> list[tuple[str, str, str, str, str]]:
        fields = raw.split(b"\0")
        if fields and fields[-1] == b"":
            fields.pop()
        if len(fields) % 5 != 0:
            raise ValueError("Git history metadata has an invalid record shape")
        records: list[tuple[str, str, str, str, str]] = []
        for index in range(0, len(fields), 5):
            commit_raw, timestamp_raw, author_raw, email_raw, subject_raw = fields[
                index : index + 5
            ]
            if any(
                len(value) > MAX_HISTORY_FIELD_BYTES
                for value in (author_raw, email_raw, subject_raw)
            ):
                raise ValueError("Git history metadata field exceeded the local limit")
            try:
                commit = commit_raw.decode("ascii")
            except UnicodeDecodeError as exc:
                raise ValueError("Git history returned a non-ASCII commit identity") from exc
            if not _valid_git_commit(commit):
                raise ValueError("Git history returned an invalid commit identity")
            records.append(
                (
                    commit,
                    _history_timestamp(timestamp_raw),
                    author_raw.decode("utf-8", errors="replace"),
                    email_raw.decode("utf-8", errors="replace"),
                    subject_raw.decode("utf-8", errors="replace"),
                )
            )
        return records

    def _history_paths(
        self, binding: InternalGitBinding, commit: str, *pathspecs: str
    ) -> tuple[tuple[str, ...], bool]:
        result = self._git_bounded_bytes(
            binding,
            "diff-tree",
            "--no-ext-diff",
            "--no-textconv",
            "--no-commit-id",
            "--root",
            "--name-only",
            "-r",
            "-z",
            commit,
            "--",
            *pathspecs,
            maximum_output_bytes=MAX_HISTORY_PATH_BYTES,
        )
        if result[0] != 0:
            raise ValueError("Git history path-range command failed")
        paths: list[str] = []
        truncated = False
        for raw_path in result[1].split(b"\0"):
            if not raw_path:
                continue
            if len(raw_path) > MAX_HISTORY_FIELD_BYTES:
                truncated = True
                continue
            try:
                path = raw_path.decode("utf-8")
            except UnicodeDecodeError:
                truncated = True
                continue
            if not self._safe_history_path(path):
                truncated = True
                continue
            if len(paths) == MAX_HISTORY_PATHS_PER_COMMIT:
                truncated = True
                continue
            paths.append(path)
        return tuple(paths), truncated

    def _git_command(
        self,
        binding: InternalGitBinding,
        arguments: tuple[str, ...],
    ) -> list[str]:
        platform = detect_platform()
        sandbox = select_git_sandbox(self._git_executable)
        git_command = [self._git_executable]
        if platform != Platform.LINUX:
            git_command.append("--no-lazy-fetch")
        git_command.extend(
            [
                "--no-replace-objects",
                f"--git-dir={binding.git_dir}",
                f"--work-tree={binding.worktree_root}",
                "-c",
                "core.fsmonitor=false",
                "-c",
                f"core.hooksPath={'NUL' if platform == Platform.WINDOWS else '/dev/null'}",
                "-c",
                "core.pager=cat",
                *(
                    [
                        "-c",
                        "credential.helper=",
                        "-c",
                        "core.askPass=",
                        "-c",
                        "diff.external=",
                        "-c",
                        "protocol.allow=never",
                        "-c",
                        "protocol.file.allow=always",
                    ]
                    if platform == Platform.WINDOWS
                    else []
                ),
                "--no-pager",
                *arguments,
            ]
        )
        return sandbox.build_command(self._git_executable, binding, git_command)

    @staticmethod
    def _fchdir_and_close_bound_directories(worktree_fd: int, *bound_fds: int) -> None:
        """Enter the bound worktree, then remove host directory capabilities before exec."""
        _posix_fchdir(worktree_fd)
        for descriptor in bound_fds:
            with suppress(OSError):
                os.close(descriptor)

    @staticmethod
    def _open_bound_git_directory(
        binding: InternalGitBinding, path: Path, expected_identity: tuple[int, int]
    ) -> DirectoryDescriptor:
        if not _is_within(path, binding.workspace_root):
            raise OSError("bound Git directory escaped the authorized workspace")
        directory_fd = _open_directory(
            binding.workspace_root, _relative_to_root(path, binding.workspace_root)
        )
        if _directory_identity(directory_fd) != expected_identity:
            _close_directory(directory_fd)
            raise OSError("bound Git directory identity changed")
        return directory_fd

    def _git_bounded_bytes(
        self, binding: InternalGitBinding, *arguments: str, maximum_output_bytes: int
    ) -> tuple[int, bytes, bytes]:
        if detect_platform() == Platform.WINDOWS:
            return self._git_bounded_bytes_windows(
                binding, *arguments, maximum_output_bytes=maximum_output_bytes
            )
        worktree_fd = self._open_bound_git_directory(
            binding, binding.worktree_root, binding.worktree_identity
        )
        assert isinstance(worktree_fd, int)
        try:
            git_dir_fd = self._open_bound_git_directory(
                binding, binding.git_dir, binding.git_dir_identity
            )
            assert isinstance(git_dir_fd, int)
        except Exception:
            _close_directory(worktree_fd)
            raise
        try:
            common_dir_fd = self._open_bound_git_directory(
                binding, binding.common_dir, binding.common_dir_identity
            )
            assert isinstance(common_dir_fd, int)
        except Exception:
            _close_directory(git_dir_fd)
            _close_directory(worktree_fd)
            raise
        process: subprocess.Popen[bytes] | None = None
        selector = selectors.DefaultSelector()
        try:
            command = self._workspace_git_command(
                binding,
                arguments,
            )
            environment = {
                **GIT_ENV,
                "GIT_COMMON_DIR": str(binding.common_dir),
            }
            try:
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=environment,
                    pass_fds=(worktree_fd, git_dir_fd, common_dir_fd),
                    preexec_fn=partial(
                        self._fchdir_and_close_bound_directories,
                        worktree_fd,
                        worktree_fd,
                        git_dir_fd,
                        common_dir_fd,
                    ),
                    start_new_session=True,
                )
            except OSError as exc:
                raise GitSandboxUnavailableError(
                    "the selected platform Git sandbox could not be launched"
                ) from exc
        finally:
            _close_directory(common_dir_fd)
            _close_directory(git_dir_fd)
            _close_directory(worktree_fd)
        assert process is not None
        assert process.stdout is not None
        assert process.stderr is not None
        outputs: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
        try:
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            deadline = time.monotonic() + self._timeout()
            while selector.get_map():
                timeout = deadline - time.monotonic()
                if timeout <= 0:
                    raise subprocess.TimeoutExpired(process.args, self._timeout())
                events = selector.select(timeout)
                if not events:
                    raise subprocess.TimeoutExpired(process.args, self._timeout())
                for key, _ in events:
                    stream = cast(BinaryIO, key.fileobj)
                    chunk = os.read(stream.fileno(), 64 * 1024)
                    if not chunk:
                        selector.unregister(stream)
                        stream.close()
                        continue
                    total = sum(len(value) for value in outputs.values()) + len(chunk)
                    if total > maximum_output_bytes:
                        raise OSError("Git command exceeded its local output limit")
                    outputs[str(key.data)].extend(chunk)
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                raise subprocess.TimeoutExpired(process.args, self._timeout())
            return_code = process.wait(timeout=timeout)
            self._verify_git_binding(binding)
            failure_reason = sandbox_failure_reason(command, return_code, bytes(outputs["stderr"]))
            if failure_reason is not None:
                raise GitSandboxUnavailableError(failure_reason)
            return return_code, bytes(outputs["stdout"]), bytes(outputs["stderr"])
        except BaseException:
            if process.poll() is None:
                with suppress(ProcessLookupError):
                    _kill_posix_process_group(process.pid)
            process.wait(timeout=self._timeout())
            raise
        finally:
            selector.close()
            for stream in (cast(BinaryIO, process.stdout), cast(BinaryIO, process.stderr)):
                if not stream.closed:
                    stream.close()

    def _git_bounded_bytes_windows(
        self, binding: InternalGitBinding, *arguments: str, maximum_output_bytes: int
    ) -> tuple[int, bytes, bytes]:
        from goodjob.platform.launcher_windows import (
            WindowsLaunchRequest,
            run_windows_process,
        )
        from goodjob.platform.sandbox_windows import WfpGitSandbox

        self._verify_git_binding(binding)
        command = self._workspace_git_command(binding, arguments)
        if not command or os.path.normcase(command[0]) != os.path.normcase(self._git_executable):
            raise GitSandboxUnavailableError(
                "Windows Git command does not start with the WFP-scoped real executable"
            )
        sandbox = select_git_sandbox(self._git_executable)
        if not isinstance(sandbox, WfpGitSandbox):
            raise GitSandboxUnavailableError("the Windows WFP Git backend was not selected")
        environment = {
            **GIT_ENV,
            "PATH": "",
            "GIT_ALLOW_PROTOCOL": "file",
            "GIT_ASKPASS": "",
            "GIT_CONFIG_GLOBAL": "NUL",
            "GIT_CONFIG_SYSTEM": "NUL",
            "GIT_COMMON_DIR": str(binding.common_dir),
            "GIT_EXTERNAL_DIFF": "",
            "SSH_ASKPASS": "",
        }
        guard = sandbox.open_network_guard()
        try:
            result = run_windows_process(
                WindowsLaunchRequest(
                    application=self._git_executable,
                    arguments=tuple(command[1:]),
                    cwd=str(binding.worktree_root),
                    environment=environment,
                    maximum_output_bytes=maximum_output_bytes,
                    timeout_seconds=self._timeout(),
                    active_process_limit=1,
                ),
                network_guard=guard,
            )
        except subprocess.TimeoutExpired:
            raise
        except OSError as exc:
            if "bounded-output" in str(exc) or "exceeded" in str(exc):
                raise
            raise GitSandboxUnavailableError(
                "the Windows WFP/Job Git boundary could not launch safely"
            ) from exc
        self._verify_git_binding(binding)
        return result.returncode, result.stdout, result.stderr

    def _verify_git_binding(self, binding: InternalGitBinding) -> None:
        for path, identity in (
            (binding.worktree_root, binding.worktree_identity),
            (binding.git_dir, binding.git_dir_identity),
            (binding.common_dir, binding.common_dir_identity),
        ):
            directory_fd = self._open_bound_git_directory(binding, path, identity)
            _close_directory(directory_fd)

    def _git(
        self,
        binding: InternalGitBinding,
        *arguments: str,
        maximum_output_bytes: int = MAX_GIT_COMMAND_BYTES,
    ) -> subprocess.CompletedProcess[str]:
        return_code, stdout, stderr = self._git_bounded_bytes(
            binding, *arguments, maximum_output_bytes=maximum_output_bytes
        )
        return subprocess.CompletedProcess(
            args=list(arguments),
            returncode=return_code,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
        )

    @staticmethod
    def _parse_git_status(output: str) -> dict[str, str]:
        records = output.split("\0")
        states: dict[str, str] = {}
        index = 0
        while index < len(records):
            record = records[index]
            index += 1
            if not record:
                continue
            if len(record) < 4:
                continue
            status, path = record[:2], record[3:]
            if status[0] in "RC" or status[1] in "RC":
                index += 1
            states[path] = "untracked" if status == "??" else "modified"
        return states
