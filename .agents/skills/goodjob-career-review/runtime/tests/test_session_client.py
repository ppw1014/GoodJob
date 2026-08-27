from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from goodjob.session_client import (
    BrokerProcessError,
    BrokerProtocolError,
    BrokerTimeoutError,
    SessionClient,
    SessionPreflightError,
)


def _write_invalid_launcher(path: Path, broker_started_path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import json
import pathlib
import sys
import time

if "--preflight-only" in sys.argv:
    print(json.dumps({"status": "ok", "can_start_broker": True, "checks": []}))
    print("untrusted launcher noise", file=sys.stderr)
    raise SystemExit(9)

pathlib.Path(BROKER_STARTED).write_text("started")
time.sleep(30)
""".replace("BROKER_STARTED", repr(str(broker_started_path))),
        encoding="utf-8",
    )


def _write_session_launcher(
    path: Path,
    report: dict[str, object],
    operations: Path,
    *,
    handshake_stderr_bytes: int = 0,
    stall_handshake: bool = False,
) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import json
import pathlib
import sys
import time

REPORT = json.loads(REPORT_JSON)
OPERATIONS = pathlib.Path(OPERATIONS_PATH)
HANDSHAKE_STDERR_BYTES = HANDSHAKE_STDERR_BYTES_VALUE
STALL_HANDSHAKE = STALL_HANDSHAKE_VALUE

if "--preflight-only" in sys.argv:
    print(json.dumps(REPORT))
    raise SystemExit(0)

for line in sys.stdin:
    request = json.loads(line)
    with OPERATIONS.open("a", encoding="utf-8") as stream:
        stream.write(request["op"] + "\\n")
    if request["op"] == "malformed_response":
        print("not-json", flush=True)
        continue
    if request["op"] == "session_handshake":
        if HANDSHAKE_STDERR_BYTES:
            sys.stderr.write("x" * HANDSHAKE_STDERR_BYTES)
            sys.stderr.flush()
        if STALL_HANDSHAKE:
            time.sleep(30)
        response = {
            "status": "ok",
            "session": {"contract_version": "goodjob-session-v1", "state": "ready"},
        }
    elif request["op"] == "session_complete":
        response = {
            "status": "ok",
            "session": {"contract_version": "goodjob-session-v1", "state": "completed"},
        }
    elif request["op"] == "session_cancel":
        response = {
            "status": "ok",
            "session": {"contract_version": "goodjob-session-v1", "state": "cancelled"},
        }
    else:
        response = {"status": "ok"}
    print(json.dumps(response), flush=True)
""".replace("REPORT_JSON", repr(json.dumps(report)))
        .replace("OPERATIONS_PATH", repr(str(operations)))
        .replace("HANDSHAKE_STDERR_BYTES_VALUE", str(handshake_stderr_bytes))
        .replace("STALL_HANDSHAKE_VALUE", repr(stall_handshake)),
        encoding="utf-8",
    )


def _write_stalling_preflight_launcher(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import time

time.sleep(30)
""",
        encoding="utf-8",
    )


def test_session_client_rejects_malformed_preflight_envelope(tmp_path: Path) -> None:
    launcher = tmp_path / "fake_launcher.py"
    _write_invalid_launcher(launcher, tmp_path / "broker-started")
    client = SessionClient(launcher_script=launcher)

    with pytest.raises(SessionPreflightError, match="contract"):
        client.run_preflight()


def test_session_client_start_cannot_bypass_strict_preflight(tmp_path: Path) -> None:
    launcher = tmp_path / "fake_launcher.py"
    broker_started = tmp_path / "broker-started"
    _write_invalid_launcher(launcher, broker_started)
    client = SessionClient(launcher_script=launcher)

    try:
        with pytest.raises(SessionPreflightError, match="contract"):
            client.start()
        assert not broker_started.exists()
    finally:
        client.close()


def test_session_client_start_requires_versioned_handshake(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    report = SessionClient(workspace=workspace).run_preflight()
    launcher = tmp_path / "fake_launcher.py"
    operations = tmp_path / "operations"
    _write_session_launcher(launcher, report, operations)

    client = SessionClient(workspace=workspace, launcher_script=launcher)
    with client:
        assert client.is_running is True

    assert operations.read_text(encoding="utf-8").splitlines()[0] == "session_handshake"


def test_session_client_complete_closes_the_task_scoped_broker(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    report = SessionClient(workspace=workspace).run_preflight()
    launcher = tmp_path / "fake_launcher.py"
    operations = tmp_path / "operations"
    _write_session_launcher(launcher, report, operations)

    client = SessionClient(workspace=workspace, launcher_script=launcher)
    try:
        client.start()
        response = client.complete()
    finally:
        client.close()

    assert response == {
        "status": "ok",
        "session": {"contract_version": "goodjob-session-v1", "state": "completed"},
    }
    assert operations.read_text(encoding="utf-8").splitlines() == [
        "session_handshake",
        "session_complete",
    ]
    assert client.is_running is False


def test_session_client_cancel_closes_the_task_scoped_broker(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    report = SessionClient(workspace=workspace).run_preflight()
    launcher = tmp_path / "fake_launcher.py"
    operations = tmp_path / "operations"
    _write_session_launcher(launcher, report, operations)

    client = SessionClient(workspace=workspace, launcher_script=launcher)
    try:
        client.start()
        response = client.cancel()
    finally:
        client.close()

    assert response == {
        "status": "ok",
        "session": {"contract_version": "goodjob-session-v1", "state": "cancelled"},
    }
    assert operations.read_text(encoding="utf-8").splitlines() == [
        "session_handshake",
        "session_cancel",
    ]
    assert client.is_running is False


def test_session_client_malformed_response_fails_closed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    report = SessionClient(workspace=workspace).run_preflight()
    launcher = tmp_path / "fake_launcher.py"
    _write_session_launcher(launcher, report, tmp_path / "operations")

    client = SessionClient(workspace=workspace, launcher_script=launcher)
    try:
        client.start()
        with pytest.raises(BrokerProtocolError, match="Invalid JSON"):
            client.send({"op": "malformed_response"})
        assert client.is_running is False
    finally:
        client.close()


def test_session_client_handshake_timeout_reaps_broker(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    report = SessionClient(workspace=workspace).run_preflight()
    launcher = tmp_path / "fake_launcher.py"
    _write_session_launcher(
        launcher,
        report,
        tmp_path / "operations",
        stall_handshake=True,
    )
    client = SessionClient(
        workspace=workspace,
        launcher_script=launcher,
        timeout_seconds=0.1,
    )

    with pytest.raises(BrokerTimeoutError, match="bounded timeout"):
        client.start()

    assert client.is_running is False


def test_session_client_drains_broker_stderr_while_waiting_for_response(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    report = SessionClient(workspace=workspace).run_preflight()
    launcher = tmp_path / "fake_launcher.py"
    _write_session_launcher(
        launcher,
        report,
        tmp_path / "operations",
        handshake_stderr_bytes=256 * 1024,
    )
    client = SessionClient(
        workspace=workspace,
        launcher_script=launcher,
        timeout_seconds=2.0,
    )

    with client:
        assert client.is_running is True

    assert client.is_running is False


def test_session_client_preflight_timeout_reaps_process(tmp_path: Path) -> None:
    launcher = tmp_path / "fake_launcher.py"
    _write_stalling_preflight_launcher(launcher)
    client = SessionClient(launcher_script=launcher, timeout_seconds=0.1)

    with pytest.raises(SessionPreflightError, match="bounded timeout"):
        client.run_preflight()


def test_session_client_preflight(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    client = SessionClient(workspace=workspace, data_dir=tmp_path / "data")
    report = client.run_preflight()

    assert isinstance(report, dict)
    assert "can_start_broker" in report
    assert "status" in report
    assert "checks" in report


def test_session_client_lifecycle_and_happy_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "package.json").write_text('{"name": "test-app"}', encoding="utf-8")

    data_dir = tmp_path / "data"
    client = SessionClient(workspace=workspace, data_dir=data_dir)

    with client:
        assert client.is_running is True

        # 1. Authorize source analysis
        auth_resp = client.authorize_source_analysis(confirmed=True)
        assert auth_resp["status"] == "ok"
        receipt_id = auth_resp["receipt"]["authorization_receipt_id"]

        # 2. Validate Job Input
        val_resp = client.validate_job_input(
            authorization_receipt_id=receipt_id, target_role="系统工程师"
        )
        assert val_resp["status"] == "ok"
        val_sha = val_resp["job_input"]["validation_sha256"]

        # 3. Scan
        scan_resp = client.scan(
            authorization_receipt_id=receipt_id,
            job_input_validation_sha256=val_sha,
        )
        assert scan_resp["status"] == "ok"
        assert scan_resp["scan_run"]["status"] in ("completed", "partial")

        # 4. Scan Overview
        overview_resp = client.scan_overview(
            authorization_receipt_id=receipt_id,
            job_input_validation_sha256=val_sha,
        )
        assert overview_resp["status"] == "ok"
        overview = overview_resp["scan_overview"]
        assert overview["found"] is True
        assert "issue_groups" in overview

    # After with block, process must be closed and reaped
    assert client.is_running is False


def test_session_client_host_rejection(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    client = SessionClient(workspace=workspace, data_dir=tmp_path / "data")
    with client:
        resp = client.authorize_source_analysis(confirmed=False)
        assert resp["status"] == "error"
        assert resp["code"] == "invalid_input"


def test_session_client_requires_explicit_owner_confirmation_argument(tmp_path: Path) -> None:
    client = SessionClient(workspace=tmp_path)

    with pytest.raises(TypeError, match="confirmed"):
        client.authorize_source_analysis()  # type: ignore[call-arg]


def test_reference_host_displays_boundaries_and_honors_owner_rejection(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    data_dir = tmp_path / "owner-data"
    script = Path(__file__).parents[1] / "scripts" / "host_session_example.py"

    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            str(script),
            "--workspace",
            str(workspace),
            "--data-dir",
            str(data_dir),
        ],
        input="decline\n",
        capture_output=True,
        text=True,
        check=False,
        timeout=10.0,
    )

    assert completed.returncode == 1
    assert "Processing category: source_analysis" in completed.stdout
    assert "Local persistence:" in completed.stdout
    assert "Model processing:" in completed.stdout
    assert "Owner did not authorize" in completed.stdout
    assert not data_dir.exists()


def test_session_client_process_reap_on_exception(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    client = SessionClient(workspace=workspace, data_dir=tmp_path / "data")
    with pytest.raises(RuntimeError, match="simulated host error"), client:
        assert client.is_running is True
        raise RuntimeError("simulated host error")

    assert client.is_running is False


def test_session_client_premature_broker_exit(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    client = SessionClient(workspace=workspace, data_dir=tmp_path / "data")
    with client:
        assert client._process is not None
        client._process.kill()
        client._process.wait()

        with pytest.raises(BrokerProcessError):
            client.authorize_source_analysis(confirmed=True)

    assert client.is_running is False


def test_session_client_reaps_a_dead_broker_before_restart(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    client = SessionClient(workspace=workspace, data_dir=tmp_path / "data")

    try:
        client.start()
        assert client._process is not None
        first_process = client._process
        first_process.kill()
        first_process.wait(timeout=5)

        client.start()

        assert client.is_running is True
        assert client._process is not first_process
        assert first_process.stdin is None or first_process.stdin.closed
        assert first_process.stdout is None or first_process.stdout.closed
        assert first_process.stderr is None or first_process.stderr.closed
    finally:
        client.close()
