from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

from goodjob.errors import GoodJobError, InvalidInputError


class SessionClientError(GoodJobError):
    """Base error for host session client operations."""


class SessionPreflightError(SessionClientError):
    """Raised when platform launcher preflight fails before starting the broker."""


class BrokerProcessError(SessionClientError):
    """Raised when the broker subprocess exits abnormally or fails to start."""


class BrokerProtocolError(SessionClientError):
    """Raised when the broker protocol receives malformed or unexpected responses."""


class SessionClient:
    """Official task-scoped host session client for GoodJob broker operations.

    Encapsulates subprocess lifecycle, preflight verification, stdin/stdout JSON lines protocol,
    and process reclamation. Strictly adheres to single-task scope without daemons or background state.
    """

    def __init__(
        self,
        *,
        workspace: str | Path | None = None,
        data_dir: str | Path | None = None,
        agent_runtime: str = "codex_task_runtime",
        launcher_script: str | Path | None = None,
        timeout_seconds: float = 300.0,
    ) -> None:
        self.workspace = str(Path(workspace).expanduser().resolve(strict=False)) if workspace else None
        self.data_dir = str(Path(data_dir).expanduser().resolve()) if data_dir else None
        self.agent_runtime = agent_runtime
        self.timeout_seconds = timeout_seconds
        if launcher_script is not None:
            self.launcher_script = Path(launcher_script).resolve()
        else:
            runtime_dir = Path(__file__).resolve().parents[2]
            self.launcher_script = runtime_dir / "scripts" / "launch_broker.py"

        self._process: subprocess.Popen[str] | None = None

    def run_preflight(self) -> dict[str, Any]:
        """Run platform preflight check without starting the broker."""
        command = [
            sys.executable,
            "-I",
            "-B",
            str(self.launcher_script),
            "--preflight-only",
        ]
        if self.workspace:
            command.extend(["--workspace", self.workspace])
        if self.data_dir:
            command.extend(["--data-dir", self.data_dir])
        if self.agent_runtime:
            command.extend(["--agent-runtime", self.agent_runtime])

        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        stdout, stderr = process.communicate(timeout=60.0)
        try:
            report: Any = json.loads(stdout if stdout.strip() else stderr)
        except (json.JSONDecodeError, ValueError) as exc:
            raise SessionPreflightError(f"Preflight output is not valid JSON: {stdout or stderr}") from exc

        if not isinstance(report, dict):
            raise SessionPreflightError(f"Preflight output is not a JSON object: {report}")
        return cast(dict[str, Any], report)

    def start(self) -> SessionClient:
        """Start the broker child process."""
        if self._process is not None and self._process.poll() is None:
            return self

        command = [
            sys.executable,
            "-I",
            "-B",
            str(self.launcher_script),
        ]
        if self.workspace:
            command.extend(["--workspace", self.workspace])
        if self.data_dir:
            command.extend(["--data-dir", self.data_dir])
        if self.agent_runtime:
            command.extend(["--agent-runtime", self.agent_runtime])

        try:
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            raise BrokerProcessError(f"Failed to launch broker process: {exc}") from exc
        return self

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send a JSON payload to the broker and return the parsed JSON response."""
        if not self.is_running or self._process is None or self._process.stdin is None or self._process.stdout is None:
            raise BrokerProcessError("Broker process is not running")

        try:
            line = json.dumps(payload) + chr(10)
            self._process.stdin.write(line)
            self._process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            self.close()
            raise BrokerProcessError("Broker stdin write failed (process may have exited)") from exc

        try:
            response_line = self._process.stdout.readline()
        except (OSError, UnicodeDecodeError) as exc:
            self.close()
            raise BrokerProcessError("Broker stdout read failed") from exc

        if not response_line:
            exit_code = self._process.poll()
            stderr_out = ""
            if self._process.stderr is not None:
                with contextlib.suppress(Exception):
                    stderr_out = self._process.stderr.read()
            self.close()
            raise BrokerProcessError(
                f"Broker process exited unexpectedly with code {exit_code}: {stderr_out.strip()}"
            )

        try:
            response = json.loads(response_line)
        except (json.JSONDecodeError, ValueError) as exc:
            raise BrokerProtocolError(f"Invalid JSON line from broker: {response_line.strip()}") from exc

        if not isinstance(response, dict):
            raise BrokerProtocolError(f"Broker returned non-dict response: {response}")
        return cast(dict[str, Any], response)

    def authorize_source_analysis(
        self,
        workspace: str | None = None,
        *,
        confirmed: bool = True,
        notice_version: str = "goodjob-source-analysis-v1",
    ) -> dict[str, Any]:
        target = workspace or self.workspace
        if not target:
            raise InvalidInputError("workspace path is required")
        return self.send(
            {
                "op": "authorize_source_analysis",
                "workspace": target,
                "confirmed": confirmed,
                "notice_version": notice_version,
            }
        )

    def verify_source_analysis(
        self,
        authorization_receipt_id: str,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        target = workspace or self.workspace
        if not target:
            raise InvalidInputError("workspace path is required")
        return self.send(
            {
                "op": "verify_source_analysis",
                "workspace": target,
                "authorization_receipt_id": authorization_receipt_id,
            }
        )

    def validate_job_input(
        self,
        authorization_receipt_id: str,
        target_role: str,
        workspace: str | None = None,
        jd_input: dict[str, Any] | None = None,
        contract_version: str = "job-input-v1",
    ) -> dict[str, Any]:
        target = workspace or self.workspace
        if not target:
            raise InvalidInputError("workspace path is required")
        return self.send(
            {
                "op": "validate_job_input",
                "workspace": target,
                "authorization_receipt_id": authorization_receipt_id,
                "job_input": {
                    "contract_version": contract_version,
                    "target_role": target_role,
                    "jd_input": jd_input or {"kind": "none"},
                },
            }
        )

    def scan(
        self,
        authorization_receipt_id: str,
        job_input_validation_sha256: str,
        workspace: str | None = None,
        config_revision: str = "goodjob-scan-config-v1",
    ) -> dict[str, Any]:
        target = workspace or self.workspace
        if not target:
            raise InvalidInputError("workspace path is required")
        return self.send(
            {
                "op": "scan",
                "workspace": target,
                "authorization_receipt_id": authorization_receipt_id,
                "job_input_validation_sha256": job_input_validation_sha256,
                "config_revision": config_revision,
            }
        )

    def scan_overview(
        self,
        authorization_receipt_id: str,
        job_input_validation_sha256: str,
        workspace: str | None = None,
        scan_run_id: str | None = None,
    ) -> dict[str, Any]:
        target = workspace or self.workspace
        if not target:
            raise InvalidInputError("workspace path is required")
        payload: dict[str, Any] = {
            "op": "scan_overview",
            "workspace": target,
            "authorization_receipt_id": authorization_receipt_id,
            "job_input_validation_sha256": job_input_validation_sha256,
        }
        if scan_run_id is not None:
            payload["scan_run_id"] = scan_run_id
        return self.send(payload)

    def prepare_start(
        self,
        authorization_receipt_id: str,
        preparation_request: dict[str, Any],
        workspace: str | None = None,
    ) -> dict[str, Any]:
        target = workspace or self.workspace
        if not target:
            raise InvalidInputError("workspace path is required")
        return self.send(
            {
                "op": "prepare_start",
                "workspace": target,
                "authorization_receipt_id": authorization_receipt_id,
                "preparation_request": preparation_request,
            }
        )

    def close(self) -> None:
        """Clean up and ensure child process is reaped."""
        if self._process is None:
            return
        proc = self._process
        self._process = None
        if proc.stdin and not proc.stdin.closed:
            try:
                proc.stdin.close()
            except Exception:
                pass
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2.0)

    def __enter__(self) -> SessionClient:
        return self.start()

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()
