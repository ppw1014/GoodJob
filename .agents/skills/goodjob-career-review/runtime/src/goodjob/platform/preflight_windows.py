"""Structured, fail-closed prerequisites for the native Windows runtime."""

from __future__ import annotations

import ctypes
import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, TypedDict, cast

from goodjob.errors import (
    GoodJobError,
    MissingDependencyError,
    PermissionRequiredError,
    UnsupportedCapabilityError,
)
from goodjob.platform.handles_windows import (
    OwnedHandle,
    RetainedOwnerCleanupError,
    close_owned_resources,
    last_error,
    load_windows_dll,
    retry_retained_owners,
)
from goodjob.platform.launcher_preflight import (
    WINDOWS_GIT_FS_NOTICE,
    WINDOWS_PREFLIGHT_REQUIRED_CHECK_IDS,
    remediation_for,
)

PreflightCode = Literal["missing_dependency", "permission_required", "unsupported_capability"]
CheckStatus = Literal["passed", "failed"]
_PASSED_CHECK_FIELDS = frozenset({"id", "status", "message"})
_FAILED_CHECK_FIELDS = frozenset({"id", "status", "message", "code", "remediation"})
_REMEDIATION_REQUIRED_FIELDS = frozenset({"action", "purpose", "requires_explicit_consent"})
_REMEDIATION_OPTIONAL_FIELDS = frozenset({"source_url"})


class PreflightRemediationDict(TypedDict, total=False):
    action: str
    purpose: str
    source_url: str
    requires_explicit_consent: bool


class PreflightCheckDict(TypedDict, total=False):
    id: str
    status: CheckStatus
    message: str
    code: PreflightCode
    remediation: PreflightRemediationDict


class WindowsPreflightReportDict(TypedDict):
    contract_version: str
    status: Literal["ok", "error"]
    can_start_broker: bool
    checks: list[PreflightCheckDict]
    notices: list[str]


class WindowsBootstrapReportDict(TypedDict):
    contract_version: str
    status: Literal["error"]
    can_start_broker: bool
    checks: list[PreflightCheckDict]
    notices: list[str]


WindowsReportDict = WindowsPreflightReportDict | WindowsBootstrapReportDict


class WindowsPrerequisiteProbes(Protocol):
    """Host probes used before authorization or protected execution."""

    def retry_retained_cleanup(self) -> None: ...

    def trusted_git_executable(self) -> Path | None: ...

    def workspace_filesystem(self, workspace: Path) -> str: ...

    def bfe_is_running(self) -> bool: ...

    def is_elevated(self) -> bool: ...

    def wfp_api_is_available(self) -> bool: ...

    def wfp_policy_write_access(self) -> bool: ...

    def runtime_modules_importable(self) -> bool: ...


def retry_windows_preflight_cleanup() -> None:
    """Retry every retained Windows owner before any new prerequisite probe."""
    first_error: RetainedOwnerCleanupError | None = None
    try:
        retry_retained_owners()
    except RetainedOwnerCleanupError as error:
        first_error = error
    from goodjob.platform.sandbox_windows import _retry_retained_wfp_engines

    try:
        _retry_retained_wfp_engines()
    except RetainedOwnerCleanupError as error:
        if first_error is None:
            first_error = error
    if first_error is not None:
        raise first_error


class SERVICE_STATUS_PROCESS(ctypes.Structure):
    _fields_ = [
        ("dwServiceType", ctypes.c_uint32),
        ("dwCurrentState", ctypes.c_uint32),
        ("dwControlsAccepted", ctypes.c_uint32),
        ("dwWin32ExitCode", ctypes.c_uint32),
        ("dwServiceSpecificExitCode", ctypes.c_uint32),
        ("dwCheckPoint", ctypes.c_uint32),
        ("dwWaitHint", ctypes.c_uint32),
        ("dwProcessId", ctypes.c_uint32),
        ("dwServiceFlags", ctypes.c_uint32),
    ]


class SystemWindowsPrerequisiteProbes:
    """Read native Windows capability state without installing or enabling anything."""

    def retry_retained_cleanup(self) -> None:
        retry_windows_preflight_cleanup()

    def trusted_git_executable(self) -> Path | None:
        retry_retained_owners()
        from goodjob.platform.sandbox_windows import find_trusted_windows_git_executable

        executable = find_trusted_windows_git_executable()
        return Path(executable) if executable is not None else None

    def workspace_filesystem(self, workspace: Path) -> str:
        retry_retained_owners()
        kernel32 = load_windows_dll("kernel32.dll")
        kernel32.GetVolumePathNameW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_uint32,
        ]
        kernel32.GetVolumePathNameW.restype = ctypes.c_int
        kernel32.GetVolumeInformationW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_wchar_p,
            ctypes.c_uint32,
        ]
        kernel32.GetVolumeInformationW.restype = ctypes.c_int
        volume = ctypes.create_unicode_buffer(32768)
        if not kernel32.GetVolumePathNameW(str(workspace), volume, len(volume)):
            raise OSError("unable to resolve the Windows workspace volume")
        filesystem = ctypes.create_unicode_buffer(32)
        if not kernel32.GetVolumeInformationW(
            volume.value,
            None,
            0,
            None,
            None,
            None,
            filesystem,
            len(filesystem),
        ):
            raise OSError("unable to identify the Windows workspace filesystem")
        return filesystem.value

    def bfe_is_running(self) -> bool:
        retry_retained_owners()
        advapi32 = load_windows_dll("advapi32.dll")
        advapi32.OpenSCManagerW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_uint32,
        ]
        advapi32.OpenSCManagerW.restype = ctypes.c_void_p
        advapi32.OpenServiceW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32]
        advapi32.OpenServiceW.restype = ctypes.c_void_p
        advapi32.QueryServiceStatusEx.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        advapi32.QueryServiceStatusEx.restype = ctypes.c_int
        advapi32.CloseServiceHandle.argtypes = [ctypes.c_void_p]
        advapi32.CloseServiceHandle.restype = ctypes.c_int
        manager_value = int(advapi32.OpenSCManagerW(None, None, 0x0001) or 0)
        if manager_value == 0:
            return False

        def close_service_handle(value: int) -> None:
            if not advapi32.CloseServiceHandle(ctypes.c_void_p(value)):
                raise OSError(last_error(), "CloseServiceHandle")

        manager = OwnedHandle(manager_value, closer=close_service_handle)
        service: OwnedHandle | None = None
        try:
            service_value = int(advapi32.OpenServiceW(manager.value, "BFE", 0x0004) or 0)
            if service_value == 0:
                running = False
            else:
                service = OwnedHandle(service_value, closer=close_service_handle)
                status = SERVICE_STATUS_PROCESS()
                needed = ctypes.c_uint32()
                ok = advapi32.QueryServiceStatusEx(
                    service.value,
                    0,
                    ctypes.cast(ctypes.byref(status), ctypes.POINTER(ctypes.c_ubyte)),
                    ctypes.sizeof(status),
                    ctypes.byref(needed),
                )
                running = bool(ok) and int(status.dwCurrentState) == 4
        except BaseException as primary_error:
            close_owned_resources((service, manager), cause=primary_error)
            raise
        close_owned_resources((service, manager))
        return running

    def is_elevated(self) -> bool:
        retry_retained_owners()
        shell32 = load_windows_dll("shell32.dll")
        shell32.IsUserAnAdmin.argtypes = []
        shell32.IsUserAnAdmin.restype = ctypes.c_int
        return bool(shell32.IsUserAnAdmin())

    def wfp_api_is_available(self) -> bool:
        retry_retained_owners()
        try:
            api = load_windows_dll("fwpuclnt.dll")
        except OSError:
            return False
        return all(
            hasattr(api, name) for name in ("FwpmEngineOpen0", "FwpmEngineClose0", "FwpmFilterAdd0")
        )

    def wfp_policy_write_access(self) -> bool:
        retry_retained_owners()
        from goodjob.platform.sandbox_windows import probe_wfp_policy_write_access

        probe_wfp_policy_write_access()
        return True

    def runtime_modules_importable(self) -> bool:
        retry_retained_owners()
        try:
            importlib.import_module("goodjob.cli")
            importlib.import_module("goodjob.platform.launcher_windows")
        except (ImportError, OSError):
            return False
        return True


@dataclass(frozen=True)
class PreflightRemediation:
    action: str
    purpose: str
    source_url: str | None = None
    requires_explicit_consent: bool = False

    def as_dict(self) -> PreflightRemediationDict:
        result = PreflightRemediationDict(
            action=self.action,
            purpose=self.purpose,
            requires_explicit_consent=self.requires_explicit_consent,
        )
        if self.source_url is not None:
            result["source_url"] = self.source_url
        return result


def _registered_remediation(check_id: str, action: str) -> PreflightRemediation:
    contract = remediation_for("windows", check_id, action)
    return PreflightRemediation(
        action=contract.action,
        purpose=contract.purpose,
        source_url=contract.source_url,
        requires_explicit_consent=contract.requires_explicit_consent,
    )


@dataclass(frozen=True)
class PreflightCheck:
    id: str
    status: CheckStatus
    message: str
    code: PreflightCode | None = None
    remediation: PreflightRemediation | None = None

    def as_dict(self) -> PreflightCheckDict:
        result = PreflightCheckDict(id=self.id, status=self.status, message=self.message)
        if self.code is not None:
            result["code"] = self.code
        if self.remediation is not None:
            result["remediation"] = self.remediation.as_dict()
        return result


@dataclass(frozen=True)
class WindowsPreflightReport:
    checks: tuple[PreflightCheck, ...]

    @property
    def can_start_broker(self) -> bool:
        return all(check.status == "passed" for check in self.checks)

    def as_dict(self) -> WindowsPreflightReportDict:
        can_start = self.can_start_broker
        return WindowsPreflightReportDict(
            contract_version="windows-prerequisite-preflight-v1",
            status="ok" if can_start else "error",
            can_start_broker=can_start,
            checks=[check.as_dict() for check in self.checks],
            notices=[WINDOWS_GIT_FS_NOTICE],
        )


@dataclass(frozen=True)
class WindowsBootstrapReport:
    check: PreflightCheck

    def as_dict(self) -> WindowsBootstrapReportDict:
        return WindowsBootstrapReportDict(
            contract_version="windows-bootstrap-report-v1",
            status="error",
            can_start_broker=False,
            checks=[self.check.as_dict()],
            notices=[],
        )


def missing_python_runtime_report() -> WindowsBootstrapReport:
    """Return the report used when no child can run the full Windows probes."""
    return WindowsBootstrapReport(
        _failed(
            "python_runtime",
            MissingDependencyError(
                "Python 3.12 or newer is unavailable; uv is optional and no usable fallback exists"
            ),
            _registered_remediation("python_runtime", "request_installation"),
        )
    )


def _passed(check_id: str, message: str) -> PreflightCheck:
    return PreflightCheck(id=check_id, status="passed", message=message)


def _failed(
    check_id: str,
    error: GoodJobError,
    remediation: PreflightRemediation,
) -> PreflightCheck:
    return PreflightCheck(
        id=check_id,
        status="failed",
        code=cast(PreflightCode, error.code),
        message=str(error),
        remediation=remediation,
    )


def preflight_protocol_failure_report(message: str) -> WindowsBootstrapReport:
    """Return a stable fail-closed report for a broken preflight command boundary."""
    return WindowsBootstrapReport(
        _failed(
            "windows_preflight",
            UnsupportedCapabilityError(message),
            _registered_remediation("windows_preflight", "repair_skill_or_use_wsl2"),
        )
    )


def _valid_failed_check(check: dict[object, object], allowed_ids: frozenset[str]) -> bool:
    check_id = check.get("id")
    remediation = check.get("remediation")
    if (
        set(check) != _FAILED_CHECK_FIELDS
        or not isinstance(check_id, str)
        or check_id not in allowed_ids
        or check.get("status") != "failed"
        or check.get("code")
        not in ("missing_dependency", "permission_required", "unsupported_capability")
        or not isinstance(check.get("message"), str)
        or not check["message"]
        or not isinstance(remediation, dict)
        or not isinstance(remediation.get("action"), str)
        or not remediation["action"]
        or not isinstance(remediation.get("purpose"), str)
        or not remediation["purpose"]
        or not isinstance(remediation.get("requires_explicit_consent"), bool)
        or not _REMEDIATION_REQUIRED_FIELDS
        <= set(remediation)
        <= (_REMEDIATION_REQUIRED_FIELDS | _REMEDIATION_OPTIONAL_FIELDS)
    ):
        return False
    if "source_url" in remediation:
        source_url = remediation["source_url"]
        if not isinstance(source_url, str) or not source_url:
            return False
    return True


def parse_windows_bootstrap_report(raw: object) -> WindowsBootstrapReportDict | None:
    """Validate a launcher-level failure produced before a full preflight is available."""
    if not isinstance(raw, dict):
        return None
    checks = raw.get("checks")
    if (
        set(raw) != {"contract_version", "status", "can_start_broker", "checks", "notices"}
        or raw.get("contract_version") != "windows-bootstrap-report-v1"
        or raw.get("status") != "error"
        or raw.get("can_start_broker") is not False
        or not isinstance(checks, list)
        or len(checks) != 1
        or raw.get("notices") != []
        or not isinstance(checks[0], dict)
        or not _valid_failed_check(checks[0], frozenset({"python_runtime", "windows_preflight"}))
    ):
        return None
    return cast(WindowsBootstrapReportDict, raw)


def parse_windows_preflight_report(raw: object) -> WindowsPreflightReportDict | None:
    """Validate the complete subprocess report before the launcher can trust it."""
    if not isinstance(raw, dict):
        return None
    can_start = raw.get("can_start_broker")
    status = raw.get("status")
    checks = raw.get("checks")
    notices = raw.get("notices")
    if (
        set(raw) != {"contract_version", "status", "can_start_broker", "checks", "notices"}
        or raw.get("contract_version") != "windows-prerequisite-preflight-v1"
        or not isinstance(can_start, bool)
        or status not in ("ok", "error")
        or not isinstance(checks, list)
        or notices != [WINDOWS_GIT_FS_NOTICE]
    ):
        return None
    seen_ids: set[str] = set()
    all_passed = True
    for check in checks:
        if not isinstance(check, dict):
            return None
        check_id = check.get("id")
        check_status = check.get("status")
        message = check.get("message")
        if (
            not isinstance(check_id, str)
            or check_id not in WINDOWS_PREFLIGHT_REQUIRED_CHECK_IDS
            or check_id in seen_ids
            or check_status not in ("passed", "failed")
            or not isinstance(message, str)
            or not message
        ):
            return None
        seen_ids.add(check_id)
        if check_status == "passed":
            if set(check) != _PASSED_CHECK_FIELDS:
                return None
        else:
            all_passed = False
            if not _valid_failed_check(check, WINDOWS_PREFLIGHT_REQUIRED_CHECK_IDS):
                return None
    if seen_ids != WINDOWS_PREFLIGHT_REQUIRED_CHECK_IDS:
        return None
    if can_start != all_passed or status != ("ok" if all_passed else "error"):
        return None
    return cast(WindowsPreflightReportDict, raw)


def _retained_owner_cleanup_report(
    python_check: PreflightCheck, *, release_enabled: bool
) -> WindowsPreflightReport:
    cleanup_error = UnsupportedCapabilityError(
        "previous Windows owner cleanup remains incomplete; no new system probe was run"
    )
    cleanup_checks = tuple(
        _failed(
            check_id,
            cleanup_error,
            _registered_remediation(check_id, "retry_cleanup_or_repair_runtime_or_use_wsl2"),
        )
        for check_id in (
            "runtime_installation",
            "trusted_git",
            "workspace_filesystem",
            "bfe_service",
            "administrator",
            "wfp_api",
            "wfp_permission",
        )
    )
    release_check = (
        _passed("native_windows_release", "native Windows release gate is enabled")
        if release_enabled
        else _failed(
            "native_windows_release",
            UnsupportedCapabilityError(
                "native Windows remains unsupported until IMP-31A-G pass on one release candidate"
            ),
            _registered_remediation("native_windows_release", "use_wsl2"),
        )
    )
    return WindowsPreflightReport((python_check, *cleanup_checks, release_check))


def evaluate_windows_preflight(
    *,
    workspace: Path,
    runtime_dir: Path,
    python_version: tuple[int, int, int] | None,
    launcher_kind: str | None,
    uv_available: bool,
    release_enabled: bool,
    probes: WindowsPrerequisiteProbes,
) -> WindowsPreflightReport:
    """Evaluate all Windows prerequisites without starting the broker."""
    checks: list[PreflightCheck] = []
    if python_version is None or python_version < (3, 12, 0):
        checks.append(
            _failed(
                "python_runtime",
                MissingDependencyError(
                    "Python 3.12 or newer is unavailable; uv is optional and no usable fallback "
                    "exists"
                ),
                _registered_remediation("python_runtime", "request_installation"),
            )
        )
    else:
        method = launcher_kind or ("uv" if uv_available else "direct_python")
        checks.append(
            _passed(
                "python_runtime",
                f"Python {'.'.join(str(part) for part in python_version)} via {method}",
            )
        )
    python_check = checks[0]

    try:
        probes.retry_retained_cleanup()
    except RetainedOwnerCleanupError:
        return _retained_owner_cleanup_report(python_check, release_enabled=release_enabled)

    runtime_files = (
        runtime_dir / "scripts" / "broker_bootstrap.py",
        runtime_dir / "scripts" / "launch_broker.py",
        runtime_dir / "scripts" / "session.py",
        runtime_dir / "scripts" / "windows_preflight.py",
        runtime_dir / "src" / "goodjob" / "__init__.py",
    )
    try:
        runtime_importable = probes.runtime_modules_importable()
    except RetainedOwnerCleanupError:
        return _retained_owner_cleanup_report(python_check, release_enabled=release_enabled)
    except (ImportError, OSError):
        runtime_importable = False
    if all(path.is_file() for path in runtime_files) and runtime_importable:
        checks.append(_passed("runtime_installation", "GoodJob runtime files are complete"))
    else:
        checks.append(
            _failed(
                "runtime_installation",
                MissingDependencyError("the installed GoodJob Skill runtime is incomplete"),
                _registered_remediation("runtime_installation", "request_reinstallation"),
            )
        )

    try:
        git_executable = probes.trusted_git_executable()
    except RetainedOwnerCleanupError:
        return _retained_owner_cleanup_report(python_check, release_enabled=release_enabled)
    except OSError:
        git_executable = None
    if git_executable is None:
        checks.append(
            _failed(
                "trusted_git",
                MissingDependencyError(
                    r"Git for Windows is missing at a trusted mingw64\bin\git.exe path"
                ),
                _registered_remediation("trusted_git", "request_installation"),
            )
        )
    else:
        checks.append(_passed("trusted_git", "trusted Git for Windows entry point is available"))

    try:
        filesystem = probes.workspace_filesystem(workspace)
    except RetainedOwnerCleanupError:
        return _retained_owner_cleanup_report(python_check, release_enabled=release_enabled)
    except OSError:
        filesystem = "unavailable"
    if filesystem.upper() != "NTFS":
        checks.append(
            _failed(
                "workspace_filesystem",
                UnsupportedCapabilityError(
                    f"native Windows scanning requires NTFS; the workspace is on {filesystem}"
                ),
                _registered_remediation("workspace_filesystem", "use_ntfs_or_wsl2"),
            )
        )
    else:
        checks.append(_passed("workspace_filesystem", "workspace is on NTFS"))

    try:
        bfe_running = probes.bfe_is_running()
    except RetainedOwnerCleanupError:
        return _retained_owner_cleanup_report(python_check, release_enabled=release_enabled)
    except OSError:
        bfe_running = False
    if bfe_running:
        checks.append(_passed("bfe_service", "Base Filtering Engine is running"))
    else:
        checks.append(
            _failed(
                "bfe_service",
                UnsupportedCapabilityError("Base Filtering Engine is not running"),
                _registered_remediation("bfe_service", "request_service_enablement"),
            )
        )

    try:
        elevated = probes.is_elevated()
    except RetainedOwnerCleanupError:
        return _retained_owner_cleanup_report(python_check, release_enabled=release_enabled)
    except OSError:
        elevated = False
    if elevated:
        checks.append(_passed("administrator", "the current token is elevated"))
    else:
        checks.append(
            _failed(
                "administrator",
                PermissionRequiredError(
                    "an elevated administrator token is required to install WFP filters"
                ),
                _registered_remediation("administrator", "request_elevation"),
            )
        )

    try:
        wfp_available = probes.wfp_api_is_available()
    except RetainedOwnerCleanupError:
        return _retained_owner_cleanup_report(python_check, release_enabled=release_enabled)
    except OSError:
        wfp_available = False
    if wfp_available:
        checks.append(_passed("wfp_api", "Windows Filtering Platform API is available"))
    else:
        checks.append(
            _failed(
                "wfp_api",
                UnsupportedCapabilityError("Windows Filtering Platform API is unavailable"),
                _registered_remediation("wfp_api", "repair_windows_or_use_wsl2"),
            )
        )

    if elevated and bfe_running and wfp_available:
        try:
            wfp_write_access = probes.wfp_policy_write_access()
            wfp_error: OSError | None = None
        except RetainedOwnerCleanupError:
            return _retained_owner_cleanup_report(python_check, release_enabled=release_enabled)
        except OSError as error:
            wfp_write_access = False
            wfp_error = error
        if wfp_write_access:
            checks.append(
                _passed(
                    "wfp_permission",
                    "WFP dynamic policy write access is available; each protected launch still "
                    "installs and reads back its filters before resuming the child",
                )
            )
        elif wfp_error is not None and not isinstance(wfp_error, PermissionError):
            checks.append(
                _failed(
                    "wfp_permission",
                    UnsupportedCapabilityError(
                        "the dynamic WFP policy store could not be verified"
                    ),
                    _registered_remediation("wfp_permission", "repair_windows_or_use_wsl2"),
                )
            )
        else:
            checks.append(
                _failed(
                    "wfp_permission",
                    PermissionRequiredError(
                        "the current token could not create a dynamic WFP policy"
                    ),
                    _registered_remediation("wfp_permission", "request_elevation"),
                )
            )
    elif not elevated:
        checks.append(
            _failed(
                "wfp_permission",
                PermissionRequiredError("WFP filter installation requires an elevated token"),
                _registered_remediation("wfp_permission", "request_elevation"),
            )
        )
    else:
        checks.append(
            _failed(
                "wfp_permission",
                UnsupportedCapabilityError(
                    "WFP filter permission cannot be established while BFE or the WFP API "
                    "is unavailable"
                ),
                _registered_remediation("wfp_permission", "repair_windows_or_use_wsl2"),
            )
        )

    if release_enabled:
        checks.append(_passed("native_windows_release", "native Windows release gate is enabled"))
    else:
        checks.append(
            _failed(
                "native_windows_release",
                UnsupportedCapabilityError(
                    "native Windows remains unsupported until IMP-31A-G pass on one release "
                    "candidate"
                ),
                _registered_remediation("native_windows_release", "use_wsl2"),
            )
        )
    return WindowsPreflightReport(tuple(checks))
