from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

RUNTIME_DIR = Path(__file__).resolve().parents[1]


def _send_json(process: subprocess.Popen[str], payload: dict[str, object]) -> dict[str, object]:
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(json.dumps(payload) + "\n")
    process.stdin.flush()
    response = json.loads(process.stdout.readline())
    assert isinstance(response, dict)
    return response


def _start_broker(
    data_dir: Path,
    *,
    cwd: Path = RUNTIME_DIR,
    preflight_workspace: Path | None = None,
) -> subprocess.Popen[str]:
    if sys.platform == "win32" and preflight_workspace is None:
        preflight_workspace = data_dir.parent / "workspace"
        preflight_workspace.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(RUNTIME_DIR / "scripts" / "session.py"),
        "--data-dir",
        str(data_dir),
    ]
    if preflight_workspace is not None:
        command.extend(["--preflight-workspace", str(preflight_workspace)])
    return subprocess.Popen(
        command,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": str(RUNTIME_DIR / "src")},
    )


def test_session_broker_rejects_authorization_outside_preflight_workspace(
    tmp_path: Path,
) -> None:
    preflight_workspace = tmp_path / "preflight-workspace"
    preflight_workspace.mkdir()
    other_workspace = tmp_path / "other-workspace"
    other_workspace.mkdir()
    broker = _start_broker(
        tmp_path / "data",
        preflight_workspace=preflight_workspace,
    )

    rejected = _send_json(
        broker,
        {
            "op": "authorize_source_analysis",
            "workspace": str(other_workspace),
            "confirmed": True,
        },
    )

    assert rejected["status"] == "error"
    assert rejected["code"] == "invalid_input"
    assert "prerequisite preflight" in str(rejected["message"])
    _stop_broker(broker)


def _stop_broker(broker: subprocess.Popen[str]) -> None:
    assert broker.stdin is not None
    broker.stdin.close()
    assert broker.wait(timeout=5) == 0
    assert broker.stderr is not None
    stderr = broker.stderr.read()
    if sys.platform == "win32":
        report = json.loads(stderr)
        assert report["can_start_broker"] is True
    else:
        assert stderr == ""


def _translation_publish_input(source: dict[str, object]) -> dict[str, object]:
    source_items = source["items"]
    assert isinstance(source_items, list)
    candidates: list[dict[str, object]] = []
    for value in source_items:
        assert isinstance(value, dict)
        export_kind = value["export_kind"]
        target = (
            {"text": "I implemented a testable Python entry point."}
            if export_kind == "resume"
            else {
                "question": "How was the testable entry point implemented?",
                "answer": "I implemented the entry point in Python.",
            }
        )
        candidates.append(
            {
                key: value[key]
                for key in (
                    "source_item_id",
                    "export_kind",
                    "claim_refs",
                    "evidence_refs",
                    "role_lens_refs",
                    "anchors",
                    "project_id",
                    "module_id",
                )
            }
            | {"target": target}
        )
    return {
        "contract_version": "translation-export-request-v1",
        "action": "publish",
        "source_artifact_snapshot_id": source["source_artifact_snapshot_id"],
        "source_projection_sha256": source["source_projection_sha256"],
        "target_language": "en",
        "export_kinds": ["resume", "interview_qa"],
        "items": candidates,
    }


def test_session_broker_reuses_one_fd_capability_until_stdin_closes(tmp_path: Path) -> None:
    workspace = tmp_path / "工作区"
    workspace.mkdir()
    data_dir = tmp_path / "data"
    broker = _start_broker(data_dir, preflight_workspace=workspace)

    rejected_notice = _send_json(
        broker,
        {
            "op": "authorize_source_analysis",
            "workspace": str(workspace),
            "confirmed": True,
            "notice_version": {},
        },
    )
    assert rejected_notice["status"] == "error"
    assert rejected_notice["code"] == "invalid_input"

    rejected_surrogate = _send_json(
        broker,
        {
            "op": "authorize_source_analysis",
            "workspace": "\ud800",
            "confirmed": True,
        },
    )
    assert rejected_surrogate["status"] == "error"
    assert rejected_surrogate["code"] == "invalid_input"

    rejected_nul = _send_json(
        broker,
        {
            "op": "authorize_source_analysis",
            "workspace": "\x00",
            "confirmed": True,
        },
    )
    assert rejected_nul["status"] == "error"
    assert rejected_nul["code"] == "invalid_input"

    assert broker.stdin is not None
    assert broker.stdout is not None
    broker.stdin.write('{"op":"authorize_source_analysis","workspace":1e9999}\n')
    broker.stdin.flush()
    rejected_non_finite = json.loads(broker.stdout.readline())
    assert rejected_non_finite["status"] == "error"
    assert rejected_non_finite["code"] == "invalid_input"

    authorized = _send_json(
        broker,
        {"op": "authorize_source_analysis", "workspace": str(workspace), "confirmed": True},
    )
    assert authorized["status"] == "ok"
    receipt = authorized["receipt"]
    assert isinstance(receipt, dict)
    verified = _send_json(
        broker,
        {
            "op": "verify_source_analysis",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt["authorization_receipt_id"],
        },
    )
    assert verified["status"] == "ok"
    assert "session_capability" not in json.dumps(authorized)
    assert "session_capability" not in json.dumps(verified)

    rejected_relation = _send_json(
        broker,
        {
            "op": "authorize_external_git_relation_probe",
            "workspace": str(workspace),
            "git_pointer": str(workspace / ".git"),
            "confirmed": True,
        },
    )
    assert rejected_relation["status"] == "error"
    assert rejected_relation["code"] == "invalid_input"

    rejected_metadata = _send_json(
        broker,
        {
            "op": "authorize_external_git_metadata",
            "workspace": str(workspace),
            "git_pointer": str(workspace / ".git"),
            "git_dir": str(tmp_path / "outside-git"),
            "common_dir": str(tmp_path / "outside-common"),
            "confirmed": True,
        },
    )
    assert rejected_metadata["status"] == "error"
    assert rejected_metadata["code"] == "invalid_input"
    rejected_unknown_grant = _send_json(
        broker,
        {
            "op": "scan",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt["authorization_receipt_id"],
            "external_git_metadata_receipt_ids": ["unknown-receipt"],
        },
    )
    assert rejected_unknown_grant["status"] == "error"
    assert rejected_unknown_grant["code"] == "invalid_input"

    _stop_broker(broker)

    status = subprocess.run(
        [sys.executable, "-m", "goodjob", "--data-dir", str(data_dir), "data-status"],
        cwd=RUNTIME_DIR,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(RUNTIME_DIR / "src")},
    )
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)["usage_bytes"]["sqlite"] > 0


def test_protected_children_ignore_a_workspace_goodjob_module(tmp_path: Path) -> None:
    workspace = tmp_path / "hostile-workspace"
    workspace.mkdir()
    marker = tmp_path / "untrusted-module-executed"
    (workspace / "goodjob.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "captured = []\n"
        "for descriptor in range(3, 256):\n"
        "    try:\n"
        "        captured.append(os.read(descriptor, 1024))\n"
        "    except OSError:\n"
        "        pass\n"
        f"Path({str(marker)!r}).write_bytes(b''.join(captured) or b'executed')\n",
        encoding="utf-8",
    )
    broker = _start_broker(tmp_path / "data", cwd=workspace, preflight_workspace=workspace)

    authorized = _send_json(
        broker,
        {"op": "authorize_source_analysis", "workspace": str(workspace), "confirmed": True},
    )

    assert authorized["status"] == "ok"
    assert not marker.exists()
    receipt = authorized["receipt"]
    assert isinstance(receipt, dict)
    validated = _send_json(
        broker,
        {
            "op": "validate_job_input",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt["authorization_receipt_id"],
            "job_input": {
                "contract_version": "job-input-v1",
                "target_role": "系统工程师",
                "jd_input": {"kind": "none"},
            },
        },
    )
    assert validated["status"] == "ok"
    _stop_broker(broker)


def test_documented_uv_launch_ignores_the_target_project(tmp_path: Path) -> None:
    uv = shutil.which("uv")
    assert uv is not None
    installed_runtime = tmp_path / "installed-skill" / "runtime"
    shutil.copytree(
        RUNTIME_DIR,
        installed_runtime,
        ignore=shutil.ignore_patterns(".venv", ".pytest_cache", "__pycache__", "*.pyc"),
    )
    workspace = tmp_path / "hostile-uv-project"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text("[project\ninvalid", encoding="utf-8")
    (workspace / "uv.toml").write_text("[invalid\nconfig", encoding="utf-8")
    marker = tmp_path / "uv-imported-workspace-module"
    (workspace / "goodjob.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n",
        encoding="utf-8",
    )
    broker = subprocess.Popen(
        [
            uv,
            "run",
            "--isolated",
            "--no-project",
            "--no-config",
            "--offline",
            "--no-python-downloads",
            "--python",
            "3.12",
            "python",
            "-I",
            "-B",
            str(installed_runtime / "scripts" / "session.py"),
            "--data-dir",
            str(tmp_path / "data"),
            "--preflight-workspace",
            str(workspace),
        ],
        cwd=workspace,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            **os.environ,
            "UV_CACHE_DIR": str(tmp_path / "empty-uv-cache"),
            "UV_NO_PROGRESS": "1",
        },
    )

    authorized = _send_json(
        broker,
        {"op": "authorize_source_analysis", "workspace": str(workspace), "confirmed": True},
    )

    assert authorized["status"] == "ok"
    assert not marker.exists()
    receipt = authorized["receipt"]
    assert isinstance(receipt, dict)
    validated = _send_json(
        broker,
        {
            "op": "validate_job_input",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt["authorization_receipt_id"],
            "job_input": {
                "contract_version": "job-input-v1",
                "target_role": "系统工程师",
                "jd_input": {"kind": "none"},
            },
        },
    )
    assert validated["status"] == "ok"
    assert broker.stdin is not None
    broker.stdin.close()
    assert broker.wait(timeout=5) == 0
    assert broker.stderr is not None
    stderr = broker.stderr.read()
    if sys.platform == "win32":
        report, _, stderr = stderr.partition("\n")
        assert json.loads(report)["can_start_broker"] is True
    assert not stderr or "Ignoring project discovery error due to `--no-project`" in stderr
    assert "uv.toml" not in stderr
    assert not list(installed_runtime.rglob("__pycache__"))
    assert not list(installed_runtime.rglob("*.pyc"))
    assert not (installed_runtime / ".venv").exists()


def _start_launcher(
    tmp_path: Path, data_dir: Path, *, env: dict[str, str] | None = None
) -> subprocess.Popen[str]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return subprocess.Popen(
        [
            sys.executable,
            str(RUNTIME_DIR / "scripts" / "launch_broker.py"),
            "--data-dir",
            str(data_dir),
            "--agent-runtime",
            "opencode_task_runtime",
            "--workspace",
            str(workspace),
        ],
        cwd=workspace,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def _assert_launcher_records_host_agent_runtime(
    broker: subprocess.Popen[str], data_dir: Path, workspace: Path
) -> None:
    assert broker.stdin is not None
    assert broker.stdout is not None
    broker.stdin.write(
        json.dumps(
            {"op": "authorize_source_analysis", "workspace": str(workspace), "confirmed": True}
        )
        + "\n"
    )
    broker.stdin.flush()
    response = broker.stdout.readline()
    if not response:
        assert broker.stderr is not None
        pytest.fail(f"launcher exited before the broker response: {broker.stderr.read()}")
    authorized = json.loads(response)

    assert authorized["status"] == "ok"
    _stop_broker(broker)
    connection = sqlite3.connect(data_dir / "goodjob.sqlite3")
    issuer_kind = connection.execute("SELECT issuer_kind FROM authorization_receipts").fetchone()
    connection.close()
    assert issuer_kind == ("opencode_task_runtime",)


def test_launcher_uses_uv_and_records_host_agent_runtime(tmp_path: Path) -> None:
    if shutil.which("uv") is None:
        pytest.skip("uv path requires uv on PATH")
    data_dir = tmp_path / "data"
    broker = _start_launcher(tmp_path, data_dir)

    _assert_launcher_records_host_agent_runtime(broker, data_dir, tmp_path / "workspace")


def _write_direct_python_fallback(executable_dir: Path, name: str) -> None:
    if sys.platform == "win32":
        (executable_dir / f"{name}.cmd").write_text(
            f'@"{sys.executable}" %*\n',
            encoding="utf-8",
        )
    else:
        (executable_dir / name).symlink_to(sys.executable)


def test_launcher_falls_back_to_python312_and_records_host_agent_runtime(
    tmp_path: Path,
) -> None:
    executable_dir = tmp_path / "bin"
    executable_dir.mkdir()
    _write_direct_python_fallback(executable_dir, "python3.12")
    data_dir = tmp_path / "data"
    broker = _start_launcher(
        tmp_path,
        data_dir,
        env={**os.environ, "PATH": str(executable_dir)},
    )

    _assert_launcher_records_host_agent_runtime(broker, data_dir, tmp_path / "workspace")


def test_launcher_falls_back_to_python3_at_least_312_and_records_host_agent_runtime(
    tmp_path: Path,
) -> None:
    executable_dir = tmp_path / "bin"
    executable_dir.mkdir()
    _write_direct_python_fallback(executable_dir, "python3")
    data_dir = tmp_path / "data"
    broker = _start_launcher(
        tmp_path,
        data_dir,
        env={**os.environ, "PATH": str(executable_dir)},
    )

    _assert_launcher_records_host_agent_runtime(broker, data_dir, tmp_path / "workspace")


def test_launcher_reports_a_stable_error_when_broker_process_cannot_start(
    tmp_path: Path,
) -> None:
    executable_dir = tmp_path / "bin"
    executable_dir.mkdir()
    fake_uv = executable_dir / ("uv.exe" if sys.platform == "win32" else "uv")
    fake_uv.write_text("#!/missing/interpreter\n", encoding="utf-8")
    fake_uv.chmod(0o700)

    result = subprocess.run(
        [sys.executable, str(RUNTIME_DIR / "scripts" / "launch_broker.py")],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": str(executable_dir)},
    )

    assert result.returncode == 2
    assert result.stdout == ""
    report = json.loads(result.stderr)
    assert report["contract_version"] == "launcher-preflight-v1"
    assert report["can_start_broker"] is False
    assert str(tmp_path) not in result.stderr
    assert "Traceback" not in result.stderr


def test_isolated_launcher_entry_ignores_python_environment_injection(
    tmp_path: Path,
) -> None:
    injection_dir = tmp_path / "injection"
    injection_dir.mkdir()
    marker = tmp_path / "sitecustomize-loaded"
    (injection_dir / "sitecustomize.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('loaded', encoding='utf-8')\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            str(RUNTIME_DIR / "scripts" / "launch_broker.py"),
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": "", "PYTHONPATH": str(injection_dir)},
    )

    assert result.returncode == 2
    assert not marker.exists()
    assert "Traceback" not in result.stderr


def test_skill_keeps_a_stable_launcher_entry_without_a_platform_matrix() -> None:
    skill_text = (RUNTIME_DIR.parent / "SKILL.md").read_text(encoding="utf-8")
    session_workflow = skill_text.split("## Session Workflow", 1)[1].split("\n## ", 1)[0]

    for expected in (
        "`runtime/scripts/launch_broker.py`",
        "isolated, no-bytecode Python 3.12+",
        "`--preflight-only`",
        "`--workspace <displayed workspace path>`",
        "`launcher-preflight-v1`",
        "stdout contains exactly one UTF-8 JSON",
        "stderr is empty",
        "`can_start_broker: true`",
        "`can_start_broker: false`",
        "stable `--agent-runtime` identifier",
        "ordinary standard input",
        (
            "If a required dependency, permission, or capability decision has no structured "
            "launcher fact"
        ),
        "Do not invoke the core CLI to manufacture a launcher report",
        "`missing_dependency`",
        "`permission_required`",
        "`unsupported_capability`",
        "explicit Owner consent",
        "stop fail-closed",
        "Do not use a platform-private preflight flag",
        "infer a branch from the environment name",
    ):
        assert expected in session_workflow

    for platform_detail in (
        "Windows",
        "macOS",
        "Linux",
        "WSL",
        "Codex",
        "OpenCode",
        "PowerShell",
        "py -3",
        "python3 -I -B",
        "https://",
        "WFP",
        "IPv6",
        "--windows-preflight-only",
        "sys.platform",
    ):
        assert platform_detail not in session_workflow


def test_documented_launcher_entry_keeps_broker_alive_until_stdin_closes(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    workspace = tmp_path / "workspace"
    broker = _start_launcher(tmp_path, data_dir)

    assert broker.poll() is None
    authorized = _send_json(
        broker,
        {"op": "authorize_source_analysis", "workspace": str(workspace), "confirmed": True},
    )
    assert authorized["status"] == "ok"
    assert broker.poll() is None

    _stop_broker(broker)

    assert broker.stdin is not None
    assert broker.stdin.closed
    assert broker.returncode == 0


def test_skill_preserves_authorization_and_task_scoped_broker_safety() -> None:
    skill_text = (RUNTIME_DIR.parent / "SKILL.md").read_text(encoding="utf-8")

    for expected in (
        "explicitly authorizes the current workspace",
        "Do not scan or open source before authorization",
        "source opened by the host agent enters the current host agent's model-processing boundary",
        "GoodJob adds no separate upload or telemetry channel",
        "does not assess legal or organization-policy compliance",
        "Never execute workspace code",
        "one task-scoped broker",
        "same broker",
        "Close its standard input and process handle",
        "background daemon",
        "disk mailbox",
        "PTY",
        "copying a receipt or capability",
        "arguments, environment variables, logs, notes, or output",
        "source revision or hash drift",
        "one transaction",
        "must not replace an existing `latest`",
        "[operation contracts](references/operation-contracts.md)",
        "[analysis contracts](references/analysis-contracts.md)",
    ):
        assert expected in skill_text


def test_job_input_preflight_blocks_bad_jd_before_scan_state(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text(
        '[project]\nname = "job-input-preflight"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    data_dir = tmp_path / "data"
    broker = _start_broker(data_dir)
    authorized = _send_json(
        broker,
        {"op": "authorize_source_analysis", "workspace": str(workspace), "confirmed": True},
    )
    receipt = authorized["receipt"]
    assert isinstance(receipt, dict)
    receipt_id = receipt["authorization_receipt_id"]
    assert isinstance(receipt_id, str)

    initially_valid = _send_json(
        broker,
        {
            "op": "validate_job_input",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
            "job_input": {
                "contract_version": "job-input-v1",
                "target_role": "应用软件工程师",
                "jd_input": {"kind": "none"},
            },
        },
    )
    initially_valid_job = initially_valid["job_input"]
    assert isinstance(initially_valid_job, dict)
    old_validation_sha256 = initially_valid_job["validation_sha256"]
    assert isinstance(old_validation_sha256, str)

    rejected = _send_json(
        broker,
        {
            "op": "validate_job_input",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
            "job_input": {
                "contract_version": "job-input-v1",
                "target_role": "应用软件工程师",
                "jd_input": {"kind": "file", "path": str(tmp_path / "missing-jd.txt")},
            },
        },
    )

    assert rejected["status"] == "error"
    assert rejected["code"] == "invalid_input"
    rejected_old_digest = _send_json(
        broker,
        {
            "op": "scan",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
            "job_input_validation_sha256": old_validation_sha256,
        },
    )
    assert rejected_old_digest["status"] == "error"
    assert rejected_old_digest["code"] == "invalid_input"
    rejected_missing_digest = _send_json(
        broker,
        {
            "op": "scan",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
        },
    )
    assert rejected_missing_digest["status"] == "error"
    assert rejected_missing_digest["code"] == "invalid_input"
    connection = sqlite3.connect(data_dir / "goodjob.sqlite3")
    assert connection.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM job_inputs").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM role_lenses").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM preparation_runs").fetchone()[0] == 0
    connection.close()

    continued = _send_json(
        broker,
        {
            "op": "validate_job_input",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
            "job_input": {
                "contract_version": "job-input-v1",
                "target_role": "应用软件工程师",
                "jd_input": {"kind": "continue_without_jd"},
            },
        },
    )
    assert continued["status"] == "ok"
    continued_job_input = continued["job_input"]
    assert isinstance(continued_job_input, dict)
    continued_sha256 = continued_job_input["validation_sha256"]
    assert isinstance(continued_sha256, str)
    scanned = _send_json(
        broker,
        {
            "op": "scan",
            "job_input_validation_sha256": continued_sha256,
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
        },
    )
    assert scanned["status"] == "ok"
    _stop_broker(broker)


def test_scan_overview_reuses_a_terminal_run_in_a_new_task(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text(
        '[project]\nname = "overview-reuse"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    (workspace / "main.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    data_dir = tmp_path / "data"

    first_broker = _start_broker(data_dir)
    first_authorized = _send_json(
        first_broker,
        {"op": "authorize_source_analysis", "workspace": str(workspace), "confirmed": True},
    )
    first_receipt = first_authorized["receipt"]
    assert isinstance(first_receipt, dict)
    first_validated = _send_json(
        first_broker,
        {
            "op": "validate_job_input",
            "workspace": str(workspace),
            "authorization_receipt_id": first_receipt["authorization_receipt_id"],
            "job_input": {
                "contract_version": "job-input-v1",
                "target_role": "应用软件工程师",
                "jd_input": {"kind": "none"},
            },
        },
    )
    first_job = first_validated["job_input"]
    assert isinstance(first_job, dict)
    scanned = _send_json(
        first_broker,
        {
            "op": "scan",
            "workspace": str(workspace),
            "authorization_receipt_id": first_receipt["authorization_receipt_id"],
            "job_input_validation_sha256": first_job["validation_sha256"],
        },
    )
    first_run = scanned["scan_run"]
    assert isinstance(first_run, dict)
    first_scan_run_id = first_run["scan_run_id"]
    _stop_broker(first_broker)

    second_broker = _start_broker(data_dir)
    second_authorized = _send_json(
        second_broker,
        {"op": "authorize_source_analysis", "workspace": str(workspace), "confirmed": True},
    )
    second_receipt = second_authorized["receipt"]
    assert isinstance(second_receipt, dict)
    second_validated = _send_json(
        second_broker,
        {
            "op": "validate_job_input",
            "workspace": str(workspace),
            "authorization_receipt_id": second_receipt["authorization_receipt_id"],
            "job_input": {
                "contract_version": "job-input-v1",
                "target_role": "系统工程师",
                "jd_input": {"kind": "none"},
            },
        },
    )
    second_job = second_validated["job_input"]
    assert isinstance(second_job, dict)
    overview_response = _send_json(
        second_broker,
        {
            "op": "scan_overview",
            "workspace": str(workspace),
            "authorization_receipt_id": second_receipt["authorization_receipt_id"],
            "job_input_validation_sha256": second_job["validation_sha256"],
        },
    )
    overview = overview_response["scan_overview"]
    assert isinstance(overview, dict)
    assert overview["found"] is True
    reused_run = overview["scan_run"]
    assert isinstance(reused_run, dict)
    assert reused_run["scan_run_id"] == first_scan_run_id
    assert overview["coverage"] == scanned["coverage"]

    connection = sqlite3.connect(data_dir / "goodjob.sqlite3")
    assert connection.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM scan_run_overviews").fetchone()[0] == 1
    connection.close()
    _stop_broker(second_broker)


def test_maximum_escaped_jd_crosses_the_broker_for_validation_and_prepare(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text(
        '[project]\nname = "large-jd"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (workspace / "main.py").write_text("def serve():\n    return True\n", encoding="utf-8")
    large_jd = "\n" * (512 * 1024 - 1) + "x"
    assert len(large_jd.encode("utf-8")) == 512 * 1024
    broker = _start_broker(tmp_path / "data")
    authorized = _send_json(
        broker,
        {"op": "authorize_source_analysis", "workspace": str(workspace), "confirmed": True},
    )
    receipt = authorized["receipt"]
    assert isinstance(receipt, dict)
    receipt_id = receipt["authorization_receipt_id"]
    validated = _send_json(
        broker,
        {
            "op": "validate_job_input",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
            "job_input": {
                "contract_version": "job-input-v1",
                "target_role": "中间件工程师",
                "jd_input": {"kind": "text", "text": large_jd},
            },
        },
    )
    job_input = validated["job_input"]
    assert isinstance(job_input, dict)
    validation_sha256 = job_input["validation_sha256"]
    scanned = _send_json(
        broker,
        {
            "op": "scan",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
            "job_input_validation_sha256": validation_sha256,
        },
    )
    scan_run = scanned["scan_run"]
    assert isinstance(scan_run, dict)
    prepared = _send_json(
        broker,
        {
            "op": "prepare_start",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
            "preparation_request": {
                "contract_version": "preparation-request-v1",
                "request_id": str(uuid.uuid4()),
                "scan_run_id": scan_run["scan_run_id"],
                "config_revision": "large-jd-test-v1",
                "target_role": "中间件工程师",
                "jd_input": {"kind": "text", "text": large_jd},
                "job_input_validation_sha256": validation_sha256,
                "requested_exports": [],
                "role_lens": {
                    "contract_version": "role-lens-v1",
                    "dimensions": [
                        {
                            "key": "middleware_depth",
                            "display_name": "中间件深度",
                            "weight_bps": 10000,
                            "evaluation_criteria": "评价实现证据",
                            "required_evidence_kinds": ["implementation"],
                        }
                    ],
                    "evidence_requirements": ["implementation"],
                    "ranking_rules": ["岗位相关性优先"],
                    "output_sections": ["中间件能力"],
                    "question_strategy": {"primary": "追问实现机制"},
                    "gap_rules": ["缺少证据则记录缺口"],
                    "assumptions": [],
                    "generator_id": "large-jd-test",
                    "prompt_contract_version": "large-jd-test-v1",
                },
            },
        },
    )
    preparation_run = prepared["preparation_run"]
    assert isinstance(preparation_run, dict)
    assert preparation_run["status"] == "analyzing"
    assert large_jd not in str(prepared)
    _stop_broker(broker)


def test_preparation_protocol_does_not_echo_private_payload_and_is_task_bound(
    tmp_path: Path,
    git_sandbox_available: None,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text(
        '[project]\nname = "session-prepare"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    (workspace / "main.py").write_text("def main():\n    return 0\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)
    subprocess.run(
        ["git", "add", "pyproject.toml", "main.py"],
        cwd=workspace,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=GoodJob Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "fixture",
        ],
        cwd=workspace,
        check=True,
        capture_output=True,
    )
    data_dir = tmp_path / "data"
    request_id = str(uuid.uuid4())
    request: dict[str, object] = {
        "contract_version": "preparation-request-v1",
        "request_id": request_id,
        "scan_run_id": "filled-after-scan",
        "config_revision": "prepare-config-v1",
        "target_role": "平台工程师",
        "jd_input": {"kind": "text", "text": "负责平台工程和可靠性。"},
        "requested_exports": [],
        "role_lens": {
            "contract_version": "role-lens-v1",
            "dimensions": [
                {
                    "key": "platform_depth",
                    "display_name": "平台深度",
                    "weight_bps": 10000,
                    "evaluation_criteria": "评价实现和运行边界",
                    "required_evidence_kinds": ["implementation"],
                }
            ],
            "evidence_requirements": ["implementation"],
            "ranking_rules": ["岗位相关性优先"],
            "output_sections": ["能力地图", "项目讲解"],
            "question_strategy": {"primary": "追问机制"},
            "gap_rules": ["证据不足则标记缺口"],
            "assumptions": [],
            "generator_id": "codex-test",
            "prompt_contract_version": "role-prompt-v1",
        },
    }
    broker = _start_broker(data_dir)
    authorized = _send_json(
        broker,
        {"op": "authorize_source_analysis", "workspace": str(workspace), "confirmed": True},
    )
    receipt = authorized["receipt"]
    assert isinstance(receipt, dict)
    receipt_id = receipt["authorization_receipt_id"]
    assert isinstance(receipt_id, str)
    validated = _send_json(
        broker,
        {
            "op": "validate_job_input",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
            "job_input": {
                "contract_version": "job-input-v1",
                "target_role": request["target_role"],
                "jd_input": request["jd_input"],
            },
        },
    )
    assert validated["status"] == "ok"
    assert "负责平台工程和可靠性" not in json.dumps(validated, ensure_ascii=False)
    validated_job_input = validated["job_input"]
    assert isinstance(validated_job_input, dict)
    validation_sha256 = validated_job_input["validation_sha256"]
    assert isinstance(validation_sha256, str)
    request["job_input_validation_sha256"] = validation_sha256
    scanned = _send_json(
        broker,
        {
            "op": "scan",
            "job_input_validation_sha256": validation_sha256,
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
        },
    )
    scan_run = scanned["scan_run"]
    assert isinstance(scan_run, dict)
    scan_run_id = scan_run["scan_run_id"]
    assert isinstance(scan_run_id, str)
    request["scan_run_id"] = scan_run_id

    prepared = _send_json(
        broker,
        {
            "op": "prepare_start",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
            "preparation_request": request,
        },
    )
    assert prepared["status"] == "ok"
    assert "负责平台工程和可靠性" not in json.dumps(prepared, ensure_ascii=False)
    preparation_run = prepared["preparation_run"]
    evidence_bundle = prepared["evidence_bundle"]
    assert isinstance(preparation_run, dict)
    assert isinstance(evidence_bundle, dict)
    preparation_run_id = preparation_run["preparation_run_id"]
    suggestions = evidence_bundle["deep_read_suggestions"]
    assert isinstance(preparation_run_id, str)
    assert isinstance(suggestions, list) and suggestions
    suggestion = suggestions[0]
    assert isinstance(suggestion, dict)
    source_revision_id = suggestion["source_revision_id"]
    assert isinstance(source_revision_id, str)
    reauthorized = _send_json(
        broker,
        {"op": "authorize_source_analysis", "workspace": str(workspace), "confirmed": True},
    )
    replacement_receipt = reauthorized["receipt"]
    assert isinstance(replacement_receipt, dict)
    replacement_receipt_id = replacement_receipt["authorization_receipt_id"]
    assert isinstance(replacement_receipt_id, str)
    repeated = _send_json(
        broker,
        {
            "op": "prepare_start",
            "workspace": str(workspace),
            "authorization_receipt_id": replacement_receipt_id,
            "preparation_request": request,
        },
    )
    assert repeated["status"] == "ok"
    repeated_run = repeated["preparation_run"]
    assert isinstance(repeated_run, dict)
    assert repeated_run["preparation_run_id"] == preparation_run_id
    checked = _send_json(
        broker,
        {
            "op": "verify_source_revision",
            "workspace": str(workspace),
            "authorization_receipt_id": replacement_receipt_id,
            "preparation_run_id": preparation_run_id,
            "source_revision_ids": [source_revision_id],
            "phase": "before_read",
        },
    )
    assert checked["status"] == "ok"
    assert checked["run_status"] == "analyzing"
    _stop_broker(broker)

    next_task = _start_broker(data_dir)
    next_authorized = _send_json(
        next_task,
        {"op": "authorize_source_analysis", "workspace": str(workspace), "confirmed": True},
    )
    next_receipt = next_authorized["receipt"]
    assert isinstance(next_receipt, dict)
    next_receipt_id = next_receipt["authorization_receipt_id"]
    assert isinstance(next_receipt_id, str)
    next_validated = _send_json(
        next_task,
        {
            "op": "validate_job_input",
            "workspace": str(workspace),
            "authorization_receipt_id": next_receipt_id,
            "job_input": {
                "contract_version": "job-input-v1",
                "target_role": request["target_role"],
                "jd_input": request["jd_input"],
            },
        },
    )
    assert next_validated["status"] == "ok"
    copied_request = _send_json(
        next_task,
        {
            "op": "prepare_start",
            "workspace": str(workspace),
            "authorization_receipt_id": next_receipt_id,
            "preparation_request": request,
        },
    )
    assert copied_request["status"] == "error"
    assert copied_request["code"] == "authorization_session_mismatch"
    copied_run = _send_json(
        next_task,
        {
            "op": "verify_source_revision",
            "workspace": str(workspace),
            "authorization_receipt_id": next_receipt_id,
            "preparation_run_id": preparation_run_id,
            "source_revision_ids": [source_revision_id],
            "phase": "before_read",
        },
    )
    assert copied_run["status"] == "error"
    assert copied_run["code"] == "invalid_input"
    _stop_broker(next_task)


def test_record_analysis_is_private_task_bound_and_idempotent(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "tests").mkdir(parents=True)
    (workspace / "pyproject.toml").write_text(
        '[project]\nname = "broker-analysis"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    (workspace / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    (workspace / "tests" / "test_app.py").write_text(
        "from app import run\n\ndef test_run():\n    assert run() == 1\n",
        encoding="utf-8",
    )
    data_dir = tmp_path / "data"
    broker = _start_broker(data_dir)
    authorized = _send_json(
        broker,
        {"op": "authorize_source_analysis", "workspace": str(workspace), "confirmed": True},
    )
    receipt = authorized["receipt"]
    assert isinstance(receipt, dict)
    receipt_id = receipt["authorization_receipt_id"]
    validated = _send_json(
        broker,
        {
            "op": "validate_job_input",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
            "job_input": {
                "contract_version": "job-input-v1",
                "target_role": "应用软件工程师",
                "jd_input": {"kind": "none"},
            },
        },
    )
    job = validated["job_input"]
    assert isinstance(job, dict)
    scanned = _send_json(
        broker,
        {
            "op": "scan",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
            "job_input_validation_sha256": job["validation_sha256"],
        },
    )
    scan_run = scanned["scan_run"]
    assert isinstance(scan_run, dict)
    prepared = _send_json(
        broker,
        {
            "op": "prepare_start",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
            "preparation_request": {
                "contract_version": "preparation-request-v1",
                "request_id": str(uuid.uuid4()),
                "scan_run_id": scan_run["scan_run_id"],
                "config_revision": "broker-analysis-v1",
                "target_role": "应用软件工程师",
                "jd_input": {"kind": "none"},
                "job_input_validation_sha256": job["validation_sha256"],
                "requested_exports": [],
                "evidence_limit_per_project": 100,
                "role_lens": {
                    "contract_version": "role-lens-v1",
                    "dimensions": [
                        {
                            "key": "implementation",
                            "display_name": "实现",
                            "weight_bps": 6000,
                            "evaluation_criteria": "评价当前实现",
                            "required_evidence_kinds": ["implementation"],
                        },
                        {
                            "key": "verification",
                            "display_name": "验证",
                            "weight_bps": 4000,
                            "evaluation_criteria": "评价测试定义",
                            "required_evidence_kinds": ["test_definition"],
                        },
                    ],
                    "evidence_requirements": ["implementation", "test_definition"],
                    "ranking_rules": ["证据覆盖优先"],
                    "output_sections": ["项目讲解"],
                    "question_strategy": {"primary": "追问实现"},
                    "gap_rules": ["缺少证据时保留缺口"],
                    "assumptions": ["未提供 JD"],
                    "generator_id": "broker-test",
                    "prompt_contract_version": "broker-test-v1",
                },
            },
        },
    )
    preparation_run = prepared["preparation_run"]
    role_lens = prepared["role_lens"]
    bundle = prepared["evidence_bundle"]
    assert isinstance(preparation_run, dict)
    assert isinstance(role_lens, dict)
    assert isinstance(bundle, dict)
    evidence_items = bundle["evidence_items"]
    coverage = bundle["coverage"]
    assert isinstance(evidence_items, list)
    assert isinstance(coverage, list)
    implementation = next(
        item
        for item in evidence_items
        if isinstance(item, dict) and item["evidence_kind"] == "implementation"
    )
    test_definition = next(
        item
        for item in evidence_items
        if isinstance(item, dict) and item["evidence_kind"] == "test_definition"
    )
    project = next(item for item in coverage if isinstance(item, dict) and item["eligible"])
    assert isinstance(implementation, dict)
    assert isinstance(test_definition, dict)
    assert isinstance(project, dict)
    context_requested = _send_json(
        broker,
        {
            "op": "request_context",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
            "context_interview_request": {
                "contract_version": "context-interview-request-v1",
                "request_id": str(uuid.uuid4()),
                "preparation_run_id": preparation_run["preparation_run_id"],
                "question_set_version": "broker-context-v1",
                "cards": [
                    {
                        "project_id": project["project_id"],
                        "questions": [
                            {
                                "question_id": "owner-role",
                                "fact_kinds": ["role"],
                                "prompt": "你在项目中承担什么职责？",
                            },
                            {
                                "question_id": "owner-learning",
                                "fact_kinds": ["learning"],
                                "prompt": "你从项目中学到了什么？",
                            },
                        ],
                    }
                ],
            },
        },
    )
    context_interview = context_requested["context_interview"]
    assert isinstance(context_interview, dict)
    context_answered = _send_json(
        broker,
        {
            "op": "interview",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
            "interview_input": {
                "contract_version": "interview-input-v1",
                "request_id": str(uuid.uuid4()),
                "mode": "context",
                "preparation_run_id": preparation_run["preparation_run_id"],
                "context_interview_id": context_interview["context_interview_id"],
                "answers": [
                    {
                        "project_id": project["project_id"],
                        "status": "answered",
                        "structured_answer": {
                            "owner-role": "负责可测试入口的实现。",
                            "owner-learning": "学会了用边界测试固定行为。",
                        },
                        "facts": [
                            {
                                "fact_key": "owner-role",
                                "fact_kind": "role",
                                "statement": "负责可测试入口的实现。",
                            },
                            {
                                "fact_key": "owner-learning",
                                "fact_kind": "learning",
                                "statement": "学会了用边界测试固定行为。",
                            },
                        ],
                    }
                ],
            },
        },
    )
    context_batch = context_answered["context_answer_batch"]
    assert isinstance(context_batch, dict)
    context_answers = context_batch["answers"]
    assert isinstance(context_answers, list)
    context_answer = context_answers[0]
    assert isinstance(context_answer, dict)
    context_facts = context_answer["facts"]
    assert isinstance(context_facts, list)
    role_fact = next(
        fact for fact in context_facts if isinstance(fact, dict) and fact["fact_kind"] == "role"
    )
    assert isinstance(role_fact, dict)
    role_evidence_id = role_fact["evidence_id"]
    assert isinstance(role_evidence_id, str)
    first_page = _send_json(
        broker,
        {
            "op": "list_context_evidence",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
            "context_evidence_request": {
                "contract_version": "context-evidence-page-request-v1",
                "preparation_run_id": preparation_run["preparation_run_id"],
                "project_id": project["project_id"],
                "limit": 1,
            },
        },
    )
    page = first_page["context_evidence_page"]
    assert isinstance(page, dict)
    assert page["item_count"] == 1
    assert page["total_items"] == 2
    assert page["has_more"] is True
    cursor = page["next_cursor"]
    assert isinstance(cursor, str)
    second_page = _send_json(
        broker,
        {
            "op": "list_context_evidence",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
            "context_evidence_request": {
                "contract_version": "context-evidence-page-request-v1",
                "preparation_run_id": preparation_run["preparation_run_id"],
                "project_id": project["project_id"],
                "cursor": cursor,
                "limit": 1,
            },
        },
    )
    second_page_value = second_page["context_evidence_page"]
    assert isinstance(second_page_value, dict)
    assert second_page_value["item_count"] == 1
    assert second_page_value["has_more"] is False
    paged_items = page["items"] + second_page_value["items"]
    assert isinstance(paged_items, list)
    assert {item["evidence_id"] for item in paged_items if isinstance(item, dict)} == {
        fact["evidence_id"] for fact in context_facts if isinstance(fact, dict)
    }
    analysis_request: dict[str, object] = {
        "contract_version": "analysis-commit-v1",
        "request_id": str(uuid.uuid4()),
        "preparation_run_id": preparation_run["preparation_run_id"],
        "role_lens_id": role_lens["role_lens_id"],
        "evidence_drafts": [],
        "claim_drafts": [
            {
                "draft_id": "broker-claim",
                "claim_key": "broker-analysis",
                "category": "implementation_method",
                "scope_kind": "project",
                "project_id": project["project_id"],
                "section": "project_story",
                "statement_tokens": [{"kind": "text", "value": "我实现了可测试入口。"}],
                "facets": ["implemented", "test_defined"],
                "support_level": "cross_checked",
                "personal_attribution": "implemented",
                "review_semantic_projection": {
                    "concept_keys": ["entry"],
                    "mechanism_keys": ["function"],
                    "behavior_contract_keys": ["return-value"],
                    "tradeoff_keys": [],
                    "technology_identifiers": ["python"],
                },
                "evidence_relations": [
                    {
                        "evidence_ref": implementation["evidence_id"],
                        "relation": "supports",
                        "supported_facets": ["implemented"],
                    },
                    {
                        "evidence_ref": test_definition["evidence_id"],
                        "relation": "supports",
                        "supported_facets": ["test_defined"],
                    },
                    {
                        "evidence_ref": role_evidence_id,
                        "relation": "contextualizes",
                        "supported_facets": [],
                    },
                ],
            }
        ],
        "project_assessments": [
            {
                "project_id": project["project_id"],
                "dimension_scores_milli": {"implementation": 800, "verification": 700},
                "coverage_bps": 10000,
                "evidence_refs": [
                    implementation["evidence_id"],
                    test_definition["evidence_id"],
                    role_evidence_id,
                ],
                "gap_refs": [],
                "rationale_tokens": [{"kind": "text", "value": "实现与测试定义均可追溯。"}],
            }
        ],
        "knowledge_gaps": [],
    }
    first = _send_json(
        broker,
        {
            "op": "record_analysis",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
            "analysis_commit_request": analysis_request,
        },
    )
    repeated = _send_json(
        broker,
        {
            "op": "record_analysis",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
            "analysis_commit_request": analysis_request,
        },
    )
    assert first == repeated
    assert first["run_status"] == "ready"
    rendered = _send_json(
        broker,
        {
            "op": "render",
            "preparation_run_id": preparation_run["preparation_run_id"],
        },
    )
    repeated_render = _send_json(
        broker,
        {
            "op": "render",
            "preparation_run_id": preparation_run["preparation_run_id"],
        },
    )
    snapshot = rendered["artifact_snapshot"]
    assert isinstance(snapshot, dict)
    assert snapshot["preparation_run_id"] == preparation_run["preparation_run_id"]
    assert repeated_render == rendered
    translation_prepared = _send_json(
        broker,
        {
            "op": "translate_export",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
            "translation_export_request": {
                "contract_version": "translation-export-request-v1",
                "action": "prepare",
                "source_artifact_snapshot_id": snapshot["artifact_snapshot_id"],
                "target_language": "en",
                "export_kinds": ["resume", "interview_qa"],
            },
        },
    )
    translation_source = translation_prepared["translation_source"]
    assert isinstance(translation_source, dict)
    translation_publish_input = _translation_publish_input(translation_source)
    translation_published = _send_json(
        broker,
        {
            "op": "translate_export",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
            "translation_export_request": translation_publish_input,
        },
    )
    derived_export = translation_published["derived_export"]
    assert isinstance(derived_export, dict)
    assert Path(derived_export["resume_markdown_path"]).is_file()
    assert Path(derived_export["interview_qa_markdown_path"]).is_file()
    _stop_broker(broker)

    fresh_broker = _start_broker(data_dir)
    fresh_render = _send_json(
        fresh_broker,
        {
            "op": "render",
            "preparation_run_id": preparation_run["preparation_run_id"],
        },
    )
    assert fresh_render == rendered
    fresh_authorized = _send_json(
        fresh_broker,
        {"op": "authorize_source_analysis", "workspace": str(workspace), "confirmed": True},
    )
    fresh_receipt = fresh_authorized["receipt"]
    assert isinstance(fresh_receipt, dict)
    rejected_translation_publish = _send_json(
        fresh_broker,
        {
            "op": "translate_export",
            "workspace": str(workspace),
            "authorization_receipt_id": fresh_receipt["authorization_receipt_id"],
            "translation_export_request": translation_publish_input,
        },
    )
    assert rejected_translation_publish["status"] == "error"
    assert rejected_translation_publish["code"] == "invalid_input"
    mock_targets = _send_json(
        fresh_broker,
        {
            "op": "interview",
            "workspace": str(workspace),
            "authorization_receipt_id": fresh_receipt["authorization_receipt_id"],
            "interview_input": {
                "contract_version": "interview-input-v1",
                "mode": "mock_review",
                "action": "list_targets",
                "preparation_run_id": preparation_run["preparation_run_id"],
            },
        },
    )
    mock_review = mock_targets["mock_review"]
    assert isinstance(mock_review, dict)
    mock_questions = mock_review["questions"]
    assert isinstance(mock_questions, list) and mock_questions
    mock_question = mock_questions[0]
    assert isinstance(mock_question, dict)
    recorded_review = _send_json(
        fresh_broker,
        {
            "op": "interview",
            "workspace": str(workspace),
            "authorization_receipt_id": fresh_receipt["authorization_receipt_id"],
            "interview_input": {
                "contract_version": "interview-input-v1",
                "request_id": str(uuid.uuid4()),
                "mode": "mock_review",
                "action": "record_review",
                "preparation_run_id": preparation_run["preparation_run_id"],
                "review_target_binding_id": mock_question["review_target_binding_id"],
                "question_id": mock_question["question_id"],
                "review": {
                    "summary": "能够解释主路径，异常边界仍需练习。",
                    "mastery_level": "developing",
                    "weak_points": ["异常边界"],
                    "next_review_at": "2026-08-20",
                },
            },
        },
    )
    interview_review = recorded_review["interview_review"]
    assert isinstance(interview_review, dict)
    assert interview_review["preparation_run_id"] == preparation_run["preparation_run_id"]
    rejected_page = _send_json(
        fresh_broker,
        {
            "op": "list_context_evidence",
            "workspace": str(workspace),
            "authorization_receipt_id": fresh_receipt["authorization_receipt_id"],
            "context_evidence_request": {
                "contract_version": "context-evidence-page-request-v1",
                "preparation_run_id": preparation_run["preparation_run_id"],
                "limit": 1,
            },
        },
    )
    assert rejected_page["status"] == "error"
    assert rejected_page["code"] == "invalid_input"
    rejected = _send_json(
        fresh_broker,
        {
            "op": "record_analysis",
            "workspace": str(workspace),
            "authorization_receipt_id": fresh_receipt["authorization_receipt_id"],
            "analysis_commit_request": analysis_request,
        },
    )
    assert rejected["status"] == "error"
    assert rejected["code"] == "invalid_input"
    _stop_broker(fresh_broker)
