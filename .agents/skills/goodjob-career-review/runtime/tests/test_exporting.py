from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from test_reporting import _authorize, _dict, _list, _prepare_and_analyze, _workspace
from windows_process_fakes import FakeProcessApi

from goodjob.db import Database
from goodjob.errors import InvalidInputError, WriterBusyError
from goodjob.exporting import ExportService, _InjectedExportInterruption
from goodjob.locks import ExclusiveWriterLock
from goodjob.paths import DataPaths
from goodjob.platform import handles_windows, process_windows
from goodjob.platform.handles_windows import OwnedHandle
from goodjob.reporting import ArtifactSnapshotService


def _is_read_only(path: Path, *, directory: bool = False) -> bool:
    mode = stat.S_IMODE(path.stat().st_mode)
    if sys.platform == "win32":
        return not mode & stat.S_IWUSR
    expected = stat.S_IRUSR | (stat.S_IXUSR if directory else 0)
    return mode == expected


_ABRUPT_EXIT_CODE = 86

_ABRUPT_EXIT_EXPORT_SCRIPT = r"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])

import goodjob.safe_fs as safe_fs
from goodjob.db import Database
from goodjob.exporting import ExportService
from goodjob.paths import DataPaths

if sys.platform == "win32":
    import goodjob.platform.detect as platform_detect

    platform_detect.NATIVE_WINDOWS_RELEASE_ENABLED = True

database = Database(DataPaths(Path(sys.argv[2])))
workspace = Path(sys.argv[3])
receipt_id = sys.argv[4]
fault_at = sys.argv[5]
exit_code = int(sys.argv[6])
request = json.loads(sys.stdin.buffer.read())
service = ExportService(database)

if fault_at == "after_temp":
    original_verify = ExportService._verify_rendered_files

    def kill_after_temp(self, relative, manifest):
        original_verify(self, relative, manifest)
        os._exit(exit_code)

    ExportService._verify_rendered_files = kill_after_temp
elif fault_at == "after_rename":
    if sys.platform == "win32":
        import goodjob.platform.fs_windows as fs_windows

        original_rename = fs_windows._rename_handle

        def kill_after_rename(*args, **kwargs):
            original_rename(*args, **kwargs)
            os._exit(exit_code)

        fs_windows._rename_handle = kill_after_rename
    else:
        original_rename = safe_fs.os.rename

        def kill_after_rename(*args, **kwargs):
            original_rename(*args, **kwargs)
            os._exit(exit_code)

        safe_fs.os.rename = kill_after_rename
elif fault_at == "before_database_commit":
    def kill_before_database_commit(self, connection, attempt, manifest_sha256):
        os._exit(exit_code)

    ExportService._record_success = kill_before_database_commit
else:
    raise RuntimeError("unknown abrupt-exit test stage")

service.translate_export(
    workspace_path=workspace,
    authorization_receipt_id=receipt_id,
    request_value=request,
)
raise RuntimeError("export unexpectedly survived abrupt-exit hook")
"""


def _published_snapshot(
    tmp_path: Path,
    data_paths: DataPaths,
    *,
    claim_statement_prefix: str | None = None,
    technology_identifiers: list[str] | None = None,
) -> tuple[Path, Database, str, dict[str, object]]:
    workspace = _workspace(tmp_path)
    database = Database(data_paths)
    receipt_id = _authorize(database, workspace)
    run_id, _ = _prepare_and_analyze(
        database,
        workspace,
        receipt_id,
        claim_statement_prefix=claim_statement_prefix,
        technology_identifiers=technology_identifiers,
    )
    snapshot = _dict(ArtifactSnapshotService(database).render(run_id)["artifact_snapshot"])
    return workspace, database, receipt_id, snapshot


def _interrupted_export_attempt(
    tmp_path: Path,
    data_paths: DataPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Database, Path, Path]:
    workspace, database, receipt_id, snapshot = _published_snapshot(tmp_path, data_paths)
    service = ExportService(database)
    source = _prepare_translation(service, workspace, receipt_id, snapshot)
    monkeypatch.setattr(
        "goodjob.exporting.process_identity",
        lambda: "pid:321;started:100",
    )
    with pytest.raises(_InjectedExportInterruption):
        service.translate_export(
            workspace_path=workspace,
            authorization_receipt_id=receipt_id,
            request_value=_translation_request(source),
            _fault_at="after_temp",
        )
    connection = sqlite3.connect(data_paths.database_file)
    try:
        row = connection.execute(
            "SELECT temp_relative_path, final_relative_path FROM export_attempts"
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    return database, data_paths.root / row[0], data_paths.root / row[1]


def _install_windows_process_probe(
    monkeypatch: pytest.MonkeyPatch,
    api: FakeProcessApi,
    *,
    last_error_value: int = 0,
    closer: Callable[[int], None] | None = None,
) -> None:
    monkeypatch.setattr(
        "goodjob.process_identity.sys",
        SimpleNamespace(platform="win32"),
    )
    monkeypatch.setattr(process_windows, "load_windows_dll", lambda _name: api)
    monkeypatch.setattr(process_windows, "last_error", lambda: last_error_value)
    close = api.closed.append if closer is None else closer
    monkeypatch.setattr(
        process_windows,
        "OwnedHandle",
        lambda value: OwnedHandle(value, closer=close),
    )


def _assert_interrupted_export_state(
    data_paths: DataPaths,
    temp_path: Path,
    final_path: Path,
    *,
    recovered: bool,
) -> None:
    connection = sqlite3.connect(data_paths.database_file)
    try:
        state = connection.execute(
            "SELECT status, finished_at, error_summary FROM export_attempts"
        ).fetchone()
        export_count = connection.execute("SELECT COUNT(*) FROM derived_exports").fetchone()
    finally:
        connection.close()
    assert state is not None
    if recovered:
        assert state[0] == "interrupted"
        assert state[1]
        assert "owner process stopped" in state[2]
        assert not temp_path.exists()
    else:
        assert state == ("running", None, None)
        assert temp_path.is_dir()
    assert not final_path.exists()
    assert export_count == (0,)


def _translation_request(
    source: dict[str, object],
    *,
    items: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    source_items = [_dict(value) for value in _list(source["items"])]
    candidates: list[dict[str, object]] = []
    for item in source_items:
        target = (
            {
                "text": (
                    "Explains a testable Python function boundary while preserving "
                    "the recorded value (1)."
                )
            }
            if item["export_kind"] == "resume"
            else {
                "question": "How is the testable function boundary implemented?",
                "answer": "It uses Python and preserves the recorded value (1).",
            }
        )
        candidates.append(
            {
                "source_item_id": item["source_item_id"],
                "export_kind": item["export_kind"],
                "claim_refs": item["claim_refs"],
                "evidence_refs": item["evidence_refs"],
                "role_lens_refs": item["role_lens_refs"],
                "anchors": item["anchors"],
                "project_id": item["project_id"],
                "module_id": item["module_id"],
                "target": target,
            }
        )
    return {
        "contract_version": "translation-export-request-v1",
        "action": "publish",
        "source_artifact_snapshot_id": source["source_artifact_snapshot_id"],
        "source_projection_sha256": source["source_projection_sha256"],
        "target_language": "en",
        "export_kinds": ["resume", "interview_qa"],
        "items": items if items is not None else candidates,
    }


def _prepare_translation(
    service: ExportService,
    workspace: Path,
    receipt_id: str,
    snapshot: dict[str, object],
) -> dict[str, object]:
    prepared = service.translate_export(
        workspace_path=workspace,
        authorization_receipt_id=receipt_id,
        request_value={
            "contract_version": "translation-export-request-v1",
            "action": "prepare",
            "source_artifact_snapshot_id": snapshot["artifact_snapshot_id"],
            "target_language": "en",
            "export_kinds": ["resume", "interview_qa"],
        },
    )
    return _dict(prepared["translation_source"])


def test_translation_prepare_reads_one_frozen_projection_without_writing_export_state(
    tmp_path: Path,
    data_paths: DataPaths,
) -> None:
    workspace, database, receipt_id, snapshot = _published_snapshot(tmp_path, data_paths)

    prepared = ExportService(database).translate_export(
        workspace_path=workspace,
        authorization_receipt_id=receipt_id,
        request_value={
            "contract_version": "translation-export-request-v1",
            "action": "prepare",
            "source_artifact_snapshot_id": snapshot["artifact_snapshot_id"],
            "target_language": "en",
            "export_kinds": ["resume", "interview_qa"],
        },
    )

    source = _dict(prepared["translation_source"])
    items = [_dict(value) for value in _list(source["items"])]
    assert source["source_artifact_snapshot_id"] == snapshot["artifact_snapshot_id"]
    assert source["source_report_bundle_sha256"] == snapshot["report_bundle_sha256"]
    assert isinstance(source["source_projection_sha256"], str)
    assert {item["export_kind"] for item in items} == {"resume", "interview_qa"}
    assert all(isinstance(item["source_text"], str) and item["source_text"] for item in items)
    assert all(
        set(item)
        == {
            "source_item_id",
            "export_kind",
            "source_text",
            "claim_refs",
            "evidence_refs",
            "role_lens_refs",
            "anchors",
            "project_id",
            "module_id",
        }
        for item in items
    )
    assert not any(data_paths.export_tmp_dir.iterdir())
    assert not [path for path in data_paths.exports_dir.iterdir() if path.name != ".tmp"]
    connection = sqlite3.connect(data_paths.database_file)
    try:
        assert connection.execute("SELECT COUNT(*) FROM export_attempts").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM derived_exports").fetchone() == (0,)
    finally:
        connection.close()
    assert Path(cast(str, snapshot["html_path"])).is_file()


def test_translation_publish_atomically_creates_one_immutable_derived_export(
    tmp_path: Path,
    data_paths: DataPaths,
) -> None:
    workspace, database, receipt_id, snapshot = _published_snapshot(tmp_path, data_paths)
    service = ExportService(database)
    latest_before = data_paths.latest_artifact_file.read_bytes()
    source_files = {
        key: Path(cast(str, snapshot[key])).read_bytes()
        for key in ("report_markdown_path", "resume_markdown_path", "html_path")
    }
    source = _prepare_translation(service, workspace, receipt_id, snapshot)

    published = service.translate_export(
        workspace_path=workspace,
        authorization_receipt_id=receipt_id,
        request_value=_translation_request(source),
    )

    derived = _dict(published["derived_export"])
    output_path = Path(cast(str, derived["output_path"]))
    resume_path = Path(cast(str, derived["resume_markdown_path"]))
    interview_path = Path(cast(str, derived["interview_qa_markdown_path"]))
    manifest_path = Path(cast(str, derived["manifest_path"]))
    assert output_path.is_dir()
    assert {path.name for path in output_path.iterdir()} == {
        "resume.en.md",
        "interview.en.md",
        "manifest.json",
    }
    assert all(path.is_file() for path in (resume_path, interview_path, manifest_path))
    assert all(_is_read_only(path) for path in (resume_path, interview_path, manifest_path))
    assert _is_read_only(output_path, directory=True)
    assert "Python" in resume_path.read_text(encoding="utf-8")
    assert "How is" in interview_path.read_text(encoding="utf-8")
    assert not list(output_path.glob("*.html"))
    manifest = cast(
        dict[str, object],
        json.loads(manifest_path.read_text(encoding="utf-8")),
    )
    assert manifest["source_artifact_snapshot_id"] == snapshot["artifact_snapshot_id"]
    assert manifest["source_report_bundle_sha256"] == snapshot["report_bundle_sha256"]
    assert manifest["source_projection_sha256"] == source["source_projection_sha256"]
    mappings = [_dict(value) for value in _list(manifest["items"])]
    assert {mapping["source_item_id"] for mapping in mappings} == {
        item["source_item_id"] for item in map(_dict, _list(source["items"]))
    }
    assert data_paths.latest_artifact_file.read_bytes() == latest_before
    assert all(
        Path(cast(str, snapshot[key])).read_bytes() == content
        for key, content in source_files.items()
    )
    assert not any(data_paths.export_tmp_dir.iterdir())
    connection = sqlite3.connect(data_paths.database_file)
    try:
        assert connection.execute("SELECT status FROM export_attempts").fetchone() == ("succeeded",)
        row = connection.execute(
            """
            SELECT source_artifact_snapshot_id, source_report_bundle_sha256,
                   source_projection_sha256, language, export_kinds, output_path
            FROM derived_exports
            """
        ).fetchone()
    finally:
        connection.close()
    assert row == (
        snapshot["artifact_snapshot_id"],
        snapshot["report_bundle_sha256"],
        source["source_projection_sha256"],
        "en",
        '["resume","interview_qa"]',
        f"exports/{derived['derived_export_id']}",
    )


@pytest.mark.parametrize(
    "damage",
    [
        "missing_item",
        "extra_item",
        "anchor_tamper",
        "technology_anchor_tamper",
        "number_omission",
    ],
)
def test_translation_publish_rejects_non_equivalent_candidate_batches_without_writes(
    tmp_path: Path,
    data_paths: DataPaths,
    damage: str,
) -> None:
    workspace, database, receipt_id, snapshot = _published_snapshot(tmp_path, data_paths)
    service = ExportService(database)
    source = _prepare_translation(service, workspace, receipt_id, snapshot)
    request = _translation_request(source)
    items = cast(list[dict[str, object]], request["items"])
    if damage == "missing_item":
        request["items"] = items[:-1]
    elif damage == "extra_item":
        extra = cast(dict[str, object], json.loads(json.dumps(items[0])))
        extra["source_item_id"] = "unknown-source-item"
        request["items"] = [*items, extra]
    elif damage == "anchor_tamper":
        anchors = _dict(items[0]["anchors"])
        anchors["numbers_and_units"] = [*_list(anchors["numbers_and_units"]), "999"]
    elif damage == "technology_anchor_tamper":
        anchors = _dict(items[0]["anchors"])
        anchors["technology_identifiers"] = [
            *_list(anchors["technology_identifiers"]),
            "MongoDB",
        ]
    elif damage == "number_omission":
        target = _dict(items[0]["target"])
        for key in target:
            target[key] = "Explains the testable Python function boundary without a new fact."

    latest_before = data_paths.latest_artifact_file.read_bytes()
    with pytest.raises(InvalidInputError):
        service.translate_export(
            workspace_path=workspace,
            authorization_receipt_id=receipt_id,
            request_value=request,
        )

    assert data_paths.latest_artifact_file.read_bytes() == latest_before
    assert not any(data_paths.export_tmp_dir.iterdir())
    assert not [path for path in data_paths.exports_dir.iterdir() if path.name != ".tmp"]
    connection = sqlite3.connect(data_paths.database_file)
    try:
        assert connection.execute("SELECT COUNT(*) FROM export_attempts").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM derived_exports").fetchone() == (0,)
    finally:
        connection.close()


def test_translation_numeric_equivalence_preserves_sign_thousands_separator_and_units(
    tmp_path: Path,
    data_paths: DataPaths,
) -> None:
    workspace, database, receipt_id, snapshot = _published_snapshot(
        tmp_path,
        data_paths,
        claim_statement_prefix=("我可以解释 Python 边界如何接受 -5 个到 1,000 个输入，并安全展示 "),
    )
    service = ExportService(database)
    source = _prepare_translation(service, workspace, receipt_id, snapshot)
    request = _translation_request(source)
    items = cast(list[dict[str, object]], request["items"])
    for item in items:
        item["target"] = (
            {"text": ("The Python boundary accepts -5 items to 1,000 items and preserves value 1.")}
            if item["export_kind"] == "resume"
            else {
                "question": "What range does the Python boundary accept?",
                "answer": "It accepts -5 items to 1,000 items and preserves value 1.",
            }
        )

    published = service.translate_export(
        workspace_path=workspace,
        authorization_receipt_id=receipt_id,
        request_value=request,
    )
    assert (
        _dict(published["derived_export"])["source_projection_sha256"]
        == source["source_projection_sha256"]
    )

    changed_sign = cast(dict[str, object], json.loads(json.dumps(request)))
    changed_items = cast(list[dict[str, object]], changed_sign["items"])
    first_target = _dict(changed_items[0]["target"])
    for key, value in first_target.items():
        first_target[key] = str(value).replace("-5", "5")
    with pytest.raises(InvalidInputError, match="numeric or unit"):
        service.translate_export(
            workspace_path=workspace,
            authorization_receipt_id=receipt_id,
            request_value=changed_sign,
        )


def test_translation_accepts_technology_aliases_without_changing_structured_anchors(
    tmp_path: Path,
    data_paths: DataPaths,
) -> None:
    workspace, database, receipt_id, snapshot = _published_snapshot(
        tmp_path,
        data_paths,
        claim_statement_prefix=("我可以解释可测试的 TypeScript 函数边界，并安全展示记录值 "),
        technology_identifiers=["typescript"],
    )
    service = ExportService(database)
    source = _prepare_translation(service, workspace, receipt_id, snapshot)
    request = _translation_request(source)
    for item in cast(list[dict[str, object]], request["items"]):
        item["target"] = (
            {
                "text": (
                    "Explains a testable TS function boundary while preserving "
                    "the recorded value (1)."
                )
            }
            if item["export_kind"] == "resume"
            else {
                "question": "How is the testable TS function boundary implemented?",
                "answer": "It uses TS and preserves the recorded value (1).",
            }
        )

    published = service.translate_export(
        workspace_path=workspace,
        authorization_receipt_id=receipt_id,
        request_value=request,
    )

    assert (
        _dict(published["derived_export"])["source_projection_sha256"]
        == source["source_projection_sha256"]
    )


def test_translation_does_not_infer_structured_technology_anchors_from_prose(
    tmp_path: Path,
    data_paths: DataPaths,
) -> None:
    workspace, database, receipt_id, snapshot = _published_snapshot(tmp_path, data_paths)
    service = ExportService(database)
    source = _prepare_translation(service, workspace, receipt_id, snapshot)
    request = _translation_request(source)
    resume = next(
        item
        for item in cast(list[dict[str, object]], request["items"])
        if item["export_kind"] == "resume"
    )
    resume["target"] = {
        "text": (
            "Go through the testable Python function boundary, React to failures, "
            "and preserve the recorded value (1)."
        )
    }

    published = service.translate_export(
        workspace_path=workspace,
        authorization_receipt_id=receipt_id,
        request_value=request,
    )

    assert _dict(published["derived_export"])["language"] == "en"


def test_translation_rejects_unrecoverable_process_identity_before_any_attempt_write(
    tmp_path: Path,
    data_paths: DataPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, database, receipt_id, snapshot = _published_snapshot(tmp_path, data_paths)
    service = ExportService(database)
    source = _prepare_translation(service, workspace, receipt_id, snapshot)
    monkeypatch.setattr(
        "goodjob.exporting.process_identity",
        lambda: f"pid:{os.getpid()};started:unknown",
    )

    with pytest.raises(InvalidInputError, match="process start marker"):
        service.translate_export(
            workspace_path=workspace,
            authorization_receipt_id=receipt_id,
            request_value=_translation_request(source),
        )

    assert not any(data_paths.export_tmp_dir.iterdir())
    assert not [path for path in data_paths.exports_dir.iterdir() if path.name != ".tmp"]
    connection = sqlite3.connect(data_paths.database_file)
    try:
        assert connection.execute("SELECT COUNT(*) FROM export_attempts").fetchone() == (0,)
    finally:
        connection.close()


def test_translation_publish_rejects_snapshot_tampering_after_prepare_without_attempt(
    tmp_path: Path,
    data_paths: DataPaths,
) -> None:
    workspace, database, receipt_id, snapshot = _published_snapshot(tmp_path, data_paths)
    service = ExportService(database)
    source = _prepare_translation(service, workspace, receipt_id, snapshot)
    html_path = Path(cast(str, snapshot["html_path"]))
    html_path.parent.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    html_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    html_path.write_text("tampered after translation prepare", encoding="utf-8")

    with pytest.raises(InvalidInputError, match="digest does not match"):
        service.translate_export(
            workspace_path=workspace,
            authorization_receipt_id=receipt_id,
            request_value=_translation_request(source),
        )

    connection = sqlite3.connect(data_paths.database_file)
    try:
        assert connection.execute("SELECT COUNT(*) FROM export_attempts").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM derived_exports").fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.parametrize("fault_at", ["after_temp", "after_publish", "before_database_commit"])
def test_dead_export_owner_recovery_cleans_only_registered_paths_and_retries_fresh(
    tmp_path: Path,
    data_paths: DataPaths,
    monkeypatch: pytest.MonkeyPatch,
    fault_at: str,
) -> None:
    workspace, database, receipt_id, snapshot = _published_snapshot(tmp_path, data_paths)
    service = ExportService(database)
    source = _prepare_translation(service, workspace, receipt_id, snapshot)
    request = _translation_request(source)
    first_success = _dict(
        service.translate_export(
            workspace_path=workspace,
            authorization_receipt_id=receipt_id,
            request_value=request,
        )["derived_export"]
    )
    first_output = Path(cast(str, first_success["output_path"]))
    first_manifest = Path(cast(str, first_success["manifest_path"])).read_bytes()
    unknown = data_paths.exports_dir / "owner-unknown-directory"
    unknown.mkdir()
    (unknown / "keep.txt").write_text("keep", encoding="utf-8")
    latest_before = data_paths.latest_artifact_file.read_bytes()
    monkeypatch.setattr(
        "goodjob.exporting.process_identity",
        lambda: "pid:999999;started:Thu Jan  1 00:00:00 1970",
    )

    with pytest.raises(_InjectedExportInterruption):
        service.translate_export(
            workspace_path=workspace,
            authorization_receipt_id=receipt_id,
            request_value=request,
            _fault_at=fault_at,
        )
    connection = sqlite3.connect(data_paths.database_file)
    try:
        interrupted_row = connection.execute(
            """
            SELECT export_attempt_id, derived_export_id, temp_relative_path,
                   final_relative_path, status
            FROM export_attempts ORDER BY rowid DESC LIMIT 1
            """
        ).fetchone()
    finally:
        connection.close()
    assert interrupted_row is not None
    old_attempt_id, old_export_id, temp_relative, final_relative, status = interrupted_row
    assert status == "running"
    if fault_at == "after_temp":
        assert (data_paths.root / temp_relative).is_dir()
        assert not (data_paths.root / final_relative).exists()
    else:
        assert not (data_paths.root / temp_relative).exists()
        assert (data_paths.root / final_relative).is_dir()

    retried = _dict(
        service.translate_export(
            workspace_path=workspace,
            authorization_receipt_id=receipt_id,
            request_value=request,
        )["derived_export"]
    )

    assert retried["export_attempt_id"] != old_attempt_id
    assert retried["derived_export_id"] != old_export_id
    assert not (data_paths.root / temp_relative).exists()
    assert not (data_paths.root / final_relative).exists()
    assert first_output.is_dir()
    assert Path(cast(str, first_success["manifest_path"])).read_bytes() == first_manifest
    assert (unknown / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert data_paths.latest_artifact_file.read_bytes() == latest_before
    connection = sqlite3.connect(data_paths.database_file)
    try:
        statuses = connection.execute(
            "SELECT status FROM export_attempts ORDER BY rowid"
        ).fetchall()
        export_count = connection.execute("SELECT COUNT(*) FROM derived_exports").fetchone()
    finally:
        connection.close()
    assert statuses == [("succeeded",), ("interrupted",), ("succeeded",)]
    assert export_count == (2,)


@pytest.mark.parametrize("fault_at", ["after_temp", "after_rename", "before_database_commit"])
def test_real_abrupt_exit_export_is_recovered_by_the_next_writer_entry(
    tmp_path: Path,
    data_paths: DataPaths,
    fault_at: str,
    exclusive_outside_sentinel: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.platform == "win32":
        import goodjob.platform.detect as platform_detect

        monkeypatch.setattr(platform_detect, "NATIVE_WINDOWS_RELEASE_ENABLED", True)
    workspace, database, receipt_id, snapshot = _published_snapshot(tmp_path, data_paths)
    service = ExportService(database)
    source = _prepare_translation(service, workspace, receipt_id, snapshot)
    request = _translation_request(source)
    first_success = _dict(
        service.translate_export(
            workspace_path=workspace,
            authorization_receipt_id=receipt_id,
            request_value=request,
        )["derived_export"]
    )
    first_output = Path(cast(str, first_success["output_path"]))
    first_manifest_path = Path(cast(str, first_success["manifest_path"]))
    first_manifest = first_manifest_path.read_bytes()
    unknown = data_paths.exports_dir / "owner-unknown-directory"
    unknown.mkdir()
    (unknown / "keep.txt").write_text("keep", encoding="utf-8")
    runtime_source = Path(__file__).resolve().parents[1] / "src"

    killed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            _ABRUPT_EXIT_EXPORT_SCRIPT,
            str(runtime_source),
            str(data_paths.root),
            str(workspace),
            receipt_id,
            fault_at,
            str(_ABRUPT_EXIT_CODE),
        ],
        input=json.dumps(request).encode("utf-8"),
        capture_output=True,
        check=False,
        timeout=15,
    )

    assert killed.returncode == _ABRUPT_EXIT_CODE, killed.stderr.decode("utf-8", "replace")
    connection = sqlite3.connect(data_paths.database_file)
    try:
        row = connection.execute(
            """
            SELECT export_attempt_id, temp_relative_path, final_relative_path, status
            FROM export_attempts ORDER BY rowid DESC LIMIT 1
            """
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    attempt_id, temp_relative, final_relative, status = row
    assert status == "running"
    if fault_at == "after_temp":
        assert (data_paths.root / temp_relative).is_dir()
        assert not (data_paths.root / final_relative).exists()
    else:
        assert not (data_paths.root / temp_relative).exists()
        assert (data_paths.root / final_relative).is_dir()

    # Schema migration is a normal writer entry unrelated to English export publication.
    database.migrate()

    assert not (data_paths.root / temp_relative).exists()
    assert not (data_paths.root / final_relative).exists()
    connection = sqlite3.connect(data_paths.database_file)
    try:
        recovered = connection.execute(
            """
            SELECT status, finished_at, error_summary
            FROM export_attempts WHERE export_attempt_id = ?
            """,
            (attempt_id,),
        ).fetchone()
        assert connection.execute(
            "SELECT COUNT(*) FROM derived_exports WHERE export_attempt_id = ?",
            (attempt_id,),
        ).fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM derived_exports").fetchone() == (1,)
    finally:
        connection.close()
    assert recovered is not None
    assert recovered[0] == "interrupted"
    assert recovered[1]
    assert "owner process stopped" in recovered[2]
    assert first_output.is_dir()
    assert first_manifest_path.read_bytes() == first_manifest
    assert (unknown / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_export_recovery_does_not_interrupt_or_clean_an_unproven_live_owner(
    tmp_path: Path,
    data_paths: DataPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, database, receipt_id, snapshot = _published_snapshot(tmp_path, data_paths)
    service = ExportService(database)
    source = _prepare_translation(service, workspace, receipt_id, snapshot)
    request = _translation_request(source)
    monkeypatch.setattr(
        "goodjob.exporting.process_identity",
        lambda: f"pid:{os.getpid()};started:unverified-test-marker",
    )
    with pytest.raises(_InjectedExportInterruption):
        service.translate_export(
            workspace_path=workspace,
            authorization_receipt_id=receipt_id,
            request_value=request,
            _fault_at="after_temp",
        )
    connection = sqlite3.connect(data_paths.database_file)
    try:
        row = connection.execute(
            "SELECT export_attempt_id, temp_relative_path FROM export_attempts"
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    old_attempt_id, temp_relative = row
    assert (data_paths.root / temp_relative).is_dir()
    monkeypatch.setattr("goodjob.recovery.owner_process_stopped", lambda _identity: False)

    succeeded = _dict(
        service.translate_export(
            workspace_path=workspace,
            authorization_receipt_id=receipt_id,
            request_value=request,
        )["derived_export"]
    )

    assert succeeded["export_attempt_id"] != old_attempt_id
    assert (data_paths.root / temp_relative).is_dir()
    connection = sqlite3.connect(data_paths.database_file)
    try:
        statuses = connection.execute(
            "SELECT status FROM export_attempts ORDER BY rowid"
        ).fetchall()
    finally:
        connection.close()
    assert statuses == [("running",), ("succeeded",)]


@pytest.mark.parametrize(
    (
        "wait_result",
        "creation_marker",
        "open_result",
        "last_error_value",
        "recovered",
        "error_match",
    ),
    [
        (process_windows.WAIT_OBJECT_0, 100, 701, 0, True, None),
        (process_windows.WAIT_TIMEOUT, 100, 701, 0, False, None),
        (process_windows.WAIT_TIMEOUT, 101, 701, 0, True, None),
        (
            process_windows.WAIT_TIMEOUT,
            100,
            0,
            process_windows.ERROR_ACCESS_DENIED,
            False,
            None,
        ),
        (process_windows.WAIT_FAILED, 100, 701, 6, False, "WaitForSingleObject"),
        (7, 100, 701, 0, False, "unexpected status: 7"),
    ],
    ids=[
        "openable-signaled-recovers",
        "live-timeout-preserves",
        "pid-reuse-recovers",
        "access-denied-preserves",
        "wait-failed-preserves",
        "unknown-wait-preserves",
    ],
)
def test_windows_process_result_controls_export_recovery_state_and_paths(
    tmp_path: Path,
    data_paths: DataPaths,
    monkeypatch: pytest.MonkeyPatch,
    wait_result: int,
    creation_marker: int,
    open_result: int,
    last_error_value: int,
    recovered: bool,
    error_match: str | None,
) -> None:
    database, temp_path, final_path = _interrupted_export_attempt(
        tmp_path,
        data_paths,
        monkeypatch,
    )
    api = FakeProcessApi(
        wait_result,
        creation_marker=creation_marker,
        open_result=open_result,
    )
    _install_windows_process_probe(
        monkeypatch,
        api,
        last_error_value=last_error_value,
    )

    if error_match is None:
        database.migrate()
    else:
        with pytest.raises(OSError, match=error_match):
            database.migrate()

    _assert_interrupted_export_state(
        data_paths,
        temp_path,
        final_path,
        recovered=recovered,
    )
    assert handles_windows._RETAINED_OWNERS == []


def test_windows_close_failure_defers_export_recovery_until_owner_retry(
    tmp_path: Path,
    data_paths: DataPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, temp_path, final_path = _interrupted_export_attempt(
        tmp_path,
        data_paths,
        monkeypatch,
    )
    api = FakeProcessApi(process_windows.WAIT_OBJECT_0)
    close_attempts: list[int] = []

    try:

        def close_fails_once(value: int) -> None:
            close_attempts.append(value)
            if len(close_attempts) == 1:
                raise OSError("injected process handle close failure")

        _install_windows_process_probe(monkeypatch, api, closer=close_fails_once)

        with pytest.raises(OSError, match="process handle close failure"):
            database.migrate()

        _assert_interrupted_export_state(
            data_paths,
            temp_path,
            final_path,
            recovered=False,
        )
        assert len(handles_windows._RETAINED_OWNERS) == 1

        handles_windows.retry_retained_owners()
        assert handles_windows._RETAINED_OWNERS == []
        database.migrate()

        _assert_interrupted_export_state(
            data_paths,
            temp_path,
            final_path,
            recovered=True,
        )
        assert close_attempts == [701, 701, 701]
    finally:
        handles_windows.retry_retained_owners()


def test_translation_publish_writer_busy_creates_no_attempt_or_files(
    tmp_path: Path,
    data_paths: DataPaths,
) -> None:
    workspace, database, receipt_id, snapshot = _published_snapshot(tmp_path, data_paths)
    service = ExportService(database)
    source = _prepare_translation(service, workspace, receipt_id, snapshot)

    with (
        ExclusiveWriterLock(data_paths.writer_lock_file),
        pytest.raises(WriterBusyError),
    ):
        service.translate_export(
            workspace_path=workspace,
            authorization_receipt_id=receipt_id,
            request_value=_translation_request(source),
        )

    assert not any(data_paths.export_tmp_dir.iterdir())
    assert not [path for path in data_paths.exports_dir.iterdir() if path.name != ".tmp"]
    connection = sqlite3.connect(data_paths.database_file)
    try:
        assert connection.execute("SELECT COUNT(*) FROM export_attempts").fetchone() == (0,)
    finally:
        connection.close()


def test_normal_export_failure_is_diagnostic_and_leaves_no_visible_partial_output(
    tmp_path: Path,
    data_paths: DataPaths,
) -> None:
    workspace, database, receipt_id, snapshot = _published_snapshot(tmp_path, data_paths)
    service = ExportService(database)
    source = _prepare_translation(service, workspace, receipt_id, snapshot)
    latest_before = data_paths.latest_artifact_file.read_bytes()

    with pytest.raises(InvalidInputError, match="ExportAttempt diagnostic"):
        service.translate_export(
            workspace_path=workspace,
            authorization_receipt_id=receipt_id,
            request_value=_translation_request(source),
            _fault_at="fail_after_temp",
        )

    assert data_paths.latest_artifact_file.read_bytes() == latest_before
    assert not any(data_paths.export_tmp_dir.iterdir())
    assert not [path for path in data_paths.exports_dir.iterdir() if path.name != ".tmp"]
    connection = sqlite3.connect(data_paths.database_file)
    try:
        row = connection.execute(
            "SELECT status, finished_at, error_summary FROM export_attempts"
        ).fetchone()
        assert connection.execute("SELECT COUNT(*) FROM derived_exports").fetchone() == (0,)
    finally:
        connection.close()
    assert row is not None
    assert row[0] == "failed"
    assert row[1]
    assert "injected normal export failure" in row[2]
