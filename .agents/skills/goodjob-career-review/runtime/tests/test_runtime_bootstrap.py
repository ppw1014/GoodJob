from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

from goodjob.platform import runtime_bootstrap
from goodjob.platform.launcher_preflight import parse_launcher_preflight_report
from goodjob.platform.preflight_windows import (
    WindowsPreflightReportDict,
    evaluate_windows_preflight,
    parse_windows_bootstrap_report,
)
from goodjob.platform.runtime_bootstrap import PythonRuntime, discover_python312


def _load_launcher() -> ModuleType:
    script = Path(__file__).resolve().parents[1] / "scripts" / "launch_broker.py"
    spec = importlib.util.spec_from_file_location("goodjob_test_launch_broker", script)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_session() -> ModuleType:
    script = Path(__file__).resolve().parents[1] / "scripts" / "session.py"
    spec = importlib.util.spec_from_file_location("goodjob_test_session", script)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@dataclass
class LauncherProbes:
    elevated: bool
    git_executable: Path | None = Path(r"C:\Program Files\Git\mingw64\bin\git.exe")
    filesystem: str = "NTFS"
    bfe: bool = True
    wfp_api: bool = True
    wfp_write: bool = True

    def retry_retained_cleanup(self) -> None:
        return None

    def trusted_git_executable(self) -> Path | None:
        return self.git_executable

    def workspace_filesystem(self, workspace: Path) -> str:
        del workspace
        return self.filesystem

    def bfe_is_running(self) -> bool:
        return self.bfe

    def is_elevated(self) -> bool:
        return self.elevated

    def wfp_api_is_available(self) -> bool:
        return self.wfp_api

    def wfp_policy_write_access(self) -> bool:
        return self.wfp_write

    def runtime_modules_importable(self) -> bool:
        return True


class FakeRunner:
    def __init__(self, results: dict[tuple[str, ...], tuple[int, str, str]]) -> None:
        self.results = results
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self,
        command: Sequence[str],
        *,
        capture_output: bool,
        text: bool,
        check: bool,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        assert capture_output is True
        assert text is True
        assert check is False
        assert timeout > 0
        key = tuple(command)
        self.calls.append(key)
        returncode, stdout, stderr = self.results.get(key, (1, "", "not found"))
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)


def test_runtime_probe_decodes_utf8_independently_of_windows_code_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def record_run(command: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, "Python 3.12.10", "")

    monkeypatch.setattr(subprocess, "run", record_run)

    result = runtime_bootstrap._run_command(
        ["python", "--version"],
        capture_output=True,
        text=True,
        check=False,
        timeout=5.0,
    )

    assert result.returncode == 0
    assert captured["encoding"] == "utf-8"
    assert captured["errors"] == "replace"


def _run_isolated_windows_launcher(
    tmp_path: Path,
    report: WindowsPreflightReportDict,
    *,
    preflight_only: bool = False,
) -> tuple[subprocess.CompletedProcess[str], bool]:
    launcher = Path(__file__).resolve().parents[1] / "scripts" / "launch_broker.py"
    preflight = tmp_path / "windows_preflight.py"
    broker = tmp_path / "session.py"
    broker_marker = tmp_path / "broker-started"
    preflight.write_text(
        "import sys\n"
        f"sys.stdout.write({json.dumps(report, sort_keys=True)!r} + '\\n')\n"
        f"raise SystemExit({0 if report['can_start_broker'] else 2})\n",
        encoding="utf-8",
    )
    broker.write_text(
        "from pathlib import Path\n"
        "class SessionBroker:\n"
        "    def __init__(self, *args, **kwargs):\n"
        f"        Path({str(broker_marker)!r}).write_text('started', encoding='utf-8')\n"
        "def run(argv=None):\n"
        "    SessionBroker(argv)\n"
        "    return 0\n",
        encoding="utf-8",
    )
    wrapper = (
        "import importlib.util,sys;"
        "from pathlib import Path;"
        "launcher_path,preflight_path,broker_path,workspace,preflight_only=sys.argv[1:];"
        "spec=importlib.util.spec_from_file_location('isolated_windows_launcher',launcher_path);"
        "module=importlib.util.module_from_spec(spec);"
        "sys.modules[spec.name]=module;"
        "spec.loader.exec_module(module);"
        "module.WINDOWS_PREFLIGHT_SCRIPT=Path(preflight_path);"
        "module.SESSION_SCRIPT=Path(broker_path);"
        "module._discover_runtime=lambda _platform: module.PythonRuntime("
        "(sys.executable,),'direct_python',tuple(sys.version_info[:3]));"
        "arguments=['--workspace',workspace]+"
        "(['--preflight-only'] if preflight_only=='true' else []);"
        "raise SystemExit(module.run(arguments,platform_name='win32'))"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            wrapper,
            str(launcher),
            str(preflight),
            str(broker),
            str(tmp_path / "workspace"),
            str(preflight_only).lower(),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30.0,
    )
    return result, broker_marker.exists()


def test_native_windows_release_is_enabled_in_fresh_isolated_python() -> None:
    source_dir = Path(__file__).resolve().parents[1] / "src"
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            (
                "import sys;"
                "sys.path.insert(0,sys.argv[1]);"
                "from goodjob.platform.detect import NATIVE_WINDOWS_RELEASE_ENABLED;"
                "print(str(NATIVE_WINDOWS_RELEASE_ENABLED).lower())"
            ),
            str(source_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30.0,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "true"


@pytest.mark.parametrize(
    "failed_check",
    [None, "trusted_git", "workspace_filesystem", "bfe_service", "administrator", "wfp"],
    ids=["all-nine-pass", "git", "ntfs", "bfe", "administrator", "wfp"],
)
@pytest.mark.parametrize("preflight_only", [False, True])
def test_isolated_windows_launcher_starts_broker_only_after_all_nine_checks_pass(
    tmp_path: Path,
    failed_check: str | None,
    preflight_only: bool,
) -> None:
    probes = LauncherProbes(elevated=True)
    if failed_check == "trusted_git":
        probes.git_executable = None
    elif failed_check == "workspace_filesystem":
        probes.filesystem = "ReFS"
    elif failed_check == "bfe_service":
        probes.bfe = False
    elif failed_check == "administrator":
        probes.elevated = False
    elif failed_check == "wfp":
        probes.wfp_write = False
    report = evaluate_windows_preflight(
        workspace=tmp_path / "workspace",
        runtime_dir=Path(__file__).resolve().parents[1],
        python_version=(
            sys.version_info.major,
            sys.version_info.minor,
            sys.version_info.micro,
        ),
        launcher_kind="direct_python",
        uv_available=False,
        release_enabled=True,
        probes=probes,
    ).as_dict()

    result, broker_started = _run_isolated_windows_launcher(
        tmp_path, report, preflight_only=preflight_only
    )

    assert result.returncode == (0 if failed_check is None else 2), result.stderr
    assert broker_started is (failed_check is None and not preflight_only)
    if preflight_only:
        emitted = json.loads(result.stdout)
        assert parse_launcher_preflight_report(emitted) == emitted
        assert emitted["checks"] == report["checks"]
        assert emitted["notices"] == report["notices"]
    if failed_check is not None:
        assert report["can_start_broker"] is False


def _write_windows_entry_shim(bin_dir: Path, name: str) -> Path:
    program = bin_dir / (f"{name}.py" if sys.platform == "win32" else name)
    program.write_text(
        f"""#!{sys.executable}
import os
import sys

args = sys.argv[1:]
name = {name!r} if sys.platform == "win32" else os.path.basename(sys.argv[0])
if name == "py":
    if args == ["-3.12", "--version"]:
        raise SystemExit(2)
    if args == ["-3", "--version"]:
        print("Python 3.13.5")
        raise SystemExit(0)
    if not args or args[0] != "-3":
        raise SystemExit(91)
    args = args[1:]
elif name == "uv":
    find_args = [
        "python", "find", "--no-project", "--no-config", "--offline",
        "--no-python-downloads", "--show-version", ">=3.12",
    ]
    if args == find_args:
        print("Python 3.13.5")
        raise SystemExit(0)
    run_prefix = [
        "run", "--isolated", "--no-project", "--no-config", "--offline",
        "--no-python-downloads", "--python",
    ]
    if (
        args[:len(run_prefix)] != run_prefix
        or len(args) <= len(run_prefix) + 1
        or args[len(run_prefix)] not in {{">=3.12", "3.13.5"}}
        or args[len(run_prefix) + 1] != "python"
    ):
        raise SystemExit(92)
    args = args[len(run_prefix) + 2:]
else:
    raise SystemExit(93)

if args[:2] != ["-I", "-B"] or len(args) < 3:
    raise SystemExit(94)
script, script_args = args[2], args[3:]
if sys.platform == "win32":
    import importlib.util

    spec = importlib.util.spec_from_file_location("entry_under_test", script)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    result = (
        module.run(script_args, platform_name="win32")
        if os.path.basename(script) == "launch_broker.py"
        else module.run(script_args)
    )
    raise SystemExit(result)
wrapper = (
    "import importlib.util,os,sys;"
    "script=sys.argv[1];"
    "spec=importlib.util.spec_from_file_location('entry_under_test',script);"
    "module=importlib.util.module_from_spec(spec);"
    "sys.modules[spec.name]=module;"
    "spec.loader.exec_module(module);"
    "args=sys.argv[2:];"
    "result=(module.run(args,platform_name='win32') "
    "if os.path.basename(script)=='launch_broker.py' else module.run(args));"
    "raise SystemExit(result)"
)
os.execv(sys.executable, [sys.executable, "-I", "-B", "-c", wrapper, script, *script_args])
""",
        encoding="utf-8",
    )
    program.chmod(0o755)
    if sys.platform != "win32":
        return program
    shim = bin_dir / f"{name}.cmd"
    shim.write_text(
        f'@"{sys.executable}" -I -B "{program}" %*\n',
        encoding="utf-8",
    )
    return shim


def test_windows_runtime_discovery_supports_python_launcher() -> None:
    py = r"C:\Windows\py.exe"
    runner = FakeRunner({(py, "-3.12", "--version"): (0, "Python 3.12.8\n", "")})

    runtime = discover_python312(
        platform_name="win32",
        which=lambda name: py if name == "py" else None,
        runner=runner,
    )

    assert runtime is not None
    assert runtime.command == (py, "-3.12")
    assert runtime.kind == "windows_py_launcher"
    assert runtime.version == (3, 12, 8)


def test_windows_runtime_discovery_accepts_py3_newer_fallback() -> None:
    py = r"C:\Windows\py.exe"
    runner = FakeRunner(
        {
            (py, "-3.12", "--version"): (1, "", "Requested Python not installed"),
            (py, "-3", "--version"): (0, "Python 3.13.5\n", ""),
        }
    )

    runtime = discover_python312(
        platform_name="win32",
        which=lambda name: py if name == "py" else None,
        runner=runner,
    )

    assert runtime is not None
    assert runtime.command == (py, "-3")
    assert runtime.kind == "windows_py_launcher"
    assert runtime.version == (3, 13, 5)


def test_windows_runtime_discovery_accepts_python_exe_fallback() -> None:
    python = r"C:\Users\Owner\AppData\Local\Programs\Python\Python312\python.exe"
    runner = FakeRunner({(python, "--version"): (0, "", "Python 3.12.4\n")})

    runtime = discover_python312(
        platform_name="win32",
        which=lambda name: python if name == "python" else None,
        runner=runner,
    )

    assert runtime is not None
    assert runtime.command == (python,)
    assert runtime.kind == "direct_python"
    assert runtime.version == (3, 12, 4)


def test_windows_runtime_discovery_accepts_uv_only_newer_python() -> None:
    uv = r"C:\tools\uv.exe"
    find_command = (
        uv,
        "python",
        "find",
        "--no-project",
        "--no-config",
        "--offline",
        "--no-python-downloads",
        "--show-version",
        ">=3.12",
    )
    runner = FakeRunner({find_command: (0, "Python 3.13.5\n", "")})

    runtime = discover_python312(
        platform_name="win32",
        which=lambda name: uv if name == "uv" else None,
        runner=runner,
    )

    assert runtime is not None
    assert runtime.command == (
        uv,
        "run",
        "--isolated",
        "--no-project",
        "--no-config",
        "--offline",
        "--no-python-downloads",
        "--python",
        "3.13.5",
        "python",
    )
    assert runtime.kind == "uv"
    assert runtime.version == (3, 13, 5)


@pytest.mark.parametrize("entry_kind", ["py", "uv"], ids=["py-3", "uv-managed"])
def test_windows_public_command_reaches_full_preflight_with_only_one_entry(
    tmp_path: Path,
    entry_kind: str,
) -> None:
    if sys.platform == "win32" and entry_kind == "uv":
        pytest.skip("the cmd shim cannot preserve uv's >= version argument")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    entry = _write_windows_entry_shim(bin_dir, entry_kind)
    launcher = Path(__file__).resolve().parents[1] / "scripts" / "launch_broker.py"
    workspace = tmp_path / "workspace"
    if entry_kind == "py":
        command = [str(entry), "-3", "-I", "-B", str(launcher)]
    else:
        command = [
            str(entry),
            "run",
            "--isolated",
            "--no-project",
            "--no-config",
            "--offline",
            "--no-python-downloads",
            "--python",
            ">=3.12",
            "python",
            "-I",
            "-B",
            str(launcher),
        ]
    command.extend(
        [
            "--windows-preflight-only",
            "--workspace",
            str(workspace),
            "--agent-runtime",
            "test-runtime",
        ]
    )

    result = subprocess.run(
        command,
        env={**os.environ, "PATH": str(bin_dir)},
        capture_output=True,
        text=True,
        check=False,
        timeout=30.0,
    )

    report = json.loads(result.stdout)
    assert result.returncode == (0 if report["can_start_broker"] else 2), result.stderr
    assert report["contract_version"] == "windows-prerequisite-preflight-v1"
    assert {check["id"] for check in report["checks"]} == {
        "python_runtime",
        "runtime_installation",
        "trusted_git",
        "workspace_filesystem",
        "bfe_service",
        "administrator",
        "wfp_api",
        "wfp_permission",
        "native_windows_release",
    }


def test_runtime_discovery_reports_missing_when_uv_and_python_are_unusable() -> None:
    uv = r"C:\tools\uv.exe"
    runner = FakeRunner(
        {
            (
                uv,
                "python",
                "find",
                "--no-project",
                "--no-config",
                "--offline",
                "--no-python-downloads",
                "--show-version",
                ">=3.12",
            ): (2, "", "No compatible Python found"),
        }
    )

    runtime = discover_python312(
        platform_name="win32",
        which=lambda name: uv if name == "uv" else None,
        runner=runner,
    )

    assert runtime is None


def test_windows_launcher_reports_missing_runtime_without_starting_broker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    launch_broker = _load_launcher()
    broker_calls: list[list[str]] = []

    def record_broker_call(command: list[str]) -> int:
        broker_calls.append(command)
        return 0

    monkeypatch.setattr(launch_broker, "discover_python312", lambda **_kwargs: None)
    monkeypatch.setattr(launch_broker, "_run_broker_process", record_broker_call)

    result = launch_broker.run(["--workspace", str(tmp_path / "workspace")], platform_name="win32")

    assert result == 2
    assert broker_calls == []
    output = capsys.readouterr()
    assert output.out == ""
    report = json.loads(output.err)
    assert parse_launcher_preflight_report(report) == report
    assert report["status"] == "error"
    assert report["can_start_broker"] is False
    assert report["checks"][0]["code"] == "missing_dependency"
    assert report["checks"][0]["remediation"]["requires_explicit_consent"] is True


@pytest.mark.parametrize(
    "runtime",
    [
        PythonRuntime((r"C:\Windows\py.exe", "-3"), "windows_py_launcher", (3, 13, 5)),
        PythonRuntime(
            (
                r"C:\tools\uv.exe",
                "run",
                "--isolated",
                "--no-project",
                "--no-config",
                "--offline",
                "--no-python-downloads",
                "--python",
                "3.13.5",
                "python",
            ),
            "uv",
            (3, 13, 5),
        ),
    ],
    ids=["py-3", "uv-managed"],
)
def test_windows_launcher_compatible_runtime_reaches_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    runtime: PythonRuntime,
) -> None:
    launch_broker = _load_launcher()
    observed: list[PythonRuntime] = []
    successful_report = evaluate_windows_preflight(
        workspace=tmp_path / "workspace",
        runtime_dir=Path(__file__).resolve().parents[1],
        python_version=runtime.version,
        launcher_kind=runtime.kind,
        uv_available=runtime.kind == "uv",
        release_enabled=True,
        probes=LauncherProbes(elevated=True),
    ).as_dict()

    def record_preflight(selected: PythonRuntime, _workspace: str) -> WindowsPreflightReportDict:
        observed.append(selected)
        return successful_report

    monkeypatch.setattr(launch_broker, "discover_python312", lambda **_kwargs: runtime)
    monkeypatch.setattr(launch_broker, "_run_windows_preflight", record_preflight)

    result = launch_broker.run(
        ["--windows-preflight-only", "--workspace", str(tmp_path / "workspace")],
        platform_name="win32",
    )

    assert result == 0
    assert observed == [runtime]
    assert json.loads(capsys.readouterr().out)["can_start_broker"] is True


def test_windows_launcher_does_not_start_broker_after_refused_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    launch_broker = _load_launcher()
    runtime = PythonRuntime((r"C:\Python312\python.exe",), "direct_python", (3, 12, 8))
    report = evaluate_windows_preflight(
        workspace=tmp_path / "workspace",
        runtime_dir=tmp_path,
        python_version=runtime.version,
        launcher_kind=runtime.kind,
        uv_available=False,
        release_enabled=True,
        probes=LauncherProbes(elevated=False),
    ).as_dict()
    broker_calls: list[list[str]] = []

    def record_broker_call(command: list[str]) -> int:
        broker_calls.append(command)
        return 0

    monkeypatch.setattr(launch_broker, "discover_python312", lambda **_kwargs: runtime)
    monkeypatch.setattr(launch_broker, "_run_windows_preflight", lambda *_args: report)
    monkeypatch.setattr(launch_broker, "_run_broker_process", record_broker_call)

    result = launch_broker.run(["--workspace", str(tmp_path / "workspace")], platform_name="win32")

    assert result == 2
    assert broker_calls == []
    response = json.loads(capsys.readouterr().err)
    assert response["can_start_broker"] is False


@pytest.mark.parametrize("preflight_only", [False, True])
def test_windows_launcher_starts_only_after_successful_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    preflight_only: bool,
) -> None:
    launch_broker = _load_launcher()
    runtime = PythonRuntime((r"C:\Windows\py.exe", "-3.12"), "windows_py_launcher", (3, 12, 8))
    runtime_tree = tmp_path / "runtime"
    (runtime_tree / "scripts").mkdir(parents=True)
    (runtime_tree / "src" / "goodjob").mkdir(parents=True)
    (runtime_tree / "scripts" / "broker_bootstrap.py").write_text("# bootstrap\n", encoding="utf-8")
    (runtime_tree / "scripts" / "launch_broker.py").write_text("# launcher\n", encoding="utf-8")
    (runtime_tree / "scripts" / "session.py").write_text("# broker\n", encoding="utf-8")
    (runtime_tree / "scripts" / "windows_preflight.py").write_text(
        "# preflight\n", encoding="utf-8"
    )
    (runtime_tree / "src" / "goodjob" / "__init__.py").write_text("", encoding="utf-8")
    report = evaluate_windows_preflight(
        workspace=tmp_path / "workspace",
        runtime_dir=runtime_tree,
        python_version=runtime.version,
        launcher_kind=runtime.kind,
        uv_available=False,
        release_enabled=True,
        probes=LauncherProbes(elevated=True),
    ).as_dict()
    broker_calls: list[list[str]] = []

    def record_broker_call(command: list[str]) -> int:
        broker_calls.append(command)
        return 0

    monkeypatch.setattr(launch_broker, "discover_python312", lambda **_kwargs: runtime)
    monkeypatch.setattr(launch_broker, "_run_windows_preflight", lambda *_args: report)
    monkeypatch.setattr(launch_broker, "_run_broker_process", record_broker_call)
    arguments = ["--workspace", str(tmp_path / "workspace")]
    if preflight_only:
        arguments.append("--windows-preflight-only")

    result = launch_broker.run(arguments, platform_name="win32")

    assert result == 0
    output = capsys.readouterr()
    if preflight_only:
        assert output.out == json.dumps(report, sort_keys=True) + "\n"
        emitted = json.loads(output.out)
        assert emitted["can_start_broker"] is True
        assert broker_calls == []
    else:
        assert output.out == ""
        assert output.err == ""
        assert len(broker_calls) == 1
        assert broker_calls[0][:2] == [r"C:\Windows\py.exe", "-3.12"]
        workspace_argument = broker_calls[0].index("--preflight-workspace")
        assert broker_calls[0][workspace_argument + 1] == str(tmp_path / "workspace")
        assert "--capability" not in " ".join(broker_calls[0])


def test_windows_launcher_decodes_preflight_utf8_independently_of_code_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launch_broker = _load_launcher()
    runtime = PythonRuntime((r"C:\Python312\python.exe",), "direct_python", (3, 12, 8))
    report = evaluate_windows_preflight(
        workspace=tmp_path / "workspace",
        runtime_dir=Path(__file__).resolve().parents[1],
        python_version=(3, 12, 8),
        launcher_kind="direct_python",
        uv_available=False,
        release_enabled=True,
        probes=LauncherProbes(elevated=False),
    ).as_dict()
    captured: dict[str, object] = {}

    class FakePopen:
        returncode = 2
        pid = 1

        def __init__(self, command: Sequence[str], **kwargs: object) -> None:
            self.command = command
            captured.update(kwargs)

        def communicate(self, *, timeout: float) -> tuple[str, str]:
            assert timeout > 0
            return json.dumps(report), "\N{SNOWMAN}"

        def poll(self) -> int:
            return self.returncode

    monkeypatch.setattr(launch_broker.subprocess, "Popen", FakePopen)

    parsed = launch_broker._run_windows_preflight(runtime, str(tmp_path / "workspace"))

    assert parsed == report
    assert captured["encoding"] == "utf-8"
    assert captured["errors"] == "replace"


def test_windows_preflight_timeout_reaps_the_probe_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launch_broker = _load_launcher()
    actual_popen = subprocess.Popen
    observed: list[subprocess.Popen[str]] = []

    def record_popen(command: Sequence[str], **kwargs: Any) -> subprocess.Popen[str]:
        process = cast("subprocess.Popen[str]", actual_popen(command, **kwargs))
        observed.append(process)
        return process

    monkeypatch.setattr(launch_broker.subprocess, "Popen", record_popen)
    monkeypatch.setattr(launch_broker, "WINDOWS_PREFLIGHT_TIMEOUT_SECONDS", 0.05)

    with pytest.raises(subprocess.TimeoutExpired):
        launch_broker._run_preflight_process(
            [sys.executable, "-I", "-B", "-c", "import time; time.sleep(60)"]
        )

    assert len(observed) == 1
    assert observed[0].poll() is not None


def test_windows_preflight_keyboard_interrupt_reaps_the_probe_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launch_broker = _load_launcher()
    process = subprocess.Popen(
        [sys.executable, "-I", "-B", "-c", "import time; time.sleep(60)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=os.name == "posix",
    )

    class InterruptingPopen:
        pid = process.pid
        returncode: int | None = None

        def communicate(self, *, timeout: float) -> tuple[str, str]:
            del timeout
            raise KeyboardInterrupt

        def poll(self) -> int | None:
            return process.poll()

        def kill(self) -> None:
            process.kill()

        def wait(self) -> int:
            return process.wait()

    monkeypatch.setattr(
        launch_broker.subprocess,
        "Popen",
        lambda *_args, **_kwargs: InterruptingPopen(),
    )

    with pytest.raises(KeyboardInterrupt):
        launch_broker._run_preflight_process(["unused"])

    assert process.poll() is not None


def test_windows_launcher_rejects_preflight_exit_report_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launch_broker = _load_launcher()
    runtime = PythonRuntime((r"C:\Python312\python.exe",), "direct_python", (3, 12, 8))
    successful_report = evaluate_windows_preflight(
        workspace=tmp_path / "workspace",
        runtime_dir=Path(__file__).resolve().parents[1],
        python_version=runtime.version,
        launcher_kind=runtime.kind,
        uv_available=False,
        release_enabled=True,
        probes=LauncherProbes(elevated=True),
    ).as_dict()
    monkeypatch.setattr(
        launch_broker,
        "_run_preflight_process",
        lambda _command: subprocess.CompletedProcess([], 2, json.dumps(successful_report), ""),
    )

    report = launch_broker._run_windows_preflight(runtime, str(tmp_path / "workspace"))

    assert parse_windows_bootstrap_report(report) == report
    assert report["can_start_broker"] is False
    assert report["checks"][0]["id"] == "windows_preflight"


def test_windows_launcher_rejects_success_report_missing_required_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launch_broker = _load_launcher()
    runtime = PythonRuntime((r"C:\Python312\python.exe",), "direct_python", (3, 12, 8))
    incomplete_report = {
        "contract_version": "windows-prerequisite-preflight-v1",
        "status": "ok",
        "can_start_broker": True,
        "checks": [],
        "notices": [],
    }
    monkeypatch.setattr(
        launch_broker,
        "_run_preflight_process",
        lambda _command: subprocess.CompletedProcess([], 0, json.dumps(incomplete_report), ""),
    )

    report = launch_broker._run_windows_preflight(runtime, str(tmp_path / "workspace"))

    assert parse_windows_bootstrap_report(report) == report
    assert report["can_start_broker"] is False
    assert report["checks"][0]["id"] == "windows_preflight"


@pytest.mark.parametrize("mutation", ["missing_notice", "failure_fields_on_passed_check"])
def test_windows_launcher_rejects_semantically_inconsistent_success_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    launch_broker = _load_launcher()
    runtime = PythonRuntime((r"C:\Python312\python.exe",), "direct_python", (3, 12, 8))
    report = evaluate_windows_preflight(
        workspace=tmp_path / "workspace",
        runtime_dir=Path(__file__).resolve().parents[1],
        python_version=(3, 12, 8),
        launcher_kind="direct_python",
        uv_available=False,
        release_enabled=True,
        probes=LauncherProbes(elevated=True),
    ).as_dict()
    if mutation == "missing_notice":
        report["notices"] = []
    else:
        report["checks"][0]["code"] = "missing_dependency"
        report["checks"][0]["remediation"] = {
            "action": "request_installation",
            "purpose": "contradict the passed state",
            "requires_explicit_consent": True,
        }
    monkeypatch.setattr(
        launch_broker,
        "_run_preflight_process",
        lambda _command: subprocess.CompletedProcess([], 0, json.dumps(report), ""),
    )

    parsed = launch_broker._run_windows_preflight(runtime, str(tmp_path / "workspace"))

    assert parse_windows_bootstrap_report(parsed) == parsed
    assert parsed["can_start_broker"] is False
    assert parsed["checks"][0]["id"] == "windows_preflight"


def test_windows_session_requires_its_own_successful_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = _load_session()
    failed_report = evaluate_windows_preflight(
        workspace=tmp_path / "workspace",
        runtime_dir=tmp_path,
        python_version=(3, 12, 8),
        launcher_kind="direct_python",
        uv_available=False,
        release_enabled=False,
        probes=LauncherProbes(elevated=True),
    ).as_dict()
    capability_calls = 0

    def record_capability() -> bytes:
        nonlocal capability_calls
        capability_calls += 1
        return b"unused"

    monkeypatch.setattr(session, "generate_capability", record_capability)
    monkeypatch.setattr(
        session,
        "_run_native_windows_session_preflight",
        lambda _workspace: failed_report,
    )

    result = session.run(
        ["--preflight-workspace", str(tmp_path / "workspace")],
        platform_name="win32",
        input_stream=[],
    )

    assert result == 2
    assert capability_calls == 0
    assert json.loads(capsys.readouterr().err)["can_start_broker"] is False


def test_windows_session_rejects_a_missing_preflight_workspace(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = _load_session()
    capability_calls = 0

    def record_capability() -> bytes:
        nonlocal capability_calls
        capability_calls += 1
        return b"unused"

    monkeypatch.setattr(session, "generate_capability", record_capability)

    result = session.run([], platform_name="win32", input_stream=[])

    assert result == 2
    assert capability_calls == 0
    report = json.loads(capsys.readouterr().err)
    assert parse_windows_bootstrap_report(report) == report
    assert report["checks"][0]["code"] == "unsupported_capability"


def test_windows_session_starts_only_after_its_preflight_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = _load_session()
    successful_report = evaluate_windows_preflight(
        workspace=tmp_path / "workspace",
        runtime_dir=Path(__file__).resolve().parents[1],
        python_version=(3, 12, 8),
        launcher_kind="direct_python",
        uv_available=False,
        release_enabled=True,
        probes=LauncherProbes(elevated=True),
    ).as_dict()
    monkeypatch.setattr(
        session,
        "_run_native_windows_session_preflight",
        lambda _workspace: successful_report,
    )

    result = session.run(
        ["--preflight-workspace", str(tmp_path / "workspace")],
        platform_name="win32",
        input_stream=[],
    )

    assert result == 0
    report = json.loads(capsys.readouterr().err)
    assert report["can_start_broker"] is True
    assert report["notices"] == [
        "Native Windows Git subprocesses do not have filesystem read isolation; "
        "use WSL2 when complete Git filesystem isolation is required."
    ]
