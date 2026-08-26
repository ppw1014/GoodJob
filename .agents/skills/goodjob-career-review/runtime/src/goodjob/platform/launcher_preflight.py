"""Versioned, platform-neutral launcher preflight reports."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, TypedDict, cast

if TYPE_CHECKING:
    from goodjob.platform.preflight_windows import WindowsReportDict
    from goodjob.platform.runtime_bootstrap import PythonRuntime

PreflightCode = Literal["missing_dependency", "permission_required", "unsupported_capability"]
CheckStatus = Literal["passed", "failed"]
PlatformName = Literal["macos", "linux", "windows", "unsupported"]
LauncherKind = Literal["uv", "direct_python", "windows_py_launcher", "unavailable"]

TOP_LEVEL_FIELDS = frozenset(
    {
        "contract_version",
        "status",
        "can_start_broker",
        "platform",
        "launcher_kind",
        "checks",
        "notices",
    }
)
PASSED_CHECK_FIELDS = frozenset({"id", "status", "message"})
FAILED_CHECK_FIELDS = frozenset({"id", "status", "message", "code", "remediation"})
REMEDIATION_REQUIRED_FIELDS = frozenset({"action", "purpose", "requires_explicit_consent"})
REMEDIATION_OPTIONAL_FIELDS = frozenset({"source_url"})
WINDOWS_GIT_FS_NOTICE = (
    "Native Windows Git subprocesses do not have filesystem read isolation; "
    "use WSL2 when complete Git filesystem isolation is required."
)
BROKER_READY_MARKER_PREFIX = "goodjob-launcher-broker-ready-v1"


def broker_ready_marker(readiness_token: str) -> str:
    return f"{BROKER_READY_MARKER_PREFIX}:{readiness_token}"


class RemediationDict(TypedDict, total=False):
    action: str
    purpose: str
    requires_explicit_consent: bool
    source_url: str


class CheckDict(TypedDict, total=False):
    id: str
    status: CheckStatus
    message: str
    code: PreflightCode
    remediation: RemediationDict


class LauncherPreflightReportDict(TypedDict):
    contract_version: str
    status: Literal["ok", "error"]
    can_start_broker: bool
    platform: PlatformName
    launcher_kind: LauncherKind
    checks: list[CheckDict]
    notices: list[str]


@dataclass(frozen=True)
class LauncherPreflightDecision:
    start_broker: bool
    failure_codes: tuple[PreflightCode, ...] = ()
    facts: tuple[LauncherPreflightFact, ...] = ()
    runtime_contract_gap: bool = False


@dataclass(frozen=True)
class LauncherPreflightFact:
    code: PreflightCode
    purpose: str
    requires_explicit_consent: bool
    source_url: str | None = None


class LauncherPreflightHost(Protocol):
    def start_broker(self) -> None: ...

    def explain(self, facts: tuple[LauncherPreflightFact, ...]) -> None: ...

    def request_explicit_consent(self, facts: tuple[LauncherPreflightFact, ...]) -> None: ...

    def report_runtime_contract_gap(self) -> None: ...


@dataclass(frozen=True)
class RemediationContract:
    code: PreflightCode
    action: str
    purpose: str
    requires_explicit_consent: bool
    source_url: str | None = None

    def as_dict(self) -> RemediationDict:
        result = RemediationDict(
            action=self.action,
            purpose=self.purpose,
            requires_explicit_consent=self.requires_explicit_consent,
        )
        if self.source_url is not None:
            result["source_url"] = self.source_url
        return result


@dataclass(frozen=True)
class CheckContract:
    id: str
    remediations: tuple[RemediationContract, ...] = ()


@dataclass(frozen=True)
class CheckLauncherRule:
    check_id: str
    passed_launchers: frozenset[LauncherKind]
    failed_launchers: frozenset[LauncherKind]

    def accepts(self, status: CheckStatus, launcher_kind: LauncherKind) -> bool:
        allowed = self.passed_launchers if status == "passed" else self.failed_launchers
        return launcher_kind in allowed


@dataclass(frozen=True)
class CheckPrerequisiteRule:
    check_id: str
    passed_requires: tuple[str, ...]

    def accepts(self, statuses: dict[str, CheckStatus]) -> bool:
        return statuses[self.check_id] != "passed" or all(
            statuses[check_id] == "passed" for check_id in self.passed_requires
        )


@dataclass(frozen=True)
class ReportShape:
    check_ids: frozenset[str]
    launcher_kinds: frozenset[LauncherKind]
    notices: tuple[str, ...] = ()
    ready_allowed: bool = False
    check_launcher_rules: tuple[CheckLauncherRule, ...] = ()
    check_prerequisite_rules: tuple[CheckPrerequisiteRule, ...] = ()
    required_statuses: tuple[tuple[str, CheckStatus], ...] = ()

    def accepts(
        self,
        statuses: dict[str, CheckStatus],
        launcher_kind: LauncherKind,
    ) -> bool:
        return (
            launcher_kind in self.launcher_kinds
            and all(
                rule.accepts(statuses[rule.check_id], launcher_kind)
                for rule in self.check_launcher_rules
            )
            and all(rule.accepts(statuses) for rule in self.check_prerequisite_rules)
            and all(statuses[check_id] == status for check_id, status in self.required_statuses)
        )


@dataclass(frozen=True)
class PlatformContract:
    checks: tuple[CheckContract, ...]
    launcher_kinds: frozenset[LauncherKind]
    report_shapes: tuple[ReportShape, ...]

    @property
    def checks_by_id(self) -> dict[str, CheckContract]:
        return {check.id: check for check in self.checks}

    def shape_for(self, check_ids: frozenset[str]) -> ReportShape | None:
        for shape in self.report_shapes:
            if check_ids == shape.check_ids:
                return shape
        return None


_PYTHON_INSTALL = RemediationContract(
    code="missing_dependency",
    action="request_installation",
    purpose="provide an isolated Python 3.12 or newer runtime for the GoodJob broker",
    requires_explicit_consent=True,
    source_url="https://docs.astral.sh/uv/getting-started/installation/",
)
_WINDOWS_PYTHON_INSTALL = RemediationContract(
    code="missing_dependency",
    action="request_installation",
    purpose="run the isolated GoodJob broker with Python 3.12 or newer",
    requires_explicit_consent=True,
    source_url="https://www.python.org/downloads/windows/",
)
_WINDOWS_CLEANUP = RemediationContract(
    code="unsupported_capability",
    action="retry_cleanup_or_repair_runtime_or_use_wsl2",
    purpose="finish retained Windows resource cleanup before any new system probe",
    requires_explicit_consent=False,
)
_WINDOWS_REPAIR_NETWORK = RemediationContract(
    code="unsupported_capability",
    action="repair_windows_or_use_wsl2",
    purpose="preserve mandatory Git network isolation",
    requires_explicit_consent=False,
)
_WINDOWS_ELEVATION = RemediationContract(
    code="permission_required",
    action="request_elevation",
    purpose="install and verify request-scoped WFP filters",
    requires_explicit_consent=True,
)
WINDOWS_PREFLIGHT_REQUIRED_CHECK_IDS = frozenset(
    {
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
)
_LAUNCHER_PROTOCOL = RemediationContract(
    code="unsupported_capability",
    action="repair_launcher_runtime",
    purpose="restore the trusted launcher preflight contract",
    requires_explicit_consent=False,
)
_BROKER_START = RemediationContract(
    code="unsupported_capability",
    action="repair_broker_runtime",
    purpose="start the installed GoodJob session broker",
    requires_explicit_consent=False,
)
_POSIX_CHECKS = frozenset({"python_runtime", "git_sandbox"})
_POSIX_LAUNCHERS: frozenset[LauncherKind] = frozenset({"uv", "direct_python", "unavailable"})
_WINDOWS_LAUNCHERS: frozenset[LauncherKind] = frozenset(
    {"uv", "direct_python", "windows_py_launcher", "unavailable"}
)
_AVAILABLE_POSIX_LAUNCHERS = _POSIX_LAUNCHERS - {"unavailable"}
_AVAILABLE_WINDOWS_LAUNCHERS = _WINDOWS_LAUNCHERS - {"unavailable"}
_UNAVAILABLE_LAUNCHER: frozenset[LauncherKind] = frozenset({"unavailable"})
_PASSED: CheckStatus = "passed"
_FAILED: CheckStatus = "failed"
_POSIX_RUNTIME_RULE = CheckLauncherRule(
    "python_runtime", _AVAILABLE_POSIX_LAUNCHERS, _UNAVAILABLE_LAUNCHER
)
_WINDOWS_RUNTIME_RULE = CheckLauncherRule(
    "python_runtime", _AVAILABLE_WINDOWS_LAUNCHERS, _UNAVAILABLE_LAUNCHER
)
_WINDOWS_WFP_PERMISSION_RULE = CheckPrerequisiteRule(
    "wfp_permission",
    ("administrator", "bfe_service", "wfp_api"),
)
_POSIX_READY_STATUSES: tuple[tuple[str, CheckStatus], ...] = (
    ("python_runtime", _PASSED),
    ("git_sandbox", _PASSED),
)
_WINDOWS_READY_STATUSES: tuple[tuple[str, CheckStatus], ...] = tuple(
    (check_id, _PASSED) for check_id in WINDOWS_PREFLIGHT_REQUIRED_CHECK_IDS
)
_POSIX_REPORT_SHAPES = (
    ReportShape(
        _POSIX_CHECKS,
        _POSIX_LAUNCHERS,
        ready_allowed=True,
        check_launcher_rules=(_POSIX_RUNTIME_RULE,),
    ),
    ReportShape(frozenset({"launcher_protocol"}), _POSIX_LAUNCHERS),
    ReportShape(
        _POSIX_CHECKS | {"broker_start"},
        _AVAILABLE_POSIX_LAUNCHERS,
        check_launcher_rules=(_POSIX_RUNTIME_RULE,),
        required_statuses=(*_POSIX_READY_STATUSES, ("broker_start", _FAILED)),
    ),
)

LAUNCHER_PREFLIGHT_REGISTRY: dict[PlatformName, PlatformContract] = {
    "macos": PlatformContract(
        checks=(
            CheckContract("python_runtime", (_PYTHON_INSTALL,)),
            CheckContract(
                "git_sandbox",
                (
                    RemediationContract(
                        code="unsupported_capability",
                        action="repair_macos_runtime",
                        purpose="establish the mandatory macOS Git sandbox",
                        requires_explicit_consent=False,
                    ),
                ),
            ),
            CheckContract("launcher_protocol", (_LAUNCHER_PROTOCOL,)),
            CheckContract("broker_start", (_BROKER_START,)),
        ),
        launcher_kinds=_POSIX_LAUNCHERS,
        report_shapes=_POSIX_REPORT_SHAPES,
    ),
    "linux": PlatformContract(
        checks=(
            CheckContract("python_runtime", (_PYTHON_INSTALL,)),
            CheckContract(
                "git_sandbox",
                (
                    RemediationContract(
                        code="missing_dependency",
                        action="request_installation",
                        purpose="establish the mandatory Linux Git sandbox with bubblewrap",
                        requires_explicit_consent=True,
                    ),
                    RemediationContract(
                        code="unsupported_capability",
                        action="repair_linux_runtime_or_use_wsl2",
                        purpose="allow bubblewrap to establish the mandatory Linux Git sandbox",
                        requires_explicit_consent=False,
                    ),
                ),
            ),
            CheckContract("launcher_protocol", (_LAUNCHER_PROTOCOL,)),
            CheckContract("broker_start", (_BROKER_START,)),
        ),
        launcher_kinds=_POSIX_LAUNCHERS,
        report_shapes=_POSIX_REPORT_SHAPES,
    ),
    "windows": PlatformContract(
        checks=(
            CheckContract("python_runtime", (_WINDOWS_PYTHON_INSTALL,)),
            CheckContract(
                "windows_preflight",
                (
                    RemediationContract(
                        code="unsupported_capability",
                        action="repair_skill_or_use_wsl2",
                        purpose="complete every mandatory prerequisite before protected execution",
                        requires_explicit_consent=False,
                    ),
                ),
            ),
            CheckContract(
                "runtime_installation",
                (
                    RemediationContract(
                        code="missing_dependency",
                        action="request_reinstallation",
                        purpose="restore the trusted GoodJob runtime files",
                        requires_explicit_consent=True,
                    ),
                    _WINDOWS_CLEANUP,
                ),
            ),
            CheckContract(
                "trusted_git",
                (
                    RemediationContract(
                        code="missing_dependency",
                        action="request_installation",
                        purpose="read local Git metadata through the fixed trusted entry point",
                        requires_explicit_consent=True,
                        source_url="https://git-scm.com/download/win",
                    ),
                    _WINDOWS_CLEANUP,
                ),
            ),
            CheckContract(
                "workspace_filesystem",
                (
                    RemediationContract(
                        code="unsupported_capability",
                        action="use_ntfs_or_wsl2",
                        purpose="preserve handle-relative filesystem authorization",
                        requires_explicit_consent=False,
                    ),
                    _WINDOWS_CLEANUP,
                ),
            ),
            CheckContract(
                "bfe_service",
                (
                    RemediationContract(
                        code="unsupported_capability",
                        action="request_service_enablement",
                        purpose="establish the mandatory WFP network boundary",
                        requires_explicit_consent=True,
                    ),
                    _WINDOWS_CLEANUP,
                ),
            ),
            CheckContract("administrator", (_WINDOWS_ELEVATION, _WINDOWS_CLEANUP)),
            CheckContract("wfp_api", (_WINDOWS_REPAIR_NETWORK, _WINDOWS_CLEANUP)),
            CheckContract(
                "wfp_permission",
                (_WINDOWS_ELEVATION, _WINDOWS_REPAIR_NETWORK, _WINDOWS_CLEANUP),
            ),
            CheckContract(
                "native_windows_release",
                (
                    RemediationContract(
                        code="unsupported_capability",
                        action="use_wsl2",
                        purpose="keep all protected execution fail-closed until release acceptance",
                        requires_explicit_consent=False,
                    ),
                ),
            ),
            CheckContract("launcher_protocol", (_LAUNCHER_PROTOCOL,)),
            CheckContract("broker_start", (_BROKER_START,)),
        ),
        launcher_kinds=_WINDOWS_LAUNCHERS,
        report_shapes=(
            ReportShape(
                frozenset({"python_runtime"}),
                _WINDOWS_LAUNCHERS,
                check_launcher_rules=(_WINDOWS_RUNTIME_RULE,),
            ),
            ReportShape(frozenset({"windows_preflight"}), _AVAILABLE_WINDOWS_LAUNCHERS),
            ReportShape(frozenset({"launcher_protocol"}), _WINDOWS_LAUNCHERS),
            ReportShape(
                WINDOWS_PREFLIGHT_REQUIRED_CHECK_IDS,
                _WINDOWS_LAUNCHERS,
                notices=(WINDOWS_GIT_FS_NOTICE,),
                ready_allowed=True,
                check_launcher_rules=(_WINDOWS_RUNTIME_RULE,),
                check_prerequisite_rules=(_WINDOWS_WFP_PERMISSION_RULE,),
            ),
            ReportShape(
                WINDOWS_PREFLIGHT_REQUIRED_CHECK_IDS | {"broker_start"},
                _AVAILABLE_WINDOWS_LAUNCHERS,
                notices=(WINDOWS_GIT_FS_NOTICE,),
                check_launcher_rules=(_WINDOWS_RUNTIME_RULE,),
                check_prerequisite_rules=(_WINDOWS_WFP_PERMISSION_RULE,),
                required_statuses=(*_WINDOWS_READY_STATUSES, ("broker_start", _FAILED)),
            ),
        ),
    ),
    "unsupported": PlatformContract(
        checks=(
            CheckContract(
                "platform_support",
                (
                    RemediationContract(
                        code="unsupported_capability",
                        action="use_supported_platform",
                        purpose="run GoodJob on macOS, Linux, WSL2, or native Windows",
                        requires_explicit_consent=False,
                    ),
                ),
            ),
        ),
        launcher_kinds=_UNAVAILABLE_LAUNCHER,
        report_shapes=(
            ReportShape(
                frozenset({"platform_support"}),
                _UNAVAILABLE_LAUNCHER,
                required_statuses=(("platform_support", _FAILED),),
            ),
        ),
    ),
}


def remediation_for(
    platform: PlatformName,
    check_id: str,
    action: str,
) -> RemediationContract:
    """Return one remediation from the registry shared by producers and parser."""
    check = LAUNCHER_PREFLIGHT_REGISTRY[platform].checks_by_id[check_id]
    for remediation in check.remediations:
        if remediation.action == action:
            return remediation
    raise KeyError((platform, check_id, action))


@dataclass(frozen=True)
class PreflightCheck:
    id: str
    status: CheckStatus
    message: str
    code: PreflightCode | None = None
    remediation: RemediationContract | None = None

    def as_dict(self) -> CheckDict:
        result = CheckDict(id=self.id, status=self.status, message=self.message)
        if self.code is not None:
            result["code"] = self.code
        if self.remediation is not None:
            result["remediation"] = self.remediation.as_dict()
        return result


def passed_check(check_id: str, message: str) -> PreflightCheck:
    return PreflightCheck(id=check_id, status="passed", message=message)


def failed_check(check_id: str, message: str, remediation: RemediationContract) -> PreflightCheck:
    return PreflightCheck(
        id=check_id,
        status="failed",
        message=message,
        code=remediation.code,
        remediation=remediation,
    )


@dataclass(frozen=True)
class LauncherPreflightReport:
    platform: PlatformName
    launcher_kind: LauncherKind
    checks: tuple[PreflightCheck, ...]
    notices: tuple[str, ...] = ()

    def as_dict(self) -> LauncherPreflightReportDict:
        can_start = bool(self.checks) and all(check.status == "passed" for check in self.checks)
        return LauncherPreflightReportDict(
            contract_version="launcher-preflight-v1",
            status="ok" if can_start else "error",
            can_start_broker=can_start,
            platform=self.platform,
            launcher_kind=self.launcher_kind,
            checks=[check.as_dict() for check in self.checks],
            notices=list(self.notices),
        )


def platform_name_for_system(platform_name: str) -> PlatformName:
    platforms: dict[str, PlatformName] = {
        "darwin": "macos",
        "linux": "linux",
        "win32": "windows",
    }
    return platforms.get(platform_name, "unsupported")


def _launcher_kind_for_platform(platform: PlatformName, raw: object) -> LauncherKind:
    if isinstance(raw, str) and raw in LAUNCHER_PREFLIGHT_REGISTRY[platform].launcher_kinds:
        return raw
    return "unavailable"


def _sandbox_is_usable(platform: PlatformName) -> bool:
    command = (
        [
            "/usr/bin/sandbox-exec",
            "-p",
            "(version 1) (allow default) (deny network*)",
            "/usr/bin/true",
        ]
        if platform == "macos"
        else [
            "/usr/bin/bwrap",
            "--die-with-parent",
            "--unshare-net",
            "--unshare-pid",
            "--ro-bind",
            "/usr",
            "/usr",
            "--ro-bind",
            "/lib",
            "/lib",
            "--ro-bind-try",
            "/lib64",
            "/lib64",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "/usr/bin/true",
        ]
    )
    try:
        result = subprocess.run(command, capture_output=True, check=False, timeout=5.0)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def evaluate_launcher_preflight(
    *,
    platform_name: str,
    runtime: PythonRuntime | None,
    path_is_file: Callable[[Path], bool] = Path.is_file,
    sandbox_is_usable: Callable[[PlatformName], bool] = _sandbox_is_usable,
) -> LauncherPreflightReportDict:
    """Evaluate side-effect-free prerequisites without a legacy Windows report."""
    platform = platform_name_for_system(platform_name)
    launcher_kind = _launcher_kind_for_platform(
        platform,
        runtime.kind if runtime is not None else None,
    )
    if platform == "unsupported":
        return LauncherPreflightReport(
            platform=platform,
            launcher_kind="unavailable",
            checks=(
                failed_check(
                    "platform_support",
                    "this operating system has no supported GoodJob launcher",
                    remediation_for("unsupported", "platform_support", "use_supported_platform"),
                ),
            ),
        ).as_dict()
    if platform == "windows":
        return LauncherPreflightReport(
            platform=platform,
            launcher_kind=launcher_kind,
            checks=(
                failed_check(
                    "windows_preflight",
                    "the native Windows prerequisite report is unavailable",
                    remediation_for("windows", "windows_preflight", "repair_skill_or_use_wsl2"),
                ),
            ),
        ).as_dict()

    checks: list[PreflightCheck] = []
    if runtime is None:
        checks.append(
            failed_check(
                "python_runtime",
                "no isolated Python 3.12 or newer runtime is available",
                remediation_for(platform, "python_runtime", "request_installation"),
            )
        )
    else:
        checks.append(
            passed_check("python_runtime", "an isolated Python 3.12+ runtime is available")
        )

    sandbox_path = Path("/usr/bin/sandbox-exec") if platform == "macos" else Path("/usr/bin/bwrap")
    sandbox_exists = path_is_file(sandbox_path)
    if sandbox_exists and sandbox_is_usable(platform):
        checks.append(
            passed_check(
                "git_sandbox",
                f"the {platform} Git sandbox backend is available",
            )
        )
    else:
        action = (
            "repair_macos_runtime"
            if platform == "macos"
            else "request_installation"
            if not sandbox_exists
            else "repair_linux_runtime_or_use_wsl2"
        )
        checks.append(
            failed_check(
                "git_sandbox",
                f"the mandatory {platform} Git sandbox backend is unavailable",
                remediation_for(platform, "git_sandbox", action),
            )
        )
    return LauncherPreflightReport(
        platform=platform,
        launcher_kind=launcher_kind,
        checks=tuple(checks),
    ).as_dict()


def launcher_report_from_windows(
    report: WindowsReportDict,
    launcher_kind: str,
) -> LauncherPreflightReportDict:
    """Losslessly wrap one already-validated legacy Windows report."""
    kind = _launcher_kind_for_platform("windows", launcher_kind)
    return LauncherPreflightReportDict(
        contract_version="launcher-preflight-v1",
        status=report["status"],
        can_start_broker=report["can_start_broker"],
        platform="windows",
        launcher_kind=kind,
        checks=deepcopy(report["checks"]),
        notices=list(report["notices"]),
    )


def launcher_protocol_failure_report(
    *,
    platform_name: str,
    runtime: PythonRuntime | None,
    message: str,
) -> LauncherPreflightReportDict:
    """Build an accepted v1 failure when the producer boundary is damaged."""
    platform = platform_name_for_system(platform_name)
    if platform == "unsupported":
        return LauncherPreflightReport(
            platform=platform,
            launcher_kind="unavailable",
            checks=(
                failed_check(
                    "platform_support",
                    message,
                    remediation_for("unsupported", "platform_support", "use_supported_platform"),
                ),
            ),
        ).as_dict()
    return LauncherPreflightReport(
        platform=platform,
        launcher_kind=_launcher_kind_for_platform(
            platform,
            runtime.kind if runtime is not None else None,
        ),
        checks=(
            failed_check(
                "launcher_protocol",
                message,
                remediation_for(platform, "launcher_protocol", "repair_launcher_runtime"),
            ),
        ),
    ).as_dict()


def broker_start_failure_report(
    report: LauncherPreflightReportDict,
    message: str,
) -> LauncherPreflightReportDict:
    """Append a trusted broker-start failure to a successful preflight report."""
    platform = report["platform"]
    if platform == "unsupported":
        return report
    failed = deepcopy(report)
    failed["status"] = "error"
    failed["can_start_broker"] = False
    failed["checks"].append(
        failed_check(
            "broker_start",
            message,
            remediation_for(platform, "broker_start", "repair_broker_runtime"),
        ).as_dict()
    )
    return failed


def _valid_remediation(raw: object, contract: CheckContract, code: object) -> bool:
    if not isinstance(raw, dict):
        return False
    fields = set(raw)
    if (
        not REMEDIATION_REQUIRED_FIELDS
        <= fields
        <= (REMEDIATION_REQUIRED_FIELDS | REMEDIATION_OPTIONAL_FIELDS)
        or not isinstance(raw.get("action"), str)
        or not isinstance(raw.get("purpose"), str)
        or not isinstance(raw.get("requires_explicit_consent"), bool)
        or ("source_url" in raw and not isinstance(raw["source_url"], str))
    ):
        return False
    return any(
        code == candidate.code and raw == candidate.as_dict() for candidate in contract.remediations
    )


def parse_launcher_preflight_report(raw: object) -> LauncherPreflightReportDict | None:
    """Accept only a complete, internally consistent launcher-preflight-v1 report."""
    if not isinstance(raw, dict) or set(raw) != TOP_LEVEL_FIELDS:
        return None
    platform = raw.get("platform")
    launcher_kind = raw.get("launcher_kind")
    if not isinstance(platform, str) or platform not in LAUNCHER_PREFLIGHT_REGISTRY:
        return None
    contract = LAUNCHER_PREFLIGHT_REGISTRY[platform]
    if not isinstance(launcher_kind, str) or launcher_kind not in contract.launcher_kinds:
        return None
    checks = raw.get("checks")
    notices = raw.get("notices")
    if (
        raw.get("contract_version") != "launcher-preflight-v1"
        or raw.get("status") not in ("ok", "error")
        or not isinstance(raw.get("can_start_broker"), bool)
        or not isinstance(checks, list)
        or not checks
        or not isinstance(notices, list)
        or any(not isinstance(notice, str) or not notice for notice in notices)
        or len(set(notices)) != len(notices)
    ):
        return None

    seen_ids: set[str] = set()
    ordered_ids: list[str] = []
    statuses: dict[str, CheckStatus] = {}
    all_passed = True
    for check in checks:
        if not isinstance(check, dict):
            return None
        check_id = check.get("id")
        status = check.get("status")
        message = check.get("message")
        if (
            not isinstance(check_id, str)
            or check_id not in contract.checks_by_id
            or check_id in seen_ids
            or status not in ("passed", "failed")
            or not isinstance(message, str)
            or not message
        ):
            return None
        seen_ids.add(check_id)
        ordered_ids.append(check_id)
        statuses[check_id] = cast(CheckStatus, status)
        if status == "passed":
            if set(check) != PASSED_CHECK_FIELDS:
                return None
        else:
            all_passed = False
            if set(check) != FAILED_CHECK_FIELDS or not _valid_remediation(
                check.get("remediation"), contract.checks_by_id[check_id], check.get("code")
            ):
                return None
    check_set = frozenset(seen_ids)
    shape = contract.shape_for(check_set)
    expected_order = [
        check_contract.id for check_contract in contract.checks if check_contract.id in check_set
    ]
    if (
        shape is None
        or notices != list(shape.notices)
        or ordered_ids != expected_order
        or not shape.accepts(statuses, launcher_kind)
    ):
        return None
    can_start = raw["can_start_broker"]
    if (
        can_start != all_passed
        or (can_start and not shape.ready_allowed)
        or raw["status"] != ("ok" if all_passed else "error")
        or (platform == "unsupported" and can_start)
        or (launcher_kind == "unavailable" and can_start)
    ):
        return None
    return cast(LauncherPreflightReportDict, raw)


def classify_launcher_preflight(
    raw: object,
    *,
    exit_code: int,
    stderr: str,
) -> LauncherPreflightDecision:
    """Classify one launcher result without inventing facts or platform branches."""
    report = parse_launcher_preflight_report(raw)
    if report is None or stderr or exit_code != (0 if report["can_start_broker"] else 2):
        return LauncherPreflightDecision(start_broker=False, runtime_contract_gap=True)
    if report["can_start_broker"]:
        return LauncherPreflightDecision(start_broker=True)
    codes: list[PreflightCode] = []
    facts: list[LauncherPreflightFact] = []
    for check in report["checks"]:
        if check["status"] == "failed":
            code = check["code"]
            if code not in codes:
                codes.append(code)
            remediation = check["remediation"]
            facts.append(
                LauncherPreflightFact(
                    code=code,
                    purpose=remediation["purpose"],
                    requires_explicit_consent=remediation["requires_explicit_consent"],
                    source_url=remediation.get("source_url"),
                )
            )
    return LauncherPreflightDecision(
        start_broker=False,
        failure_codes=tuple(codes),
        facts=tuple(facts),
    )


def apply_launcher_preflight_decision(
    decision: LauncherPreflightDecision,
    host: LauncherPreflightHost,
) -> None:
    """Route a classified result without exposing or executing remediation actions."""
    if decision.runtime_contract_gap:
        host.report_runtime_contract_gap()
        return
    if decision.start_broker:
        host.start_broker()
        return
    host.explain(decision.facts)
    if "unsupported_capability" in decision.failure_codes:
        return
    if any(fact.requires_explicit_consent for fact in decision.facts):
        host.request_explicit_consent(decision.facts)
