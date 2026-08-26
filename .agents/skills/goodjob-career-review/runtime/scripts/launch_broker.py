#!/usr/bin/env python3
"""Launcher for the GoodJob session broker with fail-closed platform preflight."""

from __future__ import annotations

import argparse
import json
import os
import queue
import secrets
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Literal, Never

RUNTIME_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = RUNTIME_DIR / "scripts"
SESSION_SCRIPT = SCRIPTS_DIR / "session.py"
BROKER_BOOTSTRAP_SCRIPT = SCRIPTS_DIR / "broker_bootstrap.py"
WINDOWS_PREFLIGHT_SCRIPT = SCRIPTS_DIR / "windows_preflight.py"
WINDOWS_PREFLIGHT_TIMEOUT_SECONDS = 30.0
TRUSTED_SOURCE_DIR = RUNTIME_DIR / "src"
sys.path.insert(0, str(TRUSTED_SOURCE_DIR))

from goodjob.platform.launcher_preflight import (  # noqa: E402
    LauncherPreflightReportDict,
    broker_ready_marker,
    broker_start_failure_report,
    evaluate_launcher_preflight,
    launcher_protocol_failure_report,
    launcher_report_from_windows,
    parse_launcher_preflight_report,
    platform_name_for_system,
)
from goodjob.platform.preflight_windows import (  # noqa: E402
    WindowsReportDict,
    missing_python_runtime_report,
    parse_windows_preflight_report,
    preflight_protocol_failure_report,
)
from goodjob.platform.runtime_bootstrap import (  # noqa: E402
    PythonRuntime,
    discover_python312,
)


def _discover_runtime(platform_name: str) -> PythonRuntime | None:
    return discover_python312(platform_name=platform_name)


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> Never:
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: invalid arguments\n")


def _preflight_protocol_failure(message: str) -> WindowsReportDict:
    return preflight_protocol_failure_report(message).as_dict()


def _stop_preflight_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        killpg = getattr(os, "killpg", None)
        sigkill = getattr(signal, "SIGKILL", None)
        if os.name == "posix" and killpg is not None and sigkill is not None:
            killpg(process.pid, sigkill)
        else:
            process.kill()
    except (OSError, ProcessLookupError):
        pass
    process.wait()


def _run_preflight_process(command: list[str]) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=os.name == "posix",
    )
    try:
        stdout, stderr = process.communicate(timeout=WINDOWS_PREFLIGHT_TIMEOUT_SECONDS)
    except BaseException:
        _stop_preflight_process(process)
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _run_windows_preflight(runtime: PythonRuntime, workspace: str) -> WindowsReportDict:
    command = [
        *runtime.command,
        "-I",
        "-B",
        str(WINDOWS_PREFLIGHT_SCRIPT),
        "--workspace",
        workspace,
        "--launcher-kind",
        runtime.kind,
    ]
    try:
        result = _run_preflight_process(command)
    except (OSError, subprocess.SubprocessError):
        return _preflight_protocol_failure("the Windows prerequisite probe could not start")
    try:
        raw: object = json.loads(result.stdout)
    except (json.JSONDecodeError, RecursionError):
        return _preflight_protocol_failure("the Windows prerequisite probe returned invalid JSON")
    report = parse_windows_preflight_report(raw)
    if report is None:
        return _preflight_protocol_failure("the Windows prerequisite report is incomplete")
    can_start = report["can_start_broker"]
    expected_status = "ok" if can_start else "error"
    expected_returncode = 0 if can_start else 2
    if report["status"] != expected_status or result.returncode != expected_returncode:
        return _preflight_protocol_failure(
            "the Windows prerequisite process and report status disagree"
        )
    return report


def _run_broker_process(command: list[str]) -> int | None:
    """Return the broker exit code only after its constructor confirms readiness."""
    bootstrap_index = command.index(str(BROKER_BOOTSTRAP_SCRIPT))
    expected_marker = broker_ready_marker(command[bootstrap_index + 1])
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert process.stdout is not None
    assert process.stderr is not None

    events: queue.Queue[BrokerStreamEvent] = queue.Queue()
    readers = [
        threading.Thread(
            target=_read_broker_stream,
            args=("stdout", process.stdout, events),
            daemon=True,
        ),
        threading.Thread(
            target=_read_broker_stream,
            args=("stderr", process.stderr, events),
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()

    open_channels: set[BrokerChannel] = {"stdout", "stderr"}
    ready_channels: set[BrokerChannel] = set()
    pending: dict[BrokerChannel, list[str]] = {"stdout": [], "stderr": []}
    try:
        while open_channels:
            event = events.get()
            if event.line is None:
                open_channels.discard(event.channel)
                continue
            if event.channel not in ready_channels:
                if event.line.rstrip("\r\n") == expected_marker:
                    ready_channels.add(event.channel)
                    if len(ready_channels) == 2:
                        for channel in ("stdout", "stderr"):
                            for line in pending[channel]:
                                _write_broker_output(channel, line)
                            pending[channel].clear()
                continue
            if len(ready_channels) == 2:
                _write_broker_output(event.channel, event.line)
            else:
                pending[event.channel].append(event.line)
        return process.wait() if len(ready_channels) == 2 else None
    except BaseException:
        if process.poll() is None:
            process.kill()
        process.wait()
        raise
    finally:
        for reader in readers:
            reader.join()


BrokerChannel = Literal["stdout", "stderr"]


@dataclass(frozen=True)
class BrokerStreamEvent:
    channel: BrokerChannel
    line: str | None


def _read_broker_stream(
    channel: BrokerChannel,
    stream: IO[str],
    events: queue.Queue[BrokerStreamEvent],
) -> None:
    try:
        for line in stream:
            events.put(BrokerStreamEvent(channel, line))
    finally:
        events.put(BrokerStreamEvent(channel, None))


def _write_broker_output(channel: BrokerChannel, line: str) -> None:
    stream = sys.stdout if channel == "stdout" else sys.stderr
    stream.write(line)
    stream.flush()


def _write_report(
    report: WindowsReportDict | LauncherPreflightReportDict,
    *,
    standard_output: bool,
) -> None:
    stream = sys.stdout if standard_output else sys.stderr
    stream.write(json.dumps(report, sort_keys=True) + "\n")


def _windows_report(
    runtime: PythonRuntime | None,
    workspace: str | None,
) -> WindowsReportDict:
    if runtime is None:
        return missing_python_runtime_report().as_dict()
    if not workspace:
        return _preflight_protocol_failure(
            "a workspace path is required for the Windows NTFS prerequisite check"
        )
    return _run_windows_preflight(runtime, workspace)


def _launcher_report(
    *,
    platform_name: str,
    runtime: PythonRuntime | None,
    workspace: str | None,
) -> LauncherPreflightReportDict:
    if platform_name == "win32":
        report = launcher_report_from_windows(
            _windows_report(runtime, workspace),
            runtime.kind if runtime is not None else "unavailable",
        )
    else:
        report = evaluate_launcher_preflight(
            platform_name=platform_name,
            runtime=runtime,
        )
    parsed = parse_launcher_preflight_report(report)
    if parsed is not None and parsed["platform"] == platform_name_for_system(platform_name):
        return parsed
    return launcher_protocol_failure_report(
        platform_name=platform_name,
        runtime=runtime,
        message="the launcher preflight producer returned an invalid report",
    )


def run(argv: list[str] | None = None, *, platform_name: str = sys.platform) -> int:
    parser = SafeArgumentParser(description="Launch the GoodJob session broker")
    parser.add_argument(
        "--agent-runtime",
        default="codex_task_runtime",
        help="host agent runtime identifier (default: codex_task_runtime)",
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="owner-local state directory override",
    )
    parser.add_argument(
        "--workspace",
        default=None,
        help="workspace used by the native Windows prerequisite preflight",
    )
    preflight_group = parser.add_mutually_exclusive_group()
    preflight_group.add_argument(
        "--preflight-only",
        action="store_true",
        help="emit the platform-neutral launcher report without starting the broker",
    )
    preflight_group.add_argument(
        "--windows-preflight-only",
        action="store_true",
        help="emit the native Windows prerequisite report without starting the broker",
    )
    args = parser.parse_args(argv)

    if args.windows_preflight_only and platform_name != "win32":
        parser.error("--windows-preflight-only is available only on native Windows")

    runtime = (
        _discover_runtime(platform_name) if platform_name in ("darwin", "linux", "win32") else None
    )
    if args.windows_preflight_only:
        windows_report = _windows_report(runtime, args.workspace)
        _write_report(windows_report, standard_output=True)
        return 0 if windows_report["can_start_broker"] else 2

    report = _launcher_report(
        platform_name=platform_name,
        runtime=runtime,
        workspace=args.workspace,
    )
    if args.preflight_only:
        _write_report(report, standard_output=True)
        return 0 if report["can_start_broker"] else 2
    if not report["can_start_broker"]:
        _write_report(report, standard_output=False)
        return 2

    assert runtime is not None

    broker_args: list[str] = []
    if args.data_dir:
        broker_args.extend(["--data-dir", args.data_dir])
    broker_args.extend(["--agent-runtime", args.agent_runtime])
    if platform_name == "win32":
        broker_args.extend(["--preflight-workspace", args.workspace])

    command = [
        *runtime.command,
        "-I",
        "-B",
        str(BROKER_BOOTSTRAP_SCRIPT),
        secrets.token_hex(32),
        str(SESSION_SCRIPT),
        *broker_args,
    ]

    try:
        return_code = _run_broker_process(command)
    except OSError:
        return_code = None
    if return_code is None:
        failure = broker_start_failure_report(
            report, "the GoodJob session broker process could not start"
        )
        assert parse_launcher_preflight_report(failure) is not None
        _write_report(failure, standard_output=False)
        return 2
    return return_code


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
