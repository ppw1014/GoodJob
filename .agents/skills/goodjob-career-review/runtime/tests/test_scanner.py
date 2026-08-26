from __future__ import annotations

import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

import pytest

import goodjob.scanner as scanner_module
from goodjob.adapters import AnalysisResult, analyze_file
from goodjob.auth import AuthorizationRepository, AuthorizationRequest, generate_capability
from goodjob.cli import run
from goodjob.config import MAX_CONFIG_FILE_BYTES
from goodjob.db import Database
from goodjob.errors import InvalidInputError
from goodjob.paths import DataPaths
from goodjob.scanner import (
    IGNORE_PATTERN_SYNTAX,
    IgnoreMatcher,
    ProjectData,
    ProjectPlan,
    WorkspaceScanner,
    _open_regular_file,
)

RUNTIME_DIR = Path(__file__).resolve().parents[1]
NOTICE_VERSION = "goodjob-source-analysis-v1"


def _scope(workspace: Path) -> dict[str, object]:
    return {
        "workspace_path": str(workspace.resolve()),
        "allowed_categories": ["source_analysis"],
    }


def _send_json(process: subprocess.Popen[str], payload: dict[str, object]) -> dict[str, object]:
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(json.dumps(payload) + "\n")
    process.stdin.flush()
    response = json.loads(process.stdout.readline())
    assert isinstance(response, dict)
    return response


def _object_field(payload: dict[str, object], field: str) -> dict[str, object]:
    value = payload[field]
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _broker(data_dir: Path, *, extra_env: Mapping[str, str] | None = None) -> subprocess.Popen[str]:
    environment = {**os.environ, "PYTHONPATH": str(RUNTIME_DIR / "src")}
    if extra_env is not None:
        environment.update(extra_env)
    command = [sys.executable, "scripts/session.py", "--data-dir", str(data_dir)]
    if sys.platform == "win32":
        workspace = data_dir.parent / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        command.extend(["--preflight-workspace", str(workspace)])
    return subprocess.Popen(
        command,
        cwd=RUNTIME_DIR,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )


def _close_broker(process: subprocess.Popen[str]) -> None:
    assert process.stdin is not None
    process.stdin.close()
    assert process.wait(timeout=5) == 0
    assert process.stderr is not None
    stderr = process.stderr.read()
    if sys.platform == "win32":
        assert json.loads(stderr)["can_start_broker"] is True
    else:
        assert stderr == ""


def _authorize_source(
    process: subprocess.Popen[str], workspace: Path
) -> tuple[dict[str, object], str]:
    authorized = _send_json(
        process,
        {"op": "authorize_source_analysis", "workspace": str(workspace), "confirmed": True},
    )
    assert authorized["status"] == "ok"
    receipt = _object_field(authorized, "receipt")
    validated = _send_json(
        process,
        {
            "op": "validate_job_input",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt["authorization_receipt_id"],
            "job_input": {
                "contract_version": "job-input-v1",
                "target_role": "测试工程师",
                "jd_input": {"kind": "none"},
            },
        },
    )
    assert validated["status"] == "ok"
    job_input = _object_field(validated, "job_input")
    validation_sha256 = job_input["validation_sha256"]
    assert isinstance(validation_sha256, str)
    return authorized, validation_sha256


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True, text=True)
    _git(path, "config", "core.autocrlf", "false")


def _git(
    path: Path, *arguments: str, extra_env: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    if extra_env is not None:
        environment.update(extra_env)
    return subprocess.run(
        ["git", "-C", str(path), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )


def _git_wrapper(tmp_path: Path) -> tuple[dict[str, str], Path]:
    git_binary = shutil.which("git")
    assert git_binary is not None
    audit = tmp_path / "git-audit.log"
    wrapper = tmp_path / "git"
    wrapper.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {shlex.quote(str(audit))}\n"
        f'exec {shlex.quote(git_binary)} "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return (
        {
            "PATH": f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}",
        },
        audit,
    )


def _rewrite_git_pointer(pointer: Path, content: str) -> None:
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
        kernel32.SetFileAttributesW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
        kernel32.SetFileAttributesW.restype = ctypes.c_int
        if not kernel32.SetFileAttributesW(str(pointer), 0x00000080):
            raise ctypes.WinError(ctypes.get_last_error())
    pointer.write_text(content, encoding="utf-8")


def _direct_scanner(
    data_dir: Path, workspace: Path, *, git_executable: str | None = None
) -> tuple[WorkspaceScanner, str]:
    database = Database(DataPaths(data_dir))
    capability = generate_capability()
    request = AuthorizationRequest.from_values(
        receipt_kind="source_analysis",
        scope=_scope(workspace),
        notice_version=NOTICE_VERSION,
    )
    receipt = AuthorizationRepository(database).issue(
        capability=capability,
        request=request,
    )
    return (
        WorkspaceScanner(database, git_executable=git_executable),
        receipt.authorization_receipt_id,
    )


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows active-root regression")
def test_native_windows_active_root_restarts_each_discovery_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import goodjob.platform.detect as platform_detect
    from goodjob.platform import fs_windows, handles_windows

    monkeypatch.setattr(platform_detect, "NATIVE_WINDOWS_RELEASE_ENABLED", True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (workspace / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    scanner, receipt_id = _direct_scanner(
        tmp_path / "data", workspace, git_executable=sys.executable
    )

    with fs_windows.bind_authorized_root(workspace):
        directories, issues = scanner._walk_directories(workspace)
        assert directories == [workspace]
        assert issues == []
        assert scanner._non_git_manifest(workspace, ".")
        plans, _issues = scanner._discover(workspace, (), datetime.now(UTC).isoformat())
        assert [plan.identity_kind for plan in plans] == ["non_git_root"]

    result = scanner.scan(
        workspace_path=str(workspace),
        config_revision="windows-active-root-v1",
        authorization_receipt_id=receipt_id,
    )

    assert result.status == "completed"
    assert result.coverage["fresh_projects"] == 1
    assert handles_windows._RETAINED_OWNERS == []


def _write_project_exclusions(
    data_dir: Path,
    rules: list[dict[str, str]],
) -> None:
    lines = ["[goodjob]", "config_version = 1"]
    for rule in rules:
        lines.extend(
            [
                "",
                "[[goodjob.excluded_projects]]",
                *(f"{key} = {json.dumps(value)}" for key, value in rule.items()),
            ]
        )
    DataPaths(data_dir).config_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_known_binary_assets_are_normal_exclusions_but_invalid_source_is_reported(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "project"
    project.mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname='binary-assets'\n", encoding="utf-8")
    (project / "app-icon.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00binary-image")
    (project / "broken.py").write_bytes(b"def broken():\n    return '\xff'\n")

    scanner, receipt_id = _direct_scanner(tmp_path / "data", workspace)
    result = scanner.scan(
        workspace_path=str(workspace),
        config_revision="binary-assets-v1",
        authorization_receipt_id=receipt_id,
    )

    undecodable = [issue for issue in result.issues if issue.kind == "file_not_utf8"]
    assert [issue.relative_path for issue in undecodable] == ["broken.py"]
    assert result.coverage["excluded_by_category"] == {
        "binary_or_undecodable": 2,
    }


def test_scan_discovers_isolated_projects_and_keeps_sensitive_bytes_out_of_sqlite(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    repository = workspace / "repository"
    nested = repository / "nested"
    standalone = workspace / "standalone"
    nested.mkdir(parents=True)
    standalone.mkdir()
    _git_init(repository)
    _git_init(nested)
    (repository / ".gitignore").write_text("nested/\n", encoding="utf-8")
    (repository / "app.py").write_text("print('outer')\n", encoding="utf-8")
    (repository / "tests").mkdir()
    (repository / "tests" / "test_app.py").write_text(
        "def test_app() -> None:\n    pass\n", encoding="utf-8"
    )
    (repository / ".env").write_text("TOP_SECRET=should_not_persist\n", encoding="utf-8")
    (repository / ".envrc").write_text("export ENVRC_SECRET=should_not_persist\n", encoding="utf-8")
    (repository / "credentials.json").write_text(
        '{"token":"should_not_persist"}\n', encoding="utf-8"
    )
    (repository / "node_modules").mkdir()
    (repository / "node_modules" / "ignored.js").write_text("ignored\n", encoding="utf-8")
    (repository / "Secrets").mkdir()
    (repository / "Secrets" / "hidden.py").write_text(
        "SECRET_DIRECTORY_BYTES = 'should_not_persist'\n", encoding="utf-8"
    )
    (nested / "main.ts").write_text("export const nested = true\n", encoding="utf-8")
    (nested / ".gitignore").write_text("hidden.ts\n", encoding="utf-8")
    (nested / "hidden.ts").write_text("export const hidden = true\n", encoding="utf-8")
    (standalone / "pyproject.toml").write_text("[project]\nname='standalone'\n", encoding="utf-8")
    (standalone / "tool.py").write_text("def main() -> None:\n    pass\n", encoding="utf-8")
    outside_source = tmp_path / "outside.py"
    outside_source.write_text("print('outside')\n", encoding="utf-8")
    (repository / "outside.py").symlink_to(outside_source)

    data_dir = tmp_path / "data"
    broker = _broker(data_dir)
    authorized, validation_sha256 = _authorize_source(broker, workspace)
    assert authorized["status"] == "ok"
    receipt = _object_field(authorized, "receipt")
    scanned = _send_json(
        broker,
        {
            "op": "scan",
            "job_input_validation_sha256": validation_sha256,
            "workspace": str(workspace),
            "authorization_receipt_id": receipt["authorization_receipt_id"],
        },
    )
    _close_broker(broker)

    assert scanned["status"] == "ok"
    scan_run = _object_field(scanned, "scan_run")
    coverage = _object_field(scanned, "coverage")
    assert scan_run["status"] in {"completed", "partial"}
    assert coverage["fresh_projects"] == 3
    excluded = _object_field(coverage, "excluded_by_category")
    assert excluded["sensitive"] == 3
    hard_excluded = excluded["hard_excluded"]
    assert isinstance(hard_excluded, int)
    assert hard_excluded >= 1
    issues = scanned["issues"]
    assert isinstance(issues, list)
    assert any(
        isinstance(issue, dict) and issue.get("kind") == "symlink_outside_authorized_root"
        for issue in issues
    )

    connection = sqlite3.connect(data_dir / "goodjob.sqlite3")
    project_count = connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
    test_evidence_count = connection.execute(
        "SELECT COUNT(*) FROM evidence WHERE evidence_kind = 'test_definition'"
    ).fetchone()[0]
    stored_text = "\n".join(
        value
        for row in connection.execute("SELECT locator, summary FROM evidence")
        for value in row
    )
    stored_paths = {
        str(row[0]) for row in connection.execute("SELECT relative_path FROM source_artifacts")
    }
    connection.close()
    assert project_count == 3
    assert test_evidence_count == 1
    assert "TOP_SECRET" not in stored_text
    assert "should_not_persist" not in stored_text
    assert "hidden.ts" not in stored_text
    assert ".envrc" not in stored_paths
    assert "Secrets/hidden.py" not in stored_paths


def test_refresh_fast_reuses_metadata_but_verify_content_detects_same_stat_change(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "project"
    project.mkdir(parents=True)
    source = project / "main.py"
    (project / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    source.write_text("one\n", encoding="utf-8")
    data_dir = tmp_path / "data"
    broker = _broker(data_dir)
    authorized, validation_sha256 = _authorize_source(broker, workspace)
    receipt = _object_field(authorized, "receipt")
    scanned = _send_json(
        broker,
        {
            "op": "scan",
            "job_input_validation_sha256": validation_sha256,
            "workspace": str(workspace),
            "authorization_receipt_id": receipt["authorization_receipt_id"],
        },
    )
    workspace_id = _object_field(scanned, "scan_run")["workspace_id"]
    assert isinstance(workspace_id, str)

    original_stat = source.stat()
    source.write_text("two\n", encoding="utf-8")
    os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    fast = _send_json(
        broker,
        {
            "op": "refresh",
            "job_input_validation_sha256": validation_sha256,
            "workspace": str(workspace),
            "workspace_id": workspace_id,
            "authorization_receipt_id": receipt["authorization_receipt_id"],
            "change_detection_mode": "fast",
        },
    )
    verify = _send_json(
        broker,
        {
            "op": "refresh",
            "job_input_validation_sha256": validation_sha256,
            "workspace": str(workspace),
            "workspace_id": workspace_id,
            "authorization_receipt_id": receipt["authorization_receipt_id"],
            "change_detection_mode": "verify_content",
        },
    )
    _close_broker(broker)

    assert fast["status"] == "ok"
    assert verify["status"] == "ok"
    connection = sqlite3.connect(data_dir / "goodjob.sqlite3")
    revision_count = connection.execute(
        """
        SELECT COUNT(*)
        FROM source_revisions AS sr
        JOIN source_artifacts AS sa ON sa.artifact_id = sr.artifact_id
        WHERE sa.relative_path = 'main.py'
        """
    ).fetchone()[0]
    stale_count = connection.execute(
        "SELECT COUNT(*) FROM evidence_validities WHERE validity = 'stale'"
    ).fetchone()[0]
    reused_evidence_count = connection.execute(
        """
        SELECT COUNT(*)
        FROM evidence AS e
        JOIN source_revisions AS sr ON sr.source_revision_id = e.source_revision_id
        JOIN source_artifacts AS sa ON sa.artifact_id = sr.artifact_id
        WHERE sa.relative_path = 'main.py'
        """
    ).fetchone()[0]
    snapshot_references = connection.execute(
        """
        SELECT COUNT(*)
        FROM project_snapshot_source_revisions AS pssr
        JOIN source_revisions AS sr ON sr.source_revision_id = pssr.source_revision_id
        JOIN source_artifacts AS sa ON sa.artifact_id = sr.artifact_id
        WHERE sa.relative_path = 'main.py'
        """
    ).fetchone()[0]
    connection.close()
    assert revision_count == 2
    assert reused_evidence_count == 2
    assert snapshot_references == 3
    assert stale_count >= 1


def test_refresh_fast_rebuilds_evidence_when_an_untracked_file_becomes_committed(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    repository = workspace / "repository"
    repository.mkdir(parents=True)
    _git_init(repository)
    _git(repository, "config", "user.name", "Test Author")
    _git(repository, "config", "user.email", "author@example.test")
    (repository / "pyproject.toml").write_text("[project]\nname='state-change'\n", encoding="utf-8")
    _git(repository, "add", "pyproject.toml")
    _git(repository, "commit", "-m", "project baseline")
    source = repository / "main.py"
    source.write_text("def implementation() -> None:\n    pass\n", encoding="utf-8")

    data_dir = tmp_path / "data"
    broker = _broker(data_dir)
    authorized, validation_sha256 = _authorize_source(broker, workspace)
    receipt = _object_field(authorized, "receipt")
    initial = _send_json(
        broker,
        {
            "op": "scan",
            "job_input_validation_sha256": validation_sha256,
            "workspace": str(workspace),
            "authorization_receipt_id": receipt["authorization_receipt_id"],
        },
    )
    workspace_id = _object_field(initial, "scan_run")["workspace_id"]
    assert isinstance(workspace_id, str)
    original_stat = source.stat()
    _git(repository, "add", "main.py")
    _git(repository, "commit", "-m", "commit implementation")
    assert source.stat().st_mtime_ns == original_stat.st_mtime_ns

    refreshed = _send_json(
        broker,
        {
            "op": "refresh",
            "job_input_validation_sha256": validation_sha256,
            "workspace": str(workspace),
            "workspace_id": workspace_id,
            "authorization_receipt_id": receipt["authorization_receipt_id"],
            "change_detection_mode": "fast",
        },
    )
    _close_broker(broker)

    assert refreshed["status"] == "ok"
    refresh_run = _object_field(refreshed, "scan_run")
    assert refresh_run["status"] == "completed"
    refresh_run_id = refresh_run["scan_run_id"]
    assert isinstance(refresh_run_id, str)
    connection = sqlite3.connect(data_dir / "goodjob.sqlite3")
    states = {
        str(row[0])
        for row in connection.execute(
            """
            SELECT DISTINCT evidence.commit_state
            FROM evidence
            JOIN source_revisions AS revision
              ON revision.source_revision_id = evidence.source_revision_id
            JOIN source_artifacts AS artifact ON artifact.artifact_id = revision.artifact_id
            WHERE artifact.relative_path = 'main.py'
            """
        )
    }
    revision_count = connection.execute(
        """
        SELECT COUNT(*)
        FROM source_revisions AS revision
        JOIN source_artifacts AS artifact ON artifact.artifact_id = revision.artifact_id
        WHERE artifact.relative_path = 'main.py'
        """
    ).fetchone()[0]
    validity_rows = connection.execute(
        """
        SELECT evidence.commit_state, validity.validity, replacement.commit_state
        FROM evidence_validities AS validity
        JOIN evidence ON evidence.evidence_id = validity.evidence_id
        JOIN source_revisions AS revision
          ON revision.source_revision_id = evidence.source_revision_id
        JOIN source_artifacts AS artifact ON artifact.artifact_id = revision.artifact_id
        LEFT JOIN evidence AS replacement
          ON replacement.evidence_id = validity.replacement_evidence_id
        WHERE validity.scan_run_id = ? AND artifact.relative_path = 'main.py'
        """,
        (refresh_run_id,),
    ).fetchall()
    connection.close()
    assert states == {"untracked", "committed"}
    assert revision_count == 1
    assert validity_rows
    assert all(
        (state == "committed") == (validity == "current") for state, validity, _ in validity_rows
    )
    assert all(
        replacement == "committed"
        for state, validity, replacement in validity_rows
        if state == "untracked" and validity == "stale"
    )


def test_scan_rejects_a_receipt_scope_for_a_different_workspace_before_creating_a_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    authorized_workspace = tmp_path / "authorized"
    other_workspace = tmp_path / "other"
    authorized_workspace.mkdir()
    other_workspace.mkdir()
    (other_workspace / "pyproject.toml").write_text("[project]\nname='other'\n", encoding="utf-8")
    data_paths = DataPaths.from_argument(str(tmp_path / "data"))
    capability = generate_capability()
    request = AuthorizationRequest.from_values(
        receipt_kind="source_analysis",
        scope=_scope(authorized_workspace),
        notice_version=NOTICE_VERSION,
    )
    receipt = AuthorizationRepository(Database(data_paths)).issue(
        capability=capability,
        request=request,
    )
    read_fd: int | None = None
    if sys.platform == "win32":
        from goodjob.platform.capability_windows import WindowsTransferPipe, write_handle

        transfer = WindowsTransferPipe.create()
        try:
            write_handle(transfer.parent_write.value, capability)
            transfer.parent_write.close()
            capability_argument = ["--capability-handle", str(transfer.child_read.detach())]
        except BaseException:
            transfer.close()
            raise
    else:
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, capability)
        finally:
            os.close(write_fd)
        capability_argument = ["--capability-fd", str(read_fd)]
    try:
        exit_code = run(
            [
                "--data-dir",
                str(data_paths.root),
                "scan",
                "--workspace",
                str(other_workspace),
                "--config-revision",
                "goodjob-scan-config-v1",
                "--authorization-receipt-id",
                receipt.authorization_receipt_id,
                "--receipt-kind",
                "source_analysis",
                "--scope-json",
                json.dumps(_scope(authorized_workspace)),
                "--notice-version",
                NOTICE_VERSION,
                *capability_argument,
            ]
        )
    finally:
        if read_fd is not None:
            os.close(read_fd)
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "authorization scope does not match" in captured.err
    with Database(data_paths).read_connection() as connection:
        scan_runs = connection.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0]
    assert scan_runs == 0


def test_scan_with_an_authorized_missing_root_performs_no_workspace_or_run_write(
    tmp_path: Path,
) -> None:
    missing_workspace = tmp_path / "missing"
    data_dir = tmp_path / "data"
    broker = _broker(data_dir)
    validation_sha256 = ""
    if sys.platform != "win32":
        authorized, validation_sha256 = _authorize_source(broker, missing_workspace)
    else:
        authorized = _send_json(
            broker,
            {
                "op": "authorize_source_analysis",
                "workspace": str(missing_workspace),
                "confirmed": True,
            },
        )
    if sys.platform == "win32":
        _close_broker(broker)
        assert authorized["status"] == "error"
        database_file = data_dir / "goodjob.sqlite3"
        if database_file.exists():
            connection = sqlite3.connect(database_file)
            workspace_count = connection.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0]
            scan_run_count = connection.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0]
            connection.close()
            assert workspace_count == 0
            assert scan_run_count == 0
        return
    receipt = _object_field(authorized, "receipt")
    response = _send_json(
        broker,
        {
            "op": "scan",
            "job_input_validation_sha256": validation_sha256,
            "workspace": str(missing_workspace),
            "authorization_receipt_id": receipt["authorization_receipt_id"],
        },
    )
    _close_broker(broker)

    assert response["status"] == "error"
    assert response["code"] == "invalid_input"
    connection = sqlite3.connect(data_dir / "goodjob.sqlite3")
    workspace_count = connection.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0]
    scan_run_count = connection.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0]
    connection.close()
    assert workspace_count == 0
    assert scan_run_count == 0


def test_descriptor_reader_rejects_a_file_or_directory_symlink(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "secret.py").write_text("secret\n", encoding="utf-8")
    (workspace / "file.py").symlink_to(outside / "secret.py")
    (workspace / "directory").symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        _open_regular_file(workspace, "file.py")
    with pytest.raises(OSError):
        _open_regular_file(workspace, "directory/secret.py")
    with pytest.raises((OSError, InvalidInputError)):
        _open_regular_file(workspace, str((outside / "secret.py").resolve()))
    with pytest.raises((OSError, InvalidInputError)):
        _open_regular_file(workspace, "../outside/secret.py")


def test_adapter_failures_and_sql_plan_boundaries_are_visible(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "project"
    project.mkdir(parents=True)
    (project / "migrations").mkdir()
    (project / "pyproject.toml").write_text(
        "[project]\nname='adapter-boundaries'\n", encoding="utf-8"
    )
    (project / "README.md").write_text("# Adapter boundaries\n", encoding="utf-8")
    (project / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    (project / "package.json").write_text("{broken", encoding="utf-8")
    (project / "plan.sql").write_text("CREATE TABLE planned_only (id INTEGER);\n", encoding="utf-8")
    (project / "migrations" / "001.sql").write_text(
        "CREATE TABLE delivered (id INTEGER PRIMARY KEY);\n", encoding="utf-8"
    )

    data_dir = tmp_path / "data"
    broker = _broker(data_dir)
    authorized, validation_sha256 = _authorize_source(broker, workspace)
    receipt = _object_field(authorized, "receipt")
    scanned = _send_json(
        broker,
        {
            "op": "scan",
            "job_input_validation_sha256": validation_sha256,
            "workspace": str(workspace),
            "authorization_receipt_id": receipt["authorization_receipt_id"],
        },
    )
    _close_broker(broker)

    scan_run = _object_field(scanned, "scan_run")
    assert scan_run["status"] == "partial"
    issues = cast(list[dict[str, object]], scanned["issues"])
    assert {issue["kind"] for issue in issues} >= {
        "source_parse_failed",
        "manifest_parse_failed",
    }
    connection = sqlite3.connect(data_dir / "goodjob.sqlite3")
    evidence_by_path = {
        str(path): (
            {str(kind) for kind in str(kinds).split(",") if kind} if kinds is not None else set()
        )
        for path, kinds in connection.execute(
            """
            SELECT artifact.relative_path, GROUP_CONCAT(evidence.evidence_kind)
            FROM source_artifacts AS artifact
            LEFT JOIN source_revisions AS revision
              ON revision.artifact_id = artifact.artifact_id
            LEFT JOIN evidence ON evidence.source_revision_id = revision.source_revision_id
            GROUP BY artifact.relative_path
            """
        )
    }
    root_adapter = connection.execute(
        """
        SELECT observation.adapter_id
        FROM module_observations AS observation
        WHERE observation.relative_root = '.'
        """
    ).fetchone()[0]
    connection.close()
    assert evidence_by_path["broken.py"] == set()
    assert evidence_by_path["package.json"] == set()
    assert evidence_by_path["plan.sql"] == {"documentation"}
    assert {"migration_definition", "schema_definition"} <= evidence_by_path["migrations/001.sql"]
    assert root_adapter == "python"


def test_refresh_marks_confirmed_dead_run_interrupted_and_never_uses_it_as_fast_baseline(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "project"
    project.mkdir(parents=True)
    source = project / "main.py"
    (project / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    source.write_text("one\n", encoding="utf-8")
    data_dir = tmp_path / "data"
    broker = _broker(data_dir)
    authorized, validation_sha256 = _authorize_source(broker, workspace)
    receipt = _object_field(authorized, "receipt")
    initial = _send_json(
        broker,
        {
            "op": "scan",
            "job_input_validation_sha256": validation_sha256,
            "workspace": str(workspace),
            "authorization_receipt_id": receipt["authorization_receipt_id"],
        },
    )
    initial_run = _object_field(initial, "scan_run")["scan_run_id"]
    workspace_id = _object_field(initial, "scan_run")["workspace_id"]
    assert isinstance(initial_run, str)
    assert isinstance(workspace_id, str)
    connection = sqlite3.connect(data_dir / "goodjob.sqlite3")
    connection.execute(
        """
        UPDATE scan_runs
        SET status = 'running', finished_at = NULL,
            owner_process_identity = 'pid:999999;started:Thu Jan  1 00:00:00 1970'
        WHERE scan_run_id = ?
        """,
        (initial_run,),
    )
    connection.commit()
    connection.close()
    original_stat = source.stat()
    source.write_text("two\n", encoding="utf-8")
    os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    refreshed = _send_json(
        broker,
        {
            "op": "refresh",
            "job_input_validation_sha256": validation_sha256,
            "workspace": str(workspace),
            "workspace_id": workspace_id,
            "authorization_receipt_id": receipt["authorization_receipt_id"],
            "change_detection_mode": "fast",
        },
    )
    _close_broker(broker)

    assert refreshed["status"] == "ok"
    connection = sqlite3.connect(data_dir / "goodjob.sqlite3")
    old_status = connection.execute(
        "SELECT status FROM scan_runs WHERE scan_run_id = ?", (initial_run,)
    ).fetchone()[0]
    revisions = connection.execute(
        """
        SELECT COUNT(*)
        FROM source_revisions AS sr
        JOIN source_artifacts AS sa ON sa.artifact_id = sr.artifact_id
        WHERE sa.relative_path = 'main.py'
        """
    ).fetchone()[0]
    connection.close()
    assert old_status == "interrupted"
    assert revisions == 2


def test_scan_recovery_probe_failure_preserves_running_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE scan_runs (
            scan_run_id TEXT PRIMARY KEY,
            owner_process_identity TEXT NOT NULL,
            status TEXT NOT NULL,
            finished_at TEXT
        )
        """
    )
    connection.execute(
        """
        INSERT INTO scan_runs(scan_run_id, owner_process_identity, status)
        VALUES ('running-scan', 'pid:321;started:100', 'running')
        """
    )

    def fail_owner_probe(_identity: str) -> bool:
        raise OSError("injected owner liveness failure")

    monkeypatch.setattr(scanner_module, "owner_process_stopped", fail_owner_probe)

    with pytest.raises(OSError, match="owner liveness failure"):
        WorkspaceScanner._recover_interrupted_runs(connection, "2026-08-16T00:00:00Z")

    state = connection.execute(
        "SELECT status, finished_at FROM scan_runs WHERE scan_run_id = 'running-scan'"
    ).fetchone()
    assert state is not None
    assert tuple(state) == ("running", None)
    connection.close()


def test_internal_git_history_uses_remote_head_and_persists_bounded_commit_evidence(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    repository = workspace / "repository"
    repository.mkdir(parents=True)
    _git_init(repository)
    _git(repository, "config", "user.name", "Test Author")
    _git(repository, "config", "user.email", "author@example.test")
    _git(repository, "branch", "-M", "main")
    (repository / "pyproject.toml").write_text("[project]\nname='history'\n", encoding="utf-8")
    (repository / "main.py").write_text("print('main')\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "main baseline")
    main_commit = _git(repository, "rev-parse", "HEAD").stdout.strip()
    _git(repository, "checkout", "-b", "feature")
    (repository / "main.py").write_text("print('feature')\n", encoding="utf-8")
    _git(repository, "add", "main.py")
    _git(repository, "commit", "-m", "feature implementation")
    feature_commit = _git(repository, "rev-parse", "HEAD").stdout.strip()
    _git(repository, "update-ref", "refs/remotes/origin/main", main_commit)
    _git(repository, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")

    data_dir = tmp_path / "data"
    audit_env, audit = _git_wrapper(tmp_path)
    broker = _broker(data_dir, extra_env=audit_env)
    authorized, validation_sha256 = _authorize_source(broker, workspace)
    receipt = _object_field(authorized, "receipt")
    scanned = _send_json(
        broker,
        {
            "op": "scan",
            "job_input_validation_sha256": validation_sha256,
            "workspace": str(workspace),
            "authorization_receipt_id": receipt["authorization_receipt_id"],
        },
    )
    _close_broker(broker)

    assert scanned["status"] == "ok"
    coverage = _object_field(scanned, "coverage")
    assert coverage["git_history_evidence"] == 2
    history_basis = _object_field(coverage, "history_basis_by_worktree")
    assert history_basis[str(repository)] == "head_plus_remote_head"
    assert not audit.exists(), "the scanner must not resolve Git through inherited PATH"

    connection = sqlite3.connect(data_dir / "goodjob.sqlite3")
    rows = connection.execute(
        """
        SELECT evidence_id, locator, summary
        FROM evidence
        WHERE origin_kind = 'git_commit' AND evidence_kind = 'git_history'
        ORDER BY locator
        """
    ).fetchall()
    basis = connection.execute("SELECT history_basis FROM worktree_observations").fetchone()[0]
    validity_count = connection.execute(
        """
        SELECT COUNT(*) FROM evidence_validities AS ev
        JOIN evidence AS e ON e.evidence_id = ev.evidence_id
        WHERE e.origin_kind = 'git_commit' AND ev.validity = 'current'
        """
    ).fetchone()[0]
    connection.close()
    locators = [json.loads(str(row[1])) for row in rows]
    assert basis == "head_plus_remote_head"
    assert {locator["commit"] for locator in locators} == {main_commit, feature_commit}
    assert all(locator["changed_paths"] for locator in locators)
    assert all("print('" not in str(row[2]) for row in rows)
    assert validity_count == 2


def test_root_external_linked_worktree_requires_two_stage_authorization_and_never_reads_history(
    tmp_path: Path,
) -> None:
    outside_repository = tmp_path / "outside-repository"
    workspace = tmp_path / "workspace"
    linked_worktree = workspace / "linked"
    outside_repository.mkdir()
    workspace.mkdir()
    _git_init(outside_repository)
    _git(outside_repository, "config", "user.name", "Test Author")
    _git(outside_repository, "config", "user.email", "author@example.test")
    (outside_repository / "pyproject.toml").write_text(
        "[project]\nname='linked'\n", encoding="utf-8"
    )
    (outside_repository / "main.py").write_text("print('linked')\n", encoding="utf-8")
    _git(outside_repository, "add", ".")
    _git(outside_repository, "commit", "-m", "linked baseline")
    _git(outside_repository, "worktree", "add", str(linked_worktree))
    pointer = linked_worktree / ".git"
    assert pointer.is_file()

    data_dir = tmp_path / "data"
    audit_env, audit = _git_wrapper(tmp_path)
    broker = _broker(data_dir, extra_env=audit_env)
    authorized, validation_sha256 = _authorize_source(broker, workspace)
    source_receipt = _object_field(authorized, "receipt")
    denied = _send_json(
        broker,
        {
            "op": "scan",
            "job_input_validation_sha256": validation_sha256,
            "workspace": str(workspace),
            "authorization_receipt_id": source_receipt["authorization_receipt_id"],
        },
    )
    assert denied["status"] == "ok"
    denied_issues = denied["issues"]
    assert isinstance(denied_issues, list)
    assert any(
        isinstance(issue, dict) and issue.get("kind") == "external_git_authorization_required"
        for issue in denied_issues
    )

    inspected = _send_json(
        broker,
        {
            "op": "inspect_external_git_candidate",
            "workspace": str(workspace),
            "git_pointer": str(pointer),
            "authorization_receipt_id": source_receipt["authorization_receipt_id"],
        },
    )
    assert inspected["status"] == "ok"
    candidate = _object_field(inspected, "candidate")
    assert candidate["marker_kind"] == "file"
    assert candidate["git_pointer_path"] == str(pointer)
    assert candidate["common_dir_candidate"] is None

    relation_authorized = _send_json(
        broker,
        {
            "op": "authorize_external_git_relation_probe",
            "workspace": str(workspace),
            "git_pointer": str(pointer),
            "confirmed": True,
        },
    )
    relation_receipt = _object_field(relation_authorized, "receipt")
    relation_scope = json.loads(str(relation_receipt["scope_descriptor"]))
    assert relation_scope["marker_kind"] == "file"
    assert relation_scope["git_dir_candidate"] == candidate["git_dir_candidate"]
    assert relation_scope["common_dir_candidate"] is None
    original_pointer = pointer.read_text(encoding="utf-8")
    _rewrite_git_pointer(pointer, f"gitdir: {tmp_path / 'replacement-git-dir'}\n")
    rejected_replacement = _send_json(
        broker,
        {
            "op": "probe_external_git_relation",
            "workspace": str(workspace),
            "git_pointer": str(pointer),
            "authorization_receipt_id": source_receipt["authorization_receipt_id"],
            "relation_authorization_receipt_id": relation_receipt["authorization_receipt_id"],
        },
    )
    assert rejected_replacement["status"] == "error"
    assert rejected_replacement["code"] == "invalid_input"
    assert not audit.exists()
    _rewrite_git_pointer(pointer, original_pointer)
    probed = _send_json(
        broker,
        {
            "op": "probe_external_git_relation",
            "workspace": str(workspace),
            "git_pointer": str(pointer),
            "authorization_receipt_id": source_receipt["authorization_receipt_id"],
            "relation_authorization_receipt_id": relation_receipt["authorization_receipt_id"],
        },
    )
    assert probed["status"] == "ok"
    relation = _object_field(probed, "relation")
    assert relation["git_pointer_path"] == str(pointer)
    assert relation["marker_kind"] == "file"
    assert isinstance(relation["git_dir"], str)
    assert isinstance(relation["common_dir"], str)
    assert isinstance(relation["git_dir_device"], int)
    assert isinstance(relation["git_dir_inode"], int)
    assert not audit.exists()
    rejected_metadata = _send_json(
        broker,
        {
            "op": "authorize_external_git_metadata",
            "workspace": str(workspace),
            "git_pointer": str(pointer),
            "git_dir": str(tmp_path / "forged-git-dir"),
            "common_dir": relation["common_dir"],
            "confirmed": True,
        },
    )
    assert rejected_metadata["status"] == "error"
    assert rejected_metadata["code"] == "invalid_input"
    metadata_authorized = _send_json(
        broker,
        {
            "op": "authorize_external_git_metadata",
            "workspace": str(workspace),
            "git_pointer": str(pointer),
            "git_dir": relation["git_dir"],
            "common_dir": relation["common_dir"],
            "confirmed": True,
        },
    )
    metadata_receipt = _object_field(metadata_authorized, "receipt")
    metadata_scope = json.loads(str(metadata_receipt["scope_descriptor"]))
    assert metadata_scope["marker_kind"] == "file"
    assert metadata_scope["git_dir_device"] == relation["git_dir_device"]
    assert metadata_scope["git_dir_inode"] == relation["git_dir_inode"]
    _rewrite_git_pointer(pointer, f"gitdir: {tmp_path / 'post-grant-replacement'}\n")
    rejected_after_metadata = _send_json(
        broker,
        {
            "op": "scan",
            "job_input_validation_sha256": validation_sha256,
            "workspace": str(workspace),
            "authorization_receipt_id": source_receipt["authorization_receipt_id"],
            "external_git_metadata_receipt_ids": [metadata_receipt["authorization_receipt_id"]],
        },
    )
    rejected_after_metadata_issues = rejected_after_metadata["issues"]
    assert isinstance(rejected_after_metadata_issues, list)
    assert any(
        isinstance(issue, dict) and issue.get("kind") == "external_git_relation_mismatch"
        for issue in rejected_after_metadata_issues
    )
    assert not audit.exists()
    _rewrite_git_pointer(pointer, original_pointer)
    (outside_repository / ".git" / "config").write_text(
        "this is intentionally invalid Git config\n", encoding="utf-8"
    )
    accepted = _send_json(
        broker,
        {
            "op": "scan",
            "job_input_validation_sha256": validation_sha256,
            "workspace": str(workspace),
            "authorization_receipt_id": source_receipt["authorization_receipt_id"],
            "external_git_metadata_receipt_ids": [metadata_receipt["authorization_receipt_id"]],
        },
    )
    _close_broker(broker)

    assert accepted["status"] == "ok"
    accepted_coverage = _object_field(accepted, "coverage")
    assert accepted_coverage["fresh_projects"] == 1
    assert accepted_coverage["git_history_evidence"] == 0
    history_basis = _object_field(accepted_coverage, "history_basis_by_worktree")
    assert history_basis[str(linked_worktree)] == "external_metadata_only"
    external_metadata = _object_field(accepted_coverage, "external_git_metadata")
    linked_metadata = external_metadata[str(linked_worktree)]
    assert isinstance(linked_metadata, dict)
    assert linked_metadata["git_dir"] == relation["git_dir"]
    assert linked_metadata["read_fields"] == ["gitdir", "commondir", "head", "ref"]
    accepted_issues = accepted["issues"]
    assert isinstance(accepted_issues, list)
    assert any(
        isinstance(issue, dict) and issue.get("kind") == "external_git_history_unavailable"
        for issue in accepted_issues
    )
    assert not audit.exists(), "external metadata mode must not invoke inherited Git"

    connection = sqlite3.connect(data_dir / "goodjob.sqlite3")
    observation = connection.execute(
        """
        SELECT history_basis, external_git_dir, external_common_dir, external_metadata_receipt_id
        FROM worktree_observations
        """
    ).fetchone()
    history_count = connection.execute(
        "SELECT COUNT(*) FROM evidence WHERE evidence_kind = 'git_history'"
    ).fetchone()[0]
    source_commit_states = {
        str(row[0])
        for row in connection.execute(
            "SELECT DISTINCT commit_state FROM evidence WHERE origin_kind = 'source_revision'"
        )
    }
    connection.close()
    assert observation == (
        "external_metadata_only",
        relation["git_dir"],
        relation["common_dir"],
        metadata_receipt["authorization_receipt_id"],
    )
    assert history_count == 0
    assert source_commit_states == {"not_applicable"}


def test_root_internal_linked_worktree_is_grouped_without_external_authorization(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    primary = workspace / "primary"
    linked = workspace / "linked"
    primary.mkdir(parents=True)
    _git_init(primary)
    _git(primary, "config", "user.name", "Test Author")
    _git(primary, "config", "user.email", "author@example.test")
    (primary / "pyproject.toml").write_text("[project]\nname='internal-linked'\n", encoding="utf-8")
    (primary / "main.py").write_text("print('primary')\n", encoding="utf-8")
    _git(primary, "add", ".")
    _git(primary, "commit", "-m", "initial")
    _git(primary, "worktree", "add", "-b", "linked-branch", str(linked))

    data_dir = tmp_path / "data"
    broker = _broker(data_dir)
    authorized, validation_sha256 = _authorize_source(broker, workspace)
    receipt = _object_field(authorized, "receipt")
    scanned = _send_json(
        broker,
        {
            "op": "scan",
            "job_input_validation_sha256": validation_sha256,
            "workspace": str(workspace),
            "authorization_receipt_id": receipt["authorization_receipt_id"],
        },
    )
    _close_broker(broker)

    assert scanned["status"] == "ok"
    coverage = _object_field(scanned, "coverage")
    assert coverage["fresh_projects"] == 1
    assert coverage["worktrees"] == 2
    assert coverage["external_git_metadata"] == {}
    issues = scanned["issues"]
    assert isinstance(issues, list)
    assert not any(
        isinstance(issue, dict) and issue.get("kind") == "external_git_authorization_required"
        for issue in issues
    )
    connection = sqlite3.connect(data_dir / "goodjob.sqlite3")
    project_count = connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
    worktree_count = connection.execute("SELECT COUNT(*) FROM worktrees").fetchone()[0]
    connection.close()
    assert project_count == 1
    assert worktree_count == 2


def test_git_directory_with_external_commondir_uses_the_same_candidate_bound_protocol(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    repository = workspace / "repository"
    repository.mkdir(parents=True)
    _git_init(repository)
    _git(repository, "config", "user.name", "Test Author")
    _git(repository, "config", "user.email", "author@example.test")
    _git(repository, "branch", "-M", "main")
    (repository / "pyproject.toml").write_text(
        "[project]\nname='external-common'\n", encoding="utf-8"
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "external common baseline")

    git_dir = repository / ".git"
    common_dir = tmp_path / "outside-common"
    common_dir.mkdir()
    for child in tuple(git_dir.iterdir()):
        if child.name not in {"HEAD", "index"}:
            shutil.move(str(child), common_dir / child.name)
    (git_dir / "commondir").write_text(str(common_dir) + "\n", encoding="utf-8")

    data_dir = tmp_path / "data"
    broker = _broker(data_dir)
    authorized, validation_sha256 = _authorize_source(broker, workspace)
    source_receipt = _object_field(authorized, "receipt")
    denied = _send_json(
        broker,
        {
            "op": "scan",
            "job_input_validation_sha256": validation_sha256,
            "workspace": str(workspace),
            "authorization_receipt_id": source_receipt["authorization_receipt_id"],
        },
    )
    assert any(
        isinstance(issue, dict) and issue.get("kind") == "external_git_authorization_required"
        for issue in cast(list[object], denied["issues"])
    )

    inspected = _send_json(
        broker,
        {
            "op": "inspect_external_git_candidate",
            "workspace": str(workspace),
            "git_pointer": str(git_dir),
            "authorization_receipt_id": source_receipt["authorization_receipt_id"],
        },
    )
    candidate = _object_field(inspected, "candidate")
    assert candidate["marker_kind"] == "directory"
    assert candidate["git_dir_candidate"] == str(git_dir)
    assert candidate["common_dir_candidate"] == str(common_dir)
    relation_authorized = _send_json(
        broker,
        {
            "op": "authorize_external_git_relation_probe",
            "workspace": str(workspace),
            "git_pointer": str(git_dir),
            "confirmed": True,
        },
    )
    relation_receipt = _object_field(relation_authorized, "receipt")
    probed = _send_json(
        broker,
        {
            "op": "probe_external_git_relation",
            "workspace": str(workspace),
            "git_pointer": str(git_dir),
            "authorization_receipt_id": source_receipt["authorization_receipt_id"],
            "relation_authorization_receipt_id": relation_receipt["authorization_receipt_id"],
        },
    )
    relation = _object_field(probed, "relation")
    assert relation["marker_kind"] == "directory"
    assert relation["git_dir"] == str(git_dir)
    assert relation["common_dir"] == str(common_dir)
    metadata_authorized = _send_json(
        broker,
        {
            "op": "authorize_external_git_metadata",
            "workspace": str(workspace),
            "git_pointer": str(git_dir),
            "git_dir": str(git_dir),
            "common_dir": str(common_dir),
            "confirmed": True,
        },
    )
    metadata_receipt = _object_field(metadata_authorized, "receipt")
    (common_dir / "config").write_text("invalid config is never read\n", encoding="utf-8")
    accepted = _send_json(
        broker,
        {
            "op": "scan",
            "job_input_validation_sha256": validation_sha256,
            "workspace": str(workspace),
            "authorization_receipt_id": source_receipt["authorization_receipt_id"],
            "external_git_metadata_receipt_ids": [metadata_receipt["authorization_receipt_id"]],
        },
    )
    _close_broker(broker)

    assert accepted["status"] == "ok"
    coverage = _object_field(accepted, "coverage")
    assert coverage["fresh_projects"] == 1
    assert coverage["git_history_evidence"] == 0
    metadata = _object_field(coverage, "external_git_metadata")
    assert str(repository) in metadata


def test_git_history_falls_back_to_main_master_or_head_only_and_handles_detached_head(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    repositories = {
        "main": "main",
        "master": "master",
        "custom": "release",
    }
    for name, branch in repositories.items():
        repository = workspace / name
        repository.mkdir(parents=True)
        _git_init(repository)
        _git(repository, "config", "user.name", "Test Author")
        _git(repository, "config", "user.email", "author@example.test")
        _git(repository, "branch", "-M", branch)
        (repository / "pyproject.toml").write_text(f"[project]\nname='{name}'\n", encoding="utf-8")
        _git(repository, "add", ".")
        _git(repository, "commit", "-m", f"{name} baseline")

    data_dir = tmp_path / "data"
    broker = _broker(data_dir)
    authorized, validation_sha256 = _authorize_source(broker, workspace)
    receipt = _object_field(authorized, "receipt")
    initial = _send_json(
        broker,
        {
            "op": "scan",
            "job_input_validation_sha256": validation_sha256,
            "workspace": str(workspace),
            "authorization_receipt_id": receipt["authorization_receipt_id"],
        },
    )
    initial_run = _object_field(initial, "scan_run")
    workspace_id = initial_run["workspace_id"]
    assert isinstance(workspace_id, str)

    connection = sqlite3.connect(data_dir / "goodjob.sqlite3")
    initial_basis = dict(
        connection.execute(
            """
            SELECT w.canonical_root, wo.history_basis
            FROM worktree_observations AS wo
            JOIN worktrees AS w ON w.worktree_id = wo.worktree_id
            WHERE wo.scan_run_id = ?
            """,
            (initial_run["scan_run_id"],),
        ).fetchall()
    )
    connection.close()
    assert initial_basis[str(workspace / "main")] == "head_plus_main"
    assert initial_basis[str(workspace / "master")] == "head_plus_master"
    assert initial_basis[str(workspace / "custom")] == "head_only_no_default_ref"

    _git(workspace / "main", "checkout", "--detach", "HEAD")
    refreshed = _send_json(
        broker,
        {
            "op": "refresh",
            "job_input_validation_sha256": validation_sha256,
            "workspace": str(workspace),
            "workspace_id": workspace_id,
            "authorization_receipt_id": receipt["authorization_receipt_id"],
            "change_detection_mode": "verify_content",
        },
    )
    _close_broker(broker)

    assert refreshed["status"] == "ok"
    refresh_run = _object_field(refreshed, "scan_run")
    connection = sqlite3.connect(data_dir / "goodjob.sqlite3")
    detached_basis = connection.execute(
        """
        SELECT wo.history_basis
        FROM worktree_observations AS wo
        JOIN worktrees AS w ON w.worktree_id = wo.worktree_id
        WHERE wo.scan_run_id = ? AND w.canonical_root = ?
        """,
        (refresh_run["scan_run_id"], str(workspace / "main")),
    ).fetchone()[0]
    connection.close()
    assert detached_basis == "head_only_detached"


def test_git_history_checks_every_remote_head_before_declaring_a_unique_default(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    repository = workspace / "repository"
    repository.mkdir(parents=True)
    _git_init(repository)
    _git(repository, "config", "user.name", "Test Author")
    _git(repository, "config", "user.email", "author@example.test")
    _git(repository, "branch", "-M", "main")
    (repository / "pyproject.toml").write_text("[project]\nname='remote-heads'\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "remote head baseline")
    head = _git(repository, "rev-parse", "HEAD").stdout.strip()
    _git(repository, "update-ref", "refs/remotes/origin/main", head)
    _git(repository, "update-ref", "refs/remotes/other/feature", head)
    for index in range(33):
        _git(
            repository,
            "symbolic-ref",
            f"refs/remotes/a{index:02d}/HEAD",
            "refs/remotes/origin/main",
        )
    _git(
        repository,
        "symbolic-ref",
        "refs/remotes/z-other/HEAD",
        "refs/remotes/other/feature",
    )

    data_dir = tmp_path / "data"
    broker = _broker(data_dir)
    authorized, validation_sha256 = _authorize_source(broker, workspace)
    receipt = _object_field(authorized, "receipt")
    scanned = _send_json(
        broker,
        {
            "op": "scan",
            "job_input_validation_sha256": validation_sha256,
            "workspace": str(workspace),
            "authorization_receipt_id": receipt["authorization_receipt_id"],
        },
    )
    _close_broker(broker)

    assert scanned["status"] == "ok"
    coverage = _object_field(scanned, "coverage")
    basis = _object_field(coverage, "history_basis_by_worktree")
    assert basis[str(repository)] == "head_only_ambiguous_remote_head"
    assert coverage["git_history_evidence"] == 1


def test_git_history_keeps_the_head_but_excludes_non_head_commits_older_than_180_days(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    repository = workspace / "repository"
    repository.mkdir(parents=True)
    _git_init(repository)
    _git(repository, "config", "user.name", "Test Author")
    _git(repository, "config", "user.email", "author@example.test")
    _git(repository, "branch", "-M", "main")
    (repository / "pyproject.toml").write_text("[project]\nname='window'\n", encoding="utf-8")
    _git(repository, "add", ".")
    old_time = (datetime.now(UTC) - timedelta(days=181)).strftime("%Y-%m-%dT%H:%M:%S+0000")
    _git(
        repository,
        "commit",
        "-m",
        "old baseline",
        extra_env={"GIT_AUTHOR_DATE": old_time, "GIT_COMMITTER_DATE": old_time},
    )
    old_commit = _git(repository, "rev-parse", "HEAD").stdout.strip()
    (repository / "main.py").write_text("print('recent')\n", encoding="utf-8")
    _git(repository, "add", "main.py")
    _git(repository, "commit", "-m", "recent implementation")
    recent_commit = _git(repository, "rev-parse", "HEAD").stdout.strip()

    data_dir = tmp_path / "data"
    broker = _broker(data_dir)
    authorized, validation_sha256 = _authorize_source(broker, workspace)
    receipt = _object_field(authorized, "receipt")
    scanned = _send_json(
        broker,
        {
            "op": "scan",
            "job_input_validation_sha256": validation_sha256,
            "workspace": str(workspace),
            "authorization_receipt_id": receipt["authorization_receipt_id"],
        },
    )
    _close_broker(broker)

    assert scanned["status"] == "ok"
    connection = sqlite3.connect(data_dir / "goodjob.sqlite3")
    commits = {
        json.loads(str(row[0]))["commit"]
        for row in connection.execute(
            "SELECT locator FROM evidence WHERE origin_kind = 'git_commit'"
        ).fetchall()
    }
    connection.close()
    assert recent_commit in commits
    assert old_commit not in commits


def test_git_history_keeps_an_old_head_as_the_current_worktree_anchor(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    repository = workspace / "repository"
    repository.mkdir(parents=True)
    _git_init(repository)
    _git(repository, "config", "user.name", "Test Author")
    _git(repository, "config", "user.email", "author@example.test")
    _git(repository, "branch", "-M", "main")
    (repository / "pyproject.toml").write_text("[project]\nname='old-head'\n", encoding="utf-8")
    _git(repository, "add", ".")
    old_time = (datetime.now(UTC) - timedelta(days=181)).strftime("%Y-%m-%dT%H:%M:%S+0000")
    _git(
        repository,
        "commit",
        "-m",
        "old current head",
        extra_env={"GIT_AUTHOR_DATE": old_time, "GIT_COMMITTER_DATE": old_time},
    )
    head_commit = _git(repository, "rev-parse", "HEAD").stdout.strip()

    data_dir = tmp_path / "data"
    broker = _broker(data_dir)
    authorized, validation_sha256 = _authorize_source(broker, workspace)
    receipt = _object_field(authorized, "receipt")
    scanned = _send_json(
        broker,
        {
            "op": "scan",
            "job_input_validation_sha256": validation_sha256,
            "workspace": str(workspace),
            "authorization_receipt_id": receipt["authorization_receipt_id"],
        },
    )
    _close_broker(broker)

    assert scanned["status"] == "ok"
    connection = sqlite3.connect(data_dir / "goodjob.sqlite3")
    commits = {
        json.loads(str(row[0]))["commit"]
        for row in connection.execute(
            "SELECT locator FROM evidence WHERE origin_kind = 'git_commit'"
        ).fetchall()
    }
    connection.close()
    assert commits == {head_commit}


def test_internal_git_config_cannot_read_an_include_outside_the_authorized_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    repository = workspace / "repository"
    outside_config = tmp_path / "outside-config"
    repository.mkdir(parents=True)
    _git_init(repository)
    (repository / "pyproject.toml").write_text("[project]\nname='sandboxed'\n", encoding="utf-8")
    outside_config.write_text("[invalid external config\n", encoding="utf-8")
    _git(repository, "config", "include.path", str(outside_config))

    broker = _broker(tmp_path / "data")
    authorized, validation_sha256 = _authorize_source(broker, workspace)
    receipt = _object_field(authorized, "receipt")
    scanned = _send_json(
        broker,
        {
            "op": "scan",
            "job_input_validation_sha256": validation_sha256,
            "workspace": str(workspace),
            "authorization_receipt_id": receipt["authorization_receipt_id"],
        },
    )
    _close_broker(broker)

    assert scanned["status"] == "ok"
    coverage = _object_field(scanned, "coverage")
    assert coverage["fresh_projects"] == 0
    assert any(
        isinstance(issue, dict) and issue.get("kind") == "git_repository_boundary_violation"
        for issue in cast(list[object], scanned["issues"])
    )
    database_text = (
        (tmp_path / "data" / "goodjob.sqlite3").read_bytes().decode("utf-8", errors="ignore")
    )
    assert "invalid external config" not in database_text


def test_repo_manifest_metadata_escape_is_one_repository_level_diagnostic(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    repository = workspace / "repository"
    (workspace / ".repo" / "projects").mkdir(parents=True)
    repository.mkdir(parents=True)
    _git_init(repository)
    (repository / "pyproject.toml").write_text(
        "[project]\nname='repo-manifest'\n",
        encoding="utf-8",
    )
    outside_config = tmp_path / "outside-config"
    outside_config.write_text("repo-manifest-private-sentinel\n", encoding="utf-8")
    (repository / ".git" / "config").unlink()
    (repository / ".git" / "config").symlink_to(outside_config)

    scanner, receipt_id = _direct_scanner(tmp_path / "data", workspace)
    result = scanner.scan(
        workspace_path=str(workspace),
        config_revision="repo-manifest-escape-v1",
        authorization_receipt_id=receipt_id,
    )

    assert result.status == "failed"
    assert result.coverage["fresh_projects"] == 0
    layout_issues = [
        issue for issue in result.issues if issue.kind == "unsupported_repository_layout"
    ]
    assert len(layout_issues) == 1
    assert "repository/.git/config" in layout_issues[0].message
    assert not any(issue.kind == "symlink_skipped" for issue in result.issues)
    database_text = (tmp_path / "data" / "goodjob.sqlite3").read_bytes().decode(
        "utf-8", errors="ignore"
    )
    assert "repo-manifest-private-sentinel" not in database_text


def test_repo_manifest_metadata_inside_authorized_root_remains_scannable(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    repository = workspace / "repository"
    metadata_root = workspace / ".repo" / "projects"
    metadata_root.mkdir(parents=True)
    repository.mkdir(parents=True)
    _git_init(repository)
    (repository / "pyproject.toml").write_text(
        "[project]\nname='repo-manifest-internal'\n",
        encoding="utf-8",
    )
    config_target = metadata_root / "repository-config"
    config_target.write_text((repository / ".git" / "config").read_text(encoding="utf-8"))
    (repository / ".git" / "config").unlink()
    (repository / ".git" / "config").symlink_to(Path("../../.repo/projects/repository-config"))

    scanner, receipt_id = _direct_scanner(tmp_path / "data", workspace)
    result = scanner.scan(
        workspace_path=str(workspace),
        config_revision="repo-manifest-internal-v1",
        authorization_receipt_id=receipt_id,
    )

    assert result.status == "completed"
    assert result.coverage["fresh_projects"] == 1
    assert not any(issue.kind == "unsupported_repository_layout" for issue in result.issues)


def test_internal_git_config_can_include_a_file_inside_the_authorized_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    repository = workspace / "repository"
    internal_config = workspace / "internal-config"
    repository.mkdir(parents=True)
    _git_init(repository)
    (repository / "pyproject.toml").write_text("[project]\nname='sandboxed'\n", encoding="utf-8")
    internal_config.write_text("[goodjob]\n\tprobe = true\n", encoding="utf-8")
    _git(repository, "config", "include.path", str(internal_config))

    broker = _broker(tmp_path / "data")
    authorized, validation_sha256 = _authorize_source(broker, workspace)
    receipt = _object_field(authorized, "receipt")
    scanned = _send_json(
        broker,
        {
            "op": "scan",
            "job_input_validation_sha256": validation_sha256,
            "workspace": str(workspace),
            "authorization_receipt_id": receipt["authorization_receipt_id"],
        },
    )
    _close_broker(broker)

    assert scanned["status"] == "ok"
    coverage = _object_field(scanned, "coverage")
    assert coverage["fresh_projects"] == 1
    assert not any(
        isinstance(issue, dict) and issue.get("kind") == "git_repository_boundary_violation"
        for issue in cast(list[object], scanned["issues"])
    )


def test_internal_git_worktree_config_cannot_include_a_root_external_file(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    repository = workspace / "repository"
    outside_config = tmp_path / "outside-config"
    repository.mkdir(parents=True)
    _git_init(repository)
    (repository / "pyproject.toml").write_text("[project]\nname='sandboxed'\n", encoding="utf-8")
    outside_config.write_text("[invalid external config\n", encoding="utf-8")
    _git(repository, "config", "extensions.worktreeConfig", "true")
    _git(repository, "config", "--worktree", "include.path", str(outside_config))

    broker = _broker(tmp_path / "data")
    authorized, validation_sha256 = _authorize_source(broker, workspace)
    receipt = _object_field(authorized, "receipt")
    scanned = _send_json(
        broker,
        {
            "op": "scan",
            "job_input_validation_sha256": validation_sha256,
            "workspace": str(workspace),
            "authorization_receipt_id": receipt["authorization_receipt_id"],
        },
    )
    _close_broker(broker)

    assert scanned["status"] == "ok"
    coverage = _object_field(scanned, "coverage")
    assert coverage["fresh_projects"] == 0
    assert any(
        isinstance(issue, dict) and issue.get("kind") == "git_repository_boundary_violation"
        for issue in cast(list[object], scanned["issues"])
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group regression")
def test_bounded_git_runner_kills_timeout_and_output_flood(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    repository = workspace / "repository"
    repository.mkdir(parents=True)
    _git_init(repository)
    scanner, _ = _direct_scanner(tmp_path / "data", workspace)
    binding = scanner._bind_internal_git(repository, workspace)
    assert binding is not None

    monkeypatch.setattr(scanner_module, "GIT_COMMAND_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(
        scanner,
        "_git_command",
        lambda _binding, _arguments: ["/bin/sleep", "5"],
    )
    started = datetime.now(UTC)
    with pytest.raises(subprocess.TimeoutExpired):
        scanner._git_bounded_bytes(binding, "status", maximum_output_bytes=1024)
    assert (datetime.now(UTC) - started).total_seconds() < 2

    monkeypatch.setattr(
        scanner,
        "_git_command",
        lambda _binding, _arguments: ["/usr/bin/yes"],
    )
    with pytest.raises(OSError, match="output limit"):
        scanner._git_bounded_bytes(binding, "status", maximum_output_bytes=1024)


def test_unexpected_scan_exception_is_terminalized_as_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    scanner, receipt_id = _direct_scanner(tmp_path / "data", workspace)

    def fail_discovery(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("injected discovery failure")

    monkeypatch.setattr(scanner, "_discover", fail_discovery)
    with pytest.raises(RuntimeError, match="injected discovery failure"):
        scanner.scan(
            workspace_path=str(workspace),
            config_revision="test-v1",
            authorization_receipt_id=receipt_id,
        )

    with scanner._database.read_connection() as connection:
        statuses = {
            str(row["status"]) for row in connection.execute("SELECT status FROM scan_runs")
        }
    assert statuses == {"failed"}


def test_fast_refresh_reanalyzes_when_adapter_version_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "project"
    project.mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname='versions'\n", encoding="utf-8")
    (project / "main.py").write_text("def main(): pass\n", encoding="utf-8")
    scanner, receipt_id = _direct_scanner(tmp_path / "data", workspace)
    first = scanner.scan(
        workspace_path=str(workspace),
        config_revision="config-v1",
        authorization_receipt_id=receipt_id,
    )
    monkeypatch.setattr(scanner_module, "ANALYZER_VERSION", "scan-upgraded")
    scanner.refresh(
        workspace_id=first.workspace_id,
        config_revision="config-v1",
        change_detection_mode="fast",
        authorization_receipt_id=receipt_id,
    )

    with scanner._database.read_connection() as connection:
        rows = connection.execute(
            """
            SELECT adapter_version, analysis_fingerprint
            FROM source_revisions AS sr
            JOIN source_artifacts AS a ON a.artifact_id = sr.artifact_id
            WHERE a.relative_path = 'main.py'
            ORDER BY observed_at
            """
        ).fetchall()
    assert len(rows) == 2
    assert len({str(row["adapter_version"]) for row in rows}) == 2
    assert len({str(row["analysis_fingerprint"]) for row in rows}) == 2


def test_verify_refresh_links_a_same_content_move_without_rewriting_history(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "project"
    project.mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname='moves'\n", encoding="utf-8")
    original = project / "old_name.py"
    moved = project / "new_name.py"
    original.write_text("def stable_symbol(): pass\n", encoding="utf-8")
    scanner, receipt_id = _direct_scanner(tmp_path / "data", workspace)
    first = scanner.scan(
        workspace_path=str(workspace),
        config_revision="config-v1",
        authorization_receipt_id=receipt_id,
    )
    original.rename(moved)
    scanner.refresh(
        workspace_id=first.workspace_id,
        config_revision="config-v1",
        change_detection_mode="verify_content",
        authorization_receipt_id=receipt_id,
    )

    with scanner._database.read_connection() as connection:
        rows = connection.execute(
            """
            SELECT current.relative_path, previous.relative_path
            FROM source_artifacts AS current
            LEFT JOIN source_artifacts AS previous
              ON previous.artifact_id = current.supersedes_artifact_id
            WHERE current.relative_path IN ('old_name.py', 'new_name.py')
            ORDER BY current.relative_path
            """
        ).fetchall()
    assert [(str(row[0]), str(row[1]) if row[1] is not None else None) for row in rows] == [
        ("new_name.py", "old_name.py"),
        ("old_name.py", None),
    ]


def test_same_content_worktrees_reuse_analysis_and_keep_expandable_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    primary = workspace / "primary"
    linked = workspace / "linked"
    primary.mkdir(parents=True)
    _git_init(primary)
    _git(primary, "config", "user.name", "Test Author")
    _git(primary, "config", "user.email", "author@example.test")
    (primary / "pyproject.toml").write_text("[project]\nname='equivalent'\n", encoding="utf-8")
    (primary / "main.py").write_text("import asyncio\ndef run(): pass\n", encoding="utf-8")
    _git(primary, "add", ".")
    _git(primary, "commit", "-m", "same-content baseline")
    _git(primary, "worktree", "add", "-b", "linked-branch", str(linked))

    analyze_calls: list[str] = []
    original_analyze_file = analyze_file

    def counted_analyze_file(
        *,
        relative_path: str,
        text: str,
        artifact_kind: str,
        adapter_id: str,
        base_evidence_kind: str,
    ) -> AnalysisResult:
        analyze_calls.append(relative_path)
        return original_analyze_file(
            relative_path=relative_path,
            text=text,
            artifact_kind=artifact_kind,
            adapter_id=adapter_id,
            base_evidence_kind=base_evidence_kind,
        )

    monkeypatch.setattr("goodjob.scanner.analyze_file", counted_analyze_file)
    scanner, receipt_id = _direct_scanner(tmp_path / "data", workspace)
    scanner.scan(
        workspace_path=str(workspace),
        config_revision="config-v1",
        authorization_receipt_id=receipt_id,
    )

    assert analyze_calls.count("main.py") == 1
    with scanner._database.read_connection() as connection:
        rows = connection.execute(
            """
            SELECT e.content_equivalence_key, COUNT(*) AS source_count
            FROM evidence AS e
            JOIN source_revisions AS sr ON sr.source_revision_id = e.source_revision_id
            JOIN source_artifacts AS a ON a.artifact_id = sr.artifact_id
            WHERE a.relative_path = 'main.py'
            GROUP BY e.content_equivalence_key
            ORDER BY e.content_equivalence_key
            """
        ).fetchall()
        roots = {
            str(row[0])
            for row in connection.execute(
                "SELECT canonical_root FROM worktrees ORDER BY canonical_root"
            )
        }
    assert rows
    assert {int(row["source_count"]) for row in rows} == {2}
    assert roots == {str(primary), str(linked)}


def test_three_worktrees_preserve_branch_state_and_divergent_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    primary = workspace / "primary"
    linked_a = workspace / "linked-a"
    linked_b = workspace / "linked-b"
    primary.mkdir(parents=True)
    _git_init(primary)
    _git(primary, "config", "user.name", "Test Author")
    _git(primary, "config", "user.email", "author@example.test")
    _git(primary, "branch", "-M", "main")
    (primary / "pyproject.toml").write_text("[project]\nname='three-worktrees'\n", encoding="utf-8")
    (primary / "shared.py").write_text(
        "def shared_value() -> str:\n    return 'shared'\n", encoding="utf-8"
    )
    (primary / "tracked.py").write_text(
        "def tracked_value() -> str:\n    return 'base'\n", encoding="utf-8"
    )
    _git(primary, "add", "pyproject.toml", "shared.py", "tracked.py")
    _git(primary, "commit", "-m", "three-worktree baseline")
    base_head = _git(primary, "rev-parse", "HEAD").stdout.strip()
    _git(primary, "worktree", "add", "-b", "linked-a", str(linked_a), base_head)
    _git(primary, "worktree", "add", "-b", "linked-b", str(linked_b), base_head)

    (linked_b / "branch_only.py").write_text(
        "def branch_only() -> str:\n    return 'linked-b'\n", encoding="utf-8"
    )
    _git(linked_b, "add", "branch_only.py")
    _git(linked_b, "commit", "-m", "linked-b implementation")
    linked_b_head = _git(linked_b, "rev-parse", "HEAD").stdout.strip()
    (linked_a / "tracked.py").write_text(
        "def tracked_value() -> str:\n    return 'linked-a modified'\n", encoding="utf-8"
    )
    (linked_b / "untracked.py").write_text(
        "def untracked_value() -> str:\n    return 'linked-b untracked'\n", encoding="utf-8"
    )

    analyze_calls: list[str] = []
    original_analyze_file = analyze_file

    def counted_analyze_file(
        *,
        relative_path: str,
        text: str,
        artifact_kind: str,
        adapter_id: str,
        base_evidence_kind: str,
    ) -> AnalysisResult:
        analyze_calls.append(relative_path)
        return original_analyze_file(
            relative_path=relative_path,
            text=text,
            artifact_kind=artifact_kind,
            adapter_id=adapter_id,
            base_evidence_kind=base_evidence_kind,
        )

    monkeypatch.setattr("goodjob.scanner.analyze_file", counted_analyze_file)
    scanner, receipt_id = _direct_scanner(tmp_path / "data", workspace)
    result = scanner.scan(
        workspace_path=str(workspace),
        config_revision="three-worktrees-v1",
        authorization_receipt_id=receipt_id,
    )

    assert result.coverage["projects"] == 1
    assert result.coverage["fresh_projects"] == 1
    assert result.coverage["worktrees"] == 3
    assert result.coverage["external_git_metadata"] == {}
    assert not any(issue.kind == "external_git_authorization_required" for issue in result.issues)
    assert analyze_calls.count("shared.py") == 1

    with scanner._database.read_connection() as connection:
        assert connection.execute("SELECT COUNT(*) AS count FROM projects").fetchone()["count"] == 1
        observation_rows = connection.execute(
            """
            SELECT wt.canonical_root, wo.branch, wo.head_commit, wo.dirty_state
            FROM worktree_observations AS wo
            JOIN worktrees AS wt ON wt.worktree_id = wo.worktree_id
            WHERE wo.scan_run_id = ?
            ORDER BY wt.canonical_root
            """,
            (result.scan_run_id,),
        ).fetchall()
        evidence_rows = connection.execute(
            """
            SELECT sa.relative_path, wt.canonical_root, e.content_equivalence_key,
                   e.commit_state, e.evidence_kind
            FROM evidence AS e
            JOIN source_revisions AS sr ON sr.source_revision_id = e.source_revision_id
            JOIN source_artifacts AS sa ON sa.artifact_id = sr.artifact_id
            JOIN worktrees AS wt ON wt.worktree_id = sa.worktree_id
            JOIN project_snapshot_evidence AS pse ON pse.evidence_id = e.evidence_id
            JOIN project_snapshots AS ps ON ps.project_snapshot_id = pse.project_snapshot_id
            WHERE ps.scan_run_id = ? AND e.origin_kind = 'source_revision'
            ORDER BY sa.relative_path, wt.canonical_root, e.evidence_kind
            """,
            (result.scan_run_id,),
        ).fetchall()

    observations = {
        str(row["canonical_root"]): (
            str(row["branch"]),
            str(row["head_commit"]),
            str(row["dirty_state"]),
        )
        for row in observation_rows
    }
    assert observations == {
        str(primary.resolve()): ("main", base_head, "clean"),
        str(linked_a.resolve()): ("linked-a", base_head, "modified"),
        str(linked_b.resolve()): ("linked-b", linked_b_head, "untracked"),
    }

    implementation_rows = [row for row in evidence_rows if row["evidence_kind"] == "implementation"]
    shared_rows = [row for row in implementation_rows if row["relative_path"] == "shared.py"]
    assert len(shared_rows) == 3
    assert {str(row["canonical_root"]) for row in shared_rows} == {
        str(primary.resolve()),
        str(linked_a.resolve()),
        str(linked_b.resolve()),
    }
    shared_keys = {row["content_equivalence_key"] for row in shared_rows}
    assert len(shared_keys) == 1
    shared_key = next(iter(shared_keys))
    assert isinstance(shared_key, str) and shared_key
    assert {str(row["commit_state"]) for row in shared_rows} == {"committed"}

    branch_rows = [row for row in implementation_rows if row["relative_path"] == "branch_only.py"]
    assert {(str(row["canonical_root"]), str(row["commit_state"])) for row in branch_rows} == {
        (str(linked_b.resolve()), "committed")
    }
    modified_rows = [
        row
        for row in implementation_rows
        if row["relative_path"] == "tracked.py" and row["commit_state"] == "modified"
    ]
    assert {str(row["canonical_root"]) for row in modified_rows} == {str(linked_a.resolve())}
    untracked_rows = [row for row in implementation_rows if row["relative_path"] == "untracked.py"]
    assert {(str(row["canonical_root"]), str(row["commit_state"])) for row in untracked_rows} == {
        (str(linked_b.resolve()), "untracked")
    }


def test_project_failure_carries_forward_its_baseline_and_keeps_other_projects_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    for name in ("alpha", "beta"):
        project = workspace / name
        project.mkdir(parents=True)
        (project / "pyproject.toml").write_text(f"[project]\nname='{name}'\n", encoding="utf-8")
        (project / "main.py").write_text(f"NAME = '{name}'\n", encoding="utf-8")
    scanner, receipt_id = _direct_scanner(tmp_path / "data", workspace)
    first = scanner.scan(
        workspace_path=str(workspace),
        config_revision="config-v1",
        authorization_receipt_id=receipt_id,
    )
    original_read_project = scanner._read_project

    def fail_beta(
        *,
        plan: ProjectPlan,
        config_revision: str,
        change_detection_mode: Literal["fast", "verify_content"] | None,
        nested_project_roots: set[Path],
    ) -> ProjectData:
        if plan.display_name == "beta":
            raise OSError("injected project failure")
        return original_read_project(
            plan=plan,
            config_revision=config_revision,
            change_detection_mode=change_detection_mode,
            nested_project_roots=nested_project_roots,
        )

    monkeypatch.setattr(scanner, "_read_project", fail_beta)
    refreshed = scanner.refresh(
        workspace_id=first.workspace_id,
        config_revision="config-v1",
        change_detection_mode="verify_content",
        authorization_receipt_id=receipt_id,
    )

    assert refreshed.status == "partial"
    assert refreshed.coverage["fresh_projects"] == 1
    assert refreshed.coverage["carried_forward_projects"] == 1


def test_all_excluded_scan_remains_visible_to_history_status_filter(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "excluded"
    project.mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname='excluded'\n", encoding="utf-8")
    (project / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    data_dir = tmp_path / "data"
    scanner, receipt_id = _direct_scanner(data_dir, workspace)
    _write_project_exclusions(
        data_dir,
        [
            {
                "match": "relative_location",
                "value": "./excluded",
                "reason": "Owner excluded the only project.",
            }
        ],
    )

    result = scanner.scan(
        workspace_path=str(workspace),
        config_revision="all-excluded-v1",
        authorization_receipt_id=receipt_id,
    )

    assert result.status == "completed"
    assert result.coverage["excluded_projects"] == 1
    with scanner._database.read_connection() as connection:
        history_eligible = connection.execute(
            """
            SELECT scan_run_id FROM scan_runs
            WHERE scan_run_id = ? AND status IN ('completed', 'partial')
            """,
            (result.scan_run_id,),
        ).fetchone()
    assert history_eligible is not None


def test_all_projects_failing_without_baseline_remains_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "failed"
    project.mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname='failed'\n", encoding="utf-8")
    (project / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    scanner, receipt_id = _direct_scanner(tmp_path / "data", workspace)

    def fail_project(**_: object) -> ProjectData:
        raise OSError("injected project failure")

    monkeypatch.setattr(scanner, "_read_project", fail_project)
    result = scanner.scan(
        workspace_path=str(workspace),
        config_revision="all-failed-v1",
        authorization_receipt_id=receipt_id,
    )

    assert result.status == "failed"
    assert result.coverage["failed_no_baseline_projects"] == 1
    assert result.coverage["excluded_projects"] == 0


def test_relative_project_exclusion_precedes_snapshot_and_stays_distinct_from_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    for name in ("included", "excluded", "failed"):
        project = workspace / name
        project.mkdir(parents=True)
        (project / "pyproject.toml").write_text(f"[project]\nname='{name}'\n", encoding="utf-8")
        (project / "main.py").write_text(f"NAME = '{name}'\n", encoding="utf-8")
    data_dir = tmp_path / "data"
    scanner, receipt_id = _direct_scanner(data_dir, workspace)
    _write_project_exclusions(
        data_dir,
        [
            {
                "match": "relative_location",
                "value": "./excluded",
                "reason": "Owner chose not to use this project for role preparation.",
            }
        ],
    )
    original_read_project = scanner._read_project

    def fail_selected_project(
        *,
        plan: ProjectPlan,
        config_revision: str,
        change_detection_mode: Literal["fast", "verify_content"] | None,
        nested_project_roots: set[Path],
    ) -> ProjectData:
        if plan.display_name == "failed":
            raise OSError("injected project failure")
        return original_read_project(
            plan=plan,
            config_revision=config_revision,
            change_detection_mode=change_detection_mode,
            nested_project_roots=nested_project_roots,
        )

    monkeypatch.setattr(scanner, "_read_project", fail_selected_project)
    result = scanner.scan(
        workspace_path=str(workspace),
        config_revision="project-exclusion-v1",
        authorization_receipt_id=receipt_id,
    )

    assert result.status == "partial"
    assert result.coverage["fresh_projects"] == 1
    assert result.coverage["excluded_projects"] == 1
    assert result.coverage["failed_no_baseline_projects"] == 1
    assert result.coverage["project_exclusions"] == [
        {
            "project_display_name": "excluded",
            "match": "relative_location",
            "value": "./excluded",
            "reason": "Owner chose not to use this project for role preparation.",
        }
    ]
    role_context = _object_field(result.coverage, "role_lens_context")
    context_projects = role_context["projects"]
    assert isinstance(context_projects, list)
    excluded_context = next(
        item
        for item in context_projects
        if isinstance(item, dict) and item.get("display_name") == "excluded"
    )
    assert excluded_context["snapshot_disposition"] == "excluded"
    assert excluded_context["project_snapshot_id"] is None
    assert excluded_context["evidence_count"] == 0

    with scanner._database.read_connection() as connection:
        excluded_row = connection.execute(
            """
            SELECT p.project_id, srp.snapshot_disposition, srp.project_snapshot_id
            FROM scan_run_projects AS srp
            JOIN projects AS p ON p.project_id = srp.project_id
            WHERE srp.scan_run_id = ? AND p.display_name = 'excluded'
            """,
            (result.scan_run_id,),
        ).fetchone()
        assert excluded_row is not None
        assert excluded_row["snapshot_disposition"] == "excluded"
        assert excluded_row["project_snapshot_id"] is None
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM project_snapshots WHERE scan_run_id = ? AND project_id = ?",
                (result.scan_run_id, excluded_row["project_id"]),
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM evidence WHERE project_id = ?",
                (excluded_row["project_id"],),
            ).fetchone()[0]
            == 0
        )


def test_identity_key_project_exclusion_is_exact(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    selected = workspace / "selected"
    other = workspace / "other"
    for project in (selected, other):
        project.mkdir(parents=True)
        (project / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
        (project / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    data_dir = tmp_path / "data"
    scanner, receipt_id = _direct_scanner(data_dir, workspace)
    _write_project_exclusions(
        data_dir,
        [
            {
                "match": "identity_key",
                "value": str(selected.resolve()),
                "reason": "Identity-specific exclusion.",
            }
        ],
    )

    result = scanner.scan(
        workspace_path=str(workspace),
        config_revision="identity-exclusion-v1",
        authorization_receipt_id=receipt_id,
    )

    assert result.coverage["fresh_projects"] == 1
    assert result.coverage["excluded_projects"] == 1
    assert result.coverage["project_exclusions"] == [
        {
            "project_display_name": "selected",
            "match": "identity_key",
            "value": str(selected.resolve()),
            "reason": "Identity-specific exclusion.",
        }
    ]


@pytest.mark.parametrize(
    ("config_text", "expected_issue", "expected_excluded"),
    (
        (
            "[goodjob\nconfig_version = 1\n",
            "project_exclusion_config_invalid",
            0,
        ),
        (
            """
[goodjob]
config_version = 1

[[goodjob.excluded_projects]]
match = "relative_location"
value = "excluded"

[[goodjob.excluded_projects]]
match = "relative_location"
value = "excluded"
reason = "The valid rule must still apply."
""",
            "project_exclusion_rule_invalid",
            1,
        ),
        (
            """
[goodjob]
config_version = 1

[[goodjob.excluded_projects]]
match = "relative_location"
value = "not-present"
reason = "This rule should be visible as unmatched."
""",
            "project_exclusion_rule_unmatched",
            0,
        ),
    ),
)
def test_bad_project_exclusion_config_warns_without_discarding_other_projects(
    tmp_path: Path,
    config_text: str,
    expected_issue: str,
    expected_excluded: int,
) -> None:
    workspace = tmp_path / "workspace"
    for name in ("included", "excluded"):
        project = workspace / name
        project.mkdir(parents=True)
        (project / "pyproject.toml").write_text(f"[project]\nname='{name}'\n", encoding="utf-8")
        (project / "main.py").write_text(f"NAME = '{name}'\n", encoding="utf-8")
    data_dir = tmp_path / "data"
    scanner, receipt_id = _direct_scanner(data_dir, workspace)
    DataPaths(data_dir).config_file.write_text(config_text, encoding="utf-8")

    result = scanner.scan(
        workspace_path=str(workspace),
        config_revision=f"bad-config-{expected_issue}",
        authorization_receipt_id=receipt_id,
    )

    assert result.status == "partial"
    assert result.coverage["excluded_projects"] == expected_excluded
    assert result.coverage["fresh_projects"] == 2 - expected_excluded
    matching_issues = [issue for issue in result.issues if issue.kind == expected_issue]
    assert len(matching_issues) == 1
    assert matching_issues[0].severity == "warning"


@pytest.mark.parametrize("invalid_config_kind", ("directory", "symlink", "oversized"))
def test_unsafe_project_exclusion_config_warns_and_scan_continues(
    tmp_path: Path,
    invalid_config_kind: str,
) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "included"
    project.mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname='included'\n", encoding="utf-8")
    (project / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    data_dir = tmp_path / "data"
    scanner, receipt_id = _direct_scanner(data_dir, workspace)
    config_file = DataPaths(data_dir).config_file
    config_file.unlink()
    if invalid_config_kind == "directory":
        config_file.mkdir()
    elif invalid_config_kind == "symlink":
        target = tmp_path / "linked-config.toml"
        target.write_text("[goodjob]\nconfig_version = 1\n", encoding="utf-8")
        config_file.symlink_to(target)
    else:
        config_file.write_bytes(b"[goodjob]\nconfig_version = 1\n#" + b"x" * MAX_CONFIG_FILE_BYTES)

    result = scanner.scan(
        workspace_path=str(workspace),
        config_revision="unreadable-config-v1",
        authorization_receipt_id=receipt_id,
    )

    assert result.coverage["fresh_projects"] == 1
    assert any(
        issue.kind == "project_exclusion_config_unreadable" and issue.severity == "warning"
        for issue in result.issues
    )


def test_config_revision_re_evaluates_project_exclusions(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    selected = workspace / "selected"
    other = workspace / "other"
    for project in (selected, other):
        project.mkdir(parents=True)
        (project / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
        (project / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    data_dir = tmp_path / "data"
    scanner, receipt_id = _direct_scanner(data_dir, workspace)
    _write_project_exclusions(
        data_dir,
        [
            {
                "match": "relative_location",
                "value": "selected",
                "reason": "Excluded in the first revision.",
            }
        ],
    )
    first = scanner.scan(
        workspace_path=str(workspace),
        config_revision="project-exclusion-v1",
        authorization_receipt_id=receipt_id,
    )
    _write_project_exclusions(data_dir, [])

    second = scanner.refresh(
        workspace_id=first.workspace_id,
        config_revision="project-exclusion-v2",
        change_detection_mode="fast",
        authorization_receipt_id=receipt_id,
    )

    assert first.coverage["excluded_projects"] == 1
    assert first.coverage["fresh_projects"] == 1
    assert second.coverage["excluded_projects"] == 0
    assert second.coverage["fresh_projects"] == 2
    with scanner._database.read_connection() as connection:
        selected_dispositions = [
            str(row["snapshot_disposition"])
            for row in connection.execute(
                """
                SELECT srp.snapshot_disposition
                FROM scan_run_projects AS srp
                JOIN projects AS p ON p.project_id = srp.project_id
                WHERE p.identity_key = ?
                ORDER BY srp.scan_run_id
                """,
                (str(selected.resolve()),),
            ).fetchall()
        ]
    assert sorted(selected_dispositions) == ["excluded", "fresh"]


def test_ignore_syntax_capabilities_are_structurally_enumerated() -> None:
    capabilities = dict(IGNORE_PATTERN_SYNTAX)

    assert {
        "literal_name",
        "star_and_question_wildcards",
        "directory_suffix",
        "negation_prefix",
        "path_pattern",
        "comment",
        "blank_line",
    } <= capabilities.keys()


def test_git_multi_segment_directory_pattern_matches_descendant() -> None:
    matcher = IgnoreMatcher(((".", "build/outputs", False, True),))

    assert matcher.matches("build/outputs/apk/app.apk") is True


def test_nested_git_multi_segment_directory_pattern_matches_descendant() -> None:
    matcher = IgnoreMatcher((("apps/mobile/ios", "Flutter/ephemeral", False, True),))

    assert matcher.matches("apps/mobile/ios/Flutter/ephemeral/Packages/x/Package.swift") is True


def test_git_multi_segment_pattern_remains_anchored_to_ignore_file() -> None:
    matcher = IgnoreMatcher(((".", "build/outputs", False, True),))

    assert matcher.matches("build/outputsX/a.txt") is False
    assert matcher.matches("other/build/outputs/a.txt") is False


def test_git_directory_suffix_does_not_match_same_named_file() -> None:
    matcher = IgnoreMatcher(((".", "cache", False, True),))

    assert matcher.matches("cache") is False
    assert matcher.matches("cache/data.py") is True


@pytest.mark.parametrize(
    ("source_line", "raw_pattern", "approximation"),
    [
        (" literal.py\n", " literal.py", "surrounding whitespace"),
        ("literal.py\t\n", "literal.py\t", "surrounding whitespace"),
        ("\t\n", "\t", "surrounding whitespace"),
        ("\\#literal.py\n", "\\#literal.py", "backslash escapes"),
        ("\\!literal.py\n", "\\!literal.py", "backslash escapes"),
        ("literal\\ \n", "literal\\ ", "backslash escapes"),
        ("/root.py\n", "/root.py", 'the leading "/"'),
        ("ignored/\n!ignored/keep.py\n", "!ignored/keep.py", "last matching rule wins"),
        ("src/*.py\n", "src/*.py", 'allows "*" and "?"'),
        ("src/[!a].py\n", "src/[!a].py", "character classes in path patterns"),
        ("[^a].txt\n", "[^a].txt", 'does not treat "^"'),
        ("[[:digit:]].log\n", "[[:digit:]].log", "POSIX named character classes"),
        ("**/cache\n", "**/cache", 'treats "**"'),
        ("cache/**\n", "cache/**", 'treats "**"'),
        ("cache/**/data\n", "cache/**/data", 'treats "**"'),
        ("cache***data\n", "cache***data", 'treats "**"'),
    ],
)
def test_git_disclosed_semantics_emit_visible_issue(
    tmp_path: Path,
    source_line: str,
    raw_pattern: str,
    approximation: str,
) -> None:
    (tmp_path / ".gitignore").write_text(source_line, encoding="utf-8")

    _, issues = IgnoreMatcher.load(tmp_path, [".gitignore"])

    unsupported = [issue for issue in issues if issue.kind == "ignore_pattern_unsupported"]
    assert len(unsupported) == 1
    assert isinstance(unsupported[0], scanner_module.IgnorePatternIssueDraft)
    assert unsupported[0].raw_pattern == raw_pattern
    assert approximation in unsupported[0].remediation


@pytest.mark.parametrize(
    "source_text",
    [
        "\n",
        "# comment\n",
        "# comment\t\n",
        "literal.py   \n",
        "*.tmp\n!important.tmp\n",
        "src/module.py\n",
        "module.py\n",
        "cache/\n",
        "doc/frotz\n",
        "*.py\n",
        "file?.[ch]\n",
    ],
)
def test_git_supported_semantics_do_not_emit_unsupported_issue(
    tmp_path: Path,
    source_text: str,
) -> None:
    (tmp_path / ".gitignore").write_text(source_text, encoding="utf-8")

    _, issues = IgnoreMatcher.load(tmp_path, [".gitignore"])

    assert all(issue.kind != "ignore_pattern_unsupported" for issue in issues)


def test_git_root_anchor_is_currently_approximated_at_any_depth(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("/build/\n", encoding="utf-8")

    matcher, _ = IgnoreMatcher.load(tmp_path, [".gitignore"])

    assert matcher.matches("nested/build/output.js") is True


def test_git_negation_below_excluded_directory_currently_reincludes(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("ignored/\n!ignored/keep.py\n", encoding="utf-8")

    matcher, _ = IgnoreMatcher.load(tmp_path, [".gitignore"])

    assert matcher.matches("ignored/drop.py") is True
    assert matcher.matches("ignored/keep.py") is False


def test_nested_ignore_negation_below_root_exclusion_reports_approximation(
    tmp_path: Path,
) -> None:
    (tmp_path / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    (tmp_path / "ignored").mkdir()
    (tmp_path / "ignored" / ".gitignore").write_text("!keep.py\n", encoding="utf-8")

    matcher, issues = IgnoreMatcher.load(
        tmp_path,
        [".gitignore", "ignored/.gitignore"],
    )

    assert matcher.matches("ignored/keep.py") is False
    assert any(
        issue.kind == "ignore_pattern_unsupported"
        and issue.relative_path == "ignored/.gitignore"
        and "!keep.py" in issue.message
        and "last matching rule wins" in issue.remediation
        for issue in issues
    )


def test_git_single_star_path_currently_crosses_directories(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("src/*.py\n", encoding="utf-8")

    matcher, issues = IgnoreMatcher.load(tmp_path, [".gitignore"])

    assert matcher.matches("src/a/b.py") is True
    assert any(
        issue.kind == "ignore_pattern_unsupported"
        and "src/*.py" in issue.message
        and 'match "/"' in issue.remediation
        for issue in issues
    )


def test_unsupported_ignore_patterns_report_source_raw_line_and_approximation(
    tmp_path: Path,
) -> None:
    (tmp_path / ".gitignore").write_text(
        "/build/\nignored/\n!ignored/keep.py\n**/cache/\n",
        encoding="utf-8",
    )

    _, issues = IgnoreMatcher.load(tmp_path, [".gitignore"])

    unsupported = [issue for issue in issues if issue.kind == "ignore_pattern_unsupported"]
    assert len(unsupported) == 3
    assert all(issue.severity == "warning" for issue in unsupported)
    assert {issue.relative_path for issue in unsupported} == {".gitignore"}
    assert any(
        "/build/" in issue.message and "any depth" in issue.remediation for issue in unsupported
    )
    assert any(
        "!ignored/keep.py" in issue.message and "last matching rule wins" in issue.remediation
        for issue in unsupported
    )
    assert any(
        "**/cache/" in issue.message and "Python fnmatch" in issue.remediation
        for issue in unsupported
    )


def test_ignore_approximations_are_visible_in_coverage_without_failing_scan(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    approximated = workspace / "approximated"
    unaffected = workspace / "unaffected"
    for project in (approximated, unaffected):
        project.mkdir(parents=True)
        (project / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
        (project / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    (approximated / ".gitignore").write_text(
        "  /build/  \nignored/\n!ignored/keep.py\n**/cache/\n",
        encoding="utf-8",
    )
    scanner, receipt_id = _direct_scanner(tmp_path / "data", workspace)

    result = scanner.scan(
        workspace_path=str(workspace),
        config_revision="ignore-approximation-v1",
        authorization_receipt_id=receipt_id,
    )

    assert result.status == "partial"
    assert result.status != "failed"
    assert result.coverage["fresh_projects"] == 2
    overview = scanner.overview(
        workspace_path=str(workspace),
        scan_run_id=result.scan_run_id,
    )
    scan_overview = _object_field(overview, "scan_overview")
    overview_coverage = _object_field(scan_overview, "coverage")
    coverage_issues = overview_coverage["ignore_pattern_issues"]
    assert isinstance(coverage_issues, list)
    assert len(coverage_issues) == 3
    assert all(
        isinstance(issue, dict)
        and issue.get("project_display_name") == "approximated"
        and issue.get("source_ignore_file") == ".gitignore"
        and issue.get("severity") == "warning"
        and isinstance(issue.get("raw_pattern_and_reason"), str)
        and isinstance(issue.get("approximation"), str)
        for issue in coverage_issues
    )
    assert {issue["raw_pattern"] for issue in coverage_issues if isinstance(issue, dict)} == {
        "  /build/  ",
        "!ignored/keep.py",
        "**/cache/",
    }
    unsupported = [issue for issue in result.issues if issue.kind == "ignore_pattern_unsupported"]
    assert len(unsupported) == 3
