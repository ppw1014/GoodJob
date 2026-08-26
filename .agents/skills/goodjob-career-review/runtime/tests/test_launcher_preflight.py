from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

from goodjob.platform.launcher_preflight import (
    LAUNCHER_PREFLIGHT_REGISTRY,
    LauncherPreflightFact,
    LauncherPreflightReport,
    apply_launcher_preflight_decision,
    classify_launcher_preflight,
    evaluate_launcher_preflight,
    failed_check,
    launcher_report_from_windows,
    parse_launcher_preflight_report,
    passed_check,
    remediation_for,
)
from goodjob.platform.preflight_windows import (
    missing_python_runtime_report,
    parse_windows_bootstrap_report,
)
from goodjob.platform.runtime_bootstrap import PythonRuntime


def _load_launcher() -> ModuleType:
    script = Path(__file__).resolve().parents[1] / "scripts" / "launch_broker.py"
    spec = importlib.util.spec_from_file_location("goodjob_test_v1_launcher", script)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _mutated(
    report: object, mutation: Any = None, path: tuple[Any, ...] = (), **changes: Any
) -> dict[str, Any]:
    result = cast(dict[str, Any], deepcopy(report))
    if mutation is not None:
        mutation(result)
    target: Any = result
    for key in path:
        target = target[key]
    target.update(changes)
    return result


@pytest.mark.parametrize(
    "path",
    ["top_level", "failed_check", "remediation"],
)
def test_windows_bootstrap_parser_rejects_unknown_fields(path: str) -> None:
    report = deepcopy(missing_python_runtime_report().as_dict())
    if path == "top_level":
        report["unexpected"] = "field"  # type: ignore[typeddict-unknown-key]
    elif path == "failed_check":
        report["checks"][0]["unexpected"] = "field"  # type: ignore[typeddict-unknown-key]
    else:
        report["checks"][0]["remediation"]["unexpected"] = "field"  # type: ignore[typeddict-unknown-key]

    assert parse_windows_bootstrap_report(report) is None


@pytest.mark.parametrize(
    ("platform_name", "sandbox_path"),
    [("darwin", Path("/usr/bin/sandbox-exec")), ("linux", Path("/usr/bin/bwrap"))],
)
def test_supported_platform_producer_reports_runtime_and_sandbox(
    platform_name: str,
    sandbox_path: Path,
) -> None:
    runtime = PythonRuntime(("python3.12",), "direct_python", (3, 12, 9))

    report = evaluate_launcher_preflight(
        platform_name=platform_name,
        runtime=runtime,
        path_is_file=lambda path: path == sandbox_path,
        sandbox_is_usable=lambda _platform: True,
    )

    assert parse_launcher_preflight_report(report) == report
    assert report["status"] == "ok"
    assert [check["id"] for check in report["checks"]] == [
        "python_runtime",
        "git_sandbox",
    ]


def test_unsupported_platform_producer_fails_closed() -> None:
    report = evaluate_launcher_preflight(
        platform_name="freebsd14",
        runtime=PythonRuntime(("python3.12",), "direct_python", (3, 12, 9)),
    )

    assert parse_launcher_preflight_report(report) == report
    assert report["platform"] == "unsupported"
    assert report["launcher_kind"] == "unavailable"
    assert report["checks"][0]["code"] == "unsupported_capability"


def test_installed_but_unusable_linux_sandbox_fails_closed() -> None:
    report = evaluate_launcher_preflight(
        platform_name="linux",
        runtime=PythonRuntime(("python3.12",), "direct_python", (3, 12, 9)),
        path_is_file=lambda _path: True,
        sandbox_is_usable=lambda _platform: False,
    )

    assert parse_launcher_preflight_report(report) == report
    assert report["checks"][1]["remediation"]["action"] == "repair_linux_runtime_or_use_wsl2"


def test_windows_adapter_losslessly_maps_the_same_old_report() -> None:
    old_report = missing_python_runtime_report().as_dict()

    report = launcher_report_from_windows(old_report, "unavailable")

    assert parse_launcher_preflight_report(report) == report
    assert report["checks"] == old_report["checks"]
    assert report["notices"] == old_report["notices"]


def test_preflight_only_emits_one_v1_report_without_starting_broker_or_creating_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    launcher = _load_launcher()
    runtime = PythonRuntime(("python3.12",), "direct_python", (3, 12, 9))
    data_dir = tmp_path / "must-not-exist"
    ready = evaluate_launcher_preflight(
        platform_name="darwin",
        runtime=runtime,
        path_is_file=lambda _path: True,
        sandbox_is_usable=lambda _platform: True,
    )
    monkeypatch.setattr(launcher, "_discover_runtime", lambda _platform: runtime)
    monkeypatch.setattr(launcher, "evaluate_launcher_preflight", lambda **_kwargs: ready)
    monkeypatch.setattr(
        launcher,
        "_run_broker_process",
        lambda *_args, **_kwargs: pytest.fail("broker must not start in preflight-only mode"),
    )

    result = launcher.run(
        ["--preflight-only", "--data-dir", str(data_dir)],
        platform_name="darwin",
    )

    output = capsys.readouterr()
    report = json.loads(output.out)
    assert result == 0
    assert output.err == ""
    assert parse_launcher_preflight_report(report) == report
    assert data_dir.exists() is False


def test_preflight_only_failure_uses_stdout_and_exit_two(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    launcher = _load_launcher()
    monkeypatch.setattr(launcher, "_discover_runtime", lambda _platform: None)

    result = launcher.run(["--preflight-only"], platform_name="linux")

    output = capsys.readouterr()
    report = json.loads(output.out)
    assert result == 2
    assert output.err == ""
    assert parse_launcher_preflight_report(report) == report
    assert report["can_start_broker"] is False


def test_normal_prebroker_failure_uses_stderr_v1_without_starting_broker(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    launcher = _load_launcher()
    monkeypatch.setattr(launcher, "_discover_runtime", lambda _platform: None)
    monkeypatch.setattr(
        launcher,
        "_run_broker_process",
        lambda *_args, **_kwargs: pytest.fail("broker must not start after failed preflight"),
    )

    result = launcher.run([], platform_name="linux")

    output = capsys.readouterr()
    report = json.loads(output.err)
    assert result == 2
    assert output.out == ""
    assert parse_launcher_preflight_report(report) == report


def test_general_windows_flag_losslessly_wraps_legacy_bootstrap_report(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    launcher = _load_launcher()
    monkeypatch.setattr(launcher, "_discover_runtime", lambda _platform: None)

    old_result = launcher.run(["--windows-preflight-only"], platform_name="win32")
    old_output = capsys.readouterr()
    new_result = launcher.run(["--preflight-only"], platform_name="win32")
    new_output = capsys.readouterr()

    old_report = json.loads(old_output.out)
    new_report = json.loads(new_output.out)
    assert old_result == new_result == 2
    assert old_output.err == new_output.err == ""
    assert parse_windows_bootstrap_report(old_report) == old_report
    assert parse_launcher_preflight_report(new_report) == new_report
    assert new_report["checks"] == old_report["checks"]
    assert new_report["notices"] == old_report["notices"]


def test_preflight_flags_are_mutually_exclusive_before_runtime_discovery(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    launcher = _load_launcher()
    monkeypatch.setattr(
        launcher,
        "_discover_runtime",
        lambda _platform: pytest.fail("syntax errors must not probe the runtime"),
    )

    with pytest.raises(SystemExit) as raised:
        launcher.run(
            ["--preflight-only", "--windows-preflight-only"],
            platform_name="win32",
        )

    output = capsys.readouterr()
    assert raised.value.code == 2
    assert output.out == ""
    assert "usage:" in output.err
    assert "error: invalid arguments" in output.err


def _v1_mutations(ready: object, failed: object) -> list[dict[str, Any]]:
    return [
        _mutated(ready, unexpected=True),
        _mutated(ready, lambda r: r.pop("notices")),
        _mutated(ready, contract_version="launcher-preflight-v2"),
        _mutated(ready, status=1),
        _mutated(ready, can_start_broker=1),
        _mutated(ready, checks={}),
        _mutated(ready, notices={}),
        _mutated(ready, lambda r: r["checks"].reverse()),
        _mutated(ready, lambda r: r["checks"].pop()),
        _mutated(ready, checks=[]),
        _mutated(ready, lambda r: r["checks"].__setitem__(1, r["checks"][0])),
        _mutated(ready, path=("checks", 0), id="unknown"),
        _mutated(ready, path=("checks", 0), status=1),
        _mutated(ready, path=("checks", 0), message=""),
        _mutated(ready, path=("checks", 0), code="missing_dependency"),
        _mutated(ready, platform=[]),
        _mutated(ready, platform="unregistered_platform"),
        _mutated(ready, launcher_kind=[]),
        _mutated(ready, launcher_kind="unregistered_launcher"),
        _mutated(ready, launcher_kind="windows_py_launcher"),
        _mutated(ready, status="error"),
        _mutated(ready, can_start_broker=False),
        _mutated(ready, notices=[""]),
        _mutated(ready, notices=["extra", "extra"]),
        _mutated(ready, notices=[1]),
        _mutated(
            ready,
            lambda r: r.update(
                checks=[{"id": "launcher_protocol", "status": "passed", "message": "bad"}]
            ),
        ),
        _mutated(failed, lambda r: r["checks"][0].pop("remediation")),
        _mutated(failed, lambda r: r["checks"][0].pop("code")),
        _mutated(failed, path=("checks", 0), code="permission_required"),
        _mutated(failed, path=("checks", 0), unexpected=True),
        _mutated(failed, path=("checks", 0, "remediation"), action="run_shell_command"),
        _mutated(failed, path=("checks", 0, "remediation"), purpose="changed"),
        _mutated(failed, path=("checks", 0, "remediation"), requires_explicit_consent=False),
        _mutated(failed, path=("checks", 0, "remediation"), source_url="https://invalid.example"),
        _mutated(failed, path=("checks", 0, "remediation"), extra=True),
        _mutated(failed, path=("checks", 0, "remediation"), requires_explicit_consent=1),
    ]


def _check_index(report: dict[str, Any], check_id: str) -> int:
    return next(index for index, check in enumerate(report["checks"]) if check["id"] == check_id)


def _windows_v1_mutations() -> list[dict[str, Any]]:
    permission = _permission_report()
    false_consent = [
        _mutated(
            permission,
            path=("checks", _check_index(permission, check_id), "remediation"),
            requires_explicit_consent=False,
        )
        for check_id in ("administrator", "wfp_permission")
    ]
    unregistered_url = _mutated(
        permission,
        path=("checks", _check_index(permission, "administrator"), "remediation"),
        source_url="https://invalid.example",
    )
    return [*false_consent, unregistered_url]


def _unsupported_v1_mutations() -> list[dict[str, Any]]:
    unsupported = evaluate_launcher_preflight(platform_name="freebsd", runtime=None)
    return [
        _mutated(unsupported, unexpected=True),
        _mutated(unsupported, status="ok", can_start_broker=True),
    ]


def _posix_reports() -> tuple[PythonRuntime, dict[str, Any], dict[str, Any]]:
    runtime = PythonRuntime(("python3.12",), "direct_python", (3, 12, 9))
    ready = evaluate_launcher_preflight(
        platform_name="darwin",
        runtime=runtime,
        path_is_file=lambda _path: True,
        sandbox_is_usable=lambda _platform: True,
    )
    failed = evaluate_launcher_preflight(
        platform_name="darwin",
        runtime=None,
        path_is_file=lambda _path: False,
    )
    return runtime, cast(dict[str, Any], ready), cast(dict[str, Any], failed)


def _permission_report() -> dict[str, Any]:
    windows_checks = []
    required = {
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
    for contract in LAUNCHER_PREFLIGHT_REGISTRY["windows"].checks:
        if contract.id not in required:
            continue
        windows_checks.append(
            failed_check(
                contract.id,
                f"{contract.id} permission is required",
                remediation_for("windows", contract.id, "request_elevation"),
            )
            if contract.id in {"administrator", "wfp_permission"}
            else passed_check(contract.id, f"{contract.id} is available")
        )
    return cast(
        dict[str, Any],
        LauncherPreflightReport(
            platform="windows",
            launcher_kind="direct_python",
            checks=tuple(windows_checks),
            notices=(
                "Native Windows Git subprocesses do not have filesystem read isolation; "
                "use WSL2 when complete Git filesystem isolation is required.",
            ),
        ).as_dict(),
    )


def _windows_ready_report() -> dict[str, Any]:
    required = {
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
    checks = tuple(
        passed_check(contract.id, f"{contract.id} is available")
        for contract in LAUNCHER_PREFLIGHT_REGISTRY["windows"].checks
        if contract.id in required
    )
    return cast(
        dict[str, Any],
        LauncherPreflightReport(
            platform="windows",
            launcher_kind="direct_python",
            checks=checks,
            notices=(
                "Native Windows Git subprocesses do not have filesystem read isolation; "
                "use WSL2 when complete Git filesystem isolation is required.",
            ),
        ).as_dict(),
    )


def test_parser_rejects_closed_world_and_cross_field_mutations() -> None:
    _runtime, ready, failed = _posix_reports()

    assert all(
        parse_launcher_preflight_report(mutation) is None
        for mutation in [
            *_v1_mutations(ready, failed),
            *_windows_v1_mutations(),
            *_unsupported_v1_mutations(),
        ]
    )


def test_launcher_replaces_every_invalid_producer_mutation_with_protocol_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_launcher()
    runtime, ready, failed = _posix_reports()

    for mutation in _v1_mutations(ready, failed):
        monkeypatch.setattr(
            launcher,
            "evaluate_launcher_preflight",
            lambda mutation=mutation, **_kwargs: mutation,
        )
        report = launcher._launcher_report(platform_name="darwin", runtime=runtime, workspace=None)
        assert parse_launcher_preflight_report(report) == report
        assert [check["id"] for check in report["checks"]] == ["launcher_protocol"]

    monkeypatch.setattr(
        launcher,
        "evaluate_launcher_preflight",
        lambda **_kwargs: _mutated(ready, platform="linux"),
    )
    wrong_platform = launcher._launcher_report(
        platform_name="darwin", runtime=runtime, workspace=None
    )
    assert [check["id"] for check in wrong_platform["checks"]] == ["launcher_protocol"]

    for mutation in _unsupported_v1_mutations():
        monkeypatch.setattr(
            launcher,
            "evaluate_launcher_preflight",
            lambda mutation=mutation, **_kwargs: mutation,
        )
        unsupported = launcher._launcher_report(
            platform_name="freebsd", runtime=None, workspace=None
        )
        assert parse_launcher_preflight_report(unsupported) == unsupported
        assert unsupported["checks"][0]["id"] == "platform_support"


def test_launcher_replaces_every_invalid_windows_mutation_with_protocol_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_launcher()
    runtime = PythonRuntime(("python3.12",), "direct_python", (3, 12, 9))
    monkeypatch.setattr(launcher, "_windows_report", lambda *_args: {})

    for mutation in _windows_v1_mutations():
        monkeypatch.setattr(
            launcher,
            "launcher_report_from_windows",
            lambda *_args, mutation=mutation: mutation,
        )
        report = launcher._launcher_report(
            platform_name="win32", runtime=runtime, workspace="C:\\workspace"
        )
        assert parse_launcher_preflight_report(report) == report
        assert [check["id"] for check in report["checks"]] == ["launcher_protocol"]


@pytest.mark.parametrize("platform_name", ["darwin", "linux"])
def test_cross_platform_runtime_kind_becomes_one_accepted_protocol_failure(
    platform_name: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    launcher = _load_launcher()
    runtime = PythonRuntime(("python3.12",), "windows_py_launcher", (3, 12, 9))
    damaged = evaluate_launcher_preflight(
        platform_name=platform_name,
        runtime=runtime,
        path_is_file=lambda _path: True,
        sandbox_is_usable=lambda _platform: True,
    )
    assert parse_launcher_preflight_report(damaged) is None
    monkeypatch.setattr(launcher, "_discover_runtime", lambda _platform: runtime)
    monkeypatch.setattr(
        launcher,
        "evaluate_launcher_preflight",
        lambda **_kwargs: damaged,
    )

    result = launcher.run(["--preflight-only"], platform_name=platform_name)

    output = capsys.readouterr()
    report = json.loads(output.out)
    assert result == 2
    assert output.err == ""
    assert "Traceback" not in output.out
    assert parse_launcher_preflight_report(report) == report
    assert report["status"] == "error"
    assert report["can_start_broker"] is False
    assert report["launcher_kind"] == "unavailable"
    assert [check["id"] for check in report["checks"]] == ["launcher_protocol"]


def test_skill_blind_matrix_starts_only_for_a_valid_ready_report() -> None:
    skill = (Path(__file__).resolve().parents[2] / "SKILL.md").read_text(encoding="utf-8")
    runtime, ready, _failed = _posix_reports()
    del runtime
    missing = evaluate_launcher_preflight(
        platform_name="darwin",
        runtime=None,
        path_is_file=lambda _path: True,
        sandbox_is_usable=lambda _platform: True,
    )
    permission = _permission_report()
    unsupported = evaluate_launcher_preflight(platform_name="freebsd", runtime=None)
    cases = {
        "ready": (ready, 0, "", ("start_broker",)),
        "missing_dependency": (
            missing,
            2,
            "",
            ("explain:missing_dependency", "request_explicit_consent"),
        ),
        "permission_required": (
            permission,
            2,
            "",
            (
                "explain:permission_required,permission_required",
                "request_explicit_consent",
            ),
        ),
        "unsupported_capability": (
            unsupported,
            2,
            "",
            ("explain:unsupported_capability",),
        ),
        "missing_report": (None, 2, "", ("runtime_contract_gap",)),
        "damaged_report": ({"damaged": True}, 2, "", ("runtime_contract_gap",)),
        "exit_mismatch": (ready, 2, "", ("runtime_contract_gap",)),
        "stderr_contamination": (
            ready,
            0,
            "PRIVATE_SENTINEL",
            ("runtime_contract_gap",),
        ),
    }

    class BlindHost:
        def __init__(self) -> None:
            self.events: list[str] = []
            self.observed_facts: list[LauncherPreflightFact] = []

        def start_broker(self) -> None:
            self.events.append("start_broker")

        def explain(self, facts: tuple[LauncherPreflightFact, ...]) -> None:
            self.observed_facts.extend(facts)
            self.events.append("explain:" + ",".join(fact.code for fact in facts))

        def request_explicit_consent(self, facts: tuple[LauncherPreflightFact, ...]) -> None:
            self.observed_facts.extend(facts)
            self.events.append("request_explicit_consent")

        def report_runtime_contract_gap(self) -> None:
            self.events.append("runtime_contract_gap")

    assert "`classify_launcher_preflight`" in skill
    assert "`apply_launcher_preflight_decision`" in skill
    for report, exit_code, stderr, expected_events in cases.values():
        decision = classify_launcher_preflight(report, exit_code=exit_code, stderr=stderr)
        host = BlindHost()
        apply_launcher_preflight_decision(decision, host)
        assert tuple(host.events) == expected_events
        assert all(not hasattr(fact, "action") for fact in host.observed_facts)


def test_parser_rejects_impossible_launcher_and_broker_start_states() -> None:
    runtime = PythonRuntime(("python3.12",), "direct_python", (3, 12, 9))
    ready = evaluate_launcher_preflight(
        platform_name="darwin",
        runtime=runtime,
        path_is_file=lambda _path: True,
        sandbox_is_usable=lambda _platform: True,
    )
    no_runtime = evaluate_launcher_preflight(
        platform_name="darwin",
        runtime=None,
        path_is_file=lambda _path: True,
        sandbox_is_usable=lambda _platform: True,
    )
    broker_failed = _mutated(ready, status="error", can_start_broker=False)
    broker_failed["checks"].append(
        {
            "id": "broker_start",
            "status": "failed",
            "message": "broker failed",
            "code": "unsupported_capability",
            "remediation": {
                "action": "repair_broker_runtime",
                "purpose": "start the installed GoodJob session broker",
                "requires_explicit_consent": False,
            },
        }
    )
    broker_failed["checks"][0] = deepcopy(no_runtime["checks"][0])

    impossible = [
        _mutated(no_runtime, launcher_kind="direct_python"),
        _mutated(ready, launcher_kind="unavailable", status="error", can_start_broker=False),
        broker_failed,
    ]
    assert all(parse_launcher_preflight_report(report) is None for report in impossible)


@pytest.mark.parametrize(
    ("failed_id", "action"),
    [
        ("administrator", "request_elevation"),
        ("bfe_service", "request_service_enablement"),
        ("wfp_api", "repair_windows_or_use_wsl2"),
    ],
)
def test_parser_rejects_wfp_permission_passed_without_every_prerequisite(
    failed_id: str,
    action: str,
) -> None:
    report = _windows_ready_report()
    failed_index = next(
        index for index, check in enumerate(report["checks"]) if check["id"] == failed_id
    )
    report["checks"][failed_index] = failed_check(
        failed_id,
        f"{failed_id} is unavailable",
        remediation_for("windows", failed_id, action),
    ).as_dict()
    report["status"] = "error"
    report["can_start_broker"] = False

    assert (
        next(check["status"] for check in report["checks"] if check["id"] == "wfp_permission")
        == "passed"
    )
    assert parse_launcher_preflight_report(report) is None


@pytest.mark.parametrize("failure_kind", ["missing", "import_crash", "constructor_crash"])
def test_interpreter_start_before_broker_failure_is_one_v1_report(
    failure_kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    launcher = _load_launcher()
    runtime = PythonRuntime((sys.executable,), "direct_python", sys.version_info[:3])
    ready = evaluate_launcher_preflight(
        platform_name="darwin",
        runtime=runtime,
        path_is_file=lambda _path: True,
        sandbox_is_usable=lambda _platform: True,
    )
    session = tmp_path / "missing-session.py"
    if failure_kind == "import_crash":
        session.write_text(
            "import os\n"
            "import sys\n"
            "print('PRIVATE_STDOUT_SENTINEL')\n"
            "sys.__stdout__.write('PRIVATE_ORIGINAL_STDOUT_SENTINEL\\n')\n"
            "sys.__stdout__.flush()\n"
            "os.write(1, b'PRIVATE_FD_STDOUT_SENTINEL\\n')\n"
            "os.write(2, b'PRIVATE_FD_STDERR_SENTINEL\\n')\n"
            "raise RuntimeError('PRIVATE_IMPORT_SENTINEL')\n",
            encoding="utf-8",
        )
    elif failure_kind == "constructor_crash":
        session.write_text(
            "import os\n"
            "import sys\n"
            "class SessionBroker:\n"
            "    def __init__(self, *args, **kwargs):\n"
            "        print('PRIVATE_STDOUT_SENTINEL', flush=True)\n"
            "        print('PRIVATE_STDERR_SENTINEL', file=sys.stderr, flush=True)\n"
            "        os.write(1, b'PRIVATE_FD_STDOUT_SENTINEL\\n')\n"
            "        os.write(2, b'PRIVATE_FD_STDERR_SENTINEL\\n')\n"
            "        raise RuntimeError('PRIVATE_CONSTRUCTOR_SENTINEL')\n"
            "def run(argv=None):\n"
            "    SessionBroker(argv)\n"
            "    return 0\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(launcher, "SESSION_SCRIPT", session)
    monkeypatch.setattr(launcher, "_discover_runtime", lambda _platform: runtime)
    monkeypatch.setattr(launcher, "evaluate_launcher_preflight", lambda **_kwargs: ready)

    result = launcher.run([], platform_name="darwin")

    output = capfd.readouterr()
    report = json.loads(output.err)
    assert result == 2
    assert output.out == ""
    assert "PRIVATE_" not in output.err
    assert str(session) not in output.err
    assert parse_launcher_preflight_report(report) == report
    assert report["checks"][-1]["id"] == "broker_start"


_PROCESS_WRAPPER = """
import importlib.util
import json
import os
import sys
from pathlib import Path

launcher_path, report_path, platform, session_path, mode, workspace = sys.argv[1:7]
launcher_args = sys.argv[7:]
spec = importlib.util.spec_from_file_location("goodjob_process_matrix_launcher", launcher_path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
report = json.loads(Path(report_path).read_text(encoding="utf-8"))
runtime = module.PythonRuntime((sys.executable,), "direct_python", tuple(sys.version_info[:3]))
module._discover_runtime = lambda _platform: runtime
if mode == "producer":
    module.evaluate_launcher_preflight = lambda **_kwargs: report
elif mode == "damaged":
    module.evaluate_launcher_preflight = lambda **_kwargs: {"damaged": True}
elif mode == "direct":
    module._launcher_report = lambda **_kwargs: report
if session_path != "default":
    module.SESSION_SCRIPT = Path(session_path)

def audit(event, args):
    if event not in {"open", "os.listdir", "os.scandir"} or not args:
        return
    try:
        path = os.fspath(args[0])
    except TypeError:
        return
    if isinstance(path, str) and path.startswith(workspace):
        raise RuntimeError("workspace source access was attempted")

sys.addaudithook(audit)
raise SystemExit(module.run(launcher_args, platform_name=platform))
"""


def _tree_snapshot(root: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            snapshot[relative] = "symlink:" + os.readlink(path)
        elif path.is_dir():
            snapshot[relative] = "directory"
        elif path.is_file():
            snapshot[relative] = "file:" + hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            snapshot[relative] = "other"
    return snapshot


@pytest.mark.parametrize(
    ("scenario", "platform", "mode", "arguments", "stdin", "expected_exit", "channel"),
    [
        ("ready", "darwin", "producer", ["--preflight-only"], "", 0, "stdout"),
        ("missing", "darwin", "producer", ["--preflight-only"], "", 2, "stdout"),
        ("permission", "win32", "direct", ["--preflight-only"], "", 2, "stdout"),
        ("unsupported", "freebsd", "producer", ["--preflight-only"], "", 2, "stdout"),
        ("windows-no-workspace", "win32", "native", ["--preflight-only"], "", 2, "stdout"),
        ("damaged", "darwin", "damaged", ["--preflight-only"], "", 2, "stdout"),
        ("unknown", "darwin", "producer", ["--unknown", "ARGV_PRIVATE"], "", 2, "syntax"),
        (
            "conflict",
            "win32",
            "producer",
            ["--preflight-only", "--windows-preflight-only"],
            "",
            2,
            "syntax",
        ),
        ("normal-ready", "darwin", "producer", [], "{}\n", 0, "stdout"),
        ("prebroker", "darwin", "producer", [], "", 2, "stderr"),
    ],
)
def test_real_process_and_distinct_sentinel_matrix(
    scenario: str,
    platform: str,
    mode: str,
    arguments: list[str],
    stdin: str,
    expected_exit: int,
    channel: str,
    tmp_path: Path,
) -> None:
    launcher = Path(__file__).resolve().parents[1] / "scripts" / "launch_broker.py"
    workspace = tmp_path / "WORKSPACE_PRIVATE_01"
    workspace.mkdir()
    source = workspace / "SOURCE_PRIVATE_02.txt"
    source.write_text("SOURCE_CONTENT_PRIVATE_03", encoding="utf-8")
    home = tmp_path / "HOME_PRIVATE_04"
    home.mkdir()
    (home / "before.txt").write_text("HOME_CONTENT_PRIVATE_05", encoding="utf-8")
    data_dir = tmp_path / "DATA_PRIVATE_06"
    report_path = tmp_path / "report.json"
    runtime, ready, missing = _posix_reports()
    del runtime
    reports = {
        "permission": _permission_report(),
        "unsupported": cast(
            dict[str, Any], evaluate_launcher_preflight(platform_name="freebsd", runtime=None)
        ),
        "missing": missing,
    }
    report_path.write_text(json.dumps(reports.get(scenario, ready)), encoding="utf-8")
    session_path = str(tmp_path / "SESSION_PRIVATE_07.py") if scenario == "prebroker" else "default"
    workspace_args = [] if scenario == "windows-no-workspace" else ["--workspace", str(workspace)]
    launcher_args = [
        *arguments,
        *workspace_args,
        "--data-dir",
        str(data_dir),
        "--agent-runtime",
        "AGENT_RUNTIME_PRIVATE_08",
    ]
    before_workspace = _tree_snapshot(workspace)
    before_home = _tree_snapshot(home)
    env = {
        **os.environ,
        "HOME": str(home),
        "GOODJOB_ENV_PRIVATE_09": "ENV_VALUE_PRIVATE_10",
        "GOODJOB_CAPABILITY_PRIVATE_11": "CAPABILITY_VALUE_PRIVATE_12",
        "GOODJOB_JD_PRIVATE_13": "JD_VALUE_PRIVATE_14",
    }

    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            _PROCESS_WRAPPER,
            str(launcher),
            str(report_path),
            platform,
            session_path,
            mode,
            str(workspace),
            *launcher_args,
        ],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=30,
    )

    assert result.returncode == expected_exit
    combined = result.stdout + result.stderr
    for sentinel in (
        str(workspace),
        str(data_dir),
        str(home),
        "ARGV_PRIVATE",
        "AGENT_RUNTIME_PRIVATE_08",
        "ENV_VALUE_PRIVATE_10",
        "CAPABILITY_VALUE_PRIVATE_12",
        "JD_VALUE_PRIVATE_14",
        "SOURCE_CONTENT_PRIVATE_03",
        "HOME_CONTENT_PRIVATE_05",
    ):
        assert sentinel not in combined
    assert _tree_snapshot(workspace) == before_workspace
    assert _tree_snapshot(home) == before_home
    if scenario == "normal-ready":
        assert json.loads(result.stdout)["code"] == "invalid_input"
        assert result.stderr == ""
    elif channel == "syntax":
        assert result.stdout == ""
        assert "usage:" in result.stderr
        assert "error: invalid arguments" in result.stderr
        assert data_dir.exists() is False
    else:
        raw = result.stdout if channel == "stdout" else result.stderr
        other = result.stderr if channel == "stdout" else result.stdout
        emitted = json.loads(raw)
        assert other == ""
        assert parse_launcher_preflight_report(emitted) == emitted
        assert emitted["can_start_broker"] is (scenario == "ready")
        assert data_dir.exists() is False


def test_normal_success_is_silent_and_preserves_broker_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    launcher = _load_launcher()
    runtime = PythonRuntime(("python3.12",), "direct_python", (3, 12, 9))
    ready = evaluate_launcher_preflight(
        platform_name="darwin",
        runtime=runtime,
        path_is_file=lambda _path: True,
        sandbox_is_usable=lambda _platform: True,
    )
    calls: list[list[str]] = []

    def record_call(command: list[str]) -> int:
        calls.append(command)
        return 17

    monkeypatch.setattr(launcher, "_discover_runtime", lambda _platform: runtime)
    monkeypatch.setattr(
        launcher,
        "evaluate_launcher_preflight",
        lambda **_kwargs: ready,
    )
    monkeypatch.setattr(launcher, "_run_broker_process", record_call)

    result = launcher.run([], platform_name="darwin")

    output = capsys.readouterr()
    assert result == 17
    assert output.out == output.err == ""
    assert len(calls) == 1


def test_runtime_wrapper_output_before_python_bootstrap_is_discarded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    launcher = _load_launcher()
    wrapper = tmp_path / "runtime-wrapper"
    wrapper.write_text(
        f"#!{sys.executable}\n"
        "import os\n"
        "import sys\n"
        "print('PRIVATE_WRAPPER_STDOUT ' + ' '.join(sys.argv[1:]), flush=True)\n"
        "print('PRIVATE_WRAPPER_STDERR ' + ' '.join(sys.argv[1:]), file=sys.stderr, flush=True)\n"
        "os.execv(sys.executable, [sys.executable, *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    session = tmp_path / "session.py"
    session.write_text(
        "import json\n"
        "class SessionBroker:\n"
        "    def __init__(self, *_args, **_kwargs):\n"
        "        pass\n"
        "def run(argv=None):\n"
        "    SessionBroker(argv)\n"
        "    print(json.dumps({'status': 'ok'}), flush=True)\n"
        "    return 0\n",
        encoding="utf-8",
    )
    runtime = PythonRuntime((str(wrapper),), "direct_python", sys.version_info[:3])
    ready = evaluate_launcher_preflight(
        platform_name="darwin",
        runtime=runtime,
        path_is_file=lambda _path: True,
        sandbox_is_usable=lambda _platform: True,
    )
    monkeypatch.setattr(launcher, "SESSION_SCRIPT", session)
    monkeypatch.setattr(launcher, "_discover_runtime", lambda _platform: runtime)
    monkeypatch.setattr(launcher, "evaluate_launcher_preflight", lambda **_kwargs: ready)

    result = launcher.run(
        ["--data-dir", str(tmp_path / "DATA_DIR_PRIVATE_SENTINEL")],
        platform_name="darwin",
    )

    output = capfd.readouterr()
    assert result == 0
    assert "PRIVATE_WRAPPER" not in output.out + output.err
    assert "DATA_DIR_PRIVATE_SENTINEL" not in output.out + output.err
    assert json.loads(output.out) == {"status": "ok"}
    assert output.err == ""


def test_public_fixed_marker_cannot_spoof_broker_readiness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    launcher = _load_launcher()
    wrapper = tmp_path / "marker-spoof-wrapper"
    wrapper.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "marker = 'goodjob-launcher-broker-ready-v1'\n"
        "print(marker, flush=True)\n"
        "print(marker, file=sys.stderr, flush=True)\n"
        "print('PRIVATE_SPOOF_ARGV ' + ' '.join(sys.argv[1:]), flush=True)\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    runtime = PythonRuntime((str(wrapper),), "direct_python", sys.version_info[:3])
    ready = evaluate_launcher_preflight(
        platform_name="darwin",
        runtime=runtime,
        path_is_file=lambda _path: True,
        sandbox_is_usable=lambda _platform: True,
    )
    monkeypatch.setattr(launcher, "_discover_runtime", lambda _platform: runtime)
    monkeypatch.setattr(launcher, "evaluate_launcher_preflight", lambda **_kwargs: ready)

    result = launcher.run(
        ["--data-dir", str(tmp_path / "DATA_DIR_PRIVATE_SPOOF_SENTINEL")],
        platform_name="darwin",
    )

    output = capfd.readouterr()
    report = json.loads(output.err)
    assert result == 2
    assert output.out == ""
    assert "PRIVATE_SPOOF" not in output.err
    assert "DATA_DIR_PRIVATE_SPOOF_SENTINEL" not in output.err
    assert parse_launcher_preflight_report(report) == report
    assert report["checks"][-1]["id"] == "broker_start"


def test_normal_start_uses_a_static_bootstrap_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_launcher()
    runtime = PythonRuntime(("python3.12",), "direct_python", (3, 12, 9))
    ready = evaluate_launcher_preflight(
        platform_name="darwin",
        runtime=runtime,
        path_is_file=lambda _path: True,
        sandbox_is_usable=lambda _platform: True,
    )
    calls: list[list[str]] = []

    def record_call(command: list[str]) -> int:
        calls.append(command)
        return 0

    monkeypatch.setattr(launcher, "_discover_runtime", lambda _platform: runtime)
    monkeypatch.setattr(launcher, "evaluate_launcher_preflight", lambda **_kwargs: ready)
    monkeypatch.setattr(launcher, "_run_broker_process", record_call)

    assert launcher.run([], platform_name="darwin") == 0
    assert len(calls) == 1
    assert "-c" not in calls[0]
    assert launcher.BROKER_BOOTSTRAP_SCRIPT in map(Path, calls[0])


def test_broker_spawn_failure_is_one_accepted_stderr_v1_report(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    launcher = _load_launcher()
    runtime = PythonRuntime(("python3.12",), "direct_python", (3, 12, 9))
    ready = evaluate_launcher_preflight(
        platform_name="darwin",
        runtime=runtime,
        path_is_file=lambda _path: True,
        sandbox_is_usable=lambda _platform: True,
    )
    monkeypatch.setattr(launcher, "_discover_runtime", lambda _platform: runtime)
    monkeypatch.setattr(
        launcher,
        "evaluate_launcher_preflight",
        lambda **_kwargs: ready,
    )
    monkeypatch.setattr(
        launcher,
        "_run_broker_process",
        lambda _command: (_ for _ in ()).throw(OSError("cannot spawn")),
    )

    result = launcher.run([], platform_name="darwin")

    output = capsys.readouterr()
    report = json.loads(output.err)
    assert result == 2
    assert output.out == ""
    assert parse_launcher_preflight_report(report) == report
    assert report["checks"][-1]["id"] == "broker_start"
