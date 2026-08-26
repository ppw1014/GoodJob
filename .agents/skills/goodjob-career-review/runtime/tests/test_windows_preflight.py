from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from goodjob.platform import sandbox_windows
from goodjob.platform.handles_windows import (
    OwnedHandle,
    RetainedOwnerCleanupError,
    close_owned_resources,
    retry_retained_owners,
)
from goodjob.platform.preflight_windows import (
    WindowsBootstrapReport,
    evaluate_windows_preflight,
    missing_python_runtime_report,
    parse_windows_bootstrap_report,
    parse_windows_preflight_report,
    preflight_protocol_failure_report,
    retry_windows_preflight_cleanup,
)


@dataclass
class FakeWindowsProbes:
    git_executable: Path | None = Path(r"C:\Program Files\Git\mingw64\bin\git.exe")
    filesystem: str = "NTFS"
    bfe: bool = True
    elevated: bool = True
    wfp_api: bool = True
    wfp_write: bool = True
    wfp_error: OSError | None = None
    runtime_importable: bool = True

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
        if self.wfp_error is not None:
            raise self.wfp_error
        return self.wfp_write

    def runtime_modules_importable(self) -> bool:
        return self.runtime_importable


@dataclass
class CleanupAwareWindowsProbes(FakeWindowsProbes):
    calls: list[str] = field(default_factory=list)

    def retry_retained_cleanup(self) -> None:
        retry_windows_preflight_cleanup()

    def _record(self, name: str) -> None:
        self.calls.append(name)

    def trusted_git_executable(self) -> Path | None:
        self._record("trusted_git")
        return super().trusted_git_executable()

    def workspace_filesystem(self, workspace: Path) -> str:
        self._record("workspace_filesystem")
        return super().workspace_filesystem(workspace)

    def bfe_is_running(self) -> bool:
        self._record("bfe_service")
        return super().bfe_is_running()

    def is_elevated(self) -> bool:
        self._record("administrator")
        return super().is_elevated()

    def wfp_api_is_available(self) -> bool:
        self._record("wfp_api")
        return super().wfp_api_is_available()

    def wfp_policy_write_access(self) -> bool:
        self._record("wfp_permission")
        return super().wfp_policy_write_access()

    def runtime_modules_importable(self) -> bool:
        self._record("runtime_installation")
        return super().runtime_modules_importable()


def _runtime_tree(tmp_path: Path) -> Path:
    runtime = tmp_path / "runtime"
    (runtime / "scripts").mkdir(parents=True)
    (runtime / "src" / "goodjob").mkdir(parents=True)
    (runtime / "scripts" / "broker_bootstrap.py").write_text("# bootstrap\n", encoding="utf-8")
    (runtime / "scripts" / "launch_broker.py").write_text("# launcher\n", encoding="utf-8")
    (runtime / "scripts" / "session.py").write_text("# broker\n", encoding="utf-8")
    (runtime / "scripts" / "windows_preflight.py").write_text("# preflight\n", encoding="utf-8")
    (runtime / "src" / "goodjob" / "__init__.py").write_text("", encoding="utf-8")
    return runtime


class _RetainedWfpApi:
    def __init__(self) -> None:
        self.cleanup_blocked = True
        self.close_calls = 0

    def FwpmEngineClose0(self, _engine: object) -> int:
        self.close_calls += 1
        return 5 if self.cleanup_blocked else 0


@pytest.mark.parametrize(
    "report",
    [
        missing_python_runtime_report(),
        preflight_protocol_failure_report("the preflight command boundary failed"),
    ],
)
def test_windows_bootstrap_reports_round_trip_through_their_parser(
    report: WindowsBootstrapReport,
) -> None:
    raw = report.as_dict()

    assert parse_windows_bootstrap_report(raw) == raw
    assert parse_windows_preflight_report(raw) is None


def test_windows_preflight_classifies_every_failed_prerequisite(tmp_path: Path) -> None:
    runtime = _runtime_tree(tmp_path)
    probes = FakeWindowsProbes(
        git_executable=None,
        filesystem="ReFS",
        bfe=False,
        elevated=False,
        wfp_api=False,
    )

    report = evaluate_windows_preflight(
        workspace=tmp_path / "workspace",
        runtime_dir=runtime,
        python_version=None,
        launcher_kind=None,
        uv_available=False,
        release_enabled=True,
        probes=probes,
    ).as_dict()

    assert report["contract_version"] == "windows-prerequisite-preflight-v1"
    assert parse_windows_preflight_report(report) == report
    assert parse_windows_bootstrap_report(report) is None
    assert report["status"] == "error"
    assert report["can_start_broker"] is False
    failed = {
        check["id"]: check["code"] for check in report["checks"] if check["status"] == "failed"
    }
    assert failed == {
        "python_runtime": "missing_dependency",
        "trusted_git": "missing_dependency",
        "workspace_filesystem": "unsupported_capability",
        "bfe_service": "unsupported_capability",
        "administrator": "permission_required",
        "wfp_api": "unsupported_capability",
        "wfp_permission": "permission_required",
    }
    python_check = next(check for check in report["checks"] if check["id"] == "python_runtime")
    assert python_check["remediation"] == {
        "action": "request_installation",
        "purpose": "run the isolated GoodJob broker with Python 3.12 or newer",
        "source_url": "https://www.python.org/downloads/windows/",
        "requires_explicit_consent": True,
    }


@pytest.mark.parametrize(
    "mutation",
    ["top", "passed_check", "failed_check", "remediation"],
)
def test_legacy_full_parser_rejects_unknown_fields(
    tmp_path: Path,
    mutation: str,
) -> None:
    report = evaluate_windows_preflight(
        workspace=tmp_path / "workspace",
        runtime_dir=_runtime_tree(tmp_path),
        python_version=(3, 12, 9),
        launcher_kind="direct_python",
        uv_available=False,
        release_enabled=True,
        probes=FakeWindowsProbes(elevated=False),
    ).as_dict()
    mutated = deepcopy(report)
    if mutation == "top":
        mutated["unexpected"] = True  # type: ignore[typeddict-unknown-key]
    elif mutation == "passed_check":
        mutated["checks"][0]["unexpected"] = True  # type: ignore[typeddict-unknown-key]
    elif mutation == "failed_check":
        failed = next(check for check in mutated["checks"] if check["status"] == "failed")
        failed["unexpected"] = True  # type: ignore[typeddict-unknown-key]
    else:
        failed = next(check for check in mutated["checks"] if check["status"] == "failed")
        failed["remediation"]["unexpected"] = True  # type: ignore[typeddict-unknown-key]

    assert parse_windows_preflight_report(mutated) is None


def test_windows_preflight_retries_after_installation_and_elevation(tmp_path: Path) -> None:
    runtime = _runtime_tree(tmp_path)
    workspace = tmp_path / "workspace"
    probes = FakeWindowsProbes(git_executable=None, bfe=False, elevated=False)

    refused = evaluate_windows_preflight(
        workspace=workspace,
        runtime_dir=runtime,
        python_version=(3, 12, 8),
        launcher_kind="direct_python",
        uv_available=False,
        release_enabled=True,
        probes=probes,
    ).as_dict()

    assert refused["can_start_broker"] is False
    assert all(
        check.get("remediation", {}).get("requires_explicit_consent", False)
        for check in refused["checks"]
        if check["status"] == "failed"
    )

    probes.git_executable = Path(r"C:\Program Files\Git\mingw64\bin\git.exe")
    probes.bfe = True
    probes.elevated = True
    retried = evaluate_windows_preflight(
        workspace=workspace,
        runtime_dir=runtime,
        python_version=(3, 12, 8),
        launcher_kind="direct_python",
        uv_available=False,
        release_enabled=True,
        probes=probes,
    ).as_dict()

    assert retried["status"] == "ok"
    assert retried["can_start_broker"] is True
    assert {check["id"] for check in retried["checks"]} == {
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


def test_windows_preflight_requires_a_successful_wfp_policy_write_probe(tmp_path: Path) -> None:
    report = evaluate_windows_preflight(
        workspace=tmp_path / "workspace",
        runtime_dir=_runtime_tree(tmp_path),
        python_version=(3, 12, 8),
        launcher_kind="direct_python",
        uv_available=False,
        release_enabled=True,
        probes=FakeWindowsProbes(wfp_write=False),
    ).as_dict()

    permission = next(check for check in report["checks"] if check["id"] == "wfp_permission")
    assert permission["status"] == "failed"
    assert permission["code"] == "permission_required"
    assert permission["remediation"]["action"] == "request_elevation"


def test_windows_preflight_classifies_non_permission_wfp_failure_as_unsupported(
    tmp_path: Path,
) -> None:
    report = evaluate_windows_preflight(
        workspace=tmp_path / "workspace",
        runtime_dir=_runtime_tree(tmp_path),
        python_version=(3, 12, 8),
        launcher_kind="direct_python",
        uv_available=False,
        release_enabled=True,
        probes=FakeWindowsProbes(wfp_error=OSError("WFP policy store is unavailable")),
    ).as_dict()

    permission = next(check for check in report["checks"] if check["id"] == "wfp_permission")
    assert permission["status"] == "failed"
    assert permission["code"] == "unsupported_capability"
    assert permission["remediation"]["action"] == "repair_windows_or_use_wsl2"


def test_windows_preflight_rejects_an_unimportable_runtime(tmp_path: Path) -> None:
    report = evaluate_windows_preflight(
        workspace=tmp_path / "workspace",
        runtime_dir=_runtime_tree(tmp_path),
        python_version=(3, 12, 8),
        launcher_kind="direct_python",
        uv_available=False,
        release_enabled=True,
        probes=FakeWindowsProbes(runtime_importable=False),
    ).as_dict()

    runtime = next(check for check in report["checks"] if check["id"] == "runtime_installation")
    assert runtime["status"] == "failed"
    assert runtime["code"] == "missing_dependency"
    assert runtime["remediation"]["action"] == "request_reinstallation"


def test_windows_preflight_keeps_unreleased_runtime_closed_without_ipv6_requirement(
    tmp_path: Path,
) -> None:
    report = evaluate_windows_preflight(
        workspace=tmp_path / "workspace",
        runtime_dir=_runtime_tree(tmp_path),
        python_version=(3, 13, 1),
        launcher_kind="uv",
        uv_available=True,
        release_enabled=False,
        probes=FakeWindowsProbes(),
    ).as_dict()

    failed = [check for check in report["checks"] if check["status"] == "failed"]
    assert [(check["id"], check["code"]) for check in failed] == [
        ("native_windows_release", "unsupported_capability")
    ]
    assert all("ipv6" not in check["id"].lower() for check in report["checks"])
    assert report["can_start_broker"] is False


def test_windows_preflight_stops_before_new_probes_until_retained_cleanup_succeeds(
    tmp_path: Path,
) -> None:
    cleanup_blocked = True

    def close_owner(_value: int) -> None:
        if cleanup_blocked:
            raise OSError("injected retained-owner cleanup failure")

    owner = OwnedHandle(17486, closer=close_owner)
    with pytest.raises(OSError, match="cleanup failure"):
        close_owned_resources((owner,))
    try:
        with pytest.raises(RetainedOwnerCleanupError):
            retry_retained_owners()
        probes = CleanupAwareWindowsProbes()
        runtime = _runtime_tree(tmp_path)

        blocked = evaluate_windows_preflight(
            workspace=tmp_path / "workspace",
            runtime_dir=runtime,
            python_version=(3, 13, 5),
            launcher_kind="windows_py_launcher",
            uv_available=False,
            release_enabled=True,
            probes=probes,
        ).as_dict()

        assert probes.calls == []
        assert parse_windows_preflight_report(blocked) == blocked
        cleanup_failures = [
            check
            for check in blocked["checks"]
            if check["id"] not in {"python_runtime", "native_windows_release"}
        ]
        assert {check["code"] for check in cleanup_failures} == {"unsupported_capability"}
        assert {check["remediation"]["action"] for check in cleanup_failures} == {
            "retry_cleanup_or_repair_runtime_or_use_wsl2"
        }
        assert all(
            check["remediation"]["action"]
            not in {"request_installation", "request_service_enablement", "request_elevation"}
            for check in blocked["checks"]
            if check["status"] == "failed"
        )

        cleanup_blocked = False
        recovered = evaluate_windows_preflight(
            workspace=tmp_path / "workspace",
            runtime_dir=runtime,
            python_version=(3, 13, 5),
            launcher_kind="windows_py_launcher",
            uv_available=False,
            release_enabled=True,
            probes=probes,
        ).as_dict()

        assert recovered["status"] == "ok"
        assert probes.calls == [
            "runtime_installation",
            "trusted_git",
            "workspace_filesystem",
            "bfe_service",
            "administrator",
            "wfp_api",
            "wfp_permission",
        ]
    finally:
        cleanup_blocked = False
        retry_retained_owners()


def test_windows_preflight_stops_before_probes_until_retained_wfp_cleanup_succeeds(
    tmp_path: Path,
) -> None:
    api = _RetainedWfpApi()
    sandbox_windows._RETAINED_WFP_ENGINES.append((api, 31901))
    probes = CleanupAwareWindowsProbes()
    runtime = _runtime_tree(tmp_path)

    try:
        blocked = evaluate_windows_preflight(
            workspace=tmp_path / "workspace",
            runtime_dir=runtime,
            python_version=(3, 13, 5),
            launcher_kind="uv",
            uv_available=True,
            release_enabled=True,
            probes=probes,
        ).as_dict()

        assert probes.calls == []
        assert api.close_calls == 1
        assert [(api, 31901)] == sandbox_windows._RETAINED_WFP_ENGINES
        assert parse_windows_preflight_report(blocked) == blocked
        cleanup_failures = [
            check
            for check in blocked["checks"]
            if check["id"] not in {"python_runtime", "native_windows_release"}
        ]
        assert {check["code"] for check in cleanup_failures} == {"unsupported_capability"}
        assert {check["remediation"]["action"] for check in cleanup_failures} == {
            "retry_cleanup_or_repair_runtime_or_use_wsl2"
        }
        assert all(
            check["remediation"]["action"]
            not in {"request_installation", "request_service_enablement", "request_elevation"}
            for check in blocked["checks"]
            if check["status"] == "failed"
        )

        api.cleanup_blocked = False
        recovered = evaluate_windows_preflight(
            workspace=tmp_path / "workspace",
            runtime_dir=runtime,
            python_version=(3, 13, 5),
            launcher_kind="uv",
            uv_available=True,
            release_enabled=True,
            probes=probes,
        ).as_dict()

        assert recovered["status"] == "ok"
        assert api.close_calls == 2
        assert sandbox_windows._RETAINED_WFP_ENGINES == []
        assert probes.calls == [
            "runtime_installation",
            "trusted_git",
            "workspace_filesystem",
            "bfe_service",
            "administrator",
            "wfp_api",
            "wfp_permission",
        ]
    finally:
        api.cleanup_blocked = False
        sandbox_windows._retry_retained_wfp_engines()
