from __future__ import annotations

import socket
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from goodjob.git_metadata import classify_git_command_failure
from goodjob.platform import detect_platform, select_git_sandbox
from goodjob.platform.detect import (
    GitSandboxUnavailableError,
    Platform,
    git_executable_candidates,
    resolve_git_executable,
    sandbox_failure_reason,
)


def test_detect_platform_returns_current_platform() -> None:
    platform = detect_platform()
    if sys.platform == "darwin":
        assert platform == Platform.MACOS
    elif sys.platform.startswith("linux"):
        assert platform == Platform.LINUX
    elif sys.platform == "win32":
        assert platform == Platform.WINDOWS
    else:
        pytest.skip(f"test does not cover platform: {sys.platform}")


def test_platform_from_sys_platform_handles_known_values() -> None:
    assert Platform.from_sys_platform("darwin") == Platform.MACOS
    assert Platform.from_sys_platform("linux") == Platform.LINUX
    assert Platform.from_sys_platform("linux2") == Platform.LINUX
    assert Platform.from_sys_platform("win32") == Platform.WINDOWS


def test_platform_from_sys_platform_rejects_unknown() -> None:
    with pytest.raises(OSError, match="unsupported host platform"):
        Platform.from_sys_platform("unsupported")


def test_select_git_sandbox_returns_matching_backend() -> None:
    platform = detect_platform()
    sandbox = select_git_sandbox(resolve_git_executable())
    if platform == Platform.MACOS:
        from goodjob.platform.sandbox_macos import SeatbeltSandbox

        assert isinstance(sandbox, SeatbeltSandbox)
    elif platform == Platform.LINUX:
        from goodjob.platform.sandbox_linux import BwrapSandbox

        assert isinstance(sandbox, BwrapSandbox)
    elif platform == Platform.WINDOWS:
        from goodjob.platform.sandbox_windows import WfpGitSandbox

        assert isinstance(sandbox, WfpGitSandbox)


def test_git_executable_candidates_includes_platform_paths() -> None:
    candidates = git_executable_candidates()
    platform = detect_platform()
    if platform == Platform.MACOS:
        assert any("Xcode" in str(c) for c in candidates)
    elif platform == Platform.LINUX:
        assert Path("/usr/bin/git") in candidates
    elif platform == Platform.WINDOWS:
        assert any(c.name.lower() == "git.exe" for c in candidates)
    assert len(candidates) > 0


def test_resolve_git_executable_returns_existing_path() -> None:
    git = resolve_git_executable()
    assert isinstance(git, str)
    assert len(git) > 0


def test_scanner_delays_missing_git_error_until_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from goodjob.db import Database
    from goodjob.paths import DataPaths
    from goodjob.scanner import WorkspaceScanner

    monkeypatch.setattr(
        "goodjob.scanner.resolve_git_executable",
        lambda: (_ for _ in ()).throw(GitSandboxUnavailableError("install Git")),
    )
    database = Database(DataPaths.from_argument(str(tmp_path / "data")))
    with pytest.raises(GitSandboxUnavailableError, match="install Git"):
        WorkspaceScanner(database)


def test_resolve_git_executable_never_uses_inherited_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    malicious = tmp_path / "git"
    marker = tmp_path / "executed"
    malicious.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    malicious.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    if sys.platform == "win32":
        monkeypatch.setattr(
            "goodjob.platform.sandbox_windows.windows_git_candidates",
            lambda: (tmp_path / "missing-system-git.exe",),
        )
        expected_error = "trusted mingw64"
    else:
        monkeypatch.setattr(
            "goodjob.platform.detect.git_executable_candidates",
            lambda: (tmp_path / "missing-system-git",),
        )
        expected_error = "trusted system path"

    with pytest.raises(GitSandboxUnavailableError, match=expected_error):
        resolve_git_executable()
    assert not marker.exists()


@pytest.mark.parametrize(
    ("command", "stderr", "expected"),
    [
        (["/usr/bin/bwrap"], b"bwrap: No permissions to create new namespace\n", "user"),
        (["/usr/bin/sandbox-exec"], b"sandbox-exec: sandbox creation failed\n", "macOS"),
        (["/usr/bin/bwrap", "/usr/bin/git"], b"fatal: bad revision\n", None),
    ],
)
def test_sandbox_failure_reason_only_classifies_launcher_failures(
    command: list[str], stderr: bytes, expected: str | None
) -> None:
    reason = sandbox_failure_reason(command, 1, stderr)
    if expected is None:
        assert reason is None
    else:
        assert reason is not None and expected in reason


@pytest.mark.parametrize(
    ("returncode", "stderr", "expected_kind"),
    [
        (128, "xcrun: error: invalid active developer path", "git_executable_unusable"),
        (
            128,
            "fatal: unable to read /outside/gitconfig: Operation not permitted",
            "git_executable_unusable",
        ),
        (
            128,
            "fatal: could not open '.git/config': Operation not permitted",
            "git_repository_boundary_violation",
        ),
        (128, "fatal: not a git repository", "broken_repository"),
        (127, "", "git_executable_unusable"),
    ],
)
def test_git_failure_diagnosis_separates_toolchain_boundary_and_repository(
    returncode: int, stderr: str, expected_kind: str
) -> None:
    diagnosis = classify_git_command_failure(
        returncode=returncode,
        stdout="",
        stderr=stderr,
    )

    assert diagnosis.kind == expected_kind
    assert diagnosis.message
    assert diagnosis.remediation


def test_git_state_reports_sandbox_unavailable_with_enablement_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from goodjob.git_metadata import GitMetadataReader
    from goodjob.platform import GitSandboxUnavailableError
    from goodjob.scanner import _issue

    workspace = tmp_path / "workspace"
    repository = workspace / "repository"
    repository.mkdir(parents=True)
    reader = GitMetadataReader(
        git_executable="/usr/bin/git",
        issue_factory=_issue,
        safe_history_path=lambda _path: True,
        git_command_timeout_seconds=lambda: 10.0,
        workspace_git_command=lambda _binding, _arguments: [],
    )
    monkeypatch.setattr(reader, "_bind_internal_git", lambda _root, _workspace: MagicMock())

    def fail_sandbox(*_arguments: object, **_keywords: object) -> object:
        raise GitSandboxUnavailableError(
            "install bwrap and enable unprivileged user namespaces/AppArmor access"
        )

    monkeypatch.setattr(reader, "_git", fail_sandbox)

    state, issue = reader._git_state(repository, workspace, scan_started_at="2026-08-12T00:00:00Z")

    assert state is None
    assert issue is not None
    assert issue.kind == "git_sandbox_unavailable"
    assert issue.severity == "error"
    assert "bwrap" in issue.remediation
    assert "user namespaces" in issue.remediation


# --- macOS SeatbeltSandbox tests ---


macos_only = pytest.mark.skipif(
    sys.platform != "darwin", reason="macOS Seatbelt sandbox tests run only on macOS"
)


@macos_only
def test_seatbelt_sandbox_is_available_on_macos() -> None:
    from goodjob.platform.sandbox_macos import SeatbeltSandbox

    sandbox = SeatbeltSandbox()
    assert sandbox.is_available()


@macos_only
def test_seatbelt_sandbox_preserves_exact_legacy_command() -> None:
    from goodjob.git_metadata import GitMetadataReader
    from goodjob.platform.sandbox_macos import GIT_SANDBOX_PROFILE

    binding = MagicMock()
    binding.workspace_root = Path("/test/workspace")
    binding.git_dir = Path("/test/workspace/.git")
    binding.worktree_root = Path("/test/workspace")

    reader = GitMetadataReader(
        git_executable="/usr/bin/git",
        issue_factory=MagicMock(),
        safe_history_path=lambda _path: True,
        git_command_timeout_seconds=lambda: 10.0,
        workspace_git_command=lambda _binding, _arguments: [],
    )
    git_command = [
        "/usr/bin/git",
        "--no-lazy-fetch",
        "--no-replace-objects",
        "--git-dir=/test/workspace/.git",
        "--work-tree=/test/workspace",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.pager=cat",
        "--no-pager",
        "rev-parse",
        "HEAD",
    ]
    command = reader._git_command(binding, ("rev-parse", "HEAD"))

    assert command == [
        "/usr/bin/sandbox-exec",
        "-p",
        GIT_SANDBOX_PROFILE,
        "-D",
        "AUTHORIZED_ROOT=/test/workspace",
        "-D",
        "GIT_EXECUTABLE=/usr/bin/git",
        *git_command,
    ]
    assert command.count("--no-lazy-fetch") == 1
    assert command.count("core.hooksPath=/dev/null") == 1


@macos_only
def test_seatbelt_sandbox_raises_when_executable_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from goodjob.platform.sandbox_macos import SeatbeltSandbox

    sandbox = SeatbeltSandbox()
    monkeypatch.setattr(
        "goodjob.platform.sandbox_macos.SANDBOX_EXECUTABLE", tmp_path / "nonexistent"
    )
    assert not sandbox.is_available()
    binding = MagicMock()
    with pytest.raises(OSError, match="sandbox-exec"):
        sandbox.build_command("/usr/bin/git", binding, [])


# --- Linux BwrapSandbox tests ---


linux_only = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="bwrap tests run only on Linux"
)


@linux_only
def test_linux_git_command_uses_environment_for_lazy_fetch_suppression() -> None:
    from goodjob.git_metadata import GitMetadataReader

    binding = MagicMock()
    binding.workspace_root = Path("/test/workspace")
    binding.git_dir = Path("/test/workspace/.git")
    binding.worktree_root = Path("/test/workspace")
    reader = GitMetadataReader(
        git_executable="/usr/bin/git",
        issue_factory=MagicMock(),
        safe_history_path=lambda _path: True,
        git_command_timeout_seconds=lambda: 10.0,
        workspace_git_command=lambda _binding, _arguments: [],
    )

    command = reader._git_command(binding, ("rev-parse", "HEAD"))

    assert "--no-lazy-fetch" not in command
    assert command[-2:] == ["rev-parse", "HEAD"]


@linux_only
def test_bwrap_sandbox_availability_matches_trusted_candidates() -> None:
    from goodjob.platform.sandbox_linux import BWRAP_EXECUTABLE_CANDIDATES, BwrapSandbox

    sandbox = BwrapSandbox()
    assert sandbox.is_available() == any(
        Path(candidate).is_file() for candidate in BWRAP_EXECUTABLE_CANDIDATES
    )


@linux_only
def test_bwrap_sandbox_never_uses_inherited_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from goodjob.platform.sandbox_linux import BwrapSandbox

    malicious = tmp_path / "bwrap"
    marker = tmp_path / "executed"
    malicious.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    malicious.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(
        "goodjob.platform.sandbox_linux.BWRAP_EXECUTABLE_CANDIDATES",
        (str(tmp_path / "missing-system-bwrap"),),
    )

    sandbox = BwrapSandbox()
    assert not sandbox.is_available()
    with pytest.raises(GitSandboxUnavailableError, match="not installed"):
        sandbox.build_command("/usr/bin/git", MagicMock(), ["/usr/bin/git", "status"])
    assert not marker.exists()


@linux_only
def test_bwrap_sandbox_raises_when_executable_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from goodjob.platform import sandbox_linux

    monkeypatch.setattr(sandbox_linux, "_find_bwrap", lambda: None)
    sandbox = sandbox_linux.BwrapSandbox()
    assert not sandbox.is_available()
    binding = MagicMock()
    with pytest.raises(OSError, match="bubblewrap"):
        sandbox.build_command("/usr/bin/git", binding, [])


@linux_only
def test_bwrap_sandbox_builds_correct_command_structure() -> None:
    from goodjob.platform.sandbox_linux import BwrapSandbox

    binding = MagicMock()
    binding.workspace_root = Path("/test/workspace")
    binding.git_dir = Path("/test/workspace/repository/.git")
    binding.worktree_root = Path("/test/workspace/repository")

    sandbox = BwrapSandbox()
    if not sandbox.is_available():
        pytest.skip("bwrap not installed")

    git_command = ["/usr/bin/git", "rev-parse", "HEAD"]
    command = sandbox.build_command("/usr/bin/git", binding, git_command)

    assert Path(command[0]).name == "bwrap"
    assert "--die-with-parent" in command
    assert "--unshare-net" in command
    assert "--unshare-pid" in command

    unshare_pid_idx = command.index("--unshare-pid")
    tmpfs_idx = command.index("--tmpfs")
    ro_bind_idx = command.index("--ro-bind")
    assert unshare_pid_idx < tmpfs_idx < ro_bind_idx
    proc_idx = command.index("--proc")
    assert proc_idx > unshare_pid_idx, "--proc must come after --unshare-pid"

    assert "--ro-bind" in command
    assert command[ro_bind_idx + 1] == "/test/workspace"
    assert command[ro_bind_idx + 2] == "/test/workspace"

    assert "--dev" in command
    assert "--tmpfs" in command
    assert command[command.index("--chdir") + 1] == "/test/workspace/repository"
    assert command[-2:] == ["rev-parse", "HEAD"]


@linux_only
def test_bwrap_sandbox_command_includes_required_system_paths() -> None:
    from goodjob.platform.sandbox_linux import BwrapSandbox

    binding = MagicMock()
    binding.workspace_root = Path("/test/workspace")
    binding.git_dir = Path("/test/workspace/.git")
    binding.worktree_root = Path("/test/workspace")

    sandbox = BwrapSandbox()
    if not sandbox.is_available():
        pytest.skip("bwrap not installed")

    command = sandbox.build_command("/usr/bin/git", binding, ["/usr/bin/git", "status"])

    command_str = " ".join(command)
    assert "--ro-bind /usr /usr" in command_str
    assert "--ro-bind /lib /lib" in command_str
    assert "--ro-bind-try /lib64 /lib64" in command_str
    assert "--ro-bind-try /etc /etc" in command_str


@linux_only
def test_real_bwrap_closes_bound_directory_fds_and_enforces_boundaries(
    tmp_path: Path, git_sandbox_available: None
) -> None:
    from goodjob.git_metadata import GitMetadataReader, InternalGitBinding
    from goodjob.platform.sandbox_linux import BwrapSandbox

    workspace = tmp_path / "workspace"
    worktree = workspace / "repository"
    git_dir = worktree / ".git"
    outside = tmp_path / "outside-secret"
    git_dir.mkdir(parents=True)
    outside.write_bytes(b"outside-secret")
    original_workspace = {
        path.name: path.read_bytes() for path in workspace.iterdir() if path.is_file()
    }
    python = next(
        (path for path in (Path("/usr/bin/python3"), Path("/bin/python3")) if path.is_file()),
        None,
    )
    if python is None:
        pytest.skip("real bwrap test requires system Python")
    worktree_stat = worktree.stat()
    git_dir_stat = git_dir.stat()
    binding = InternalGitBinding(
        workspace_root=workspace,
        worktree_root=worktree,
        git_dir=git_dir,
        common_dir=git_dir,
        worktree_identity=(worktree_stat.st_dev, worktree_stat.st_ino),
        git_dir_identity=(git_dir_stat.st_dev, git_dir_stat.st_ino),
        common_dir_identity=(git_dir_stat.st_dev, git_dir_stat.st_ino),
    )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    probe = """
import os, socket, stat, sys
if os.getcwd() != sys.argv[1]:
    raise SystemExit(f'unexpected sandbox cwd: {os.getcwd()}')
directory_fds = []
for descriptor in range(3, 256):
    try:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            directory_fds.append(descriptor)
    except OSError:
        pass
if directory_fds:
    raise SystemExit(f'inherited directory fds: {directory_fds}')
try:
    open('write-probe', 'wb').write(b'changed')
except OSError:
    pass
else:
    raise SystemExit('authorized root remained writable')
try:
    open(sys.argv[2], 'rb').read()
except OSError:
    pass
else:
    raise SystemExit('root-external path remained readable')
connection = socket.socket()
if connection.connect_ex(('127.0.0.1', int(sys.argv[3]))) == 0:
    raise SystemExit('host network remained reachable')
print('sandbox-boundaries-ok')
"""
    sandbox = BwrapSandbox()
    reader = GitMetadataReader(
        git_executable=str(python),
        issue_factory=MagicMock(),
        safe_history_path=lambda _path: True,
        git_command_timeout_seconds=lambda: 10.0,
        workspace_git_command=lambda bound, _arguments: sandbox.build_command(
            str(python),
            bound,
            [str(python), "-c", probe, str(worktree), str(outside), str(port)],
        ),
    )
    try:
        returncode, stdout, stderr = reader._git_bounded_bytes(
            binding, "probe", maximum_output_bytes=4096
        )
    finally:
        listener.close()

    assert returncode == 0, stderr.decode("utf-8", errors="replace")
    assert stdout.strip() == b"sandbox-boundaries-ok"
    assert not (worktree / "write-probe").exists()
    assert outside.read_bytes() == b"outside-secret"
    assert {
        path.name: path.read_bytes() for path in workspace.iterdir() if path.is_file()
    } == original_workspace


# --- process_identity platform tests ---


def test_process_identity_returns_consistent_marker_for_current_process() -> None:
    from goodjob.process_identity import process_identity, process_start_marker

    identity = process_identity()
    assert identity.startswith("pid:")
    assert ";started:" in identity

    pid_str = identity.split(";")[0].removeprefix("pid:")
    pid = int(pid_str)
    marker = process_start_marker(pid)
    assert marker is not None
    assert marker == identity.split(";started:")[1]


def test_process_start_marker_returns_none_for_nonexistent_pid() -> None:
    from goodjob.process_identity import process_start_marker

    result = process_start_marker(99999999)
    assert result is None


def test_owner_process_stopped_detects_pid_reuse(monkeypatch: pytest.MonkeyPatch) -> None:
    from goodjob.process_identity import owner_process_stopped

    monkeypatch.setattr("goodjob.process_identity.os.kill", lambda _pid, _signal: None)
    monkeypatch.setattr("goodjob.process_identity.process_start_marker", lambda _pid: "new-start")

    assert owner_process_stopped("pid:123;started:old-start")


@macos_only
def test_macos_start_marker_uses_ps() -> None:
    import os

    from goodjob.process_identity import _macos_start_marker

    marker = _macos_start_marker(os.getpid())
    assert marker is not None
    assert any(c.isalpha() for c in marker)


@linux_only
def test_linux_start_marker_uses_proc_stat() -> None:
    import os

    from goodjob.process_identity import _linux_start_marker

    marker = _linux_start_marker(os.getpid())
    assert marker is not None
    assert marker.isdigit()


@linux_only
def test_linux_start_marker_reads_field_22_of_proc_stat() -> None:
    import os

    from goodjob.process_identity import _linux_start_marker

    raw_path = f"/proc/{os.getpid()}/stat"
    with open(raw_path, "rb") as f:
        content = f.read().decode("utf-8", errors="replace")
    right_paren = content.rfind(")")
    fields = content[right_paren + 2 :].split()
    expected_starttime = fields[19]

    marker = _linux_start_marker(os.getpid())
    assert marker == expected_starttime
