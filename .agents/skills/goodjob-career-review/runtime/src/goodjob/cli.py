"""JSON-only command boundary for the GoodJob local runtime."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from goodjob.analysis import AnalysisService
from goodjob.auth import (
    AuthorizationRepository,
    AuthorizationRequest,
    ReceiptKind,
    decode_scope,
    read_capability_from_fd,
    read_capability_from_handle,
    receipt_kind_values,
)
from goodjob.context import ContextInterviewService
from goodjob.db import Database
from goodjob.errors import GoodJobError, InvalidInputError
from goodjob.exporting import ExportService
from goodjob.history import MAX_HISTORY_QUERY_CANDIDATES, HistoryQueryService
from goodjob.paths import DataPaths
from goodjob.platform.detect import require_released_runtime
from goodjob.preparation import (
    MAX_PRIVATE_PAYLOAD_BYTES,
    PreparationService,
    validate_job_input,
)
from goodjob.reporting import ArtifactSnapshotService
from goodjob.review import ReviewService
from goodjob.scanner import (
    ExternalGitGrant,
    WorkspaceScanner,
    inspect_external_git_candidate,
    probe_external_git_relation,
)

MAX_PROTECTED_PAYLOAD_BYTES = MAX_PRIVATE_PAYLOAD_BYTES


def _write_json(stream: Any, payload: dict[str, Any]) -> None:
    stream.write(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")


def _data_usage(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(entry.stat().st_size for entry in path.rglob("*") if entry.is_file())


def _add_authorization_arguments(
    parser: argparse.ArgumentParser, *, needs_receipt_id: bool, needs_confirmation: bool
) -> None:
    if needs_receipt_id:
        parser.add_argument("--authorization-receipt-id", required=True)
    parser.add_argument("--receipt-kind", choices=receipt_kind_values(), required=True)
    parser.add_argument("--scope-json", required=True)
    parser.add_argument("--notice-version", required=True)
    if sys.platform == "win32":
        parser.add_argument("--capability-handle", type=int, required=True)
    else:
        parser.add_argument("--capability-fd", type=int, required=True)
    if needs_confirmation:
        parser.add_argument(
            "--confirmed",
            action="store_true",
            help="record that the Skill already showed and received required owner confirmation",
        )


def _add_payload_argument(parser: argparse.ArgumentParser) -> None:
    if sys.platform == "win32":
        parser.add_argument(
            "--payload-handle",
            type=int,
            required=True,
            help="allowlisted inherited HANDLE carrying bounded private structured input",
        )
    else:
        parser.add_argument(
            "--payload-fd",
            type=int,
            required=True,
            help="inherited descriptor carrying bounded private structured input",
        )


def _authorization_request(args: argparse.Namespace) -> AuthorizationRequest:
    return AuthorizationRequest.from_values(
        receipt_kind=args.receipt_kind,
        scope=decode_scope(args.scope_json),
        notice_version=args.notice_version,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="goodjob")
    parser.add_argument(
        "--data-dir",
        help="owner-local state directory; defaults to a platform-appropriate location",
    )
    parser.add_argument(
        "--agent-runtime",
        default="codex_task_runtime",
        help="host agent runtime identifier recorded in authorization receipts",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("bootstrap", help="create the owner-local layout and apply migrations")
    subparsers.add_parser(
        "data-status", help="show owner-local storage usage without project contents"
    )

    authorize = subparsers.add_parser(
        "authorize", help="record a session-bound authorization receipt"
    )
    _add_authorization_arguments(authorize, needs_receipt_id=False, needs_confirmation=True)

    verify = subparsers.add_parser(
        "verify-authorization", help="verify a protected request's current session binding"
    )
    _add_authorization_arguments(verify, needs_receipt_id=True, needs_confirmation=False)

    scan = subparsers.add_parser("scan", help="create a full immutable workspace scan snapshot")
    scan.add_argument("--workspace", required=True)
    scan.add_argument("--config-revision", required=True)
    scan.add_argument("--external-git-metadata-grants-json", default="[]")
    _add_authorization_arguments(scan, needs_receipt_id=True, needs_confirmation=False)

    refresh = subparsers.add_parser("refresh", help="create an explicit incremental scan snapshot")
    refresh.add_argument("--workspace-id", required=True)
    refresh.add_argument("--config-revision", required=True)
    refresh.add_argument(
        "--change-detection-mode", choices=("fast", "verify_content"), required=True
    )
    refresh.add_argument("--external-git-metadata-grants-json", default="[]")
    _add_authorization_arguments(refresh, needs_receipt_id=True, needs_confirmation=False)

    scan_overview = subparsers.add_parser(
        "scan-overview",
        help="return one reusable terminal scan without reading project sources",
    )
    scan_overview.add_argument("--workspace", required=True)
    scan_overview.add_argument("--scan-run-id")
    _add_authorization_arguments(scan_overview, needs_receipt_id=True, needs_confirmation=False)

    job_input = subparsers.add_parser(
        "validate-job-input",
        help="validate private role and JD inputs without creating business state",
    )
    job_input.add_argument("--workspace", required=True)
    _add_payload_argument(job_input)
    _add_authorization_arguments(job_input, needs_receipt_id=True, needs_confirmation=False)

    prepare = subparsers.add_parser(
        "prepare-start", help="freeze one dynamic RoleLens against a terminal scan"
    )
    prepare.add_argument("--workspace", required=True)
    _add_payload_argument(prepare)
    _add_authorization_arguments(prepare, needs_receipt_id=True, needs_confirmation=False)

    source_check = subparsers.add_parser(
        "verify-source-revision",
        help="record an exact before-read source revision check",
    )
    source_check.add_argument("--workspace", required=True)
    source_check.add_argument("--preparation-run-id", required=True)
    _add_payload_argument(source_check)
    _add_authorization_arguments(source_check, needs_receipt_id=True, needs_confirmation=False)

    context_request = subparsers.add_parser(
        "request-context",
        help="persist one project-batched context interview for an active preparation run",
    )
    context_request.add_argument("--workspace", required=True)
    _add_payload_argument(context_request)
    _add_authorization_arguments(context_request, needs_receipt_id=True, needs_confirmation=False)

    interview = subparsers.add_parser(
        "interview",
        help="append structured context answers or one bounded mock-interview review",
    )
    interview.add_argument("--workspace", required=True)
    _add_payload_argument(interview)
    _add_authorization_arguments(interview, needs_receipt_id=True, needs_confirmation=False)

    context_evidence = subparsers.add_parser(
        "list-context-evidence",
        help="page through context Evidence frozen into an active preparation run",
    )
    context_evidence.add_argument("--workspace", required=True)
    _add_payload_argument(context_evidence)
    _add_authorization_arguments(
        context_evidence,
        needs_receipt_id=True,
        needs_confirmation=False,
    )

    analysis = subparsers.add_parser(
        "record-analysis",
        help="validate and atomically freeze Evidence, Claims, gaps, and assessments",
    )
    analysis.add_argument("--workspace", required=True)
    _add_payload_argument(analysis)
    _add_authorization_arguments(analysis, needs_receipt_id=True, needs_confirmation=False)

    render = subparsers.add_parser(
        "render",
        help="atomically render one frozen analysis into an immutable offline snapshot",
    )
    render.add_argument("--preparation-run-id", required=True)

    translate_export = subparsers.add_parser(
        "translate-export",
        help="prepare or atomically publish one English export from a frozen snapshot",
    )
    translate_export.add_argument("--workspace", required=True)
    _add_payload_argument(translate_export)
    _add_authorization_arguments(
        translate_export,
        needs_receipt_id=True,
        needs_confirmation=False,
    )

    candidate_inspection = subparsers.add_parser(
        "inspect-external-git-candidate",
        help="inspect only a root-internal .git marker before relation authorization",
    )
    candidate_inspection.add_argument("--workspace", required=True)
    candidate_inspection.add_argument("--git-pointer", required=True)
    _add_authorization_arguments(
        candidate_inspection, needs_receipt_id=True, needs_confirmation=False
    )

    relation_probe = subparsers.add_parser(
        "probe-external-git-relation",
        help="resolve one explicitly authorized linked-worktree relation without invoking Git",
    )
    relation_probe.add_argument("--workspace", required=True)
    relation_probe.add_argument("--git-pointer", required=True)
    _add_authorization_arguments(relation_probe, needs_receipt_id=True, needs_confirmation=False)
    relation_probe.add_argument("--relation-authorization-receipt-id", required=True)
    relation_probe.add_argument("--relation-scope-json", required=True)
    relation_probe.add_argument("--relation-notice-version", required=True)

    history_query = subparsers.add_parser(
        "query-history-candidates",
        help="return bounded older-history candidates for one frozen internal Git baseline",
    )
    _add_history_query_arguments(history_query)
    history_query.add_argument(
        "--maximum-candidates",
        type=int,
        default=MAX_HISTORY_QUERY_CANDIDATES,
    )
    _add_authorization_arguments(history_query, needs_receipt_id=True, needs_confirmation=False)

    history_read = subparsers.add_parser(
        "read-history-candidate",
        help="read one previously selected bounded history candidate path",
    )
    _add_history_query_arguments(history_read)
    history_read.add_argument("--candidate-id", required=True)
    history_read.add_argument("--selected-path", required=True)
    _add_authorization_arguments(history_read, needs_receipt_id=True, needs_confirmation=False)
    return parser


def _add_history_query_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--preparation-run-id", required=True)
    parser.add_argument("--scan-run-id", required=True)
    parser.add_argument("--role-lens-id", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--worktree-id")
    parser.add_argument("--relative-paths-json", required=True)
    parser.add_argument("--query-reason", required=True)


def _handle_bootstrap(paths: DataPaths) -> dict[str, Any]:
    schema_version = Database(paths).migrate()
    return {
        "status": "ok",
        "schema_version": schema_version,
        "data_dir": str(paths.root),
    }


def _handle_data_status(paths: DataPaths) -> dict[str, Any]:
    database = Database(paths)
    database.migrate()
    with database.read_connection() as connection:
        snapshot_count = int(
            connection.execute("SELECT COUNT(*) AS count FROM project_snapshots").fetchone()[
                "count"
            ]
        )
    return {
        "status": "ok",
        "data_dir": str(paths.root),
        "usage_bytes": {
            "sqlite": _data_usage(paths.database_file),
            "artifacts": _data_usage(paths.artifacts_dir),
            "exports": _data_usage(paths.exports_dir),
            "drafts": _data_usage(paths.drafts_dir),
        },
        "snapshot_count": snapshot_count,
    }


def _read_capability(args: argparse.Namespace) -> bytes:
    if sys.platform == "win32":
        return read_capability_from_handle(int(args.capability_handle))
    return read_capability_from_fd(int(args.capability_fd))


def _handle_authorize(args: argparse.Namespace, paths: DataPaths) -> dict[str, Any]:
    if not args.confirmed:
        raise InvalidInputError(
            "owner confirmation is required before recording an authorization receipt"
        )
    capability = _read_capability(args)
    receipt = AuthorizationRepository(Database(paths)).issue(
        capability=capability,
        request=_authorization_request(args),
        issuer_kind=args.agent_runtime,
    )
    return {"status": "ok", "receipt": receipt.as_json()}


def _handle_verify(args: argparse.Namespace, paths: DataPaths) -> dict[str, Any]:
    capability = _read_capability(args)
    receipt = AuthorizationRepository(Database(paths)).require_valid(
        authorization_receipt_id=args.authorization_receipt_id,
        capability=capability,
        request=_authorization_request(args),
    )
    return {"status": "ok", "receipt": receipt.as_json()}


def _scope_workspace(request: AuthorizationRequest) -> Path:
    try:
        scope = json.loads(request.scope_descriptor)
    except json.JSONDecodeError as exc:
        raise InvalidInputError("source-analysis scope must be a JSON object") from exc
    if not isinstance(scope, dict):
        raise InvalidInputError("source-analysis scope must be a JSON object")
    workspace = scope.get("workspace_path")
    categories = scope.get("allowed_categories")
    if (
        not isinstance(workspace, str)
        or not isinstance(categories, list)
        or categories != ["source_analysis"]
    ):
        raise InvalidInputError("source-analysis scope is missing its authorized workspace")
    return Path(workspace).expanduser().resolve(strict=False)


def _verify_scan_authorization(
    args: argparse.Namespace, paths: DataPaths, expected_workspace: Path
) -> bytes:
    capability = _read_capability(args)
    request = _authorization_request(args)
    if request.receipt_kind.value != "source_analysis":
        raise InvalidInputError("scan and refresh require a source_analysis authorization receipt")
    if _scope_workspace(request) != expected_workspace.resolve(strict=False):
        raise InvalidInputError("authorization scope does not match the requested workspace")
    AuthorizationRepository(Database(paths)).require_valid(
        authorization_receipt_id=args.authorization_receipt_id,
        capability=capability,
        request=request,
    )
    return capability


def _scope_object(request: AuthorizationRequest, label: str) -> dict[str, object]:
    try:
        scope = json.loads(request.scope_descriptor)
    except json.JSONDecodeError as exc:
        raise InvalidInputError(f"{label} scope must be a JSON object") from exc
    if not isinstance(scope, dict):
        raise InvalidInputError(f"{label} scope must be a JSON object")
    return scope


def _normal_workspace_child(workspace: Path, raw_path: str, label: str) -> Path:
    raw = Path(raw_path).expanduser()
    candidate = raw if raw.is_absolute() else workspace / raw
    canonical = Path(os.path.normpath(str(candidate)))
    if not _is_within_path(canonical, workspace):
        raise InvalidInputError(f"{label} must remain inside the authorized workspace")
    return canonical


def _is_within_path(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _metadata_grants(
    raw_grants: str,
    *,
    paths: DataPaths,
    capability: bytes,
    workspace: Path,
) -> tuple[ExternalGitGrant, ...]:
    try:
        values = json.loads(raw_grants)
    except json.JSONDecodeError as exc:
        raise InvalidInputError("external_git_metadata_grants_json must be valid JSON") from exc
    if not isinstance(values, list) or len(values) > 64:
        raise InvalidInputError("external_git_metadata_grants_json must be a bounded JSON list")
    repository = AuthorizationRepository(Database(paths))
    grants: list[ExternalGitGrant] = []
    seen_pointers: set[Path] = set()
    for value in values:
        if not isinstance(value, dict):
            raise InvalidInputError("external Git metadata grant must be a JSON object")
        receipt_id = value.get("authorization_receipt_id")
        scope = value.get("scope")
        notice_version = value.get("notice_version")
        if not isinstance(receipt_id, str) or not isinstance(notice_version, str):
            raise InvalidInputError("external Git metadata grant is incomplete")
        request = AuthorizationRequest.from_values(
            receipt_kind=ReceiptKind.EXTERNAL_GIT_METADATA.value,
            scope=scope,
            notice_version=notice_version,
        )
        scope_object = _scope_object(request, "external Git metadata")
        allowed = scope_object.get("allowed_categories")
        scope_workspace = scope_object.get("workspace_path")
        pointer_text = scope_object.get("git_pointer_path")
        git_dir_text = scope_object.get("git_dir")
        common_dir_text = scope_object.get("common_dir")
        marker_kind = scope_object.get("marker_kind")
        git_dir_device = scope_object.get("git_dir_device")
        git_dir_inode = scope_object.get("git_dir_inode")
        common_dir_device = scope_object.get("common_dir_device")
        common_dir_inode = scope_object.get("common_dir_inode")
        if (
            allowed != ["external_git_metadata"]
            or not isinstance(scope_workspace, str)
            or Path(scope_workspace).expanduser().resolve(strict=False) != workspace
            or not isinstance(pointer_text, str)
            or not isinstance(git_dir_text, str)
            or not isinstance(common_dir_text, str)
            or marker_kind not in {"file", "directory"}
            or not isinstance(git_dir_device, int)
            or isinstance(git_dir_device, bool)
            or not isinstance(git_dir_inode, int)
            or isinstance(git_dir_inode, bool)
            or not isinstance(common_dir_device, int)
            or isinstance(common_dir_device, bool)
            or not isinstance(common_dir_inode, int)
            or isinstance(common_dir_inode, bool)
            or min(git_dir_device, git_dir_inode, common_dir_device, common_dir_inode) < 0
        ):
            raise InvalidInputError(
                "external Git metadata grant scope is not valid for this workspace"
            )
        receipt = repository.require_valid(
            authorization_receipt_id=receipt_id,
            capability=capability,
            request=request,
        )
        pointer = _normal_workspace_child(workspace, pointer_text, "git_pointer_path")
        if pointer.name != ".git":
            raise InvalidInputError("git_pointer_path must name a .git file")
        git_dir = Path(git_dir_text).expanduser().resolve(strict=False)
        common_dir = Path(common_dir_text).expanduser().resolve(strict=False)
        if pointer in seen_pointers:
            raise InvalidInputError("external Git metadata grant repeats a .git pointer")
        seen_pointers.add(pointer)
        grants.append(
            ExternalGitGrant(
                pointer,
                marker_kind,
                git_dir,
                common_dir,
                git_dir_device,
                git_dir_inode,
                common_dir_device,
                common_dir_inode,
                receipt.authorization_receipt_id,
                receipt.confirmed_at,
            )
        )
    return tuple(grants)


def _handle_scan(args: argparse.Namespace, paths: DataPaths) -> dict[str, Any]:
    workspace = Path(args.workspace).expanduser().resolve(strict=False)
    capability = _verify_scan_authorization(args, paths, workspace)
    grants = _metadata_grants(
        args.external_git_metadata_grants_json,
        paths=paths,
        capability=capability,
        workspace=workspace,
    )
    return (
        WorkspaceScanner(Database(paths))
        .scan(
            workspace_path=str(workspace),
            config_revision=args.config_revision,
            authorization_receipt_id=args.authorization_receipt_id,
            external_git_grants=grants,
        )
        .as_json()
    )


def _handle_refresh(args: argparse.Namespace, paths: DataPaths) -> dict[str, Any]:
    database = Database(paths)
    with database.read_connection() as connection:
        row = connection.execute(
            "SELECT canonical_root FROM workspaces WHERE workspace_id = ?", (args.workspace_id,)
        ).fetchone()
    if row is None:
        raise InvalidInputError("workspace_id is not registered")
    workspace = Path(str(row["canonical_root"])).resolve(strict=False)
    capability = _verify_scan_authorization(args, paths, workspace)
    grants = _metadata_grants(
        args.external_git_metadata_grants_json,
        paths=paths,
        capability=capability,
        workspace=workspace,
    )
    return (
        WorkspaceScanner(database)
        .refresh(
            workspace_id=args.workspace_id,
            config_revision=args.config_revision,
            change_detection_mode=args.change_detection_mode,
            authorization_receipt_id=args.authorization_receipt_id,
            external_git_grants=grants,
        )
        .as_json()
    )


def _handle_scan_overview(args: argparse.Namespace, paths: DataPaths) -> dict[str, Any]:
    workspace = Path(args.workspace).expanduser().resolve(strict=False)
    _verify_scan_authorization(args, paths, workspace)
    return WorkspaceScanner(Database(paths)).overview(
        workspace_path=str(workspace),
        scan_run_id=args.scan_run_id,
    )


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"unsupported JSON constant: {value}")


def _read_payload_from_fd(fd: int, *, capability_fd: int) -> dict[str, object]:
    if fd < 0 or fd == capability_fd:
        raise InvalidInputError("payload file descriptor must be distinct and non-negative")
    chunks: list[bytes] = []
    remaining = MAX_PROTECTED_PAYLOAD_BYTES + 1
    try:
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError as exc:
        raise InvalidInputError("unable to read the protected structured payload") from exc
    raw = b"".join(chunks)
    if len(raw) > MAX_PROTECTED_PAYLOAD_BYTES:
        raise InvalidInputError("protected structured payload exceeds the byte limit")
    return _decode_payload(raw)


def _decode_payload(raw: bytes) -> dict[str, object]:
    try:
        payload: object = json.loads(raw.decode("utf-8"), parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise InvalidInputError("protected structured payload must be valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise InvalidInputError("protected structured payload must be a JSON object")
    return payload


def _read_payload(args: argparse.Namespace) -> dict[str, object]:
    if sys.platform == "win32":
        capability_handle = int(args.capability_handle)
        payload_handle = int(args.payload_handle)
        if payload_handle <= 0 or payload_handle == capability_handle:
            raise InvalidInputError("payload handle must be distinct and positive")
        from goodjob.platform.capability_windows import read_bytes_from_handle

        return _decode_payload(
            read_bytes_from_handle(payload_handle, maximum_bytes=MAX_PROTECTED_PAYLOAD_BYTES + 1)
        )
    return _read_payload_from_fd(int(args.payload_fd), capability_fd=int(args.capability_fd))


def _handle_prepare_start(args: argparse.Namespace, paths: DataPaths) -> dict[str, Any]:
    workspace = Path(args.workspace).expanduser().resolve(strict=False)
    _verify_scan_authorization(args, paths, workspace)
    payload = _read_payload(args)
    return PreparationService(Database(paths)).start(
        workspace_path=workspace,
        authorization_receipt_id=args.authorization_receipt_id,
        request_value=payload,
    )


def _handle_validate_job_input(args: argparse.Namespace, paths: DataPaths) -> dict[str, Any]:
    workspace = Path(args.workspace).expanduser().resolve(strict=False)
    _verify_scan_authorization(args, paths, workspace)
    payload = _read_payload(args)
    return validate_job_input(payload)


def _handle_source_check(args: argparse.Namespace, paths: DataPaths) -> dict[str, Any]:
    workspace = Path(args.workspace).expanduser().resolve(strict=False)
    _verify_scan_authorization(args, paths, workspace)
    payload = _read_payload(args)
    raw_ids = payload.get("source_revision_ids")
    if not isinstance(raw_ids, list) or any(not isinstance(value, str) for value in raw_ids):
        raise InvalidInputError("source_revision_ids must be a JSON string list")
    phase = payload.get("phase")
    if not isinstance(phase, str):
        raise InvalidInputError("source check phase must be a string")
    return PreparationService(Database(paths)).verify_source_revisions(
        preparation_run_id=args.preparation_run_id,
        authorization_receipt_id=args.authorization_receipt_id,
        source_revision_ids=tuple(raw_ids),
        phase=phase,
    )


def _handle_context_request(args: argparse.Namespace, paths: DataPaths) -> dict[str, Any]:
    workspace = Path(args.workspace).expanduser().resolve(strict=False)
    _verify_scan_authorization(args, paths, workspace)
    payload = _read_payload(args)
    return ContextInterviewService(Database(paths)).request_context(
        authorization_receipt_id=args.authorization_receipt_id,
        request_value=payload,
    )


def _handle_interview(args: argparse.Namespace, paths: DataPaths) -> dict[str, Any]:
    workspace = Path(args.workspace).expanduser().resolve(strict=False)
    _verify_scan_authorization(args, paths, workspace)
    payload = _read_payload(args)
    if payload.get("mode") == "mock_review":
        return ReviewService(Database(paths)).interview(
            workspace_path=workspace,
            authorization_receipt_id=args.authorization_receipt_id,
            request_value=payload,
        )
    return ContextInterviewService(Database(paths)).record_context(
        authorization_receipt_id=args.authorization_receipt_id,
        request_value=payload,
    )


def _handle_context_evidence(args: argparse.Namespace, paths: DataPaths) -> dict[str, Any]:
    workspace = Path(args.workspace).expanduser().resolve(strict=False)
    _verify_scan_authorization(args, paths, workspace)
    payload = _read_payload(args)
    return ContextInterviewService(Database(paths)).list_context_evidence(
        authorization_receipt_id=args.authorization_receipt_id,
        request_value=payload,
    )


def _handle_record_analysis(args: argparse.Namespace, paths: DataPaths) -> dict[str, Any]:
    workspace = Path(args.workspace).expanduser().resolve(strict=False)
    _verify_scan_authorization(args, paths, workspace)
    payload = _read_payload(args)
    return AnalysisService(Database(paths)).record_analysis(
        workspace_path=workspace,
        authorization_receipt_id=args.authorization_receipt_id,
        request_value=payload,
    )


def _handle_render(args: argparse.Namespace, paths: DataPaths) -> dict[str, Any]:
    return ArtifactSnapshotService(Database(paths)).render(args.preparation_run_id)


def _handle_translate_export(args: argparse.Namespace, paths: DataPaths) -> dict[str, Any]:
    workspace = Path(args.workspace).expanduser().resolve(strict=False)
    _verify_scan_authorization(args, paths, workspace)
    payload = _read_payload(args)
    return ExportService(Database(paths)).translate_export(
        workspace_path=workspace,
        authorization_receipt_id=args.authorization_receipt_id,
        request_value=payload,
    )


def _history_paths(raw_paths: str) -> tuple[str, ...]:
    try:
        values = json.loads(raw_paths)
    except json.JSONDecodeError as exc:
        raise InvalidInputError("relative_paths_json must be valid JSON") from exc
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise InvalidInputError("relative_paths_json must be a JSON string list")
    return tuple(values)


def _handle_history_query(args: argparse.Namespace, paths: DataPaths) -> dict[str, Any]:
    workspace = Path(args.workspace).expanduser().resolve(strict=False)
    _verify_scan_authorization(args, paths, workspace)
    return HistoryQueryService(Database(paths)).query_candidates(
        workspace_path=workspace,
        preparation_run_id=args.preparation_run_id,
        scan_run_id=args.scan_run_id,
        role_lens_id=args.role_lens_id,
        project_id=args.project_id,
        worktree_id=args.worktree_id,
        relative_paths=_history_paths(args.relative_paths_json),
        query_reason=args.query_reason,
        maximum_candidates=args.maximum_candidates,
    )


def _handle_history_read(args: argparse.Namespace, paths: DataPaths) -> dict[str, Any]:
    workspace = Path(args.workspace).expanduser().resolve(strict=False)
    _verify_scan_authorization(args, paths, workspace)
    return HistoryQueryService(Database(paths)).read_candidate(
        workspace_path=workspace,
        preparation_run_id=args.preparation_run_id,
        scan_run_id=args.scan_run_id,
        role_lens_id=args.role_lens_id,
        project_id=args.project_id,
        worktree_id=args.worktree_id,
        relative_paths=_history_paths(args.relative_paths_json),
        query_reason=args.query_reason,
        candidate_id=args.candidate_id,
        selected_path=args.selected_path,
    )


def _handle_relation_probe(args: argparse.Namespace, paths: DataPaths) -> dict[str, Any]:
    workspace = Path(args.workspace).expanduser().resolve(strict=False)
    capability = _verify_scan_authorization(args, paths, workspace)
    relation_request = AuthorizationRequest.from_values(
        receipt_kind=ReceiptKind.EXTERNAL_GIT_RELATION_PROBE.value,
        scope=decode_scope(args.relation_scope_json),
        notice_version=args.relation_notice_version,
    )
    scope = _scope_object(relation_request, "external Git relation")
    pointer = _normal_workspace_child(workspace, args.git_pointer, "git_pointer")
    marker_kind = scope.get("marker_kind")
    git_dir_candidate_text = scope.get("git_dir_candidate")
    common_dir_candidate_text = scope.get("common_dir_candidate")
    if (
        scope.get("allowed_categories") != ["external_git_relation_probe"]
        or scope.get("workspace_path") != str(workspace)
        or scope.get("git_pointer_path") != str(pointer)
        or marker_kind not in {"file", "directory"}
        or not isinstance(git_dir_candidate_text, str)
        or not (common_dir_candidate_text is None or isinstance(common_dir_candidate_text, str))
    ):
        raise InvalidInputError("external Git relation scope does not match the requested pointer")
    git_dir_candidate = Path(git_dir_candidate_text)
    common_dir_candidate = (
        Path(common_dir_candidate_text) if isinstance(common_dir_candidate_text, str) else None
    )
    if not git_dir_candidate.is_absolute() or (
        common_dir_candidate is not None and not common_dir_candidate.is_absolute()
    ):
        raise InvalidInputError("external Git relation candidates must be absolute paths")
    AuthorizationRepository(Database(paths)).require_valid(
        authorization_receipt_id=args.relation_authorization_receipt_id,
        capability=capability,
        request=relation_request,
    )
    return {
        "status": "ok",
        "relation": probe_external_git_relation(
            workspace,
            pointer,
            marker_kind=marker_kind,
            git_dir_candidate=git_dir_candidate,
            common_dir_candidate=common_dir_candidate,
        ),
    }


def _handle_candidate_inspection(args: argparse.Namespace, paths: DataPaths) -> dict[str, Any]:
    workspace = Path(args.workspace).expanduser().resolve(strict=False)
    _verify_scan_authorization(args, paths, workspace)
    pointer = _normal_workspace_child(workspace, args.git_pointer, "git_pointer")
    return {
        "status": "ok",
        "candidate": inspect_external_git_candidate(workspace, pointer),
    }


def run(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    paths = DataPaths.from_argument(args.data_dir)
    try:
        require_released_runtime()
        if args.command == "bootstrap":
            payload = _handle_bootstrap(paths)
        elif args.command == "data-status":
            payload = _handle_data_status(paths)
        elif args.command == "authorize":
            payload = _handle_authorize(args, paths)
        elif args.command == "verify-authorization":
            payload = _handle_verify(args, paths)
        elif args.command == "scan":
            payload = _handle_scan(args, paths)
        elif args.command == "refresh":
            payload = _handle_refresh(args, paths)
        elif args.command == "scan-overview":
            payload = _handle_scan_overview(args, paths)
        elif args.command == "validate-job-input":
            payload = _handle_validate_job_input(args, paths)
        elif args.command == "prepare-start":
            payload = _handle_prepare_start(args, paths)
        elif args.command == "verify-source-revision":
            payload = _handle_source_check(args, paths)
        elif args.command == "request-context":
            payload = _handle_context_request(args, paths)
        elif args.command == "interview":
            payload = _handle_interview(args, paths)
        elif args.command == "list-context-evidence":
            payload = _handle_context_evidence(args, paths)
        elif args.command == "record-analysis":
            payload = _handle_record_analysis(args, paths)
        elif args.command == "render":
            payload = _handle_render(args, paths)
        elif args.command == "translate-export":
            payload = _handle_translate_export(args, paths)
        elif args.command == "query-history-candidates":
            payload = _handle_history_query(args, paths)
        elif args.command == "read-history-candidate":
            payload = _handle_history_read(args, paths)
        elif args.command == "inspect-external-git-candidate":
            payload = _handle_candidate_inspection(args, paths)
        elif args.command == "probe-external-git-relation":
            payload = _handle_relation_probe(args, paths)
        else:
            raise AssertionError(f"unexpected command: {args.command}")
    except GoodJobError as exc:
        _write_json(sys.stderr, {"status": "error", "code": exc.code, "message": str(exc)})
        return 2
    _write_json(sys.stdout, payload)
    return 0


def main() -> None:
    sys.exit(run())
