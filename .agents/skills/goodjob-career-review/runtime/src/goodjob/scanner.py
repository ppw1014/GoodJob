"""Deterministic, local-only workspace discovery and evidence indexing."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import uuid
from collections import Counter
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal, Protocol, cast

import goodjob.git_metadata as _git_metadata
from goodjob.adapters import (
    AnalysisDiagnostic,
    AnalysisFact,
    AnalysisResult,
    adapter_version,
    analyze_file,
)
from goodjob.config import ExcludedProjectRule, ProjectExclusionConfig, load_project_exclusions
from goodjob.db import Database
from goodjob.errors import InvalidInputError
from goodjob.git_metadata import (
    MAX_FILE_BYTES,
    MAX_GIT_COMMAND_BYTES,
    GitHistoryEntry,
    GitMetadataReader,
    GitState,
    _child_relative,
    _close_directory,
    _is_within,
    _open_directory,
    _read_open_file,
    _relative_path,
    _relative_to_root,
    _safe_lstat,
)
from goodjob.platform.detect import resolve_git_executable
from goodjob.platform.filesystem_probe import FilesystemProbeResult, probe_workspace_filesystem
from goodjob.platform.fs_windows import WindowsDirectory
from goodjob.process_identity import owner_process_stopped, process_identity
from goodjob.source_io import close_file_descriptor

HISTORY_WINDOW_DAYS = _git_metadata.HISTORY_WINDOW_DAYS
MAX_HISTORY_METADATA_BYTES = _git_metadata.MAX_HISTORY_METADATA_BYTES
ExternalGitGrant = _git_metadata.ExternalGitGrant
InternalGitBinding = _git_metadata.InternalGitBinding
_open_regular_file = _git_metadata._open_regular_file
inspect_external_git_candidate = _git_metadata.inspect_external_git_candidate
probe_external_git_relation = _git_metadata.probe_external_git_relation


class _BoundDirectoryEntry(Protocol):
    name: str

    def stat(self, *, follow_symlinks: bool = True) -> os.stat_result: ...


def _bound_directory_entries(directory: int | WindowsDirectory) -> list[_BoundDirectoryEntry]:
    if isinstance(directory, WindowsDirectory):
        return cast(list[_BoundDirectoryEntry], directory.list_entries())
    with os.scandir(os.dup(directory)) as scanned:
        return cast(list[_BoundDirectoryEntry], list(scanned))


def _bound_readlink(directory: int | WindowsDirectory, name: str) -> str:
    if isinstance(directory, WindowsDirectory):
        return directory.readlink(name)
    return os.readlink(name, dir_fd=directory)


ANALYZER_VERSION = "scan-v2"
ROLE_LENS_CONTEXT_CONTRACT_VERSION = "role-lens-context-v1"
MAX_ROLE_LENS_CONTEXT_PROJECTS = 200
MAX_ROLE_LENS_CONTEXT_MODULES = 500
MAX_ROLE_LENS_CONTEXT_MODULES_PER_PROJECT = 20
MAX_ROLE_LENS_CONTEXT_EVIDENCE_SAMPLES = 500
MAX_ROLE_LENS_CONTEXT_EVIDENCE_PER_PROJECT = 10
SCAN_OVERVIEW_CONTRACT_VERSION = "scan-overview-v2"
MAX_SCAN_OVERVIEW_ISSUES = 200
MAX_SCAN_OVERVIEW_GROUPS = 200
MAX_SCAN_OVERVIEW_SAMPLES_PER_GROUP = 3
IGNORE_PATTERN_SYNTAX: tuple[tuple[str, str], ...] = (
    ("literal_name", "supported"),
    ("star_and_question_wildcards", "supported_with_python_fnmatch_approximation"),
    ("directory_suffix", "supported"),
    ("negation_prefix", "supported_as_last_match_reinclusion"),
    ("path_pattern", "supported_with_python_fnmatch_approximation"),
    ("comment", "supported"),
    ("blank_line", "supported"),
    ("root_anchor", "approximated_as_unanchored"),
    ("double_star", "approximated_as_repeated_star"),
)
GIT_COMMAND_TIMEOUT_SECONDS = 10.0
HARD_EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        ".cache",
        ".aws",
        ".dart_tool",
        ".gnupg",
        ".idea",
        ".next",
        ".pytest_cache",
        ".terraform",
        ".venv",
        ".ssh",
        "__pycache__",
        "bower_components",
        "build",
        "coverage",
        "credentials",
        "dist",
        "env",
        "node_modules",
        ".repo",
        "secrets",
        "target",
        "venv",
    }
)
PACKAGED_APP_DIRECTORY_SUFFIXES = frozenset(
    {
        ".app",
        ".framework",
        ".xcarchive",
    }
)
MANIFEST_NAMES = frozenset(
    {
        "Cargo.toml",
        "CMakeLists.txt",
        "Package.swift",
        "build.gradle",
        "build.gradle.kts",
        "go.mod",
        "package.json",
        "pom.xml",
        "pubspec.yaml",
        "pyproject.toml",
    }
)
SOURCE_EXTENSIONS = {
    ".c": "cpp",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".dart": "dart",
    ".h": "cpp",
    ".hh": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
    ".py": "python",
    ".rs": "rust",
    ".sql": "sql",
    ".ts": "typescript",
    ".tsx": "typescript",
}
CONFIG_EXTENSIONS = frozenset({".json", ".toml", ".yaml", ".yml"})
BINARY_ASSET_EXTENSIONS = frozenset(
    {
        ".7z",
        ".a",
        ".aab",
        ".avi",
        ".bin",
        ".bmp",
        ".bz2",
        ".class",
        ".db",
        ".dll",
        ".dylib",
        ".exe",
        ".flac",
        ".gif",
        ".gz",
        ".icns",
        ".ico",
        ".ipa",
        ".jar",
        ".jpeg",
        ".jpg",
        ".m4a",
        ".mov",
        ".mp3",
        ".mp4",
        ".o",
        ".ogg",
        ".otf",
        ".pdf",
        ".png",
        ".rar",
        ".so",
        ".sqlite",
        ".sqlite3",
        ".tar",
        ".tif",
        ".tiff",
        ".ttf",
        ".wav",
        ".wasm",
        ".webm",
        ".webp",
        ".woff",
        ".woff2",
        ".xz",
        ".zip",
    }
)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _new_id() -> str:
    return str(uuid.uuid4())


def _short(value: str, maximum: int = 500) -> str:
    return value[:maximum]


def _diagnostics_json(diagnostics: tuple[AnalysisDiagnostic, ...]) -> str:
    return json.dumps(
        [
            {
                "kind": diagnostic.kind,
                "message": diagnostic.message,
                "remediation": diagnostic.remediation,
            }
            for diagnostic in diagnostics
        ],
        separators=(",", ":"),
        sort_keys=True,
    )


def _analysis_diagnostics(raw: str) -> tuple[AnalysisDiagnostic, ...]:
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("stored analysis diagnostics are invalid") from exc
    if not isinstance(values, list) or len(values) > 8:
        raise ValueError("stored analysis diagnostics are invalid")
    diagnostics: list[AnalysisDiagnostic] = []
    for value in values:
        if not isinstance(value, dict):
            raise ValueError("stored analysis diagnostic is invalid")
        kind = value.get("kind")
        message = value.get("message")
        remediation = value.get("remediation")
        if (
            not isinstance(kind, str)
            or not isinstance(message, str)
            or not isinstance(remediation, str)
            or not kind
            or max(len(kind), len(message), len(remediation)) > 500
        ):
            raise ValueError("stored analysis diagnostic is invalid")
        diagnostics.append(AnalysisDiagnostic(kind, message, remediation))
    return tuple(diagnostics)


def _is_packaged_app_root(name: str) -> bool:
    lower = name.lower()
    return any(lower.endswith(suffix) for suffix in PACKAGED_APP_DIRECTORY_SUFFIXES)


def _safe_history_path(path: str) -> bool:
    pure_path = PurePosixPath(path)
    if not path or pure_path.is_absolute() or ".." in pure_path.parts:
        return False
    if any(
        part.lower() in HARD_EXCLUDED_DIRECTORIES or _is_packaged_app_root(part)
        for part in pure_path.parts[:-1]
    ):
        return False
    return not WorkspaceScanner._is_sensitive(pure_path.name)


def _symlink_may_escape(directory_relative: str, target: str) -> bool:
    windows_target = PureWindowsPath(target)
    if windows_target.is_absolute() or windows_target.drive:
        return True
    target_path = PurePosixPath(target)
    if target_path.is_absolute():
        return True
    components = [] if directory_relative == "." else directory_relative.split("/")
    for part in target_path.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not components:
                return True
            components.pop()
        else:
            components.append(part)
    return False


def _normalized_link_target(base: Path, target: str) -> Path:
    candidate = Path(target)
    if not candidate.is_absolute():
        candidate = base / PurePosixPath(target)
    return Path(os.path.normpath(str(candidate)))


def _link_chain_may_escape(workspace_root: Path, base: Path, target: str) -> bool:
    """Check a metadata link chain without following any filesystem link."""
    current = _normalized_link_target(base, target)
    seen: set[Path] = set()
    for _ in range(8):
        if not _is_within(current, workspace_root) or current in seen:
            return True
        seen.add(current)
        try:
            relative = _relative_to_root(current, workspace_root)
            current_stat = _safe_lstat(workspace_root, relative)
        except OSError:
            return True
        if not stat.S_ISLNK(current_stat.st_mode):
            return False
        parent_relative = _relative_to_root(current.parent, workspace_root)
        try:
            parent_fd = _open_directory(workspace_root, parent_relative)
            try:
                next_target = _bound_readlink(parent_fd, current.name)
            finally:
                _close_directory(parent_fd)
        except OSError:
            return True
        current = _normalized_link_target(current.parent, next_target)
    return True


@dataclass(frozen=True)
class ScanIssueDraft:
    issue_id: str
    kind: str
    severity: Literal["info", "warning", "error"]
    relative_path: str | None
    message: str
    remediation: str


@dataclass(frozen=True)
class IgnorePatternIssueDraft(ScanIssueDraft):
    raw_pattern: str


def _issue(
    kind: str,
    severity: Literal["info", "warning", "error"],
    message: str,
    remediation: str,
    relative_path: str | None = None,
) -> ScanIssueDraft:
    return ScanIssueDraft(
        issue_id=_new_id(),
        kind=kind,
        severity=severity,
        relative_path=_short(relative_path) if relative_path else None,
        message=_short(message),
        remediation=_short(remediation),
    )


def _overview_issue_quotas(
    *,
    issue_limit: int,
    error_count: int,
    warning_count: int,
    info_count: int,
) -> tuple[int, int, int]:
    """Bound the raw list without allowing one serious severity to starve another."""
    serious_count = error_count + warning_count
    if serious_count <= issue_limit:
        return error_count, warning_count, min(info_count, issue_limit - serious_count)

    quotas = {"error": 0, "warning": 0}
    counts = {"error": error_count, "warning": warning_count}
    remaining = issue_limit
    while remaining:
        made_progress = False
        for severity in ("error", "warning"):
            if remaining == 0:
                break
            if quotas[severity] >= counts[severity]:
                continue
            quotas[severity] += 1
            remaining -= 1
            made_progress = True
        if not made_progress:
            break
    return quotas["error"], quotas["warning"], 0


@dataclass(frozen=True)
class WorktreePlan:
    root: Path
    git_state: GitState | None


@dataclass(frozen=True)
class ProjectPlan:
    identity_kind: Literal["git_common_dir", "non_git_root"]
    identity_key: str
    display_name: str
    worktrees: tuple[WorktreePlan, ...]


@dataclass(frozen=True)
class PriorObservation:
    artifact_id: str
    source_revision_id: str
    byte_size: int
    mtime_ns: int
    adapter_id: str
    adapter_version: str
    config_revision: str
    commit_state: str
    analysis_diagnostics: tuple[AnalysisDiagnostic, ...]


@dataclass(frozen=True)
class FileObservation:
    relative_path: str
    artifact_kind: str
    adapter_id: str
    adapter_version: str
    evidence_kind: str
    commit_state: str
    byte_size: int
    mtime_ns: int
    content_sha256: str | None
    reused_source_revision_id: str | None
    analysis_facts: tuple[AnalysisFact, ...] | None
    analysis_diagnostics: tuple[AnalysisDiagnostic, ...]


@dataclass(frozen=True)
class WorktreeData:
    plan: WorktreePlan
    files: tuple[FileObservation, ...]
    issues: tuple[ScanIssueDraft, ...]
    excluded: Counter[str]


@dataclass(frozen=True)
class ProjectData:
    plan: ProjectPlan
    worktrees: tuple[WorktreeData, ...]


@dataclass(frozen=True)
class ProjectRecord:
    project_id: str
    display_name: str


@dataclass(frozen=True)
class ScanResult:
    scan_run_id: str
    workspace_id: str
    status: str
    mode: str
    change_detection_mode: str | None
    coverage: dict[str, object]
    issues: tuple[ScanIssueDraft, ...]

    def as_json(self) -> dict[str, object]:
        return {
            "status": "ok",
            "scan_run": {
                "scan_run_id": self.scan_run_id,
                "workspace_id": self.workspace_id,
                "status": self.status,
                "mode": self.mode,
                "change_detection_mode": self.change_detection_mode,
            },
            "coverage": self.coverage,
            "issues": [
                {
                    "issue_id": issue.issue_id,
                    "kind": issue.kind,
                    "severity": issue.severity,
                    "relative_path": issue.relative_path,
                    "message": issue.message,
                    "remediation": issue.remediation,
                }
                for issue in self.issues
            ],
        }


class IgnoreMatcher:
    """A deterministic subset of ``gitignore(5)`` PATTERN FORMAT semantics.

    Git semantic | Runtime behavior | Disposition
    blank line | skipped | support
    unescaped leading ``#`` | treated as a comment | support
    backslash escapes for ``#``, ``!``, or trailing space | retained literally | disclose
    unescaped trailing spaces | stripped | support
    leading or non-space trailing whitespace | stripped though Git keeps it significant | disclose
    leading ``!`` and last match | toggles inclusion | support
    reinclusion below an excluded parent | last match incorrectly reincludes | disclose
    leading ``/`` | stripped and may become unanchored | disclose
    internal ``/`` | anchored to the ignore-file directory | support
    no ``/`` | matches any component below the ignore-file directory | support
    trailing ``/`` | matches directory ancestors, not the final file | support
    a pattern matching a directory | matches that directory's descendants | support
    ``*``, ``?``, or simple ranges without ``/`` | Python fnmatch per component | support
    ``^``-negated or POSIX named ranges | Python fnmatch lacks the Git bracket syntax | disclose
    ``*`` or ``?`` with ``/`` | Python fnmatch may cross separators | disclose
    a character range with ``/`` | a negated Python range may match a separator | disclose
    leading ``**/`` | treated as repeated ``*`` | disclose
    trailing ``/**`` | treated as repeated ``*`` | disclose
    middle ``/**/`` | treated as repeated ``*`` | disclose
    other consecutive ``**`` | treated as repeated ``*`` | disclose
    """

    def __init__(self, patterns: tuple[tuple[str, str, bool, bool], ...]) -> None:
        self._patterns = patterns

    @classmethod
    def load(
        cls, root: Path, ignore_files: list[str]
    ) -> tuple[IgnoreMatcher, list[ScanIssueDraft]]:
        patterns: list[tuple[str, str, bool, bool]] = []
        issues: list[ScanIssueDraft] = []
        for relative in sorted(ignore_files):
            try:
                file_fd, _ = _open_regular_file(root, relative)
                try:
                    text = _read_open_file(file_fd).decode("utf-8")
                finally:
                    close_file_descriptor(file_fd)
            except (OSError, UnicodeError):
                issues.append(
                    _issue(
                        "ignore_unreadable",
                        "warning",
                        "Could not decode a project ignore file.",
                        "Use UTF-8 for .gitignore or review files selected by the scanner.",
                        relative,
                    )
                )
                continue
            parent = PurePosixPath(relative).parent
            base = "." if str(parent) == "." else parent.as_posix()
            for raw_line in text.splitlines():
                git_space_trimmed = raw_line.rstrip(" ")
                line = raw_line.strip()
                whitespace_approximated = bool(git_space_trimmed) and line != git_space_trimmed
                if not line:
                    if whitespace_approximated:
                        issues.append(
                            cls._unsupported_issue(
                                relative,
                                git_space_trimmed,
                                (
                                    "surrounding whitespace is stripped even though Git only "
                                    "ignores unescaped trailing spaces"
                                ),
                                source_line=raw_line,
                            )
                        )
                    continue
                if raw_line.startswith("#"):
                    continue
                raw_pattern = line
                additional_approximations: list[str] = []
                if whitespace_approximated:
                    additional_approximations.append(
                        "surrounding whitespace is stripped even though Git only ignores "
                        "unescaped trailing spaces"
                    )
                if "\\" in line:
                    additional_approximations.append(
                        "backslash escapes remain literal after surrounding whitespace is stripped"
                    )
                if line.startswith("#"):
                    issues.append(
                        cls._unsupported_issue(
                            relative,
                            raw_pattern,
                            "; ".join(additional_approximations),
                            source_line=raw_line,
                        )
                    )
                    continue
                include = line.startswith("!")
                if include:
                    line = line[1:]
                approximation_reported = False
                if line.startswith("/"):
                    approximation = (
                        'the leading "/" is removed and the remaining pattern may '
                        "match the same name at any depth"
                    )
                    issues.append(
                        cls._unsupported_issue(
                            relative,
                            raw_pattern,
                            "; ".join((approximation, *additional_approximations)),
                            source_line=raw_line,
                        )
                    )
                    approximation_reported = True
                directory_only = line.endswith("/")
                line = line.strip("/")
                if line:
                    if include and cls._reincludes_ignored_descendant(patterns, base, line):
                        approximation = (
                            "last matching rule wins, so this entry is re-included even "
                            "though Git keeps files below an excluded directory ignored"
                        )
                        issues.append(
                            cls._unsupported_issue(
                                relative,
                                raw_pattern,
                                "; ".join((approximation, *additional_approximations)),
                                source_line=raw_line,
                            )
                        )
                        approximation_reported = True
                    if "**" in line:
                        approximation = (
                            'Python fnmatch treats "**" as repeated "*"; stars may '
                            'match "/" and have no special Git double-star semantics'
                        )
                        issues.append(
                            cls._unsupported_issue(
                                relative,
                                raw_pattern,
                                "; ".join((approximation, *additional_approximations)),
                                source_line=raw_line,
                            )
                        )
                        approximation_reported = True
                    elif "[^" in line or "[[:" in line:
                        if "[^" in line:
                            approximation = (
                                'Python fnmatch does not treat "^" as Git range negation'
                            )
                        else:
                            approximation = (
                                "Python fnmatch does not implement Git POSIX named "
                                "character classes"
                            )
                        issues.append(
                            cls._unsupported_issue(
                                relative,
                                raw_pattern,
                                "; ".join((approximation, *additional_approximations)),
                                source_line=raw_line,
                            )
                        )
                        approximation_reported = True
                    elif "/" in line and any(character in line for character in "*?["):
                        if any(character in line for character in "*?"):
                            approximation = (
                                'Python fnmatch allows "*" and "?" in path patterns to '
                                'match "/", unlike Git wildcards'
                            )
                        else:
                            approximation = (
                                "Python fnmatch character classes in path patterns may match "
                                '"/", unlike Git FNM_PATHNAME ranges'
                            )
                        issues.append(
                            cls._unsupported_issue(
                                relative,
                                raw_pattern,
                                "; ".join((approximation, *additional_approximations)),
                                source_line=raw_line,
                            )
                        )
                        approximation_reported = True
                    patterns.append((base, line, include, directory_only))
                if additional_approximations and not approximation_reported:
                    issues.append(
                        cls._unsupported_issue(
                            relative,
                            raw_pattern,
                            "; ".join(additional_approximations),
                            source_line=raw_line,
                        )
                    )
        return cls(tuple(patterns)), issues

    @staticmethod
    def _unsupported_issue(
        source: str,
        raw_pattern: str,
        approximation: str,
        *,
        source_line: str,
    ) -> ScanIssueDraft:
        return IgnorePatternIssueDraft(
            issue_id=_new_id(),
            kind="ignore_pattern_unsupported",
            severity="warning",
            relative_path=_short(source),
            message=_short(f"Ignore pattern {raw_pattern!r} uses syntax with non-Git semantics."),
            remediation=_short(f"Approximation applied: {approximation}."),
            raw_pattern=source_line,
        )

    @classmethod
    def _reincludes_ignored_descendant(
        cls,
        patterns: list[tuple[str, str, bool, bool]],
        base: str,
        pattern: str,
    ) -> bool:
        parts = PurePosixPath(pattern).parts
        matcher = cls(tuple(patterns))
        if base != "." and matcher._matches(base, candidate_is_directory=True):
            return True
        for length in range(1, len(parts)):
            parent = PurePosixPath(*parts[:length]).as_posix()
            candidate = parent if base == "." else f"{base}/{parent}"
            if matcher._matches(candidate, candidate_is_directory=True):
                return True
        return False

    def matches(self, relative_path: str) -> bool:
        return self._matches(relative_path, candidate_is_directory=False)

    def _matches(self, relative_path: str, *, candidate_is_directory: bool) -> bool:
        ignored = False
        for base, pattern, include, directory_only in self._patterns:
            if base == ".":
                candidate = relative_path
            elif relative_path.startswith(f"{base}/"):
                candidate = relative_path.removeprefix(f"{base}/")
            else:
                continue
            parts = candidate.split("/")
            terminal_matches = not directory_only or candidate_is_directory
            if "/" in pattern:
                matched = terminal_matches and fnmatch.fnmatchcase(candidate, pattern)
                if not matched:
                    matched = any(
                        fnmatch.fnmatchcase("/".join(parts[:length]), pattern)
                        for length in range(1, len(parts))
                    )
            else:
                matchable_parts = parts if terminal_matches else parts[:-1]
                matched = any(fnmatch.fnmatchcase(part, pattern) for part in matchable_parts)
            if matched:
                ignored = not include
        return ignored


class WorkspaceScanner:
    """Build immutable scan snapshots after the caller has verified authorization."""

    def __init__(
        self,
        database: Database,
        *,
        git_executable: str | None = None,
        filesystem_probe: Callable[[Path], FilesystemProbeResult] = probe_workspace_filesystem,
    ) -> None:
        self._database = database
        resolved_git_executable = (
            resolve_git_executable() if git_executable is None else git_executable
        )
        self._git_executable = str(Path(resolved_git_executable).resolve(strict=True))
        self._filesystem_probe = filesystem_probe
        self._git_metadata = GitMetadataReader(
            git_executable=self._git_executable,
            issue_factory=_issue,
            safe_history_path=_safe_history_path,
            git_command_timeout_seconds=lambda: GIT_COMMAND_TIMEOUT_SECONDS,
            workspace_git_command=lambda binding, arguments: self._git_command(binding, arguments),
            filesystem_probe=self._filesystem_probe,
        )
        self._analysis_cache: dict[tuple[str, str, str, str, str, str], AnalysisResult] = {}

    def scan(
        self,
        *,
        workspace_path: str,
        config_revision: str,
        authorization_receipt_id: str,
        external_git_grants: tuple[ExternalGitGrant, ...] = (),
    ) -> ScanResult:
        root = Path(workspace_path).expanduser().resolve(strict=False)
        return self._run(
            root=root,
            workspace_id=None,
            mode="full",
            change_detection_mode=None,
            config_revision=config_revision,
            authorization_receipt_id=authorization_receipt_id,
            external_git_grants=external_git_grants,
        )

    def refresh(
        self,
        *,
        workspace_id: str,
        config_revision: str,
        change_detection_mode: Literal["fast", "verify_content"],
        authorization_receipt_id: str,
        external_git_grants: tuple[ExternalGitGrant, ...] = (),
    ) -> ScanResult:
        with self._database.read_connection() as connection:
            row = connection.execute(
                "SELECT canonical_root FROM workspaces WHERE workspace_id = ?", (workspace_id,)
            ).fetchone()
        if row is None:
            raise InvalidInputError("workspace_id is not registered")
        return self._run(
            root=Path(str(row["canonical_root"])),
            workspace_id=workspace_id,
            mode="refresh",
            change_detection_mode=change_detection_mode,
            config_revision=config_revision,
            authorization_receipt_id=authorization_receipt_id,
            external_git_grants=external_git_grants,
        )

    def overview(
        self,
        *,
        workspace_path: str,
        scan_run_id: str | None = None,
    ) -> dict[str, object]:
        """Return one reusable terminal scan without reading project sources."""
        root = Path(workspace_path).expanduser().resolve(strict=False)
        with self._database.read_connection() as connection:
            parameters: list[object] = [str(root)]
            requested_filter = ""
            if scan_run_id is not None:
                if not scan_run_id.strip() or len(scan_run_id) > 200:
                    raise InvalidInputError("scan_run_id must be a bounded non-empty value")
                requested_filter = " AND sr.scan_run_id = ?"
                parameters.append(scan_run_id)
            row = connection.execute(
                f"""
                SELECT sr.scan_run_id, sr.workspace_id, sr.status, sr.mode,
                       sr.change_detection_mode, sr.config_revision, sr.started_at,
                       sr.finished_at, sro.coverage_json
                FROM scan_runs AS sr
                JOIN workspaces AS w ON w.workspace_id = sr.workspace_id
                LEFT JOIN scan_run_overviews AS sro ON sro.scan_run_id = sr.scan_run_id
                WHERE w.canonical_root = ?
                  AND sr.status IN ('completed', 'partial', 'failed')
                  AND (sr.status IN ('completed', 'partial') OR sro.scan_run_id IS NOT NULL)
                  {requested_filter}
                ORDER BY sr.started_at DESC, sr.scan_run_id DESC
                LIMIT 1
                """,
                tuple(parameters),
            ).fetchone()
            if row is None:
                return {
                    "status": "ok",
                    "scan_overview": {
                        "contract_version": SCAN_OVERVIEW_CONTRACT_VERSION,
                        "found": False,
                        "workspace_path": str(root),
                        "scan_run": None,
                        "coverage": None,
                        "issues": [],
                        "issue_groups": [],
                        "limits": {
                            "issue_limit": MAX_SCAN_OVERVIEW_ISSUES,
                            "available_issues": 0,
                            "issues_truncated": False,
                            "group_limit": MAX_SCAN_OVERVIEW_GROUPS,
                            "group_count": 0,
                            "groups_truncated": False,
                            "error_count": 0,
                            "warning_count": 0,
                            "info_count": 0,
                        },
                    },
                }
            selected_scan_run_id = str(row["scan_run_id"])
            coverage = self._overview_coverage(connection, row, selected_scan_run_id)
            count_rows = connection.execute(
                """
                SELECT severity, COUNT(*) AS issue_count
                FROM scan_issues
                WHERE scan_run_id = ?
                GROUP BY severity
                """,
                (selected_scan_run_id,),
            ).fetchall()
            counts_by_severity = {
                str(item["severity"]): int(item["issue_count"]) for item in count_rows
            }
            error_count = counts_by_severity.get("error", 0)
            warning_count = counts_by_severity.get("warning", 0)
            info_count = counts_by_severity.get("info", 0)
            issue_count = error_count + warning_count + info_count
            error_quota, warning_quota, info_quota = _overview_issue_quotas(
                issue_limit=MAX_SCAN_OVERVIEW_ISSUES,
                error_count=error_count,
                warning_count=warning_count,
                info_count=info_count,
            )
            group_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) AS group_count
                    FROM (
                        SELECT 1
                        FROM scan_issues
                        WHERE scan_run_id = ?
                        GROUP BY severity, kind, remediation
                    )
                    """,
                    (selected_scan_run_id,),
                ).fetchone()["group_count"]
            )

            group_rows = connection.execute(
                """
                SELECT severity, kind, remediation, COUNT(*) AS issue_count
                FROM scan_issues
                WHERE scan_run_id = ?
                GROUP BY severity, kind, remediation
                ORDER BY CASE severity
                    WHEN 'error' THEN 0
                    WHEN 'warning' THEN 1
                    ELSE 2
                END, kind, remediation
                LIMIT ?
                """,
                (selected_scan_run_id, MAX_SCAN_OVERVIEW_GROUPS),
            ).fetchall()
            sample_rows = connection.execute(
                """
                WITH selected_groups AS (
                    SELECT severity, kind, remediation
                    FROM scan_issues
                    WHERE scan_run_id = ?
                    GROUP BY severity, kind, remediation
                    ORDER BY CASE severity
                        WHEN 'error' THEN 0
                        WHEN 'warning' THEN 1
                        ELSE 2
                    END, kind, remediation
                    LIMIT ?
                ), ranked_samples AS (
                    SELECT issues.issue_id, issues.project_id, issues.artifact_id, issues.kind,
                           issues.severity, issues.relative_path, issues.message,
                           issues.remediation,
                           ROW_NUMBER() OVER (
                               PARTITION BY issues.severity, issues.kind, issues.remediation
                               ORDER BY CASE WHEN issues.relative_path IS NULL THEN 1 ELSE 0 END,
                                        issues.relative_path, issues.message,
                                        COALESCE(issues.project_id, ''),
                                        COALESCE(issues.artifact_id, ''), issues.issue_id
                           ) AS sample_rank
                    FROM scan_issues AS issues
                    JOIN selected_groups AS groups
                      ON groups.severity = issues.severity
                     AND groups.kind = issues.kind
                     AND groups.remediation = issues.remediation
                    WHERE issues.scan_run_id = ?
                )
                SELECT issue_id, project_id, artifact_id, kind, severity,
                       relative_path, message, remediation
                FROM ranked_samples
                WHERE sample_rank <= ?
                ORDER BY CASE severity
                    WHEN 'error' THEN 0
                    WHEN 'warning' THEN 1
                    ELSE 2
                END, kind, remediation, sample_rank
                """,
                (
                    selected_scan_run_id,
                    MAX_SCAN_OVERVIEW_GROUPS,
                    selected_scan_run_id,
                    MAX_SCAN_OVERVIEW_SAMPLES_PER_GROUP,
                ),
            ).fetchall()
            samples_by_group: dict[tuple[str, str, str], list[sqlite3.Row]] = {}
            for sample in sample_rows:
                key = (str(sample["severity"]), str(sample["kind"]), str(sample["remediation"]))
                samples_by_group.setdefault(key, []).append(sample)

            issue_groups: list[dict[str, object]] = []
            for group in group_rows:
                key = (str(group["severity"]), str(group["kind"]), str(group["remediation"]))
                samples = [
                    {
                        "issue_id": str(sample["issue_id"]),
                        "project_id": sample["project_id"],
                        "artifact_id": sample["artifact_id"],
                        "relative_path": sample["relative_path"],
                        "message": str(sample["message"]),
                    }
                    for sample in samples_by_group.get(key, ())
                ]
                count = int(group["issue_count"])
                issue_groups.append(
                    {
                        "severity": key[0],
                        "kind": key[1],
                        "remediation": key[2],
                        "count": count,
                        "samples": samples,
                        "omitted_count": max(0, count - len(samples)),
                    }
                )

            issue_rows = connection.execute(
                """
                WITH ranked_issues AS (
                    SELECT issue_id, project_id, artifact_id, kind, severity,
                           relative_path, message, remediation,
                           ROW_NUMBER() OVER (
                               PARTITION BY severity
                               ORDER BY CASE WHEN relative_path IS NULL THEN 1 ELSE 0 END,
                                        relative_path, kind, remediation, message,
                                        COALESCE(project_id, ''), COALESCE(artifact_id, ''),
                                        issue_id
                           ) AS severity_rank
                    FROM scan_issues
                    WHERE scan_run_id = ?
                )
                SELECT issue_id, project_id, artifact_id, kind, severity,
                       relative_path, message, remediation
                FROM ranked_issues
                WHERE (severity = 'error' AND severity_rank <= ?)
                   OR (severity = 'warning' AND severity_rank <= ?)
                   OR (severity = 'info' AND severity_rank <= ?)
                ORDER BY CASE severity
                    WHEN 'error' THEN 0
                    WHEN 'warning' THEN 1
                    ELSE 2
                END, CASE WHEN relative_path IS NULL THEN 1 ELSE 0 END,
                     relative_path, kind, remediation, message,
                     COALESCE(project_id, ''), COALESCE(artifact_id, ''), issue_id
                """,
                (selected_scan_run_id, error_quota, warning_quota, info_quota),
            ).fetchall()
        return {
            "status": "ok",
            "scan_overview": {
                "contract_version": SCAN_OVERVIEW_CONTRACT_VERSION,
                "found": True,
                "workspace_path": str(root),
                "scan_run": {
                    "scan_run_id": selected_scan_run_id,
                    "workspace_id": str(row["workspace_id"]),
                    "status": str(row["status"]),
                    "mode": str(row["mode"]),
                    "change_detection_mode": row["change_detection_mode"],
                    "config_revision": str(row["config_revision"]),
                    "started_at": str(row["started_at"]),
                    "finished_at": row["finished_at"],
                },
                "coverage": coverage,
                "issues": [
                    {
                        "issue_id": str(issue["issue_id"]),
                        "project_id": issue["project_id"],
                        "artifact_id": issue["artifact_id"],
                        "kind": str(issue["kind"]),
                        "severity": str(issue["severity"]),
                        "relative_path": issue["relative_path"],
                        "message": str(issue["message"]),
                        "remediation": str(issue["remediation"]),
                    }
                    for issue in issue_rows
                ],
                "issue_groups": issue_groups,
                "limits": {
                    "issue_limit": MAX_SCAN_OVERVIEW_ISSUES,
                    "available_issues": issue_count,
                    "issues_truncated": issue_count > len(issue_rows),
                    "group_limit": MAX_SCAN_OVERVIEW_GROUPS,
                    "group_count": group_count,
                    "groups_truncated": group_count > len(issue_groups),
                    "error_count": error_count,
                    "warning_count": warning_count,
                    "info_count": info_count,
                },
            },
        }

    def _overview_coverage(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        scan_run_id: str,
    ) -> dict[str, object]:
        stored = row["coverage_json"]
        if stored is not None:
            try:
                value = json.loads(str(stored))
            except (json.JSONDecodeError, RecursionError) as exc:
                raise InvalidInputError("stored scan overview is not valid JSON") from exc
            if not isinstance(value, dict):
                raise InvalidInputError("stored scan overview is not a JSON object")
            return cast(dict[str, object], value)
        dispositions = Counter(
            {
                str(disposition["snapshot_disposition"]): int(disposition["count"])
                for disposition in connection.execute(
                    """
                    SELECT snapshot_disposition, COUNT(*) AS count
                    FROM scan_run_projects WHERE scan_run_id = ?
                    GROUP BY snapshot_disposition
                    """,
                    (scan_run_id,),
                ).fetchall()
            }
        )
        coverage = self._coverage(scan_run_id, dispositions, Counter(), [], {})
        coverage["overview_provenance"] = "reconstructed_without_exclusion_counts"
        coverage["excluded_by_category_available"] = False
        return coverage

    def _run(
        self,
        *,
        root: Path,
        workspace_id: str | None,
        mode: Literal["full", "refresh"],
        change_detection_mode: Literal["fast", "verify_content"] | None,
        config_revision: str,
        authorization_receipt_id: str,
        external_git_grants: tuple[ExternalGitGrant, ...],
    ) -> ScanResult:
        if sys.platform == "win32":
            from goodjob.platform.fs_windows import bind_authorized_root

            with bind_authorized_root(root):
                return self._run_authorized(
                    root=root,
                    workspace_id=workspace_id,
                    mode=mode,
                    change_detection_mode=change_detection_mode,
                    config_revision=config_revision,
                    authorization_receipt_id=authorization_receipt_id,
                    external_git_grants=external_git_grants,
                )
        return self._run_authorized(
            root=root,
            workspace_id=workspace_id,
            mode=mode,
            change_detection_mode=change_detection_mode,
            config_revision=config_revision,
            authorization_receipt_id=authorization_receipt_id,
            external_git_grants=external_git_grants,
        )

    def _run_authorized(
        self,
        *,
        root: Path,
        workspace_id: str | None,
        mode: Literal["full", "refresh"],
        change_detection_mode: Literal["fast", "verify_content"] | None,
        config_revision: str,
        authorization_receipt_id: str,
        external_git_grants: tuple[ExternalGitGrant, ...],
    ) -> ScanResult:
        if not config_revision.strip():
            raise InvalidInputError("config_revision must not be empty")
        root_issue = self._workspace_root_issue(root)
        if root_issue is not None:
            raise InvalidInputError(root_issue.message)
        scan_started_at = _now()
        workspace_id, scan_run_id = self._start_run(
            root=root,
            workspace_id=workspace_id,
            mode=mode,
            change_detection_mode=change_detection_mode,
            config_revision=config_revision,
            authorization_receipt_id=authorization_receipt_id,
            started_at=scan_started_at,
        )
        exclusion_config = load_project_exclusions(self._database.paths.config_file)
        try:
            return self._run_started(
                root=root,
                workspace_id=workspace_id,
                scan_run_id=scan_run_id,
                mode=mode,
                change_detection_mode=change_detection_mode,
                config_revision=config_revision,
                external_git_grants=external_git_grants,
                scan_started_at=scan_started_at,
                exclusion_config=exclusion_config,
            )
        except BaseException:
            with suppress(Exception):
                self._finish_run(scan_run_id, "failed", None)
            raise

    def _run_started(
        self,
        *,
        root: Path,
        workspace_id: str,
        scan_run_id: str,
        mode: Literal["full", "refresh"],
        change_detection_mode: Literal["fast", "verify_content"] | None,
        config_revision: str,
        external_git_grants: tuple[ExternalGitGrant, ...],
        scan_started_at: str,
        exclusion_config: ProjectExclusionConfig,
    ) -> ScanResult:
        filesystem_issue = self._workspace_filesystem_issue(root)
        if filesystem_issue is not None:
            self._persist_issues(scan_run_id, None, (filesystem_issue,))
            coverage = self._coverage(
                scan_run_id,
                Counter(),
                Counter(),
                [],
                {},
            )
            self._finish_run(scan_run_id, "failed", coverage)
            return ScanResult(
                scan_run_id=scan_run_id,
                workspace_id=workspace_id,
                status="failed",
                mode=mode,
                change_detection_mode=change_detection_mode,
                coverage=coverage,
                issues=(filesystem_issue,),
            )
        plans, discovery_issues = self._discover(root, external_git_grants, scan_started_at)
        exclusion_matches, unmatched_issues = self._match_project_exclusions(
            root, plans, exclusion_config.rules
        )
        config_issues = [
            _issue(issue.kind, "warning", issue.message, issue.remediation)
            for issue in exclusion_config.issues
        ]
        run_issues = [*config_issues, *discovery_issues, *unmatched_issues]
        self._persist_issues(scan_run_id, None, tuple(run_issues))
        all_issues = list(run_issues)
        dispositions: Counter[str] = Counter()
        excluded: Counter[str] = Counter()
        project_exclusions: list[dict[str, str]] = []
        discovered_ids: set[str] = set()

        for plan in plans:
            project = self._upsert_project(workspace_id, scan_run_id, root, plan)
            discovered_ids.add(project.project_id)
            matching_rules = exclusion_matches.get(plan.identity_key, ())
            if matching_rules:
                chosen_rule = matching_rules[0]
                self._record_project_exclusion(scan_run_id, project.project_id)
                dispositions["excluded"] += 1
                project_exclusions.append(
                    {
                        "project_display_name": project.display_name,
                        "match": chosen_rule.match,
                        "value": chosen_rule.value,
                        "reason": chosen_rule.reason,
                    }
                )
                continue
            try:
                project_data = self._read_project(
                    plan=plan,
                    config_revision=config_revision,
                    change_detection_mode=change_detection_mode,
                    nested_project_roots={
                        worktree.root
                        for candidate in plans
                        for worktree in candidate.worktrees
                        if worktree.root != plan.worktrees[0].root
                    },
                )
                project_issues, project_excluded = self._persist_project(
                    scan_run_id=scan_run_id,
                    project=project,
                    data=project_data,
                    config_revision=config_revision,
                    workspace_root=root,
                )
                all_issues.extend(project_issues)
                excluded.update(project_excluded)
                dispositions["fresh"] += 1
            except (OSError, sqlite3.Error, ValueError, UnicodeError):
                issue = _issue(
                    "project_index_failed",
                    "error",
                    "This project could not be indexed during the current run.",
                    "Fix local access or repository metadata, then run refresh.",
                    _relative_path(plan.worktrees[0].root, root),
                )
                disposition = self._record_project_failure(scan_run_id, project.project_id, issue)
                dispositions[disposition] += 1
                all_issues.append(issue)

        carried_issues, carried_dispositions = self._carry_forward_missing_projects(
            scan_run_id=scan_run_id,
            workspace_id=workspace_id,
            discovered_project_ids=discovered_ids,
        )
        all_issues.extend(carried_issues)
        dispositions.update(carried_dispositions)
        status = self._final_status(scan_run_id, dispositions, all_issues)
        coverage = self._coverage(
            scan_run_id,
            dispositions,
            excluded,
            project_exclusions,
            {
                issue.issue_id: issue.raw_pattern
                for issue in all_issues
                if isinstance(issue, IgnorePatternIssueDraft)
            },
        )
        self._finish_run(scan_run_id, status, coverage)
        return ScanResult(
            scan_run_id=scan_run_id,
            workspace_id=workspace_id,
            status=status,
            mode=mode,
            change_detection_mode=change_detection_mode,
            coverage=coverage,
            issues=tuple(all_issues),
        )

    def _workspace_filesystem_issue(self, root: Path) -> ScanIssueDraft | None:
        try:
            result = self._filesystem_probe(root)
        except (OSError, ValueError):
            result = FilesystemProbeResult(
                status="error",
                filesystem_type="unknown",
                flags=None,
                message="The workspace filesystem capability could not be probed safely.",
                remediation="Choose a readable local workspace whose filesystem type can be "
                "verified, then run preflight again.",
            )
        if result.status == "supported":
            return None
        return _issue(
            "workspace_filesystem_unsupported",
            "error",
            result.message,
            result.remediation,
            ".",
        )

    @staticmethod
    def _match_project_exclusions(
        root: Path,
        plans: list[ProjectPlan],
        rules: tuple[ExcludedProjectRule, ...],
    ) -> tuple[dict[str, tuple[ExcludedProjectRule, ...]], list[ScanIssueDraft]]:
        matches: dict[str, tuple[ExcludedProjectRule, ...]] = {}
        matched_rule_indexes: set[int] = set()
        for plan in plans:
            relative_location = _relative_path(plan.worktrees[0].root, root)
            project_matches = tuple(
                rule
                for rule in rules
                if rule.matches(
                    identity_key=plan.identity_key,
                    relative_location=relative_location,
                )
            )
            if project_matches:
                matches[plan.identity_key] = project_matches
                matched_rule_indexes.update(rule.index for rule in project_matches)
        unmatched = [
            _issue(
                "project_exclusion_rule_unmatched",
                "warning",
                (
                    f"Project exclusion rule {rule.index} matched no discovered project "
                    f"by {rule.match}: {_short(rule.value, 180)}."
                ),
                "Correct the exact project identity or workspace-relative location.",
            )
            for rule in rules
            if rule.index not in matched_rule_indexes
        ]
        return matches, unmatched

    @staticmethod
    def _workspace_root_issue(root: Path) -> ScanIssueDraft | None:
        try:
            root_fd = _open_directory(root)
            try:
                if isinstance(root_fd, WindowsDirectory):
                    return None
                root_stat = os.fstat(root_fd)
            finally:
                _close_directory(root_fd)
        except OSError:
            return _issue(
                "workspace_unavailable",
                "error",
                "The authorized workspace is not a readable directory.",
                "Choose an existing local directory and run scan again.",
            )
        if not stat.S_ISDIR(root_stat.st_mode):
            return _issue(
                "workspace_root_changed",
                "error",
                "The workspace root changed after authorization and was not scanned.",
                "Reauthorize the current canonical workspace path and run scan again.",
            )
        return None

    def _start_run(
        self,
        *,
        root: Path,
        workspace_id: str | None,
        mode: str,
        change_detection_mode: str | None,
        config_revision: str,
        authorization_receipt_id: str,
        started_at: str,
    ) -> tuple[str, str]:
        now = started_at
        with self._database.write_transaction() as connection:
            self._recover_interrupted_runs(connection, now)
            if workspace_id is None:
                row = connection.execute(
                    "SELECT workspace_id FROM workspaces WHERE canonical_root = ?", (str(root),)
                ).fetchone()
                workspace_id = str(row["workspace_id"]) if row else _new_id()
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO workspaces(
                            workspace_id, canonical_root, display_name,
                            registered_at, config_revision
                        )
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (workspace_id, str(root), root.name or str(root), now, config_revision),
                    )
                else:
                    connection.execute(
                        "UPDATE workspaces SET config_revision = ? WHERE workspace_id = ?",
                        (config_revision, workspace_id),
                    )
            else:
                connection.execute(
                    "UPDATE workspaces SET config_revision = ? WHERE workspace_id = ?",
                    (config_revision, workspace_id),
                )
            scan_run_id = _new_id()
            connection.execute(
                """
                INSERT INTO scan_runs(
                    scan_run_id, workspace_id, authorization_receipt_id, owner_process_identity,
                    mode, change_detection_mode, config_revision, started_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running')
                """,
                (
                    scan_run_id,
                    workspace_id,
                    authorization_receipt_id,
                    process_identity(),
                    mode,
                    change_detection_mode,
                    config_revision,
                    now,
                ),
            )
        return workspace_id, scan_run_id

    @staticmethod
    def _recover_interrupted_runs(connection: sqlite3.Connection, now: str) -> None:
        rows = connection.execute(
            """
            SELECT scan_run_id, owner_process_identity
            FROM scan_runs
            WHERE status = 'running'
            """
        ).fetchall()
        for row in rows:
            if not owner_process_stopped(str(row["owner_process_identity"])):
                continue
            connection.execute(
                """
                UPDATE scan_runs
                SET status = 'interrupted', finished_at = ?
                WHERE scan_run_id = ? AND status = 'running'
                """,
                (now, str(row["scan_run_id"])),
            )

    def _discover(
        self,
        root: Path,
        external_git_grants: tuple[ExternalGitGrant, ...],
        scan_started_at: str,
    ) -> tuple[list[ProjectPlan], list[ScanIssueDraft]]:
        directories, issues = self._walk_directories(root)
        manifest_roots = tuple(
            directory
            for directory in directories
            if self._has_repo_manifest_marker(root, directory)
        )
        git_worktrees: list[WorktreePlan] = []
        blocked_roots: set[Path] = set()
        topology_violations: dict[Path, tuple[str, ...]] = {}
        grants_by_pointer = {grant.git_pointer_path: grant for grant in external_git_grants}
        for directory in directories:
            marker = directory / ".git"
            marker_relative = _child_relative(_relative_to_root(directory, root), ".git")
            try:
                marker_stat = _safe_lstat(root, marker_relative)
            except FileNotFoundError:
                continue
            except OSError:
                issues.append(
                    _issue(
                        "git_marker_unreadable",
                        "warning",
                        "A Git marker could not be inspected.",
                        "Fix local permissions and run refresh.",
                        _relative_path(directory, root),
                    )
                )
                blocked_roots.add(directory)
                continue
            blocked_roots.add(directory)
            manifest_scoped = any(
                _is_within(directory, manifest_root) for manifest_root in manifest_roots
            )
            topology_entries = self._git_metadata_topology(
                root,
                marker_relative,
                marker_stat,
                manifest_scoped=manifest_scoped,
            )
            if topology_entries:
                topology_violations[directory] = topology_entries
                continue
            if stat.S_ISLNK(marker_stat.st_mode):
                issues.append(
                    _issue(
                        "untrusted_git_pointer",
                        "warning",
                        "A symbolic-link .git marker was not followed.",
                        (
                            "Use a normal repository root or grant a future explicit Git relation "
                            "probe."
                        ),
                        _relative_path(directory, root),
                    )
                )
                continue
            if stat.S_ISREG(marker_stat.st_mode):
                pointer_target = self._git_pointer_target_at(root, marker_relative)
                if pointer_target is None:
                    issues.append(
                        _issue(
                            "untrusted_git_pointer",
                            "warning",
                            "A linked-worktree .git pointer could not be parsed safely.",
                            "Repair the local Git pointer and run refresh.",
                            _relative_path(directory, root),
                        )
                    )
                    continue
                relation_state = self._linked_worktree_relation_state(root, marker, pointer_target)
                if relation_state == "trusted":
                    git_state, git_issue = self._git_state(
                        directory, root, scan_started_at=scan_started_at
                    )
                    if git_issue is not None:
                        issues.append(git_issue)
                        continue
                    assert git_state is not None
                    git_worktrees.append(WorktreePlan(directory, git_state))
                    continue
                if relation_state == "invalid":
                    issues.append(
                        _issue(
                            "untrusted_git_pointer",
                            "warning",
                            "A root-internal linked-worktree relation is malformed or changed.",
                            "Repair the local Git pointer relation and run refresh.",
                            _relative_path(directory, root),
                        )
                    )
                    continue
                grant = grants_by_pointer.get(marker)
                if grant is None:
                    issues.append(
                        _issue(
                            "external_git_authorization_required",
                            "warning",
                            (
                                "A linked-worktree .git pointer requires explicit two-stage "
                                "external Git authorization for its untrusted metadata candidate: "
                                f"{_short(str(pointer_target), 220)}."
                            ),
                            "Authorize the Git relation probe before indexing this worktree.",
                            _relative_path(directory, root),
                        )
                    )
                    continue
                git_state, git_issue = self._external_git_state(directory, root, marker, grant)
                if git_issue is not None:
                    issues.append(git_issue)
                    continue
                assert git_state is not None
                git_worktrees.append(WorktreePlan(directory, git_state))
                continue
            if not stat.S_ISDIR(marker_stat.st_mode):
                issues.append(
                    _issue(
                        "broken_repository",
                        "warning",
                        "The .git marker is neither a directory nor a supported pointer file.",
                        "Repair the local repository metadata and run refresh.",
                        _relative_path(directory, root),
                    )
                )
                continue
            directory_relation = self._git_directory_relation_state(root, marker_relative)
            if directory_relation == "invalid":
                issues.append(
                    _issue(
                        "untrusted_git_pointer",
                        "warning",
                        "A .git directory has a malformed or changing commondir relation.",
                        "Repair the local Git metadata and run refresh.",
                        _relative_path(directory, root),
                    )
                )
                continue
            if directory_relation == "external":
                grant = grants_by_pointer.get(marker)
                if grant is None:
                    common_candidate = self._relation_target_at(root, marker_relative, "commondir")
                    issues.append(
                        _issue(
                            "external_git_authorization_required",
                            "warning",
                            (
                                "A .git directory requires explicit two-stage external Git "
                                "authorization for its commondir candidate: "
                                f"{_short(str(common_candidate), 220)}."
                            ),
                            "Inspect and authorize the Git relation before indexing this worktree.",
                            _relative_path(directory, root),
                        )
                    )
                    continue
                git_state, git_issue = self._external_git_state(directory, root, marker, grant)
                if git_issue is not None:
                    issues.append(git_issue)
                    continue
                assert git_state is not None
                git_worktrees.append(WorktreePlan(directory, git_state))
                continue
            git_state, git_issue = self._git_state(directory, root, scan_started_at=scan_started_at)
            if git_issue is not None:
                issues.append(git_issue)
                continue
            assert git_state is not None
            git_worktrees.append(WorktreePlan(directory, git_state))

        if topology_violations:
            sample_entries = sorted(
                {
                    f"{_relative_path(directory, root)}/{entry}"
                    for directory, entries in topology_violations.items()
                    for entry in entries
                }
            )[:8]
            sample_text = ", ".join(sample_entries) if sample_entries else "metadata links"
            issue_root = (
                min(manifest_roots, key=lambda path: str(path))
                if manifest_roots
                else min(topology_violations, key=lambda path: str(path))
            )
            issues.append(
                _issue(
                    "unsupported_repository_layout",
                    "warning",
                    (
                        "A repo/manifest Git layout uses metadata links outside the authorized "
                        f"manifest root ({len(topology_violations)} repository roots; samples: "
                        f"{sample_text})."
                    ),
                    (
                        "Select and authorize the manifest root that contains all required Git "
                        "metadata; GoodJob will not expand the current root automatically."
                    ),
                    _relative_path(issue_root, root),
                )
            )

        grouped: dict[str, list[WorktreePlan]] = {}
        for worktree in git_worktrees:
            assert worktree.git_state is not None
            grouped.setdefault(str(worktree.git_state.common_dir), []).append(worktree)
        projects: list[ProjectPlan] = [
            ProjectPlan(
                identity_kind="git_common_dir",
                identity_key=identity_key,
                display_name=sorted(worktrees, key=lambda item: str(item.root))[0].root.name,
                worktrees=tuple(sorted(worktrees, key=lambda item: str(item.root))),
            )
            for identity_key, worktrees in sorted(grouped.items())
        ]

        git_roots = {worktree.root for worktree in git_worktrees} | blocked_roots
        for directory in directories:
            if any(_is_within(directory, git_root) for git_root in git_roots):
                continue
            if not self._non_git_manifest(root, _relative_to_root(directory, root)):
                continue
            projects.append(
                ProjectPlan(
                    identity_kind="non_git_root",
                    identity_key=str(directory),
                    display_name=directory.name,
                    worktrees=(WorktreePlan(directory, None),),
                )
            )
        projects.sort(key=lambda project: (project.identity_kind, project.identity_key))
        return projects, issues

    _linked_worktree_relation_state = staticmethod(
        GitMetadataReader._linked_worktree_relation_state
    )

    _git_directory_relation_state = staticmethod(GitMetadataReader._git_directory_relation_state)

    _bind_internal_git = staticmethod(GitMetadataReader._bind_internal_git)

    @staticmethod
    def _has_repo_manifest_marker(workspace_root: Path, directory: Path) -> bool:
        try:
            relative_directory = _relative_to_root(directory, workspace_root)
            directory_fd = _open_directory(workspace_root, relative_directory)
        except OSError:
            return False
        try:
            try:
                entries = _bound_directory_entries(directory_fd)
            except OSError:
                return False
            for entry in entries:
                if entry.name.casefold() != ".repo":
                    continue
                try:
                    marker_stat = entry.stat(follow_symlinks=False)
                except OSError:
                    return False
                return stat.S_ISDIR(marker_stat.st_mode)
            return False
        finally:
            _close_directory(directory_fd)

    def _git_metadata_topology(
        self,
        workspace_root: Path,
        marker_relative: str,
        marker_stat: os.stat_result,
        *,
        manifest_scoped: bool,
    ) -> tuple[str, ...]:
        """Inspect top-level Git metadata links before any Git subprocess starts."""
        if stat.S_ISREG(marker_stat.st_mode):
            git_dir = self._git_pointer_target_at(workspace_root, marker_relative)
            if git_dir is None:
                return ()
            if not _is_within(git_dir, workspace_root):
                is_repo_metadata = any(part.casefold() == ".repo" for part in git_dir.parts)
                return (".git/gitdir",) if manifest_scoped or is_repo_metadata else ()
        elif stat.S_ISDIR(marker_stat.st_mode):
            git_dir = workspace_root / PurePosixPath(marker_relative)
        else:
            return ()

        try:
            git_dir_relative = _relative_to_root(git_dir, workspace_root)
            git_dir_fd = _open_directory(workspace_root, git_dir_relative)
        except OSError:
            return ()
        entries: list[_BoundDirectoryEntry]
        try:
            try:
                entries = sorted(_bound_directory_entries(git_dir_fd), key=lambda entry: entry.name)
            except OSError:
                return ()
            violations: list[str] = []
            for entry in entries:
                try:
                    entry_stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if not stat.S_ISLNK(entry_stat.st_mode):
                    continue
                entry_relative = _child_relative(marker_relative, entry.name)
                try:
                    raw_target = _bound_readlink(git_dir_fd, entry.name)
                except OSError:
                    violations.append(entry_relative)
                    continue
                if _link_chain_may_escape(workspace_root, git_dir, raw_target):
                    violations.append(entry_relative)

            if manifest_scoped:
                for relation_name in ("commondir", "gitdir"):
                    relation_relative = _child_relative(git_dir_relative, relation_name)
                    try:
                        relation_stat = _safe_lstat(workspace_root, relation_relative)
                    except FileNotFoundError:
                        continue
                    except OSError:
                        continue
                    if not stat.S_ISREG(relation_stat.st_mode):
                        continue
                    relation_target = self._relation_target_at(
                        workspace_root,
                        git_dir_relative,
                        relation_name,
                    )
                    if relation_target is not None and not _is_within(
                        relation_target, workspace_root
                    ):
                        violations.append(_child_relative(marker_relative, relation_name))
            return tuple(sorted(set(violations)))
        finally:
            _close_directory(git_dir_fd)

    def _walk_directories(self, root: Path) -> tuple[list[Path], list[ScanIssueDraft]]:
        directories: list[Path] = []
        issues: list[ScanIssueDraft] = []
        stack = ["."]
        seen: set[str] = set()
        while stack:
            relative_directory = stack.pop()
            if relative_directory in seen:
                continue
            seen.add(relative_directory)
            directory = root if relative_directory == "." else root / relative_directory
            directories.append(directory)
            try:
                directory_fd = _open_directory(root, relative_directory)
            except OSError:
                issues.append(
                    _issue(
                        "directory_unreadable",
                        "warning",
                        "A directory could not be listed during discovery.",
                        "Fix local permissions and run refresh.",
                        _relative_path(directory, root),
                    )
                )
                continue
            try:
                try:
                    entries = sorted(
                        _bound_directory_entries(directory_fd), key=lambda entry: entry.name
                    )
                except OSError:
                    issues.append(
                        _issue(
                            "directory_unreadable",
                            "warning",
                            "A directory could not be listed during discovery.",
                            "Fix local permissions and run refresh.",
                            _relative_path(directory, root),
                        )
                    )
                    continue
                for entry in reversed(entries):
                    try:
                        entry_stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    child_relative = _child_relative(relative_directory, entry.name)
                    if entry.name.lower() == ".repo":
                        issues.append(
                            _issue(
                                "repository_metadata_excluded",
                                "info",
                                "Repo/manifest metadata was excluded from source discovery.",
                                (
                                    "Authorize the manifest root when its Git metadata is "
                                    "required; "
                                    "GoodJob does not index .repo contents as source."
                                ),
                                child_relative,
                            )
                        )
                        continue
                    if stat.S_ISLNK(entry_stat.st_mode):
                        try:
                            target = _bound_readlink(directory_fd, entry.name)
                        except OSError:
                            target = ".."
                        kind = (
                            "symlink_outside_authorized_root"
                            if _symlink_may_escape(relative_directory, target)
                            else "symlink_skipped"
                        )
                        issues.append(
                            _issue(
                                kind,
                                "warning" if kind == "symlink_outside_authorized_root" else "info",
                                "A symbolic link was not followed during workspace discovery.",
                                "Use a regular file or directory when it must be indexed.",
                                child_relative,
                            )
                        )
                        continue
                    if (
                        stat.S_ISDIR(entry_stat.st_mode)
                        and entry.name.lower() not in HARD_EXCLUDED_DIRECTORIES
                        and not _is_packaged_app_root(entry.name)
                    ):
                        stack.append(child_relative)
            finally:
                _close_directory(directory_fd)
        return directories, issues

    def _external_git_state(
        self,
        root: Path,
        workspace_root: Path,
        marker: Path,
        grant: ExternalGitGrant,
    ) -> tuple[GitState | None, ScanIssueDraft | None]:
        return self._git_metadata._external_git_state(root, workspace_root, marker, grant)

    _external_head_state = staticmethod(GitMetadataReader._external_head_state)

    _safe_git_reference = staticmethod(GitMetadataReader._safe_git_reference)

    _external_ref_commit = staticmethod(GitMetadataReader._external_ref_commit)

    _external_directory_identity_matches = staticmethod(
        GitMetadataReader._external_directory_identity_matches
    )

    _git_pointer_target = staticmethod(GitMetadataReader._git_pointer_target)

    _git_pointer_target_at = staticmethod(GitMetadataReader._git_pointer_target_at)

    _relation_target_from_fd = staticmethod(GitMetadataReader._relation_target_from_fd)

    _relation_target_at = staticmethod(GitMetadataReader._relation_target_at)

    def _git_state(
        self,
        root: Path,
        workspace_root: Path,
        *,
        scan_started_at: str,
    ) -> tuple[GitState | None, ScanIssueDraft | None]:
        return self._git_metadata._git_state(root, workspace_root, scan_started_at=scan_started_at)

    def _recent_git_history(
        self,
        binding: InternalGitBinding,
        head: str | None,
        branch: str | None,
        scan_started_at: str,
    ) -> tuple[str, tuple[GitHistoryEntry, ...], tuple[ScanIssueDraft, ...]]:
        return self._git_metadata._recent_git_history(binding, head, branch, scan_started_at)

    def _default_history_commit(self, binding: InternalGitBinding) -> tuple[str | None, str]:
        return self._git_metadata._default_history_commit(binding)

    def _verified_git_commit(self, binding: InternalGitBinding, reference: str) -> str | None:
        return self._git_metadata._verified_git_commit(binding, reference)

    def _read_recent_history(
        self,
        binding: InternalGitBinding,
        revisions: tuple[str, ...],
        head: str,
        scan_started_at: str,
    ) -> tuple[tuple[GitHistoryEntry, ...], bool]:
        return self._git_metadata._read_recent_history(binding, revisions, head, scan_started_at)

    _parse_history_metadata = staticmethod(GitMetadataReader._parse_history_metadata)

    def _history_paths(
        self, binding: InternalGitBinding, commit: str, *pathspecs: str
    ) -> tuple[tuple[str, ...], bool]:
        return self._git_metadata._history_paths(binding, commit, *pathspecs)

    def _git_command(
        self,
        binding: InternalGitBinding,
        arguments: tuple[str, ...],
    ) -> list[str]:
        return self._git_metadata._git_command(binding, arguments)

    _open_bound_git_directory = staticmethod(GitMetadataReader._open_bound_git_directory)

    def _git_bounded_bytes(
        self, binding: InternalGitBinding, *arguments: str, maximum_output_bytes: int
    ) -> tuple[int, bytes, bytes]:
        return self._git_metadata._git_bounded_bytes(
            binding, *arguments, maximum_output_bytes=maximum_output_bytes
        )

    def _verify_git_binding(self, binding: InternalGitBinding) -> None:
        self._git_metadata._verify_git_binding(binding)

    def _git(
        self,
        binding: InternalGitBinding,
        *arguments: str,
        maximum_output_bytes: int = MAX_GIT_COMMAND_BYTES,
    ) -> subprocess.CompletedProcess[str]:
        return self._git_metadata._git(
            binding, *arguments, maximum_output_bytes=maximum_output_bytes
        )

    _parse_git_status = staticmethod(GitMetadataReader._parse_git_status)

    @staticmethod
    def _non_git_manifest(root: Path, relative_directory: str) -> bool:
        try:
            directory_fd = _open_directory(root, relative_directory)
        except OSError:
            return False
        try:
            try:
                for entry in _bound_directory_entries(directory_fd):
                    try:
                        entry_stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISREG(entry_stat.st_mode) and (
                        entry.name in MANIFEST_NAMES
                        or entry.name.endswith(".sln")
                        or entry.name.endswith(".csproj")
                    ):
                        return True
            except OSError:
                return False
            return False
        finally:
            _close_directory(directory_fd)

    def _upsert_project(
        self, workspace_id: str, scan_run_id: str, workspace_root: Path, plan: ProjectPlan
    ) -> ProjectRecord:
        now = _now()
        with self._database.write_transaction() as connection:
            row = connection.execute(
                "SELECT project_id, display_name FROM projects WHERE identity_key = ?",
                (plan.identity_key,),
            ).fetchone()
            if row is None:
                project_id = _new_id()
                connection.execute(
                    """
                    INSERT INTO projects(
                        project_id, identity_kind, identity_key, display_name, first_seen_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (project_id, plan.identity_kind, plan.identity_key, plan.display_name, now),
                )
            else:
                project_id = str(row["project_id"])
            relative_location = _relative_path(plan.worktrees[0].root, workspace_root)
            connection.execute(
                """
                INSERT INTO workspace_projects(
                    workspace_id, project_id, relative_location, first_seen_run_id
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(workspace_id, project_id) DO NOTHING
                """,
                (workspace_id, project_id, relative_location, scan_run_id),
            )
        return ProjectRecord(project_id, plan.display_name)

    def _read_project(
        self,
        *,
        plan: ProjectPlan,
        config_revision: str,
        change_detection_mode: Literal["fast", "verify_content"] | None,
        nested_project_roots: set[Path],
    ) -> ProjectData:
        worktrees: list[WorktreeData] = []
        for worktree in plan.worktrees:
            prior = self._prior_observations(plan.identity_key, worktree.root)
            files, issues, excluded = self._read_worktree(
                worktree=worktree,
                config_revision=config_revision,
                change_detection_mode=change_detection_mode,
                prior=prior,
                nested_project_roots=nested_project_roots,
            )
            worktrees.append(WorktreeData(worktree, tuple(files), tuple(issues), excluded))
        return ProjectData(plan, tuple(worktrees))

    def _prior_observations(
        self, identity_key: str, worktree_root: Path
    ) -> dict[str, PriorObservation]:
        with self._database.read_connection() as connection:
            project = connection.execute(
                "SELECT project_id FROM projects WHERE identity_key = ?", (identity_key,)
            ).fetchone()
            if project is None:
                return {}
            worktree = connection.execute(
                """
                SELECT worktree_id FROM worktrees
                WHERE project_id = ? AND canonical_root = ?
                """,
                (str(project["project_id"]), str(worktree_root)),
            ).fetchone()
            if worktree is None:
                return {}
            rows = connection.execute(
                """
                SELECT a.relative_path, ao.artifact_id, ao.source_revision_id, ao.byte_size,
                       ao.mtime_ns, source.adapter_id, source.adapter_version,
                       source.config_revision, source.analysis_diagnostics,
                       (
                           SELECT evidence.commit_state
                           FROM project_snapshot_evidence AS pse
                           JOIN evidence ON evidence.evidence_id = pse.evidence_id
                           WHERE pse.project_snapshot_id = ao.project_snapshot_id
                             AND evidence.source_revision_id = ao.source_revision_id
                           LIMIT 1
                       ) AS commit_state
                FROM artifact_observations AS ao
                JOIN source_artifacts AS a ON a.artifact_id = ao.artifact_id
                JOIN source_revisions AS source
                  ON source.source_revision_id = ao.source_revision_id
                JOIN project_snapshots AS ps ON ps.project_snapshot_id = ao.project_snapshot_id
                JOIN scan_runs AS sr ON sr.scan_run_id = ps.scan_run_id
                WHERE a.worktree_id = ? AND sr.status IN ('completed', 'partial')
                ORDER BY ps.created_at DESC
                """,
                (str(worktree["worktree_id"]),),
            ).fetchall()
        observations: dict[str, PriorObservation] = {}
        for row in rows:
            relative = str(row["relative_path"])
            observations.setdefault(
                relative,
                PriorObservation(
                    artifact_id=str(row["artifact_id"]),
                    source_revision_id=str(row["source_revision_id"]),
                    byte_size=int(row["byte_size"]),
                    mtime_ns=int(row["mtime_ns"]),
                    adapter_id=str(row["adapter_id"]),
                    adapter_version=str(row["adapter_version"]),
                    config_revision=str(row["config_revision"]),
                    commit_state=str(row["commit_state"]),
                    analysis_diagnostics=_analysis_diagnostics(str(row["analysis_diagnostics"])),
                ),
            )
        return observations

    def _read_worktree(
        self,
        *,
        worktree: WorktreePlan,
        config_revision: str,
        change_detection_mode: Literal["fast", "verify_content"] | None,
        prior: dict[str, PriorObservation],
        nested_project_roots: set[Path],
    ) -> tuple[list[FileObservation], list[ScanIssueDraft], Counter[str]]:
        issues: list[ScanIssueDraft] = list(
            worktree.git_state.history_issues if worktree.git_state else ()
        )
        excluded: Counter[str] = Counter()
        files: list[FileObservation] = []
        statuses = worktree.git_state.path_states if worktree.git_state else {}
        candidate_paths = self._iter_project_files(
            project_root=worktree.root,
            nested_project_roots=nested_project_roots,
            issues=issues,
            excluded=excluded,
        )
        matcher, matcher_issues = IgnoreMatcher.load(
            worktree.root,
            [
                _relative_path(path, worktree.root)
                for path in candidate_paths
                if path.name == ".gitignore"
            ],
        )
        issues.extend(matcher_issues)
        for path in candidate_paths:
            relative = _relative_path(path, worktree.root)
            if self._is_sensitive(path.name):
                excluded["sensitive"] += 1
                continue
            if matcher.matches(relative):
                excluded["gitignore"] += 1
                continue
            if path.suffix.lower() in BINARY_ASSET_EXTENSIONS:
                excluded["binary_or_undecodable"] += 1
                continue
            try:
                file_fd, file_stat = _open_regular_file(worktree.root, relative)
            except OSError:
                issues.append(
                    _issue(
                        "file_unreadable",
                        "warning",
                        "A file could not be statted during indexing.",
                        "Fix local permissions and run refresh.",
                        relative,
                    )
                )
                continue
            try:
                if file_stat.st_size > MAX_FILE_BYTES:
                    excluded["oversized"] += 1
                    issues.append(
                        _issue(
                            "file_too_large",
                            "warning",
                            "A file exceeded the configured local indexing limit.",
                            (
                                "Review this file manually if needed; it remains outside the "
                                "local index while other files continue."
                            ),
                            relative,
                        )
                    )
                    continue
                if relative in statuses:
                    commit_state = statuses[relative]
                elif worktree.git_state is None or worktree.git_state.external_metadata_only:
                    commit_state = "not_applicable"
                else:
                    commit_state = "committed"
                artifact_kind, adapter_id, evidence_kind = self._classify(path, relative)
                current_adapter_version = f"{ANALYZER_VERSION}:{adapter_version(adapter_id)}"
                previous = prior.get(relative)
                should_reuse = (
                    change_detection_mode == "fast"
                    and previous is not None
                    and previous.byte_size == file_stat.st_size
                    and previous.mtime_ns == file_stat.st_mtime_ns
                    and previous.adapter_id == adapter_id
                    and previous.adapter_version == current_adapter_version
                    and previous.config_revision == config_revision
                    and previous.commit_state == commit_state
                    and commit_state not in {"modified", "untracked"}
                )
                if should_reuse:
                    assert previous is not None
                    for diagnostic in previous.analysis_diagnostics:
                        issues.append(
                            _issue(
                                diagnostic.kind,
                                "warning",
                                diagnostic.message,
                                diagnostic.remediation,
                                relative,
                            )
                        )
                    files.append(
                        FileObservation(
                            relative_path=relative,
                            artifact_kind=artifact_kind,
                            adapter_id=adapter_id,
                            adapter_version=current_adapter_version,
                            evidence_kind=evidence_kind,
                            commit_state=commit_state,
                            byte_size=file_stat.st_size,
                            mtime_ns=file_stat.st_mtime_ns,
                            content_sha256=None,
                            reused_source_revision_id=previous.source_revision_id,
                            analysis_facts=None,
                            analysis_diagnostics=previous.analysis_diagnostics,
                        )
                    )
                    continue
                content = _read_open_file(file_fd)
                decoded_content = content.decode("utf-8")
            except UnicodeDecodeError:
                excluded["binary_or_undecodable"] += 1
                issues.append(
                    _issue(
                        "file_not_utf8",
                        "warning",
                        "A non-text file was not retained as source evidence.",
                        "Keep binary artifacts outside the source index.",
                        relative,
                    )
                )
                continue
            except OSError:
                issues.append(
                    _issue(
                        "file_unreadable",
                        "warning",
                        "A file could not be read during indexing.",
                        "Fix local permissions and run refresh.",
                        relative,
                    )
                )
                continue
            finally:
                close_file_descriptor(file_fd)
            content_sha256 = hashlib.sha256(content).hexdigest()
            cache_key = (
                content_sha256,
                adapter_id,
                current_adapter_version,
                config_revision,
                artifact_kind,
                Path(relative).name,
            )
            analysis_result = self._analysis_cache.get(cache_key)
            if analysis_result is None:
                analysis_result = analyze_file(
                    relative_path=relative,
                    text=decoded_content,
                    artifact_kind=artifact_kind,
                    adapter_id=adapter_id,
                    base_evidence_kind=evidence_kind,
                )
                self._analysis_cache[cache_key] = analysis_result
            for diagnostic in analysis_result.diagnostics:
                issues.append(
                    _issue(
                        diagnostic.kind,
                        "warning",
                        diagnostic.message,
                        diagnostic.remediation,
                        relative,
                    )
                )
            files.append(
                FileObservation(
                    relative_path=relative,
                    artifact_kind=artifact_kind,
                    adapter_id=adapter_id,
                    adapter_version=current_adapter_version,
                    evidence_kind=evidence_kind,
                    commit_state=commit_state,
                    byte_size=file_stat.st_size,
                    mtime_ns=file_stat.st_mtime_ns,
                    content_sha256=content_sha256,
                    reused_source_revision_id=None,
                    analysis_facts=analysis_result.facts,
                    analysis_diagnostics=analysis_result.diagnostics,
                )
            )
        return files, issues, excluded

    def _iter_project_files(
        self,
        *,
        project_root: Path,
        nested_project_roots: set[Path],
        issues: list[ScanIssueDraft],
        excluded: Counter[str],
    ) -> list[Path]:
        files: list[Path] = []
        stack = ["."]
        while stack:
            relative_directory = stack.pop()
            directory = (
                project_root if relative_directory == "." else project_root / relative_directory
            )
            try:
                directory_fd = _open_directory(project_root, relative_directory)
            except OSError:
                issues.append(
                    _issue(
                        "directory_unreadable",
                        "warning",
                        "A project directory could not be listed.",
                        "Fix local permissions and run refresh.",
                        _relative_path(directory, project_root),
                    )
                )
                continue
            try:
                try:
                    children = sorted(
                        _bound_directory_entries(directory_fd), key=lambda entry: entry.name
                    )
                except OSError:
                    issues.append(
                        _issue(
                            "directory_unreadable",
                            "warning",
                            "A project directory could not be listed.",
                            "Fix local permissions and run refresh.",
                            _relative_path(directory, project_root),
                        )
                    )
                    continue
                for entry in reversed(children):
                    relative = _child_relative(relative_directory, entry.name)
                    path = project_root / relative
                    try:
                        entry_stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        issues.append(
                            _issue(
                                "path_unreadable",
                                "warning",
                                "A filesystem entry could not be inspected.",
                                "Fix local permissions and run refresh.",
                                relative,
                            )
                        )
                        continue
                    if stat.S_ISLNK(entry_stat.st_mode):
                        excluded["symlink"] += 1
                        continue
                    if stat.S_ISDIR(entry_stat.st_mode):
                        if path in nested_project_roots:
                            excluded["nested_project"] += 1
                        elif (
                            entry.name.lower() in HARD_EXCLUDED_DIRECTORIES
                            or _is_packaged_app_root(entry.name)
                        ):
                            excluded["hard_excluded"] += 1
                        else:
                            stack.append(relative)
                    elif stat.S_ISREG(entry_stat.st_mode):
                        files.append(path)
            finally:
                _close_directory(directory_fd)
        files.sort(key=lambda path: str(path))
        return files

    @staticmethod
    def _is_sensitive(filename: str) -> bool:
        lower = filename.lower()
        return (
            lower == ".env"
            or lower.startswith(".env.")
            or lower
            in {
                ".envrc",
                ".netrc",
                ".npmrc",
                ".pypirc",
                "auth.json",
                "credentials",
                "credentials.json",
                "credentials.yaml",
                "credentials.yml",
                "id_dsa",
                "id_ecdsa",
                "id_ed25519",
                "id_rsa",
                "secret.json",
                "secret.yaml",
                "secret.yml",
                "secrets.json",
                "secrets.yaml",
                "secrets.yml",
                "tokens.json",
            }
            or lower.endswith((".env", ".key", ".p12", ".pem", ".pfx"))
        )

    @staticmethod
    def _classify(path: Path, relative: str) -> tuple[str, str, str]:
        lower_name = path.name.lower()
        suffix = path.suffix.lower()
        manifest_adapters = {
            "cargo.toml": "rust",
            "cmakelists.txt": "cpp",
            "package.json": "typescript",
            "pubspec.yaml": "dart",
            "pyproject.toml": "python",
        }
        if lower_name.startswith("requirements") and lower_name.endswith(".txt"):
            return "manifest", "python", "manifest"
        if path.name in MANIFEST_NAMES or lower_name.endswith((".sln", ".csproj")):
            return "manifest", manifest_adapters.get(lower_name, "generic"), "manifest"
        if suffix in SOURCE_EXTENSIONS:
            adapter = SOURCE_EXTENSIONS[suffix]
            path_parts = {part.lower() for part in Path(relative).parts[:-1]}
            if adapter == "sql" and path_parts.intersection(
                {
                    "design",
                    "designs",
                    "doc",
                    "docs",
                    "plan",
                    "plans",
                    "proposal",
                    "proposals",
                }
            ):
                return "documentation", "generic", "documentation"
            sql_name_tokens = set(re.split(r"[._-]+", lower_name))
            if adapter == "sql" and sql_name_tokens.intersection(
                {"design", "draft", "plan", "planned", "proposal", "proposed"}
            ):
                return "documentation", "generic", "documentation"
            if (
                path_parts.intersection({"test", "tests", "__tests__"})
                or lower_name.startswith("test_")
                or ".test." in lower_name
                or ".spec." in lower_name
            ):
                return "test", adapter, "test_definition"
            if adapter == "sql":
                if path_parts.intersection({"migration", "migrations"}):
                    return "source", adapter, "migration_definition"
                if "schema" in path_parts or lower_name.startswith("schema."):
                    return "source", adapter, "schema_definition"
                return "source", adapter, "query_definition"
            return "source", adapter, "implementation"
        if suffix in {".md", ".rst", ".txt"} or lower_name.startswith("readme"):
            return "documentation", "generic", "documentation"
        if suffix in CONFIG_EXTENSIONS or path.name.startswith("."):
            return "configuration", "generic", "configuration"
        return "unknown", "generic", "structure"

    def _persist_project(
        self,
        *,
        scan_run_id: str,
        project: ProjectRecord,
        data: ProjectData,
        config_revision: str,
        workspace_root: Path,
    ) -> tuple[tuple[ScanIssueDraft, ...], Counter[str]]:
        issues = tuple(issue for worktree in data.worktrees for issue in worktree.issues)
        excluded: Counter[str] = Counter()
        for worktree in data.worktrees:
            excluded.update(worktree.excluded)
        now = _now()
        with self._database.write_transaction() as connection:
            worktree_ids = {
                worktree.plan.root: self._upsert_worktree(
                    connection, project.project_id, worktree.plan
                )
                for worktree in data.worktrees
            }
            for worktree in data.worktrees:
                state = worktree.plan.git_state
                connection.execute(
                    """
                    INSERT INTO worktree_observations(
                        worktree_id, scan_run_id, branch, head_commit, dirty_state, history_basis,
                        external_git_dir, external_common_dir, external_metadata_receipt_id,
                        external_metadata_confirmed_at, external_metadata_read_fields, observed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        worktree_ids[worktree.plan.root],
                        scan_run_id,
                        state.branch if state else None,
                        state.head_commit if state else None,
                        state.dirty_state if state else "not_applicable",
                        state.history_basis if state else "not_applicable",
                        str(state.external_git_grant.git_dir)
                        if state and state.external_git_grant
                        else None,
                        str(state.external_git_grant.common_dir)
                        if state and state.external_git_grant
                        else None,
                        state.external_git_grant.authorization_receipt_id
                        if state and state.external_git_grant
                        else None,
                        state.external_git_grant.confirmed_at
                        if state and state.external_git_grant
                        else None,
                        json.dumps(state.external_metadata_read_fields, separators=(",", ":"))
                        if state and state.external_git_grant
                        else None,
                        now,
                    ),
                )
            coverage_status = (
                "partial" if any(issue.severity != "info" for issue in issues) else "complete"
            )
            snapshot_id = _new_id()
            connection.execute(
                """
                INSERT INTO project_snapshots(
                    project_snapshot_id, project_id, scan_run_id, created_at, coverage_status
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (snapshot_id, project.project_id, scan_run_id, now, coverage_status),
            )
            connection.execute(
                """
                INSERT INTO scan_run_projects(
                    scan_run_id, project_id, snapshot_disposition, project_snapshot_id
                )
                VALUES (?, ?, 'fresh', ?)
                """,
                (scan_run_id, project.project_id, snapshot_id),
            )
            module_ids = self._upsert_modules(connection, project.project_id, data)
            current_revisions: dict[str, str] = {}
            current_history_evidence: dict[tuple[str, str], str] = {}
            for worktree in data.worktrees:
                state = worktree.plan.git_state
                if state is None:
                    continue
                worktree_root = _relative_path(worktree.plan.root, workspace_root)
                for entry in state.history_entries:
                    evidence_id = self._history_evidence(
                        connection,
                        project_id=project.project_id,
                        project_snapshot_id=snapshot_id,
                        worktree_root=worktree_root,
                        history_basis=state.history_basis,
                        entry=entry,
                        now=now,
                    )
                    connection.execute(
                        """
                        INSERT INTO project_snapshot_evidence(project_snapshot_id, evidence_id)
                        VALUES (?, ?)
                        ON CONFLICT(project_snapshot_id, evidence_id) DO NOTHING
                        """,
                        (snapshot_id, evidence_id),
                    )
                    current_history_evidence[(worktree_root, entry.commit)] = evidence_id
            for worktree in data.worktrees:
                worktree_id = worktree_ids[worktree.plan.root]
                current_paths = {file.relative_path for file in worktree.files}
                for file in worktree.files:
                    artifact_id = self._upsert_artifact(
                        connection,
                        project.project_id,
                        worktree_id,
                        file.relative_path,
                        file.artifact_kind,
                        file.content_sha256,
                        current_paths,
                    )
                    revision_id = self._source_revision(
                        connection,
                        artifact_id,
                        file,
                        config_revision,
                        now,
                    )
                    connection.execute(
                        """
                        INSERT INTO project_snapshot_source_revisions(
                            project_snapshot_id, source_revision_id
                        )
                        VALUES (?, ?)
                        ON CONFLICT(project_snapshot_id, source_revision_id) DO NOTHING
                        """,
                        (snapshot_id, revision_id),
                    )
                    connection.execute(
                        """
                        INSERT INTO artifact_observations(
                            project_snapshot_id, artifact_id, source_revision_id,
                            byte_size, mtime_ns
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (snapshot_id, artifact_id, revision_id, file.byte_size, file.mtime_ns),
                    )
                    module_id = self._module_for_file(module_ids, file.relative_path)
                    evidence_ids = self._evidence_entries(
                        connection,
                        project_id=project.project_id,
                        project_snapshot_id=snapshot_id,
                        module_id=module_id,
                        source_revision_id=revision_id,
                        file=file,
                        now=now,
                    )
                    for evidence_id in evidence_ids:
                        connection.execute(
                            """
                            INSERT INTO project_snapshot_evidence(project_snapshot_id, evidence_id)
                            VALUES (?, ?)
                            ON CONFLICT(project_snapshot_id, evidence_id) DO NOTHING
                            """,
                            (snapshot_id, evidence_id),
                        )
                    current_revisions[artifact_id] = revision_id
            for module_key, module_id in module_ids.items():
                adapter_id = self._module_adapter(data, module_key)
                connection.execute(
                    """
                    INSERT INTO module_observations(
                        module_id, project_snapshot_id, relative_root,
                        manifest_evidence_id, adapter_id
                    ) VALUES (?, ?, ?, NULL, ?)
                    """,
                    (module_id, snapshot_id, module_key, adapter_id),
                )
            self._insert_issues(connection, scan_run_id, project.project_id, issues)
            self._resolve_evidence_validity(
                connection,
                scan_run_id=scan_run_id,
                project_id=project.project_id,
                project_snapshot_id=snapshot_id,
                current_revisions=current_revisions,
                now=now,
            )
            self._resolve_history_evidence_validity(
                connection,
                scan_run_id=scan_run_id,
                project_id=project.project_id,
                current_history_evidence=current_history_evidence,
                now=now,
            )
        return issues, excluded

    def _upsert_worktree(
        self, connection: sqlite3.Connection, project_id: str, plan: WorktreePlan
    ) -> str:
        row = connection.execute(
            """
            SELECT worktree_id FROM worktrees
            WHERE project_id = ? AND canonical_root = ?
            """,
            (project_id, str(plan.root)),
        ).fetchone()
        if row is not None:
            return str(row["worktree_id"])
        worktree_id = _new_id()
        connection.execute(
            """
            INSERT INTO worktrees(worktree_id, project_id, canonical_root, git_dir)
            VALUES (?, ?, ?, ?)
            """,
            (
                worktree_id,
                project_id,
                str(plan.root),
                str(plan.git_state.git_dir) if plan.git_state else None,
            ),
        )
        return worktree_id

    def _upsert_modules(
        self, connection: sqlite3.Connection, project_id: str, data: ProjectData
    ) -> dict[str, str]:
        module_specs: dict[str, tuple[str, str]] = {".": (data.plan.display_name, "project_root")}
        for worktree in data.worktrees:
            for file in worktree.files:
                if file.artifact_kind != "manifest":
                    continue
                parent = str(Path(file.relative_path).parent)
                module_key = "." if parent == "." else parent.replace("\\", "/")
                module_specs.setdefault((module_key), (Path(module_key).name, "manifest_module"))
        modules: dict[str, str] = {}
        for key, (name, kind) in module_specs.items():
            row = connection.execute(
                "SELECT module_id FROM modules WHERE project_id = ? AND module_key = ?",
                (project_id, key),
            ).fetchone()
            if row is None:
                module_id = _new_id()
                connection.execute(
                    """
                    INSERT INTO modules(module_id, project_id, module_key, name, kind)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (module_id, project_id, key, name, kind),
                )
            else:
                module_id = str(row["module_id"])
            modules[key] = module_id
        return modules

    @staticmethod
    def _module_for_file(module_ids: dict[str, str], relative_path: str) -> str:
        parent = Path(relative_path).parent.as_posix()
        candidates = [
            key for key in module_ids if key == "." or parent == key or parent.startswith(f"{key}/")
        ]
        return module_ids[max(candidates, key=len)]

    @staticmethod
    def _module_adapter(data: ProjectData, module_key: str) -> str:
        prefix = "" if module_key == "." else f"{module_key}/"
        files = [
            file
            for worktree in data.worktrees
            for file in worktree.files
            if file.relative_path.startswith(prefix)
        ]
        root_manifest_adapters = {
            file.adapter_id
            for file in files
            if file.artifact_kind == "manifest"
            and Path(file.relative_path).parent.as_posix() == module_key
            and file.adapter_id != "generic"
        }
        if root_manifest_adapters:
            return sorted(root_manifest_adapters)[0]
        adapter_counts = Counter(file.adapter_id for file in files if file.adapter_id != "generic")
        if not adapter_counts:
            return "generic"
        return min(adapter_counts, key=lambda adapter: (-adapter_counts[adapter], adapter))

    @staticmethod
    def _upsert_artifact(
        connection: sqlite3.Connection,
        project_id: str,
        worktree_id: str,
        relative_path: str,
        artifact_kind: str,
        content_sha256: str | None,
        current_paths: set[str],
    ) -> str:
        row = connection.execute(
            """
            SELECT artifact_id FROM source_artifacts
            WHERE project_id = ? AND worktree_id = ? AND relative_path = ?
            """,
            (project_id, worktree_id, relative_path),
        ).fetchone()
        if row is not None:
            return str(row["artifact_id"])
        supersedes_artifact_id: str | None = None
        if content_sha256 is not None:
            candidates = connection.execute(
                """
                SELECT a.artifact_id, a.relative_path, MAX(ps.created_at) AS last_seen_at
                FROM source_artifacts AS a
                JOIN source_revisions AS sr ON sr.artifact_id = a.artifact_id
                JOIN artifact_observations AS ao
                  ON ao.source_revision_id = sr.source_revision_id
                JOIN project_snapshots AS ps
                  ON ps.project_snapshot_id = ao.project_snapshot_id
                WHERE a.project_id = ? AND a.worktree_id = ?
                  AND a.relative_path <> ? AND sr.content_sha256 = ?
                GROUP BY a.artifact_id, a.relative_path
                ORDER BY last_seen_at DESC, a.artifact_id
                """,
                (project_id, worktree_id, relative_path, content_sha256),
            ).fetchall()
            missing_candidates = [
                candidate
                for candidate in candidates
                if str(candidate["relative_path"]) not in current_paths
            ]
            if missing_candidates and (
                len(missing_candidates) == 1
                or missing_candidates[0]["last_seen_at"] != missing_candidates[1]["last_seen_at"]
            ):
                supersedes_artifact_id = str(missing_candidates[0]["artifact_id"])
        artifact_id = _new_id()
        connection.execute(
            """
            INSERT INTO source_artifacts(
                artifact_id, project_id, worktree_id, relative_path, artifact_kind,
                supersedes_artifact_id
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                artifact_id,
                project_id,
                worktree_id,
                relative_path,
                artifact_kind,
                supersedes_artifact_id,
            ),
        )
        return artifact_id

    @staticmethod
    def _source_revision(
        connection: sqlite3.Connection,
        artifact_id: str,
        file: FileObservation,
        config_revision: str,
        now: str,
    ) -> str:
        if file.reused_source_revision_id is not None:
            return file.reused_source_revision_id
        assert file.content_sha256 is not None
        fingerprint = hashlib.sha256(
            "\0".join(
                (file.content_sha256, file.adapter_id, file.adapter_version, config_revision)
            ).encode("utf-8")
        ).hexdigest()
        row = connection.execute(
            """
            SELECT source_revision_id FROM source_revisions
            WHERE artifact_id = ? AND analysis_fingerprint = ?
            """,
            (artifact_id, fingerprint),
        ).fetchone()
        if row is not None:
            return str(row["source_revision_id"])
        revision_id = _new_id()
        connection.execute(
            """
            INSERT INTO source_revisions(
                source_revision_id, artifact_id, content_sha256, byte_size,
                analysis_fingerprint, adapter_id, adapter_version, config_revision,
                analysis_diagnostics, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                revision_id,
                artifact_id,
                file.content_sha256,
                file.byte_size,
                fingerprint,
                file.adapter_id,
                file.adapter_version,
                config_revision,
                _diagnostics_json(file.analysis_diagnostics),
                now,
            ),
        )
        return revision_id

    @staticmethod
    def _evidence_entries(
        connection: sqlite3.Connection,
        *,
        project_id: str,
        project_snapshot_id: str,
        module_id: str,
        source_revision_id: str,
        file: FileObservation,
        now: str,
    ) -> tuple[str, ...]:
        if file.analysis_facts is None:
            rows = connection.execute(
                """
                SELECT evidence_id FROM evidence
                WHERE source_revision_id = ? AND commit_state = ?
                ORDER BY evidence_kind, locator
                """,
                (source_revision_id, file.commit_state),
            ).fetchall()
            if rows:
                return tuple(str(row["evidence_id"]) for row in rows)
            if file.analysis_diagnostics:
                return ()
            raise ValueError("reused source revision has no reusable evidence")
        revision = connection.execute(
            "SELECT analysis_fingerprint FROM source_revisions WHERE source_revision_id = ?",
            (source_revision_id,),
        ).fetchone()
        assert revision is not None
        evidence_ids: list[str] = []
        for fact in file.analysis_facts:
            locator_object = fact.locator(file.relative_path)
            locator = json.dumps(locator_object, separators=(",", ":"), sort_keys=True)
            row = connection.execute(
                """
                SELECT evidence_id FROM evidence
                WHERE source_revision_id = ? AND evidence_kind = ?
                  AND locator = ? AND commit_state = ?
                """,
                (source_revision_id, fact.evidence_kind, locator, file.commit_state),
            ).fetchone()
            if row is not None:
                evidence_ids.append(str(row["evidence_id"]))
                continue
            semantic_locator = {
                key: value for key, value in locator_object.items() if key != "relative_path"
            }
            equivalence = hashlib.sha256(
                "\0".join(
                    (
                        str(revision["analysis_fingerprint"]),
                        fact.evidence_kind,
                        json.dumps(semantic_locator, separators=(",", ":"), sort_keys=True),
                    )
                ).encode("utf-8")
            ).hexdigest()
            evidence_id = _new_id()
            connection.execute(
                """
                INSERT INTO evidence(
                    evidence_id, project_id, acquisition_scope, project_snapshot_id, module_id,
                    source_revision_id, content_equivalence_key, origin_kind, evidence_kind,
                    locator, summary, commit_state, created_at
                ) VALUES (?, ?, 'scan', ?, ?, ?, ?, 'source_revision', ?, ?, ?, ?, ?)
                """,
                (
                    evidence_id,
                    project_id,
                    project_snapshot_id,
                    module_id,
                    source_revision_id,
                    equivalence,
                    fact.evidence_kind,
                    locator,
                    _short(fact.summary),
                    file.commit_state,
                    now,
                ),
            )
            evidence_ids.append(evidence_id)
        return tuple(evidence_ids)

    @staticmethod
    def _history_evidence(
        connection: sqlite3.Connection,
        *,
        project_id: str,
        project_snapshot_id: str,
        worktree_root: str,
        history_basis: str,
        entry: GitHistoryEntry,
        now: str,
    ) -> str:
        locator = json.dumps(
            {
                "author_email": entry.author_email,
                "author_name": entry.author_name,
                "changed_paths": list(entry.changed_paths),
                "commit": entry.commit,
                "committed_at": entry.committed_at,
                "history_basis": history_basis,
                "paths_truncated": entry.paths_truncated,
                "subject": entry.subject,
                "worktree_root": worktree_root,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        row = connection.execute(
            """
            SELECT evidence_id FROM evidence
            WHERE project_snapshot_id = ? AND origin_kind = 'git_commit'
              AND evidence_kind = 'git_history' AND locator = ? AND commit_state = 'historical'
            """,
            (project_snapshot_id, locator),
        ).fetchone()
        if row is not None:
            return str(row["evidence_id"])
        evidence_id = _new_id()
        connection.execute(
            """
            INSERT INTO evidence(
                evidence_id, project_id, acquisition_scope, project_snapshot_id, module_id,
                source_revision_id, content_equivalence_key, origin_kind, evidence_kind, locator,
                summary, commit_state, created_at
            ) VALUES (?, ?, 'scan', ?, NULL, NULL, NULL, 'git_commit', 'git_history', ?, ?,
                      'historical', ?)
            """,
            (
                evidence_id,
                project_id,
                project_snapshot_id,
                locator,
                _short(
                    "Indexed bounded local Git commit metadata and changed-path range "
                    f"for {entry.commit[:12]}."
                ),
                now,
            ),
        )
        return evidence_id

    def _resolve_evidence_validity(
        self,
        connection: sqlite3.Connection,
        *,
        scan_run_id: str,
        project_id: str,
        project_snapshot_id: str,
        current_revisions: dict[str, str],
        now: str,
    ) -> None:
        current_rows = connection.execute(
            """
            SELECT e.evidence_id, e.evidence_kind, e.locator, sr.artifact_id
            FROM project_snapshot_evidence AS pse
            JOIN evidence AS e ON e.evidence_id = pse.evidence_id
            JOIN source_revisions AS sr ON sr.source_revision_id = e.source_revision_id
            WHERE pse.project_snapshot_id = ? AND e.origin_kind = 'source_revision'
            """,
            (project_snapshot_id,),
        ).fetchall()
        current_ids = {str(row["evidence_id"]) for row in current_rows}
        current_by_key = {
            (
                str(row["artifact_id"]),
                str(row["evidence_kind"]),
                str(row["locator"]),
            ): str(row["evidence_id"])
            for row in current_rows
        }
        rows = connection.execute(
            """
            SELECT e.evidence_id, e.evidence_kind, e.locator, sr.artifact_id
            FROM evidence AS e
            JOIN source_revisions AS sr ON sr.source_revision_id = e.source_revision_id
            WHERE e.project_id = ? AND e.acquisition_scope = 'scan'
              AND e.origin_kind = 'source_revision'
            """,
            (project_id,),
        ).fetchall()
        for row in rows:
            evidence_id = str(row["evidence_id"])
            artifact_id = str(row["artifact_id"])
            current_revision = current_revisions.get(artifact_id)
            validity = (
                "current"
                if evidence_id in current_ids
                else "missing"
                if current_revision is None
                else "stale"
            )
            replacement = (
                current_by_key.get(
                    (
                        artifact_id,
                        str(row["evidence_kind"]),
                        str(row["locator"]),
                    )
                )
                if validity == "stale"
                else None
            )
            connection.execute(
                """
                INSERT INTO evidence_validities(
                    scan_run_id, evidence_id, validity, replacement_evidence_id, resolved_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (scan_run_id, evidence_id, validity, replacement, now),
            )

    @staticmethod
    def _resolve_history_evidence_validity(
        connection: sqlite3.Connection,
        *,
        scan_run_id: str,
        project_id: str,
        current_history_evidence: dict[tuple[str, str], str],
        now: str,
    ) -> None:
        rows = connection.execute(
            """
            SELECT evidence_id, locator
            FROM evidence
            WHERE project_id = ? AND acquisition_scope = 'scan'
              AND origin_kind = 'git_commit' AND evidence_kind = 'git_history'
            """,
            (project_id,),
        ).fetchall()
        for row in rows:
            evidence_id = str(row["evidence_id"])
            try:
                locator = json.loads(str(row["locator"]))
                if not isinstance(locator, dict):
                    raise ValueError("history locator is not an object")
                worktree_root = locator["worktree_root"]
                commit = locator["commit"]
                if not isinstance(worktree_root, str) or not isinstance(commit, str):
                    raise ValueError("history locator is incomplete")
            except (KeyError, ValueError, json.JSONDecodeError):
                key: tuple[str, str] | None = None
            else:
                key = (worktree_root, commit)
            replacement = current_history_evidence.get(key) if key is not None else None
            validity = "current" if replacement == evidence_id else "stale"
            connection.execute(
                """
                INSERT INTO evidence_validities(
                    scan_run_id, evidence_id, validity, replacement_evidence_id, resolved_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (scan_run_id, evidence_id, validity, replacement, now),
            )

    def _record_project_failure(
        self, scan_run_id: str, project_id: str, issue: ScanIssueDraft
    ) -> str:
        with self._database.write_transaction() as connection:
            baseline = connection.execute(
                """
                SELECT ps.project_snapshot_id
                FROM project_snapshots AS ps
                JOIN scan_runs AS sr ON sr.scan_run_id = ps.scan_run_id
                WHERE ps.project_id = ? AND sr.status IN ('completed', 'partial')
                ORDER BY ps.created_at DESC
                LIMIT 1
                """,
                (project_id,),
            ).fetchone()
            if baseline is None:
                disposition = "failed_no_baseline"
                snapshot_id = None
            else:
                disposition = "carried_forward"
                snapshot_id = str(baseline["project_snapshot_id"])
            connection.execute(
                """
                INSERT INTO scan_run_projects(
                    scan_run_id, project_id, snapshot_disposition, project_snapshot_id
                )
                VALUES (?, ?, ?, ?)
                """,
                (scan_run_id, project_id, disposition, snapshot_id),
            )
            self._insert_issues(connection, scan_run_id, project_id, (issue,))
        return disposition

    def _record_project_exclusion(self, scan_run_id: str, project_id: str) -> None:
        with self._database.write_transaction() as connection:
            connection.execute(
                """
                INSERT INTO scan_run_projects(
                    scan_run_id, project_id, snapshot_disposition, project_snapshot_id
                )
                VALUES (?, ?, 'excluded', NULL)
                """,
                (scan_run_id, project_id),
            )

    def _carry_forward_missing_projects(
        self,
        *,
        scan_run_id: str,
        workspace_id: str,
        discovered_project_ids: set[str],
    ) -> tuple[list[ScanIssueDraft], Counter[str]]:
        with self._database.read_connection() as connection:
            rows = connection.execute(
                """
                SELECT project_id, relative_location
                FROM workspace_projects
                WHERE workspace_id = ?
                """,
                (workspace_id,),
            ).fetchall()
        issues: list[ScanIssueDraft] = []
        dispositions: Counter[str] = Counter()
        for row in rows:
            project_id = str(row["project_id"])
            if project_id in discovered_project_ids:
                continue
            issue = _issue(
                "project_not_discovered",
                "warning",
                "A previously indexed project was not discovered in this workspace run.",
                (
                    "Restore the project directory or explicitly remove it in a future maintenance "
                    "workflow."
                ),
                str(row["relative_location"]),
            )
            disposition = self._record_project_failure(scan_run_id, project_id, issue)
            issues.append(issue)
            dispositions[disposition] += 1
        return issues, dispositions

    def _persist_issues(
        self, scan_run_id: str, project_id: str | None, issues: tuple[ScanIssueDraft, ...]
    ) -> None:
        if not issues:
            return
        with self._database.write_transaction() as connection:
            self._insert_issues(connection, scan_run_id, project_id, issues)

    @staticmethod
    def _insert_issues(
        connection: sqlite3.Connection,
        scan_run_id: str,
        project_id: str | None,
        issues: tuple[ScanIssueDraft, ...],
    ) -> None:
        for issue in issues:
            connection.execute(
                """
                INSERT INTO scan_issues(
                    issue_id, scan_run_id, project_id, artifact_id, kind, severity,
                    relative_path, message, remediation
                ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?)
                """,
                (
                    issue.issue_id,
                    scan_run_id,
                    project_id,
                    issue.kind,
                    issue.severity,
                    issue.relative_path,
                    issue.message,
                    issue.remediation,
                ),
            )

    @staticmethod
    def _final_status(
        scan_run_id: str, dispositions: Counter[str], issues: list[ScanIssueDraft]
    ) -> str:
        del scan_run_id
        available = dispositions["fresh"] + dispositions["carried_forward"]
        if available == 0 and dispositions["excluded"] == 0:
            return "failed"
        if dispositions["carried_forward"] or dispositions["failed_no_baseline"]:
            return "partial"
        if any(issue.severity != "info" for issue in issues):
            return "partial"
        return "completed"

    def _finish_run(
        self,
        scan_run_id: str,
        status: str,
        coverage: dict[str, object] | None,
    ) -> None:
        with self._database.write_transaction() as connection:
            updated = connection.execute(
                """
                UPDATE scan_runs
                SET status = ?, finished_at = ?
                WHERE scan_run_id = ? AND status = 'running'
                """,
                (status, _now(), scan_run_id),
            )
            if coverage is not None and updated.rowcount == 1:
                connection.execute(
                    """
                    INSERT INTO scan_run_overviews(scan_run_id, coverage_json, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (
                        scan_run_id,
                        json.dumps(
                            coverage,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                            allow_nan=False,
                        ),
                        _now(),
                    ),
                )

    def _coverage(
        self,
        scan_run_id: str,
        dispositions: Counter[str],
        excluded: Counter[str],
        project_exclusions: list[dict[str, str]],
        raw_ignore_patterns: dict[str, str],
    ) -> dict[str, object]:
        with self._database.read_connection() as connection:
            worktrees = int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM worktree_observations WHERE scan_run_id = ?",
                    (scan_run_id,),
                ).fetchone()["count"]
            )
            modules = int(
                connection.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM module_observations AS mo
                    JOIN project_snapshots AS ps ON ps.project_snapshot_id = mo.project_snapshot_id
                    WHERE ps.scan_run_id = ?
                    """,
                    (scan_run_id,),
                ).fetchone()["count"]
            )
            files = int(
                connection.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM artifact_observations AS ao
                    JOIN project_snapshots AS ps ON ps.project_snapshot_id = ao.project_snapshot_id
                    WHERE ps.scan_run_id = ?
                    """,
                    (scan_run_id,),
                ).fetchone()["count"]
            )
            history_evidence = int(
                connection.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM project_snapshot_evidence AS pse
                    JOIN evidence AS e ON e.evidence_id = pse.evidence_id
                    JOIN project_snapshots AS ps ON ps.project_snapshot_id = pse.project_snapshot_id
                    WHERE ps.scan_run_id = ? AND e.origin_kind = 'git_commit'
                      AND e.evidence_kind = 'git_history'
                    """,
                    (scan_run_id,),
                ).fetchone()["count"]
            )
            observations = connection.execute(
                """
                SELECT w.canonical_root, wo.history_basis, wo.external_git_dir,
                       wo.external_common_dir, wo.external_metadata_receipt_id,
                       wo.external_metadata_confirmed_at, wo.external_metadata_read_fields
                FROM worktree_observations AS wo
                JOIN worktrees AS w ON w.worktree_id = wo.worktree_id
                WHERE wo.scan_run_id = ?
                ORDER BY w.canonical_root
                """,
                (scan_run_id,),
            ).fetchall()
            ignore_issue_rows = connection.execute(
                """
                SELECT si.issue_id, si.project_id, p.display_name, si.severity, si.relative_path,
                       si.message, si.remediation
                FROM scan_issues AS si
                LEFT JOIN projects AS p ON p.project_id = si.project_id
                WHERE si.scan_run_id = ? AND si.kind = 'ignore_pattern_unsupported'
                ORDER BY p.display_name, si.relative_path, si.message, si.issue_id
                """,
                (scan_run_id,),
            ).fetchall()
            role_lens_context = self._role_lens_context(connection, scan_run_id)
        history_basis_by_worktree = {
            str(row["canonical_root"]): str(row["history_basis"]) for row in observations
        }
        external_git_metadata = {
            str(row["canonical_root"]): {
                "git_dir": str(row["external_git_dir"]),
                "common_dir": str(row["external_common_dir"]),
                "authorization_receipt_id": str(row["external_metadata_receipt_id"]),
                "confirmed_at": str(row["external_metadata_confirmed_at"]),
                "read_fields": json.loads(str(row["external_metadata_read_fields"])),
            }
            for row in observations
            if row["external_git_dir"] is not None
            and row["external_common_dir"] is not None
            and row["external_metadata_receipt_id"] is not None
            and row["external_metadata_confirmed_at"] is not None
            and row["external_metadata_read_fields"] is not None
        }
        return {
            "overview_provenance": "recorded",
            "excluded_by_category_available": True,
            "projects": sum(dispositions.values()),
            "fresh_projects": dispositions["fresh"],
            "carried_forward_projects": dispositions["carried_forward"],
            "failed_no_baseline_projects": dispositions["failed_no_baseline"],
            "excluded_projects": dispositions["excluded"],
            "project_exclusions": sorted(
                project_exclusions,
                key=lambda item: (
                    item["project_display_name"],
                    item["match"],
                    item["value"],
                    item["reason"],
                ),
            ),
            "worktrees": worktrees,
            "modules": modules,
            "indexed_files": files,
            "git_history_evidence": history_evidence,
            "history_basis_by_worktree": history_basis_by_worktree,
            "external_git_metadata": external_git_metadata,
            "excluded_by_category": dict(sorted(excluded.items())),
            "ignore_pattern_issues": [
                {
                    "project_id": row["project_id"],
                    "project_display_name": row["display_name"],
                    "source_ignore_file": row["relative_path"],
                    "severity": str(row["severity"]),
                    "raw_pattern_and_reason": str(row["message"]),
                    "approximation": str(row["remediation"]),
                    **(
                        {"raw_pattern": raw_ignore_patterns[str(row["issue_id"])]}
                        if str(row["issue_id"]) in raw_ignore_patterns
                        else {}
                    ),
                }
                for row in ignore_issue_rows
            ],
            "role_lens_context": role_lens_context,
        }

    @staticmethod
    def _role_lens_context(connection: sqlite3.Connection, scan_run_id: str) -> dict[str, object]:
        total_projects = int(
            connection.execute(
                "SELECT COUNT(*) AS count FROM scan_run_projects WHERE scan_run_id = ?",
                (scan_run_id,),
            ).fetchone()["count"]
        )
        project_rows = connection.execute(
            """
            SELECT srp.project_id, p.display_name, srp.snapshot_disposition,
                   srp.project_snapshot_id, ps.coverage_status
            FROM scan_run_projects AS srp
            JOIN projects AS p ON p.project_id = srp.project_id
            LEFT JOIN project_snapshots AS ps
              ON ps.project_snapshot_id = srp.project_snapshot_id
            WHERE srp.scan_run_id = ?
            ORDER BY srp.project_id
            LIMIT ?
            """,
            (scan_run_id, MAX_ROLE_LENS_CONTEXT_PROJECTS),
        ).fetchall()
        available_modules = int(
            connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM scan_run_projects AS srp
                JOIN module_observations AS mo
                  ON mo.project_snapshot_id = srp.project_snapshot_id
                WHERE srp.scan_run_id = ?
                """,
                (scan_run_id,),
            ).fetchone()["count"]
        )
        available_evidence = int(
            connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM scan_run_projects AS srp
                JOIN project_snapshot_evidence AS pse
                  ON pse.project_snapshot_id = srp.project_snapshot_id
                WHERE srp.scan_run_id = ?
                """,
                (scan_run_id,),
            ).fetchone()["count"]
        )
        projects: list[dict[str, object]] = []
        returned_modules = 0
        returned_evidence_samples = 0
        for project in project_rows:
            snapshot_id = project["project_snapshot_id"]
            module_kind_counts: dict[str, int] = {}
            adapter_counts: dict[str, int] = {}
            modules: list[dict[str, object]] = []
            evidence_kind_counts: dict[str, int] = {}
            evidence_samples: list[dict[str, object]] = []
            project_module_count = 0
            project_evidence_count = 0
            if snapshot_id is not None:
                module_count_row = connection.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM module_observations
                    WHERE project_snapshot_id = ?
                    """,
                    (snapshot_id,),
                ).fetchone()
                assert module_count_row is not None
                project_module_count = int(module_count_row["count"])
                for row in connection.execute(
                    """
                    SELECT m.kind, mo.adapter_id, COUNT(*) AS count
                    FROM module_observations AS mo
                    JOIN modules AS m ON m.module_id = mo.module_id
                    WHERE mo.project_snapshot_id = ?
                    GROUP BY m.kind, mo.adapter_id
                    ORDER BY m.kind, mo.adapter_id
                    """,
                    (snapshot_id,),
                ).fetchall():
                    count = int(row["count"])
                    kind = str(row["kind"])
                    adapter = str(row["adapter_id"])
                    module_kind_counts[kind] = module_kind_counts.get(kind, 0) + count
                    adapter_counts[adapter] = adapter_counts.get(adapter, 0) + count
                module_limit = min(
                    MAX_ROLE_LENS_CONTEXT_MODULES_PER_PROJECT,
                    MAX_ROLE_LENS_CONTEXT_MODULES - returned_modules,
                )
                if module_limit > 0:
                    module_rows = connection.execute(
                        """
                        SELECT m.module_id, m.name, m.kind, mo.relative_root, mo.adapter_id
                        FROM module_observations AS mo
                        JOIN modules AS m ON m.module_id = mo.module_id
                        WHERE mo.project_snapshot_id = ?
                        ORDER BY m.module_id
                        LIMIT ?
                        """,
                        (snapshot_id, module_limit),
                    ).fetchall()
                    modules = [
                        {
                            "module_id": str(row["module_id"]),
                            "name": str(row["name"]),
                            "kind": str(row["kind"]),
                            "relative_root": str(row["relative_root"]),
                            "adapter_id": str(row["adapter_id"]),
                        }
                        for row in module_rows
                    ]
                    returned_modules += len(modules)
                for row in connection.execute(
                    """
                    SELECT e.evidence_kind, COUNT(*) AS count
                    FROM project_snapshot_evidence AS pse
                    JOIN evidence AS e ON e.evidence_id = pse.evidence_id
                    WHERE pse.project_snapshot_id = ?
                    GROUP BY e.evidence_kind
                    ORDER BY e.evidence_kind
                    """,
                    (snapshot_id,),
                ).fetchall():
                    evidence_kind_counts[str(row["evidence_kind"])] = int(row["count"])
                project_evidence_count = sum(evidence_kind_counts.values())
                evidence_limit = min(
                    MAX_ROLE_LENS_CONTEXT_EVIDENCE_PER_PROJECT,
                    MAX_ROLE_LENS_CONTEXT_EVIDENCE_SAMPLES - returned_evidence_samples,
                )
                if evidence_limit > 0:
                    evidence_rows = connection.execute(
                        """
                        WITH representatives AS (
                            SELECT e.evidence_kind, MIN(e.evidence_id) AS evidence_id
                            FROM project_snapshot_evidence AS pse
                            JOIN evidence AS e ON e.evidence_id = pse.evidence_id
                            WHERE pse.project_snapshot_id = ?
                            GROUP BY e.evidence_kind
                        )
                        SELECT e.evidence_id, e.module_id, e.evidence_kind,
                               e.summary, e.commit_state
                        FROM representatives AS representative
                        JOIN evidence AS e ON e.evidence_id = representative.evidence_id
                        ORDER BY e.evidence_kind, e.evidence_id
                        LIMIT ?
                        """,
                        (snapshot_id, evidence_limit),
                    ).fetchall()
                    evidence_samples = [
                        {
                            "evidence_id": str(row["evidence_id"]),
                            "module_id": row["module_id"],
                            "evidence_kind": str(row["evidence_kind"]),
                            "summary": str(row["summary"]),
                            "commit_state": str(row["commit_state"]),
                        }
                        for row in evidence_rows
                    ]
                    returned_evidence_samples += len(evidence_samples)
            projects.append(
                {
                    "project_id": str(project["project_id"]),
                    "display_name": str(project["display_name"]),
                    "snapshot_disposition": str(project["snapshot_disposition"]),
                    "project_snapshot_id": snapshot_id,
                    "coverage_status": project["coverage_status"],
                    "module_count": project_module_count,
                    "module_kind_counts": module_kind_counts,
                    "adapter_counts": adapter_counts,
                    "modules": modules,
                    "modules_truncated": project_module_count > len(modules),
                    "evidence_count": project_evidence_count,
                    "evidence_kind_counts": evidence_kind_counts,
                    "evidence_samples": evidence_samples,
                    "evidence_samples_truncated": project_evidence_count > len(evidence_samples),
                }
            )
        return {
            "contract_version": ROLE_LENS_CONTEXT_CONTRACT_VERSION,
            "untrusted_data": True,
            "scan_run_id": scan_run_id,
            "projects": projects,
            "limits": {
                "maximum_projects": MAX_ROLE_LENS_CONTEXT_PROJECTS,
                "available_projects": total_projects,
                "returned_projects": len(projects),
                "projects_truncated": total_projects > len(projects),
                "maximum_modules": MAX_ROLE_LENS_CONTEXT_MODULES,
                "available_modules": available_modules,
                "returned_modules": returned_modules,
                "modules_truncated": available_modules > returned_modules,
                "maximum_evidence_samples": MAX_ROLE_LENS_CONTEXT_EVIDENCE_SAMPLES,
                "evidence_sample_strategy": "one_per_evidence_kind_per_project",
                "available_evidence": available_evidence,
                "returned_evidence_samples": returned_evidence_samples,
                "evidence_samples_truncated": available_evidence > returned_evidence_samples,
            },
        }
