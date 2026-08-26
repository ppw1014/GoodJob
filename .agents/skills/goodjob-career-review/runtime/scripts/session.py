#!/usr/bin/env python3
"""A task-scoped, stdin-bound broker for protected GoodJob core commands."""

from __future__ import annotations

import argparse
import hmac
import json
import math
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TypeGuard, cast

RUNTIME_DIR = Path(__file__).resolve().parents[1]
TRUSTED_SOURCE_DIR = RUNTIME_DIR / "src"
sys.path.insert(0, str(TRUSTED_SOURCE_DIR))

from goodjob.auth import ReceiptKind, generate_capability  # noqa: E402
from goodjob.errors import GoodJobError, InvalidInputError  # noqa: E402
from goodjob.platform.detect import require_released_runtime  # noqa: E402
from goodjob.platform.preflight_windows import (  # noqa: E402
    WindowsPreflightReportDict,
    WindowsReportDict,
    preflight_protocol_failure_report,
)

CORE_BOOTSTRAP = (
    "import sys;"
    f"sys.path.insert(0, {str(TRUSTED_SOURCE_DIR)!r});"
    "from goodjob.cli import main;main()"
)
NOTICE_VERSION = "goodjob-source-analysis-v1"
RELATION_NOTICE_VERSION = "goodjob-external-git-relation-probe-v1"
METADATA_NOTICE_VERSION = "goodjob-external-git-metadata-v1"
MAX_HISTORY_QUERY_PATHS = 32
MAX_HISTORY_QUERY_CANDIDATES = 20
MAX_HISTORY_CANDIDATE_PATHS = 200
MAX_SOURCE_REVISION_BATCH = 200
MAX_CORE_OUTPUT_BYTES = 16 * 1024 * 1024
CORE_CHILD_TIMEOUT_SECONDS = 300.0
type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]


def _is_json_value(value: object) -> TypeGuard[JsonValue]:
    if isinstance(value, float):
        return math.isfinite(value)
    if value is None or isinstance(value, bool | int | str):
        return True
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_json_value(item) for key, item in value.items())
    return False


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"unsupported JSON constant: {value}")


def _json_object(raw: str) -> JsonObject:
    try:
        payload: object = json.loads(raw, parse_constant=_reject_json_constant)
        is_json_object = isinstance(payload, dict) and _is_json_value(payload)
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise InvalidInputError("session broker input must be valid JSON") from exc
    if not is_json_object:
        raise InvalidInputError("session broker input must be a JSON object")
    return cast(JsonObject, payload)


def _required_text(message: JsonObject, key: str) -> str:
    value = message.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InvalidInputError(f"session broker field {key!r} must be a non-empty string")
    return _require_utf8(value, key)


def _required_object(message: JsonObject, key: str) -> JsonObject:
    value = message.get(key)
    if not isinstance(value, dict) or not _is_json_value(value):
        raise InvalidInputError(f"session broker field {key!r} must be a JSON object")
    return cast(JsonObject, value)


def _optional_text(message: JsonObject, key: str, default: str) -> str:
    value = message.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise InvalidInputError(f"session broker field {key!r} must be a non-empty string")
    return _require_utf8(value, key)


def _require_utf8(value: str, field_name: str) -> str:
    if "\x00" in value:
        raise InvalidInputError(f"session broker field {field_name!r} must not contain NUL")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise InvalidInputError(
            f"session broker field {field_name!r} must contain valid UTF-8 text"
        ) from exc
    return value


def _required_nonnegative_integer(message: JsonObject, key: str) -> int:
    value = message.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise InvalidInputError(f"session broker field {key!r} must be a non-negative integer")
    return value


def _required_text_list(message: JsonObject, key: str, *, maximum: int) -> tuple[str, ...]:
    value = message.get(key)
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= maximum
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        raise InvalidInputError(
            f"session broker field {key!r} must be a bounded non-empty string list"
        )
    return tuple(_require_utf8(cast(str, item), key) for item in value)


def _scope(workspace: str) -> str:
    return json.dumps(
        {
            "workspace_path": str(Path(workspace).expanduser().resolve(strict=False)),
            "allowed_categories": ["source_analysis"],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _workspace_path(workspace: str) -> Path:
    return Path(workspace).expanduser().resolve(strict=False)


def _workspace_child(workspace: str, raw_path: str, field_name: str) -> Path:
    root = _workspace_path(workspace)
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    normalized = Path(os.path.normpath(str(candidate)))
    try:
        normalized.relative_to(root)
    except ValueError as exc:
        raise InvalidInputError(
            f"session broker field {field_name!r} must remain inside the authorized workspace"
        ) from exc
    return normalized


def _relation_scope(workspace: str, git_pointer: str, candidate: ExternalGitCandidate) -> str:
    pointer = _workspace_child(workspace, git_pointer, "git_pointer")
    if pointer.name != ".git":
        raise InvalidInputError("session broker field 'git_pointer' must name a .git file")
    return json.dumps(
        {
            "workspace_path": str(_workspace_path(workspace)),
            "git_pointer_path": str(pointer),
            "marker_kind": candidate.marker_kind,
            "git_dir_candidate": candidate.git_dir_candidate,
            "common_dir_candidate": candidate.common_dir_candidate,
            "allowed_categories": ["external_git_relation_probe"],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _metadata_scope(workspace: str, git_pointer: str, relation: ExternalGitRelation) -> str:
    pointer = _workspace_child(workspace, git_pointer, "git_pointer")
    if pointer.name != ".git":
        raise InvalidInputError("session broker field 'git_pointer' must name a .git file")
    return json.dumps(
        {
            "workspace_path": str(_workspace_path(workspace)),
            "git_pointer_path": str(pointer),
            "marker_kind": relation.marker_kind,
            "git_dir": relation.git_dir,
            "common_dir": relation.common_dir,
            "git_dir_device": relation.git_dir_device,
            "git_dir_inode": relation.git_dir_inode,
            "common_dir_device": relation.common_dir_device,
            "common_dir_inode": relation.common_dir_inode,
            "allowed_categories": ["external_git_metadata"],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


@dataclass(frozen=True)
class CoreResponse:
    """The validated JSON envelope returned by a short-lived core child."""

    payload: JsonObject

    @property
    def status(self) -> str:
        return _required_text(self.payload, "status")

    @classmethod
    def from_process(cls, result: subprocess.CompletedProcess[str]) -> CoreResponse:
        raw_output = result.stdout if result.returncode == 0 else result.stderr
        try:
            payload = _json_object(raw_output)
            status = _required_text(payload, "status")
        except InvalidInputError:
            return cls(
                {
                    "status": "error",
                    "code": "core_protocol_error",
                    "message": "GoodJob core returned an invalid JSON response",
                }
            )
        if result.returncode == 0 and status != "ok":
            return cls(
                {
                    "status": "error",
                    "code": "core_protocol_error",
                    "message": "GoodJob core returned an unexpected successful response",
                }
            )
        if result.returncode != 0 and status != "error":
            return cls(
                {
                    "status": "error",
                    "code": "core_protocol_error",
                    "message": "GoodJob core returned an unexpected error response",
                }
            )
        return cls(payload)

    def require_receipt(self) -> CoreResponse:
        if self.status != "ok":
            return self
        try:
            ReceiptEnvelope.from_response(self)
        except InvalidInputError:
            return _protocol_error("GoodJob core returned an invalid authorization receipt")
        return self


@dataclass(frozen=True)
class ReceiptEnvelope:
    """The complete stable receipt response required by the session protocol."""

    authorization_receipt_id: str
    receipt_kind: ReceiptKind
    scope_descriptor: str
    notice_version: str
    confirmed_at: str

    @classmethod
    def from_response(cls, response: CoreResponse) -> ReceiptEnvelope:
        receipt = response.payload.get("receipt")
        if not isinstance(receipt, dict) or not _is_json_value(receipt):
            raise InvalidInputError("authorization receipt is missing")
        payload = cast(JsonObject, receipt)
        required = {
            field: _required_text(payload, field)
            for field in (
                "authorization_receipt_id",
                "receipt_kind",
                "scope_descriptor",
                "notice_version",
                "confirmed_at",
            )
        }
        try:
            kind = ReceiptKind(required["receipt_kind"])
        except ValueError as exc:
            raise InvalidInputError("authorization receipt kind is invalid") from exc
        return cls(
            authorization_receipt_id=required["authorization_receipt_id"],
            receipt_kind=kind,
            scope_descriptor=required["scope_descriptor"],
            notice_version=required["notice_version"],
            confirmed_at=required["confirmed_at"],
        )


@dataclass(frozen=True)
class ExternalGitCandidate:
    workspace_path: str
    git_pointer_path: str
    marker_kind: str
    git_dir_candidate: str
    common_dir_candidate: str | None


@dataclass(frozen=True)
class ExternalGitRelation:
    workspace_path: str
    git_pointer_path: str
    marker_kind: str
    git_dir: str
    common_dir: str
    git_dir_device: int
    git_dir_inode: int
    common_dir_device: int
    common_dir_inode: int


@dataclass(frozen=True)
class HistoryCandidateBinding:
    workspace_path: str
    source_receipt_id: str
    preparation_run_id: str
    scan_run_id: str
    role_lens_id: str
    project_id: str
    worktree_id: str | None
    relative_paths: tuple[str, ...]
    query_reason: str
    candidate_id: str
    changed_paths: tuple[str, ...]


@dataclass(frozen=True)
class HistoryCandidateReadBinding:
    workspace_path: str
    source_receipt_id: str
    preparation_run_id: str
    scan_run_id: str
    role_lens_id: str
    project_id: str
    worktree_id: str
    relative_paths: tuple[str, ...]
    query_reason: str
    candidate_id: str
    selected_path: str
    commit: str
    metadata_sha256: str
    diff_sha256: str
    blob_sha256: str | None


@dataclass(frozen=True)
class PreparationBinding:
    workspace_path: str
    scan_run_id: str
    role_lens_id: str


@dataclass(frozen=True)
class TranslationProjectionBinding:
    workspace_path: str
    authorization_receipt_id: str
    source_artifact_snapshot_id: str
    source_report_bundle_sha256: str
    source_projection_sha256: str


def _receipt_arguments(receipt: ReceiptEnvelope) -> list[str]:
    return [
        "--authorization-receipt-id",
        receipt.authorization_receipt_id,
        "--receipt-kind",
        receipt.receipt_kind.value,
        "--scope-json",
        receipt.scope_descriptor,
        "--notice-version",
        receipt.notice_version,
    ]


def _protocol_error(message: str) -> CoreResponse:
    return CoreResponse({"status": "error", "code": "core_protocol_error", "message": message})


def _write_all(file_descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    fpathconf = cast(Callable[[int, str], int] | None, getattr(os, "fpathconf", None))
    if fpathconf is None:
        chunk_size = 512
    else:
        try:
            chunk_size = max(1, fpathconf(file_descriptor, "PC_PIPE_BUF"))
        except OSError:
            chunk_size = 512
    while view:
        written = os.write(file_descriptor, view[:chunk_size])
        if written <= 0:
            raise OSError("unable to write protected child input")
        view = view[written:]


class SessionBroker:
    """Hold one raw capability only until the parent task closes standard input."""

    def __init__(
        self,
        data_dir: str | None,
        agent_runtime: str | None = None,
        preflight_workspace: str | None = None,
    ) -> None:
        self._capability = generate_capability()
        self._data_dir = str(Path(data_dir).expanduser().resolve()) if data_dir else None
        self._agent_runtime = agent_runtime or "codex_task_runtime"
        self._preflight_workspace = (
            os.path.normcase(str(_workspace_path(preflight_workspace)))
            if preflight_workspace
            else None
        )
        self._source_receipts: dict[str, ReceiptEnvelope] = {}
        self._relation_receipts: dict[str, ReceiptEnvelope] = {}
        self._metadata_receipts: dict[str, ReceiptEnvelope] = {}
        self._candidates: dict[tuple[str, str], ExternalGitCandidate] = {}
        self._relations: dict[tuple[str, str], ExternalGitRelation] = {}
        self._history_candidates: dict[str, HistoryCandidateBinding] = {}
        self._history_candidate_reads: dict[tuple[str, str], HistoryCandidateReadBinding] = {}
        self._preparation_runs: dict[str, PreparationBinding] = {}
        self._validated_job_inputs: dict[str, str] = {}
        self._translation_projections: dict[str, TranslationProjectionBinding] = {}

    def dispatch(self, message: JsonObject) -> JsonObject:
        require_released_runtime()
        if self._preflight_workspace is not None and "workspace" in message:
            workspace = os.path.normcase(str(_workspace_path(_required_text(message, "workspace"))))
            if workspace != self._preflight_workspace:
                raise InvalidInputError(
                    "the requested workspace does not match this session's prerequisite preflight"
                )
        operation = _required_text(message, "op")
        if operation == "authorize_source_analysis":
            return self._authorize_source_analysis(message).payload
        if operation == "verify_source_analysis":
            return self._verify_source_analysis(message).payload
        if operation == "inspect_external_git_candidate":
            return self._inspect_external_git_candidate(message).payload
        if operation == "authorize_external_git_relation_probe":
            return self._authorize_external_git_relation_probe(message).payload
        if operation == "probe_external_git_relation":
            return self._probe_external_git_relation(message).payload
        if operation == "authorize_external_git_metadata":
            return self._authorize_external_git_metadata(message).payload
        if operation == "scan":
            return self._scan(message).payload
        if operation == "refresh":
            return self._refresh(message).payload
        if operation == "scan_overview":
            return self._scan_overview(message).payload
        if operation == "validate_job_input":
            return self._validate_job_input(message).payload
        if operation == "prepare_start":
            return self._prepare_start(message).payload
        if operation == "verify_source_revision":
            return self._verify_source_revision(message).payload
        if operation == "request_context":
            return self._request_context(message).payload
        if operation == "interview":
            return self._interview(message).payload
        if operation == "list_context_evidence":
            return self._list_context_evidence(message).payload
        if operation == "record_analysis":
            return self._record_analysis(message).payload
        if operation == "render":
            return self._render(message).payload
        if operation == "translate_export":
            return self._translate_export(message).payload
        if operation == "query_history_candidates":
            return self._query_history_candidates(message).payload
        if operation == "read_history_candidate":
            return self._read_history_candidate(message).payload
        raise InvalidInputError(f"unsupported session broker operation: {operation}")

    def _authorize_source_analysis(self, message: JsonObject) -> CoreResponse:
        if message.get("confirmed") is not True:
            raise InvalidInputError("owner confirmation is required before source authorization")
        workspace = _required_text(message, "workspace")
        notice_version = _optional_text(message, "notice_version", NOTICE_VERSION)
        response = self._run_protected_child(
            [
                "authorize",
                "--receipt-kind",
                "source_analysis",
                "--scope-json",
                _scope(workspace),
                "--notice-version",
                notice_version,
                "--confirmed",
            ]
        ).require_receipt()
        self._remember_receipt(response, ReceiptKind.SOURCE_ANALYSIS, self._source_receipts)
        return response

    def _verify_source_analysis(self, message: JsonObject) -> CoreResponse:
        workspace = _required_text(message, "workspace")
        receipt = self._source_receipt(message)
        expected_scope = _scope(workspace)
        if receipt.scope_descriptor != expected_scope:
            raise InvalidInputError(
                "source authorization receipt does not match the requested workspace"
            )
        return self._run_protected_child(
            ["verify-authorization", *_receipt_arguments(receipt)]
        ).require_receipt()

    def _authorize_external_git_relation_probe(self, message: JsonObject) -> CoreResponse:
        if message.get("confirmed") is not True:
            raise InvalidInputError("owner confirmation is required before relation authorization")
        workspace = _required_text(message, "workspace")
        pointer = _required_text(message, "git_pointer")
        canonical_workspace = str(_workspace_path(workspace))
        canonical_pointer = str(_workspace_child(workspace, pointer, "git_pointer"))
        candidate = self._candidates.get((canonical_workspace, canonical_pointer))
        if candidate is None:
            raise InvalidInputError(
                "inspect the root-internal Git candidate in this session before relation "
                "authorization"
            )
        notice_version = _optional_text(message, "notice_version", RELATION_NOTICE_VERSION)
        response = self._run_protected_child(
            [
                "authorize",
                "--receipt-kind",
                ReceiptKind.EXTERNAL_GIT_RELATION_PROBE.value,
                "--scope-json",
                _relation_scope(workspace, pointer, candidate),
                "--notice-version",
                notice_version,
                "--confirmed",
            ]
        ).require_receipt()
        self._remember_receipt(
            response, ReceiptKind.EXTERNAL_GIT_RELATION_PROBE, self._relation_receipts
        )
        return response

    def _inspect_external_git_candidate(self, message: JsonObject) -> CoreResponse:
        workspace = _required_text(message, "workspace")
        pointer = _required_text(message, "git_pointer")
        source = self._source_receipt(message)
        self._require_source_scope(source, workspace)
        canonical_pointer = str(_workspace_child(workspace, pointer, "git_pointer"))
        response = self._run_protected_child(
            [
                "inspect-external-git-candidate",
                "--workspace",
                workspace,
                "--git-pointer",
                canonical_pointer,
                *_receipt_arguments(source),
            ]
        )
        if response.status != "ok":
            return response
        candidate_value = response.payload.get("candidate")
        if not isinstance(candidate_value, dict) or not _is_json_value(candidate_value):
            return _protocol_error("GoodJob core returned an invalid external Git candidate")
        candidate_object = cast(JsonObject, candidate_value)
        common_value = candidate_object.get("common_dir_candidate")
        if common_value is not None and not isinstance(common_value, str):
            return _protocol_error("GoodJob core returned an invalid commondir candidate")
        try:
            marker_kind = _required_text(candidate_object, "marker_kind")
            if marker_kind not in {"file", "directory"}:
                raise InvalidInputError("external Git marker kind is invalid")
            candidate = ExternalGitCandidate(
                workspace_path=str(_workspace_path(workspace)),
                git_pointer_path=_required_text(candidate_object, "git_pointer_path"),
                marker_kind=marker_kind,
                git_dir_candidate=_required_text(candidate_object, "git_dir_candidate"),
                common_dir_candidate=(
                    _require_utf8(common_value, "common_dir_candidate")
                    if isinstance(common_value, str)
                    else None
                ),
            )
        except InvalidInputError:
            return _protocol_error("GoodJob core returned an incomplete external Git candidate")
        if candidate.git_pointer_path != canonical_pointer:
            return _protocol_error("GoodJob core returned a candidate for another Git marker")
        self._candidates[(candidate.workspace_path, candidate.git_pointer_path)] = candidate
        return response

    def _probe_external_git_relation(self, message: JsonObject) -> CoreResponse:
        workspace = _required_text(message, "workspace")
        pointer = _required_text(message, "git_pointer")
        source = self._source_receipt(message)
        relation_id = _required_text(message, "relation_authorization_receipt_id")
        relation = self._relation_receipts.get(relation_id)
        if relation is None:
            raise InvalidInputError(
                "external Git relation receipt is not registered in this session"
            )
        try:
            relation_scope = json.loads(relation.scope_descriptor)
        except json.JSONDecodeError:
            raise AssertionError("registered receipt scopes are canonical JSON") from None
        canonical_pointer = str(_workspace_child(workspace, pointer, "git_pointer"))
        if (
            not isinstance(relation_scope, dict)
            or relation_scope.get("workspace_path") != str(_workspace_path(workspace))
            or relation_scope.get("git_pointer_path") != canonical_pointer
        ):
            raise InvalidInputError(
                "external Git relation receipt does not match the requested pointer"
            )
        response = self._run_protected_child(
            [
                "probe-external-git-relation",
                "--workspace",
                workspace,
                "--git-pointer",
                str(_workspace_child(workspace, pointer, "git_pointer")),
                *_receipt_arguments(source),
                "--relation-authorization-receipt-id",
                relation.authorization_receipt_id,
                "--relation-scope-json",
                relation.scope_descriptor,
                "--relation-notice-version",
                relation.notice_version,
            ]
        )
        if response.status != "ok":
            return response
        relation_value = response.payload.get("relation")
        if not isinstance(relation_value, dict) or not _is_json_value(relation_value):
            return _protocol_error("GoodJob core returned an invalid external Git relation")
        relation_object = cast(JsonObject, relation_value)
        try:
            resolved = ExternalGitRelation(
                workspace_path=str(_workspace_path(workspace)),
                git_pointer_path=_required_text(relation_object, "git_pointer_path"),
                marker_kind=_required_text(relation_object, "marker_kind"),
                git_dir=_required_text(relation_object, "git_dir"),
                common_dir=_required_text(relation_object, "common_dir"),
                git_dir_device=_required_nonnegative_integer(relation_object, "git_dir_device"),
                git_dir_inode=_required_nonnegative_integer(relation_object, "git_dir_inode"),
                common_dir_device=_required_nonnegative_integer(
                    relation_object, "common_dir_device"
                ),
                common_dir_inode=_required_nonnegative_integer(relation_object, "common_dir_inode"),
            )
        except InvalidInputError:
            return _protocol_error("GoodJob core returned an incomplete external Git relation")
        if resolved.git_pointer_path != str(_workspace_child(workspace, pointer, "git_pointer")):
            return _protocol_error("GoodJob core returned a relation for a different Git pointer")
        if resolved.marker_kind not in {"file", "directory"}:
            return _protocol_error("GoodJob core returned an invalid Git marker kind")
        self._relations[(resolved.workspace_path, resolved.git_pointer_path)] = resolved
        return response

    def _authorize_external_git_metadata(self, message: JsonObject) -> CoreResponse:
        if message.get("confirmed") is not True:
            raise InvalidInputError("owner confirmation is required before metadata authorization")
        workspace = _required_text(message, "workspace")
        pointer = _required_text(message, "git_pointer")
        git_dir = _required_text(message, "git_dir")
        common_dir = _required_text(message, "common_dir")
        canonical_pointer = str(_workspace_child(workspace, pointer, "git_pointer"))
        relation = self._relations.get((str(_workspace_path(workspace)), canonical_pointer))
        if relation is None:
            raise InvalidInputError(
                "complete an external Git relation probe in this session before "
                "metadata authorization"
            )
        if (
            str(Path(git_dir).expanduser().resolve(strict=False)) != relation.git_dir
            or str(Path(common_dir).expanduser().resolve(strict=False)) != relation.common_dir
        ):
            raise InvalidInputError(
                "external Git metadata paths do not match the completed relation probe"
            )
        notice_version = _optional_text(message, "notice_version", METADATA_NOTICE_VERSION)
        response = self._run_protected_child(
            [
                "authorize",
                "--receipt-kind",
                ReceiptKind.EXTERNAL_GIT_METADATA.value,
                "--scope-json",
                _metadata_scope(workspace, pointer, relation),
                "--notice-version",
                notice_version,
                "--confirmed",
            ]
        ).require_receipt()
        self._remember_receipt(response, ReceiptKind.EXTERNAL_GIT_METADATA, self._metadata_receipts)
        return response

    def _scan(self, message: JsonObject) -> CoreResponse:
        workspace = _required_text(message, "workspace")
        source = self._source_receipt(message)
        self._require_source_scope(source, workspace)
        self._require_validated_job_input(message, workspace)
        config_revision = _optional_text(message, "config_revision", "goodjob-scan-config-v1")
        return self._run_protected_child(
            [
                "scan",
                "--workspace",
                workspace,
                "--config-revision",
                config_revision,
                *_receipt_arguments(source),
                "--external-git-metadata-grants-json",
                self._metadata_grants_json(message),
            ]
        )

    def _refresh(self, message: JsonObject) -> CoreResponse:
        workspace = _required_text(message, "workspace")
        workspace_id = _required_text(message, "workspace_id")
        source = self._source_receipt(message)
        self._require_source_scope(source, workspace)
        self._require_validated_job_input(message, workspace)
        config_revision = _optional_text(message, "config_revision", "goodjob-scan-config-v1")
        mode = _optional_text(message, "change_detection_mode", "fast")
        if mode not in {"fast", "verify_content"}:
            raise InvalidInputError("change_detection_mode must be fast or verify_content")
        return self._run_protected_child(
            [
                "refresh",
                "--workspace-id",
                workspace_id,
                "--config-revision",
                config_revision,
                "--change-detection-mode",
                mode,
                *_receipt_arguments(source),
                "--external-git-metadata-grants-json",
                self._metadata_grants_json(message),
            ]
        )

    def _scan_overview(self, message: JsonObject) -> CoreResponse:
        workspace = _required_text(message, "workspace")
        source = self._source_receipt(message)
        self._require_source_scope(source, workspace)
        self._require_validated_job_input(message, workspace)
        arguments = [
            "scan-overview",
            "--workspace",
            workspace,
            *_receipt_arguments(source),
        ]
        scan_run_id = message.get("scan_run_id")
        if scan_run_id is not None:
            if not isinstance(scan_run_id, str) or not scan_run_id.strip():
                raise InvalidInputError("scan_run_id must be a non-empty string when provided")
            arguments.extend(["--scan-run-id", _require_utf8(scan_run_id, "scan_run_id")])
        return self._run_protected_child(arguments)

    def _prepare_start(self, message: JsonObject) -> CoreResponse:
        workspace = _required_text(message, "workspace")
        source = self._source_receipt(message)
        self._require_source_scope(source, workspace)
        request = _required_object(message, "preparation_request")
        self._require_validated_job_input(request, workspace)
        expected_scan_run_id = _required_text(request, "scan_run_id")
        response = self._run_protected_child(
            [
                "prepare-start",
                "--workspace",
                workspace,
                *_receipt_arguments(source),
            ],
            payload=request,
        )
        if response.status != "ok":
            return response
        preparation_value = response.payload.get("preparation_run")
        role_lens_value = response.payload.get("role_lens")
        if (
            not isinstance(preparation_value, dict)
            or not _is_json_value(preparation_value)
            or not isinstance(role_lens_value, dict)
            or not _is_json_value(role_lens_value)
        ):
            return _protocol_error("GoodJob core returned an invalid PreparationRun")
        preparation = cast(JsonObject, preparation_value)
        role_lens = cast(JsonObject, role_lens_value)
        try:
            preparation_run_id = _required_text(preparation, "preparation_run_id")
            scan_run_id = _required_text(preparation, "scan_run_id")
            role_lens_id = _required_text(role_lens, "role_lens_id")
        except InvalidInputError:
            return _protocol_error("GoodJob core returned an incomplete PreparationRun")
        if scan_run_id != expected_scan_run_id or preparation.get("role_lens_id") != role_lens_id:
            return _protocol_error("GoodJob core returned a PreparationRun for another input")
        binding = PreparationBinding(
            workspace_path=str(_workspace_path(workspace)),
            scan_run_id=scan_run_id,
            role_lens_id=role_lens_id,
        )
        existing = self._preparation_runs.get(preparation_run_id)
        if existing is not None and existing != binding:
            return _protocol_error("GoodJob core returned a colliding PreparationRun")
        self._preparation_runs[preparation_run_id] = binding
        return response

    def _require_validated_job_input(self, message: JsonObject, workspace: str) -> str:
        canonical_workspace = str(_workspace_path(workspace))
        validation_sha256 = _required_text(message, "job_input_validation_sha256")
        current = self._validated_job_inputs.get(canonical_workspace)
        if current is None or not hmac.compare_digest(validation_sha256, current):
            raise InvalidInputError(
                "validate this exact job input in the current session before protected analysis"
            )
        return validation_sha256

    def _validate_job_input(self, message: JsonObject) -> CoreResponse:
        workspace = _required_text(message, "workspace")
        source = self._source_receipt(message)
        self._require_source_scope(source, workspace)
        canonical_workspace = str(_workspace_path(workspace))
        self._validated_job_inputs.pop(canonical_workspace, None)
        job_input = _required_object(message, "job_input")
        response = self._run_protected_child(
            [
                "validate-job-input",
                "--workspace",
                workspace,
                *_receipt_arguments(source),
            ],
            payload=job_input,
        )
        if response.status != "ok":
            return response
        result_value = response.payload.get("job_input")
        if not isinstance(result_value, dict) or not _is_json_value(result_value):
            return _protocol_error("GoodJob core returned an invalid JobInput validation")
        result = cast(JsonObject, result_value)
        try:
            validation_sha256 = _required_text(result, "validation_sha256")
        except InvalidInputError:
            return _protocol_error("GoodJob core returned an incomplete JobInput validation")
        self._validated_job_inputs[canonical_workspace] = validation_sha256
        return response

    def _verify_source_revision(self, message: JsonObject) -> CoreResponse:
        workspace = _required_text(message, "workspace")
        source = self._source_receipt(message)
        self._require_source_scope(source, workspace)
        preparation_run_id = _required_text(message, "preparation_run_id")
        binding = self._preparation_runs.get(preparation_run_id)
        if binding is None:
            raise InvalidInputError(
                "start this PreparationRun in the current session before checking its sources"
            )
        if binding.workspace_path != str(_workspace_path(workspace)):
            raise InvalidInputError("PreparationRun does not match the requested workspace")
        source_revision_ids = _required_text_list(
            message, "source_revision_ids", maximum=MAX_SOURCE_REVISION_BATCH
        )
        phase = _required_text(message, "phase")
        if phase != "before_read":
            raise InvalidInputError("public source checks only support the before_read phase")
        response = self._run_protected_child(
            [
                "verify-source-revision",
                "--workspace",
                workspace,
                "--preparation-run-id",
                preparation_run_id,
                *_receipt_arguments(source),
            ],
            payload={
                "phase": phase,
                "source_revision_ids": list(source_revision_ids),
            },
        )
        if (
            response.status == "ok"
            and response.payload.get("preparation_run_id") != preparation_run_id
        ):
            return _protocol_error("GoodJob core checked sources for another PreparationRun")
        return response

    def _request_context(self, message: JsonObject) -> CoreResponse:
        workspace = _required_text(message, "workspace")
        source = self._source_receipt(message)
        self._require_source_scope(source, workspace)
        request = _required_object(message, "context_interview_request")
        preparation_run_id = _required_text(request, "preparation_run_id")
        self._require_preparation_binding(workspace, preparation_run_id)
        response = self._run_protected_child(
            [
                "request-context",
                "--workspace",
                workspace,
                *_receipt_arguments(source),
            ],
            payload=request,
        )
        if response.status != "ok":
            return response
        interview_value = response.payload.get("context_interview")
        if not isinstance(interview_value, dict) or not _is_json_value(interview_value):
            return _protocol_error("GoodJob core returned an invalid context interview")
        interview = cast(JsonObject, interview_value)
        if interview.get("preparation_run_id") != preparation_run_id:
            return _protocol_error("GoodJob core returned context for another PreparationRun")
        return response

    def _interview(self, message: JsonObject) -> CoreResponse:
        workspace = _required_text(message, "workspace")
        source = self._source_receipt(message)
        self._require_source_scope(source, workspace)
        request = _required_object(message, "interview_input")
        preparation_run_id = _required_text(request, "preparation_run_id")
        mode = _required_text(request, "mode")
        if mode == "context":
            self._require_preparation_binding(workspace, preparation_run_id)
        elif mode != "mock_review":
            raise InvalidInputError("interview mode must be context or mock_review")
        response = self._run_protected_child(
            ["interview", "--workspace", workspace, *_receipt_arguments(source)],
            payload=request,
        )
        if response.status != "ok":
            return response
        if mode == "context":
            batch_value = response.payload.get("context_answer_batch")
            if not isinstance(batch_value, dict) or not _is_json_value(batch_value):
                return _protocol_error("GoodJob core returned an invalid context answer batch")
            batch = cast(JsonObject, batch_value)
            if batch.get("preparation_run_id") != preparation_run_id:
                return _protocol_error("GoodJob core returned answers for another PreparationRun")
            return response
        action = _required_text(request, "action")
        response_key = "mock_review" if action == "list_targets" else "interview_review"
        value = response.payload.get(response_key)
        if not isinstance(value, dict) or not _is_json_value(value):
            return _protocol_error("GoodJob core returned an invalid mock-review payload")
        mock_review = cast(JsonObject, value)
        if mock_review.get("preparation_run_id") != preparation_run_id:
            return _protocol_error("GoodJob core reviewed another PreparationRun")
        return response

    def _list_context_evidence(self, message: JsonObject) -> CoreResponse:
        workspace = _required_text(message, "workspace")
        source = self._source_receipt(message)
        self._require_source_scope(source, workspace)
        request = _required_object(message, "context_evidence_request")
        preparation_run_id = _required_text(request, "preparation_run_id")
        self._require_preparation_binding(workspace, preparation_run_id)
        response = self._run_protected_child(
            [
                "list-context-evidence",
                "--workspace",
                workspace,
                *_receipt_arguments(source),
            ],
            payload=request,
        )
        if response.status != "ok":
            return response
        page_value = response.payload.get("context_evidence_page")
        if not isinstance(page_value, dict) or not _is_json_value(page_value):
            return _protocol_error("GoodJob core returned an invalid context Evidence page")
        page = cast(JsonObject, page_value)
        if page.get("preparation_run_id") != preparation_run_id:
            return _protocol_error("GoodJob core returned context Evidence for another run")
        return response

    def _record_analysis(self, message: JsonObject) -> CoreResponse:
        workspace = _required_text(message, "workspace")
        source = self._source_receipt(message)
        self._require_source_scope(source, workspace)
        request = _required_object(message, "analysis_commit_request")
        if "_history_proofs" in request:
            raise InvalidInputError("analysis_commit_request contains a reserved broker field")
        preparation_run_id = _required_text(request, "preparation_run_id")
        role_lens_id = _required_text(request, "role_lens_id")
        preparation = self._require_preparation_binding(workspace, preparation_run_id)
        if preparation.role_lens_id != role_lens_id:
            raise InvalidInputError("analysis does not match the active RoleLens")
        raw_drafts = request.get("evidence_drafts", [])
        if not isinstance(raw_drafts, list):
            raise InvalidInputError("analysis evidence_drafts must be a JSON list")
        proofs: list[JsonObject] = []
        seen_reads: set[tuple[str, str]] = set()
        for raw_draft in raw_drafts:
            if not isinstance(raw_draft, dict) or not _is_json_value(raw_draft):
                raise InvalidInputError("analysis contains a malformed EvidenceDraft")
            draft = cast(JsonObject, raw_draft)
            if draft.get("origin_kind") != "git_commit":
                continue
            candidate_id = _required_text(draft, "candidate_id")
            selected_path = _required_text(draft, "selected_path")
            key = (candidate_id, selected_path)
            read = self._history_candidate_reads.get(key)
            if read is None:
                raise InvalidInputError(
                    "read this exact history candidate in the current task before analysis"
                )
            if key in seen_reads:
                raise InvalidInputError("one history candidate path may be committed only once")
            seen_reads.add(key)
            blob_value = draft.get("blob_sha256")
            if blob_value is not None and not isinstance(blob_value, str):
                raise InvalidInputError("Git EvidenceDraft blob_sha256 must be a string or null")
            if (
                read.workspace_path != str(_workspace_path(workspace))
                or read.preparation_run_id != preparation_run_id
                or read.role_lens_id != role_lens_id
                or draft.get("project_id") != read.project_id
                or draft.get("worktree_id") != read.worktree_id
                or draft.get("query_reason") != read.query_reason
                or draft.get("commit") != read.commit
                or draft.get("metadata_sha256") != read.metadata_sha256
                or draft.get("diff_sha256") != read.diff_sha256
                or blob_value != read.blob_sha256
            ):
                raise InvalidInputError("Git EvidenceDraft does not match its task-scoped read")
            proofs.append(
                {
                    "candidate_id": read.candidate_id,
                    "preparation_run_id": read.preparation_run_id,
                    "scan_run_id": read.scan_run_id,
                    "role_lens_id": read.role_lens_id,
                    "project_id": read.project_id,
                    "worktree_id": read.worktree_id,
                    "relative_paths": list(read.relative_paths),
                    "query_reason": read.query_reason,
                    "selected_path": read.selected_path,
                    "commit": read.commit,
                    "metadata_sha256": read.metadata_sha256,
                    "diff_sha256": read.diff_sha256,
                    "blob_sha256": read.blob_sha256,
                }
            )
        protected_request = dict(request)
        protected_request["_history_proofs"] = cast(list[JsonValue], proofs)
        response = self._run_protected_child(
            [
                "record-analysis",
                "--workspace",
                workspace,
                *_receipt_arguments(source),
            ],
            payload=protected_request,
        )
        if response.status != "ok":
            return response
        commit_value = response.payload.get("analysis_commit")
        if commit_value is None:
            if response.payload.get("preparation_run_id") != preparation_run_id:
                return _protocol_error("GoodJob core rejected analysis for another run")
            return response
        if not isinstance(commit_value, dict) or not _is_json_value(commit_value):
            return _protocol_error("GoodJob core returned an invalid analysis commit")
        commit = cast(JsonObject, commit_value)
        if commit.get("preparation_run_id") != preparation_run_id:
            return _protocol_error("GoodJob core committed analysis for another run")
        return response

    def _render(self, message: JsonObject) -> CoreResponse:
        preparation_run_id = _required_text(message, "preparation_run_id")
        response = self._run_unprotected_child(
            ["render", "--preparation-run-id", preparation_run_id]
        )
        if response.status != "ok":
            return response
        snapshot_value = response.payload.get("artifact_snapshot")
        if not isinstance(snapshot_value, dict) or not _is_json_value(snapshot_value):
            return _protocol_error("GoodJob core returned an invalid artifact snapshot")
        snapshot = cast(JsonObject, snapshot_value)
        if snapshot.get("preparation_run_id") != preparation_run_id:
            return _protocol_error("GoodJob core rendered another PreparationRun")
        return response

    def _translate_export(self, message: JsonObject) -> CoreResponse:
        workspace = _required_text(message, "workspace")
        source_receipt = self._source_receipt(message)
        self._require_source_scope(source_receipt, workspace)
        request = _required_object(message, "translation_export_request")
        action = _required_text(request, "action")
        source_snapshot_id = _required_text(request, "source_artifact_snapshot_id")
        canonical_workspace = str(_workspace_path(workspace))
        if action == "prepare":
            response = self._run_protected_child(
                [
                    "translate-export",
                    "--workspace",
                    workspace,
                    *_receipt_arguments(source_receipt),
                ],
                payload=request,
            )
            if response.status != "ok":
                return response
            source_value = response.payload.get("translation_source")
            if not isinstance(source_value, dict) or not _is_json_value(source_value):
                return _protocol_error("GoodJob core returned an invalid translation source")
            translation_source = cast(JsonObject, source_value)
            try:
                returned_snapshot_id = _required_text(
                    translation_source, "source_artifact_snapshot_id"
                )
                report_bundle_sha256 = _required_text(
                    translation_source, "source_report_bundle_sha256"
                )
                projection_sha256 = _required_text(translation_source, "source_projection_sha256")
            except InvalidInputError:
                return _protocol_error("GoodJob core returned an incomplete translation source")
            if returned_snapshot_id != source_snapshot_id:
                return _protocol_error("GoodJob core prepared another ArtifactSnapshot")
            items = translation_source.get("items")
            if not isinstance(items, list) or not _is_json_value(items):
                return _protocol_error("GoodJob core returned malformed translation source items")
            prepared_binding = TranslationProjectionBinding(
                workspace_path=canonical_workspace,
                authorization_receipt_id=source_receipt.authorization_receipt_id,
                source_artifact_snapshot_id=source_snapshot_id,
                source_report_bundle_sha256=report_bundle_sha256,
                source_projection_sha256=projection_sha256,
            )
            existing = self._translation_projections.get(source_snapshot_id)
            if existing is not None and existing != prepared_binding:
                return _protocol_error("GoodJob core returned a changed translation projection")
            self._translation_projections[source_snapshot_id] = prepared_binding
            return response
        if action != "publish":
            raise InvalidInputError("translation export action must be prepare or publish")
        publication_binding = self._translation_projections.get(source_snapshot_id)
        if publication_binding is None:
            raise InvalidInputError(
                "prepare this ArtifactSnapshot translation in the current task before publishing"
            )
        projection_sha256 = _required_text(request, "source_projection_sha256")
        if (
            publication_binding.workspace_path != canonical_workspace
            or publication_binding.authorization_receipt_id
            != source_receipt.authorization_receipt_id
            or not hmac.compare_digest(
                publication_binding.source_projection_sha256, projection_sha256
            )
        ):
            raise InvalidInputError(
                "translation publication does not match its task-scoped source projection"
            )
        response = self._run_protected_child(
            [
                "translate-export",
                "--workspace",
                workspace,
                *_receipt_arguments(source_receipt),
            ],
            payload=request,
        )
        if response.status != "ok":
            return response
        export_value = response.payload.get("derived_export")
        if not isinstance(export_value, dict) or not _is_json_value(export_value):
            return _protocol_error("GoodJob core returned an invalid DerivedExport")
        derived_export = cast(JsonObject, export_value)
        if (
            derived_export.get("source_artifact_snapshot_id") != source_snapshot_id
            or derived_export.get("source_report_bundle_sha256")
            != publication_binding.source_report_bundle_sha256
            or derived_export.get("source_projection_sha256")
            != publication_binding.source_projection_sha256
        ):
            return _protocol_error("GoodJob core published another translation source")
        return response

    def _require_preparation_binding(
        self,
        workspace: str,
        preparation_run_id: str,
    ) -> PreparationBinding:
        binding = self._preparation_runs.get(preparation_run_id)
        if binding is None:
            raise InvalidInputError(
                "start this PreparationRun in the current task before continuing analysis"
            )
        if binding.workspace_path != str(_workspace_path(workspace)):
            raise InvalidInputError("PreparationRun does not match the requested workspace")
        return binding

    def _query_history_candidates(self, message: JsonObject) -> CoreResponse:
        workspace = _required_text(message, "workspace")
        source = self._source_receipt(message)
        self._require_source_scope(source, workspace)
        preparation_run_id = _required_text(message, "preparation_run_id")
        scan_run_id = _required_text(message, "scan_run_id")
        role_lens_id = _required_text(message, "role_lens_id")
        preparation = self._preparation_runs.get(preparation_run_id)
        if preparation is None:
            raise InvalidInputError(
                "start this PreparationRun in the current session before querying history"
            )
        if preparation != PreparationBinding(
            workspace_path=str(_workspace_path(workspace)),
            scan_run_id=scan_run_id,
            role_lens_id=role_lens_id,
        ):
            raise InvalidInputError("history query does not match the active PreparationRun")
        project_id = _required_text(message, "project_id")
        worktree_value = message.get("worktree_id")
        if worktree_value is not None and not isinstance(worktree_value, str):
            raise InvalidInputError("worktree_id must be a string when provided")
        worktree_id = (
            _require_utf8(worktree_value, "worktree_id")
            if isinstance(worktree_value, str)
            else None
        )
        relative_paths = _required_text_list(
            message, "relative_paths", maximum=MAX_HISTORY_QUERY_PATHS
        )
        query_reason = _required_text(message, "query_reason")
        maximum_value = message.get("maximum_candidates", MAX_HISTORY_QUERY_CANDIDATES)
        if (
            not isinstance(maximum_value, int)
            or isinstance(maximum_value, bool)
            or not 1 <= maximum_value <= MAX_HISTORY_QUERY_CANDIDATES
        ):
            raise InvalidInputError("maximum_candidates must be a bounded positive integer")
        arguments = [
            "query-history-candidates",
            "--workspace",
            workspace,
            "--preparation-run-id",
            preparation_run_id,
            "--scan-run-id",
            scan_run_id,
            "--role-lens-id",
            role_lens_id,
            "--project-id",
            project_id,
            "--relative-paths-json",
            json.dumps(list(relative_paths), ensure_ascii=False, separators=(",", ":")),
            "--query-reason",
            query_reason,
            "--maximum-candidates",
            str(maximum_value),
            *_receipt_arguments(source),
        ]
        if worktree_id is not None:
            arguments.extend(["--worktree-id", worktree_id])
        response = self._run_protected_child(arguments)
        if response.status != "ok":
            return response
        history_value = response.payload.get("history_query")
        if not isinstance(history_value, dict) or not _is_json_value(history_value):
            return _protocol_error("GoodJob core returned an invalid history query")
        history = cast(JsonObject, history_value)
        candidates_value = history.get("candidates")
        resolved_worktree = history.get("worktree_id")
        if not isinstance(candidates_value, list) or len(candidates_value) > maximum_value:
            return _protocol_error("GoodJob core returned invalid history candidates")
        if not isinstance(resolved_worktree, str) or (
            worktree_id is not None and resolved_worktree != worktree_id
        ):
            return _protocol_error("GoodJob core returned an invalid history worktree")
        if (
            history.get("scan_run_id") != scan_run_id
            or history.get("preparation_run_id") != preparation_run_id
            or history.get("role_lens_id") != role_lens_id
            or history.get("project_id") != project_id
            or history.get("relative_paths") != sorted(set(relative_paths))
            or history.get("query_reason") != query_reason
        ):
            return _protocol_error("GoodJob core returned a history query for another scope")
        for candidate_value in candidates_value:
            if not isinstance(candidate_value, dict) or not _is_json_value(candidate_value):
                return _protocol_error("GoodJob core returned a malformed history candidate")
            candidate = cast(JsonObject, candidate_value)
            try:
                candidate_id = _required_text(candidate, "candidate_id")
                changed_paths = _required_text_list(
                    candidate, "changed_paths", maximum=MAX_HISTORY_CANDIDATE_PATHS
                )
            except InvalidInputError:
                return _protocol_error("GoodJob core returned an incomplete history candidate")
            binding = HistoryCandidateBinding(
                workspace_path=str(_workspace_path(workspace)),
                source_receipt_id=source.authorization_receipt_id,
                preparation_run_id=preparation_run_id,
                scan_run_id=scan_run_id,
                role_lens_id=role_lens_id,
                project_id=project_id,
                worktree_id=resolved_worktree,
                relative_paths=tuple(sorted(set(relative_paths))),
                query_reason=query_reason,
                candidate_id=candidate_id,
                changed_paths=changed_paths,
            )
            existing = self._history_candidates.get(candidate_id)
            if existing is not None and existing != binding:
                return _protocol_error("GoodJob core returned a colliding history candidate")
            self._history_candidates[candidate_id] = binding
        return response

    def _read_history_candidate(self, message: JsonObject) -> CoreResponse:
        workspace = _required_text(message, "workspace")
        source = self._source_receipt(message)
        self._require_source_scope(source, workspace)
        candidate_id = _required_text(message, "candidate_id")
        selected_path = _required_text(message, "selected_path")
        binding = self._history_candidates.get(candidate_id)
        if binding is None:
            raise InvalidInputError(
                "query this history candidate in the current session before reading it"
            )
        if (
            binding.workspace_path != str(_workspace_path(workspace))
            or binding.source_receipt_id != source.authorization_receipt_id
            or selected_path not in binding.changed_paths
        ):
            raise InvalidInputError("history candidate does not match this session request")
        arguments = [
            "read-history-candidate",
            "--workspace",
            workspace,
            "--preparation-run-id",
            binding.preparation_run_id,
            "--scan-run-id",
            binding.scan_run_id,
            "--role-lens-id",
            binding.role_lens_id,
            "--project-id",
            binding.project_id,
            "--relative-paths-json",
            json.dumps(list(binding.relative_paths), ensure_ascii=False, separators=(",", ":")),
            "--query-reason",
            binding.query_reason,
            "--candidate-id",
            binding.candidate_id,
            "--selected-path",
            selected_path,
            *_receipt_arguments(source),
        ]
        if binding.worktree_id is not None:
            arguments.extend(["--worktree-id", binding.worktree_id])
        response = self._run_protected_child(arguments)
        if response.status != "ok":
            return response
        read_value = response.payload.get("history_candidate_read")
        if not isinstance(read_value, dict) or not _is_json_value(read_value):
            return _protocol_error("GoodJob core returned an invalid history candidate read")
        read = cast(JsonObject, read_value)
        candidate_value = read.get("candidate")
        if not isinstance(candidate_value, dict) or not _is_json_value(candidate_value):
            return _protocol_error("GoodJob core returned incomplete history candidate metadata")
        candidate = cast(JsonObject, candidate_value)
        try:
            returned_candidate_id = _required_text(candidate, "candidate_id")
            commit = _required_text(candidate, "commit")
            metadata_sha256 = _required_text(candidate, "metadata_sha256")
            returned_path = _required_text(read, "selected_path")
            returned_reason = _required_text(read, "query_reason")
            diff_sha256 = _required_text(read, "diff_sha256")
        except InvalidInputError:
            return _protocol_error("GoodJob core returned incomplete history candidate hashes")
        blob_value = read.get("blob_sha256")
        if blob_value is not None and not isinstance(blob_value, str):
            return _protocol_error("GoodJob core returned an invalid history blob hash")
        if (
            binding.worktree_id is None
            or returned_candidate_id != binding.candidate_id
            or returned_path != selected_path
            or returned_reason != binding.query_reason
        ):
            return _protocol_error("GoodJob core read another history candidate scope")
        read_binding = HistoryCandidateReadBinding(
            workspace_path=binding.workspace_path,
            source_receipt_id=binding.source_receipt_id,
            preparation_run_id=binding.preparation_run_id,
            scan_run_id=binding.scan_run_id,
            role_lens_id=binding.role_lens_id,
            project_id=binding.project_id,
            worktree_id=binding.worktree_id,
            relative_paths=binding.relative_paths,
            query_reason=binding.query_reason,
            candidate_id=binding.candidate_id,
            selected_path=selected_path,
            commit=commit,
            metadata_sha256=metadata_sha256,
            diff_sha256=diff_sha256,
            blob_sha256=blob_value,
        )
        existing = self._history_candidate_reads.get((binding.candidate_id, selected_path))
        if existing is not None and existing != read_binding:
            return _protocol_error("GoodJob core returned a changed history candidate read")
        self._history_candidate_reads[(binding.candidate_id, selected_path)] = read_binding
        return response

    def _source_receipt(self, message: JsonObject) -> ReceiptEnvelope:
        receipt_id = _required_text(message, "authorization_receipt_id")
        receipt = self._source_receipts.get(receipt_id)
        if receipt is None:
            raise InvalidInputError(
                "source authorization receipt is not registered in this session"
            )
        return receipt

    @staticmethod
    def _require_source_scope(receipt: ReceiptEnvelope, workspace: str) -> None:
        if receipt.scope_descriptor != _scope(workspace):
            raise InvalidInputError(
                "source authorization receipt does not match the requested workspace"
            )

    @staticmethod
    def _remember_receipt(
        response: CoreResponse,
        expected_kind: ReceiptKind,
        destination: dict[str, ReceiptEnvelope],
    ) -> None:
        if response.status != "ok":
            return
        try:
            receipt = ReceiptEnvelope.from_response(response)
        except InvalidInputError:
            return
        if receipt.receipt_kind == expected_kind:
            destination[receipt.authorization_receipt_id] = receipt

    def _metadata_grants_json(self, message: JsonObject) -> str:
        raw_ids = message.get("external_git_metadata_receipt_ids", [])
        if not isinstance(raw_ids, list) or len(raw_ids) > 64:
            raise InvalidInputError("external_git_metadata_receipt_ids must be a bounded JSON list")
        grants: list[dict[str, object]] = []
        seen: set[str] = set()
        for value in raw_ids:
            if not isinstance(value, str):
                raise InvalidInputError("external Git metadata receipt IDs must be strings")
            receipt_id = _require_utf8(value, "external_git_metadata_receipt_ids")
            if receipt_id in seen:
                raise InvalidInputError("external Git metadata receipt IDs must not repeat")
            seen.add(receipt_id)
            receipt = self._metadata_receipts.get(receipt_id)
            if receipt is None:
                raise InvalidInputError(
                    "external Git metadata receipt is not registered in this session"
                )
            try:
                scope = json.loads(receipt.scope_descriptor)
            except json.JSONDecodeError:
                raise AssertionError("registered receipt scopes are canonical JSON") from None
            grants.append(
                {
                    "authorization_receipt_id": receipt.authorization_receipt_id,
                    "scope": scope,
                    "notice_version": receipt.notice_version,
                }
            )
        return json.dumps(grants, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    def _run_protected_child(
        self, arguments: list[str], *, payload: JsonObject | None = None
    ) -> CoreResponse:
        payload_bytes: bytes | None = None
        if payload is not None:
            try:
                payload_bytes = json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                    allow_nan=False,
                ).encode("utf-8")
            except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
                raise InvalidInputError("protected payload must contain canonical JSON") from exc
        if sys.platform == "win32":
            return self._run_windows_child(
                arguments, payload_bytes=payload_bytes, include_capability=True
            )
        try:
            capability_read_fd, capability_write_fd = os.pipe()
        except OSError as exc:
            raise InvalidInputError("unable to allocate protected capability channel") from exc
        payload_read_fd: int | None = None
        payload_write_fd: int | None = None
        if payload is not None:
            try:
                payload_read_fd, payload_write_fd = os.pipe()
            except OSError as exc:
                os.close(capability_read_fd)
                os.close(capability_write_fd)
                raise InvalidInputError("unable to allocate protected payload channel") from exc
        command = [sys.executable, "-I", "-B", "-c", CORE_BOOTSTRAP]
        if self._data_dir:
            command.extend(["--data-dir", self._data_dir])
        command.extend(["--agent-runtime", self._agent_runtime])
        child_arguments = list(arguments)
        pass_fds = [capability_read_fd]
        if payload_read_fd is not None:
            child_arguments.extend(["--payload-fd", str(payload_read_fd)])
            pass_fds.append(payload_read_fd)
        full_command = [
            *command,
            *child_arguments,
            "--capability-fd",
            str(capability_read_fd),
        ]
        child_environment = {
            key: value for key, value in os.environ.items() if not key.startswith("PYTHON")
        }
        try:
            process = subprocess.Popen(
                full_command,
                cwd=RUNTIME_DIR,
                env=child_environment,
                close_fds=True,
                pass_fds=tuple(pass_fds),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except Exception:
            os.close(capability_read_fd)
            os.close(capability_write_fd)
            if payload_read_fd is not None:
                os.close(payload_read_fd)
            if payload_write_fd is not None:
                os.close(payload_write_fd)
            raise
        os.close(capability_read_fd)
        if payload_read_fd is not None:
            os.close(payload_read_fd)
        write_error: OSError | None = None
        capability_write_open = True
        try:
            _write_all(capability_write_fd, self._capability)
            os.close(capability_write_fd)
            capability_write_open = False
            if payload_write_fd is not None and payload_bytes is not None:
                _write_all(payload_write_fd, payload_bytes)
        except OSError as exc:
            write_error = exc
        finally:
            if capability_write_open:
                os.close(capability_write_fd)
            if payload_write_fd is not None:
                os.close(payload_write_fd)
        if write_error is not None:
            with suppress(ProcessLookupError):
                process.kill()
            process.communicate()
            raise InvalidInputError(
                "unable to deliver protected input to GoodJob core"
            ) from write_error
        stdout, stderr = process.communicate()
        return CoreResponse.from_process(
            subprocess.CompletedProcess(full_command, process.returncode, stdout, stderr)
        )

    def _run_windows_child(
        self,
        arguments: list[str],
        *,
        payload_bytes: bytes | None,
        include_capability: bool,
    ) -> CoreResponse:
        from goodjob.platform.launcher_windows import (
            ProtectedInput,
            WindowsLaunchRequest,
            run_windows_process,
        )

        command_arguments = ["-I", "-B", "-c", CORE_BOOTSTRAP]
        if self._data_dir:
            command_arguments.extend(["--data-dir", self._data_dir])
        command_arguments.extend(["--agent-runtime", self._agent_runtime, *arguments])
        inputs: list[ProtectedInput] = []
        if include_capability:
            inputs.append(ProtectedInput("--capability-handle", self._capability))
        if payload_bytes is not None:
            inputs.append(ProtectedInput("--payload-handle", payload_bytes))
        child_environment = {
            key: value for key, value in os.environ.items() if not key.startswith("PYTHON")
        }
        result = run_windows_process(
            WindowsLaunchRequest(
                application=sys.executable,
                arguments=tuple(command_arguments),
                cwd=str(RUNTIME_DIR),
                environment=child_environment,
                maximum_output_bytes=MAX_CORE_OUTPUT_BYTES,
                timeout_seconds=CORE_CHILD_TIMEOUT_SECONDS,
            ),
            inputs=inputs,
        )
        return CoreResponse.from_process(
            subprocess.CompletedProcess(
                result.args,
                result.returncode,
                result.stdout.decode("utf-8", errors="replace"),
                result.stderr.decode("utf-8", errors="replace"),
            )
        )

    def _run_unprotected_child(self, arguments: list[str]) -> CoreResponse:
        if sys.platform == "win32":
            return self._run_windows_child(arguments, payload_bytes=None, include_capability=False)
        command = [sys.executable, "-I", "-B", "-c", CORE_BOOTSTRAP]
        if self._data_dir:
            command.extend(["--data-dir", self._data_dir])
        command.extend(["--agent-runtime", self._agent_runtime])
        full_command = [*command, *arguments]
        child_environment = {
            key: value for key, value in os.environ.items() if not key.startswith("PYTHON")
        }
        completed = subprocess.run(
            full_command,
            cwd=RUNTIME_DIR,
            env=child_environment,
            capture_output=True,
            text=True,
            check=False,
        )
        return CoreResponse.from_process(completed)


def _run_native_windows_session_preflight(workspace: str) -> WindowsPreflightReportDict:
    from goodjob.platform.detect import NATIVE_WINDOWS_RELEASE_ENABLED
    from goodjob.platform.preflight_windows import (
        SystemWindowsPrerequisiteProbes,
        evaluate_windows_preflight,
    )

    return evaluate_windows_preflight(
        workspace=_workspace_path(workspace),
        runtime_dir=RUNTIME_DIR,
        python_version=(sys.version_info.major, sys.version_info.minor, sys.version_info.micro),
        launcher_kind="session_runtime",
        uv_available=shutil.which("uv") is not None,
        release_enabled=NATIVE_WINDOWS_RELEASE_ENABLED,
        probes=SystemWindowsPrerequisiteProbes(),
    ).as_dict()


def _write_preflight_report(report: WindowsReportDict) -> None:
    sys.stderr.write(json.dumps(report, sort_keys=True) + "\n")


def run(
    argv: list[str] | None = None,
    *,
    platform_name: str = sys.platform,
    input_stream: Iterable[str] | None = None,
) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir")
    parser.add_argument(
        "--agent-runtime",
        default="codex_task_runtime",
        help="host agent runtime identifier for authorization receipts",
    )
    parser.add_argument(
        "--preflight-workspace",
        help="workspace bound by a successful native Windows prerequisite preflight",
    )
    args = parser.parse_args(argv)
    if platform_name == "win32":
        if not args.preflight_workspace:
            _write_preflight_report(
                preflight_protocol_failure_report(
                    "native Windows session startup requires a prerequisite-preflight workspace"
                ).as_dict()
            )
            return 2
        preflight_report: WindowsReportDict
        try:
            preflight_report = _run_native_windows_session_preflight(args.preflight_workspace)
        except Exception:
            preflight_report = preflight_protocol_failure_report(
                "the native Windows session prerequisite preflight could not complete"
            ).as_dict()
        if not preflight_report["can_start_broker"]:
            _write_preflight_report(preflight_report)
            return 2
        _write_preflight_report(preflight_report)
    broker = SessionBroker(args.data_dir, args.agent_runtime, args.preflight_workspace)
    for line in sys.stdin if input_stream is None else input_stream:
        response: JsonObject
        try:
            response = broker.dispatch(_json_object(line))
        except GoodJobError as exc:
            response = {"status": "error", "code": exc.code, "message": str(exc)}
        except (UnicodeError, OSError):
            response = {
                "status": "error",
                "code": "invalid_input",
                "message": "session broker input could not be processed safely",
            }
        print(json.dumps(response, ensure_ascii=True, sort_keys=True), flush=True)
    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
