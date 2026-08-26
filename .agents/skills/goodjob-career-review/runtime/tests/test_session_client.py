from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from goodjob.session_client import (
    BrokerProcessError,
    BrokerProtocolError,
    SessionClient,
    SessionClientError,
    SessionPreflightError,
)


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
        val_resp = client.validate_job_input(authorization_receipt_id=receipt_id, target_role="系统工程师")
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


def test_session_client_process_reap_on_exception(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    client = SessionClient(workspace=workspace, data_dir=tmp_path / "data")
    with pytest.raises(RuntimeError, match="simulated host error"):
        with client:
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

