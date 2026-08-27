from __future__ import annotations

import contextlib
import json
import math
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, BinaryIO, cast

from goodjob.errors import GoodJobError, InvalidInputError
from goodjob.platform.launcher_preflight import (
    LauncherPreflightFact,
    apply_launcher_preflight_decision,
    classify_launcher_preflight,
)


class SessionClientError(GoodJobError):
    """Base error for host session client operations."""


class SessionPreflightError(SessionClientError):
    """Raised when platform launcher preflight fails before starting the broker."""


class BrokerProcessError(SessionClientError):
    """Raised when the broker subprocess exits abnormally or fails to start."""


class BrokerProtocolError(SessionClientError):
    """Raised when the broker protocol receives malformed or unexpected responses."""


class BrokerTimeoutError(BrokerProcessError):
    """Raised when a bounded broker exchange does not complete in time."""


MAX_BROKER_JSONL_BYTES = 64 * 1024
MAX_BROKER_PENDING_RESPONSES = 8
MAX_BROKER_STDERR_BYTES = 16 * 1024
_STREAM_POLL_SECONDS = 0.05


class _SessionPreflightRouter:
    """Record the trusted routing outcome without executing remediation actions."""

    def __init__(self) -> None:
        self.start_allowed = False
        self.runtime_contract_gap = False

    def start_broker(self) -> None:
        self.start_allowed = True

    def explain(self, _facts: tuple[LauncherPreflightFact, ...]) -> None:
        return None

    def request_explicit_consent(self, _facts: tuple[LauncherPreflightFact, ...]) -> None:
        return None

    def report_runtime_contract_gap(self) -> None:
        self.runtime_contract_gap = True


class SessionClient:
    """Official task-scoped host session client for GoodJob broker operations.

    Encapsulates subprocess lifecycle, preflight verification, stdin/stdout JSON lines protocol,
    and process reclamation. Strictly adheres to single-task scope without daemons.
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
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise InvalidInputError("timeout_seconds must be a finite positive number")
        self.workspace = (
            str(Path(workspace).expanduser().resolve(strict=False)) if workspace else None
        )
        self.data_dir = str(Path(data_dir).expanduser().resolve()) if data_dir else None
        self.agent_runtime = agent_runtime
        self.timeout_seconds = timeout_seconds
        if launcher_script is not None:
            self.launcher_script = Path(launcher_script).resolve()
        else:
            runtime_dir = Path(__file__).resolve().parents[2]
            self.launcher_script = runtime_dir / "scripts" / "launch_broker.py"

        self._process: subprocess.Popen[bytes] | None = None
        self._process_group_isolated = False
        self._stdout_lines: queue.Queue[str | BaseException | None] = queue.Queue(
            maxsize=MAX_BROKER_PENDING_RESPONSES
        )
        self._stdout_fault: BaseException | None = None
        self._stdout_fault_lock = threading.Lock()
        self._stderr_chunks: deque[bytes] = deque()
        self._stderr_size = 0
        self._stderr_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._reader_threads: tuple[threading.Thread, ...] = ()

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

        try:
            process, process_group_isolated = self._spawn_process(command)
        except OSError as exc:
            raise SessionPreflightError(f"Preflight process could not start: {exc}") from exc
        try:
            stdout_bytes, stderr_bytes = process.communicate(
                timeout=min(self.timeout_seconds, 60.0)
            )
        except subprocess.TimeoutExpired as exc:
            self._terminate_process_tree(process, process_group_isolated)
            raise SessionPreflightError("Preflight process exceeded its bounded timeout") from exc
        try:
            stdout = stdout_bytes.decode("utf-8")
            stderr = stderr_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SessionPreflightError("Launcher preflight output must be valid UTF-8") from exc
        try:
            report: Any = json.loads(stdout)
        except (json.JSONDecodeError, ValueError):
            report = None
        decision = classify_launcher_preflight(
            report,
            exit_code=process.returncode,
            stderr=stderr,
        )
        router = _SessionPreflightRouter()
        apply_launcher_preflight_decision(decision, router)
        if router.runtime_contract_gap:
            raise SessionPreflightError("Launcher preflight contract validation failed")
        if not router.start_allowed:
            codes = ", ".join(decision.failure_codes) or "unknown"
            raise SessionPreflightError(f"Launcher preflight blocked broker startup: {codes}")
        if not isinstance(report, dict):
            raise AssertionError("validated launcher preflight reports are JSON objects")
        return cast(dict[str, Any], report)

    def start(self) -> SessionClient:
        """Start the broker child process."""
        if self._process is not None:
            if self._process.poll() is None:
                return self
            self.close()

        self.run_preflight()

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
            self._process, self._process_group_isolated = self._spawn_process(
                command, stdin=subprocess.PIPE
            )
        except OSError as exc:
            raise BrokerProcessError(f"Failed to launch broker process: {exc}") from exc
        self._start_stream_readers()
        try:
            self.handshake()
        except SessionClientError:
            self.close()
            raise
        return self

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send a JSON payload to the broker and return the parsed JSON response."""
        with self._request_lock:
            return self._send_unlocked(payload)

    def _send_unlocked(self, payload: dict[str, Any]) -> dict[str, Any]:
        if (
            not self.is_running
            or self._process is None
            or self._process.stdin is None
            or self._process.stdout is None
        ):
            raise BrokerProcessError("Broker process is not running")

        try:
            line = json.dumps(payload).encode("utf-8") + b"\n"
            self._process.stdin.write(line)
            self._process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            self.close()
            raise BrokerProcessError("Broker stdin write failed (process may have exited)") from exc

        deadline = time.monotonic() + self.timeout_seconds
        while True:
            self._raise_stdout_fault()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._terminate_process()
                raise BrokerTimeoutError("Broker response exceeded its bounded timeout")
            try:
                response_item = self._stdout_lines.get(timeout=min(remaining, _STREAM_POLL_SECONDS))
                break
            except queue.Empty:
                continue

        if response_item is None:
            self._raise_stdout_fault()
            exit_code = self._process.poll()
            stderr_out = self._stderr_tail()
            self.close()
            raise BrokerProcessError(
                f"Broker process exited unexpectedly with code {exit_code}: {stderr_out.strip()}"
            )
        if isinstance(response_item, BaseException):
            self.close()
            raise BrokerProcessError("Broker stdout read failed") from response_item
        response_line = response_item

        try:
            response = json.loads(response_line)
        except (json.JSONDecodeError, RecursionError, ValueError) as exc:
            self.close()
            raise BrokerProtocolError(
                f"Invalid JSON line from broker: {response_line.strip()}"
            ) from exc

        if not isinstance(response, dict):
            self.close()
            raise BrokerProtocolError(f"Broker returned non-dict response: {response}")
        return cast(dict[str, Any], response)

    def _start_stream_readers(self) -> None:
        if self._process is None or self._process.stdout is None or self._process.stderr is None:
            raise AssertionError("broker streams must exist before readers start")
        self._stdout_lines = queue.Queue(maxsize=MAX_BROKER_PENDING_RESPONSES)
        with self._stdout_fault_lock:
            self._stdout_fault = None
        self._stderr_chunks = deque()
        self._stderr_size = 0
        stdout_reader = threading.Thread(
            target=self._read_stdout,
            args=(self._process.stdout,),
            name="goodjob-session-stdout",
            daemon=True,
        )
        stderr_reader = threading.Thread(
            target=self._drain_stderr,
            args=(self._process.stderr,),
            name="goodjob-session-stderr",
            daemon=True,
        )
        self._reader_threads = (stdout_reader, stderr_reader)
        for reader in self._reader_threads:
            reader.start()

    def _read_stdout(self, stream: BinaryIO) -> None:
        try:
            while raw_line := stream.readline(MAX_BROKER_JSONL_BYTES + 1):
                if len(raw_line) > MAX_BROKER_JSONL_BYTES or not raw_line.endswith(b"\n"):
                    self._record_stdout_fault(
                        BrokerProtocolError(
                            "Broker returned an oversized or unterminated JSON line"
                        )
                    )
                    return
                try:
                    line = raw_line.decode("utf-8")
                except UnicodeDecodeError:
                    self._record_stdout_fault(
                        BrokerProtocolError("Broker returned a non-UTF-8 JSON line")
                    )
                    return
                try:
                    self._stdout_lines.put_nowait(line)
                except queue.Full:
                    self._record_stdout_fault(
                        BrokerProtocolError("Broker exceeded the bounded pending response limit")
                    )
                    return
        except (OSError, UnicodeError, ValueError) as exc:
            self._record_stdout_fault(exc)
        finally:
            with contextlib.suppress(queue.Full):
                self._stdout_lines.put_nowait(None)

    def _drain_stderr(self, stream: BinaryIO) -> None:
        try:
            while chunk := stream.read(4096):
                with self._stderr_lock:
                    self._stderr_chunks.append(chunk)
                    self._stderr_size += len(chunk)
                    while self._stderr_size > MAX_BROKER_STDERR_BYTES and self._stderr_chunks:
                        removed = self._stderr_chunks.popleft()
                        self._stderr_size -= len(removed)
        except (OSError, UnicodeError, ValueError):
            return

    def _stderr_tail(self) -> str:
        with self._stderr_lock:
            return b"".join(self._stderr_chunks)[-MAX_BROKER_STDERR_BYTES:].decode(
                "utf-8", errors="replace"
            )

    def _record_stdout_fault(self, fault: BaseException) -> None:
        with self._stdout_fault_lock:
            if self._stdout_fault is None:
                self._stdout_fault = fault

    def _raise_stdout_fault(self) -> None:
        with self._stdout_fault_lock:
            fault = self._stdout_fault
        if fault is None:
            return
        self.close()
        if isinstance(fault, BrokerProtocolError):
            raise fault
        raise BrokerProcessError("Broker stdout read failed") from fault

    def handshake(self) -> dict[str, Any]:
        """Confirm the broker's versioned task-scoped session protocol."""
        response = self.send({"op": "session_handshake"})
        if response != {
            "status": "ok",
            "session": {"contract_version": "goodjob-session-v1", "state": "ready"},
        }:
            raise BrokerProtocolError("Broker returned an invalid session handshake")
        return response

    def complete(self) -> dict[str, Any]:
        """Complete the task-scoped broker session and reap its process."""
        response = self.send({"op": "session_complete"})
        expected = {
            "status": "ok",
            "session": {"contract_version": "goodjob-session-v1", "state": "completed"},
        }
        if response != expected:
            self.close()
            raise BrokerProtocolError("Broker returned an invalid session completion")
        self.close()
        return response

    def cancel(self) -> dict[str, Any]:
        """Cancel an idle task-scoped broker session and reap its process."""
        response = self.send({"op": "session_cancel"})
        expected = {
            "status": "ok",
            "session": {"contract_version": "goodjob-session-v1", "state": "cancelled"},
        }
        if response != expected:
            self.close()
            raise BrokerProtocolError("Broker returned an invalid session cancellation")
        self.close()
        return response

    def authorize_source_analysis(
        self,
        workspace: str | None = None,
        *,
        confirmed: bool,
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
        process_group_isolated = self._process_group_isolated
        self._process_group_isolated = False
        if proc.stdin and not proc.stdin.closed:
            with contextlib.suppress(Exception):
                proc.stdin.close()
        try:
            proc.wait(timeout=min(self.timeout_seconds, 5.0))
        except subprocess.TimeoutExpired:
            self._terminate_process_tree(proc, process_group_isolated)
        self._close_process_streams(proc)

    def _terminate_process(self) -> None:
        if self._process is None:
            return
        proc = self._process
        self._process = None
        process_group_isolated = self._process_group_isolated
        self._process_group_isolated = False
        self._terminate_process_tree(proc, process_group_isolated)
        self._close_process_streams(proc)

    def _spawn_process(
        self,
        command: list[str],
        *,
        stdin: int | None = None,
    ) -> tuple[subprocess.Popen[bytes], bool]:
        process_options: dict[str, Any] = {
            "stdin": stdin,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
        }
        process_group_isolated = False
        if os.name == "posix":
            process_options["start_new_session"] = True
            process_group_isolated = True
        elif os.name == "nt":
            creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            if creation_flags:
                process_options["creationflags"] = creation_flags
                process_group_isolated = True
        return cast(subprocess.Popen[bytes], subprocess.Popen(command, **process_options)), (
            process_group_isolated
        )

    def _terminate_process_tree(
        self, proc: subprocess.Popen[bytes], process_group_isolated: bool
    ) -> None:
        terminated_tree = False
        if process_group_isolated and os.name == "posix":
            killpg = getattr(os, "killpg", None)
            sigkill = getattr(signal, "SIGKILL", None)
            if killpg is not None and sigkill is not None:
                with contextlib.suppress(OSError, ProcessLookupError):
                    killpg(proc.pid, sigkill)
                    terminated_tree = True
        elif process_group_isolated and os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    check=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=min(self.timeout_seconds, 5.0),
                )
                terminated_tree = True
            except (OSError, subprocess.SubprocessError):
                pass
        if not terminated_tree and proc.poll() is None:
            with contextlib.suppress(OSError, ProcessLookupError):
                proc.kill()
        try:
            proc.wait(timeout=min(self.timeout_seconds, 5.0))
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError, ProcessLookupError):
                proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=min(self.timeout_seconds, 5.0))

    def _close_process_streams(self, proc: subprocess.Popen[bytes]) -> None:
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None and not stream.closed:
                with contextlib.suppress(Exception):
                    stream.close()
        for reader in self._reader_threads:
            reader.join(timeout=1.0)
        self._reader_threads = ()

    def __enter__(self) -> SessionClient:
        return self.start()

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if exc_type is None and self.is_running:
            self.complete()
        else:
            self.close()
