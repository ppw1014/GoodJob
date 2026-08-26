from __future__ import annotations

import json
import sqlite3
import stat
import sys
import uuid
from pathlib import Path
from typing import cast

import pytest

import goodjob.reporting as reporting
from goodjob.analysis import AnalysisService
from goodjob.auth import AuthorizationRepository, AuthorizationRequest, generate_capability
from goodjob.db import Database
from goodjob.errors import InvalidInputError, WriterBusyError
from goodjob.locks import ExclusiveWriterLock
from goodjob.paths import DataPaths
from goodjob.preparation import PreparationService, validate_job_input
from goodjob.reporting import (
    _EMBEDDED_JSON_ESCAPES,
    ArtifactSnapshotService,
    ReportBundleBuilder,
    _artifact_snapshot_id,
    _embedded_json,
    _validate_embedded_json,
    canonical_report_bundle,
    render_dashboard_html,
    render_report_markdown,
    report_bundle_sha256,
)
from goodjob.review import ReviewService
from goodjob.scanner import WorkspaceScanner


def _is_read_only(path: Path, *, directory: bool = False) -> bool:
    mode = stat.S_IMODE(path.stat().st_mode)
    if sys.platform == "win32":
        return not mode & stat.S_IWUSR
    expected = stat.S_IRUSR | (stat.S_IXUSR if directory else 0)
    return mode == expected


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "dashboard-injection-onerror"
    (workspace / "tests").mkdir(parents=True)
    (workspace / "pyproject.toml").write_text(
        '[project]\nname = "reporting-demo"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    (workspace / "app.py").write_text(
        "def calculate(value: int) -> int:\n    return value + 1\n",
        encoding="utf-8",
    )
    (workspace / "tests" / "test_app.py").write_text(
        "from app import calculate\n\ndef test_calculate():\n    assert calculate(1) == 2\n",
        encoding="utf-8",
    )
    return workspace


def _authorize(database: Database, workspace: Path) -> str:
    receipt = AuthorizationRepository(database).issue(
        capability=generate_capability(),
        request=AuthorizationRequest.from_values(
            receipt_kind="source_analysis",
            scope={
                "workspace_path": str(workspace.resolve()),
                "allowed_categories": ["source_analysis"],
            },
            notice_version="goodjob-source-analysis-v1",
        ),
    )
    return receipt.authorization_receipt_id


def _lens() -> dict[str, object]:
    return {
        "contract_version": "role-lens-v1",
        "dimensions": [
            {
                "key": "implementation_depth",
                "display_name": "实现深度",
                "weight_bps": 6000,
                "evaluation_criteria": "评价实际实现机制与边界",
                "required_evidence_kinds": ["implementation"],
            },
            {
                "key": "verification",
                "display_name": "验证能力",
                "weight_bps": 4000,
                "evaluation_criteria": "评价测试定义与验证路径",
                "required_evidence_kinds": ["test_definition"],
            },
        ],
        "evidence_requirements": ["当前实现", "测试定义"],
        "ranking_rules": ["证据覆盖与岗位相关性共同决定排序"],
        "output_sections": ["项目讲解", "学习要点", "简历材料", "面试题库"],
        "question_strategy": {"primary": "追问实现机制、验证边界与替代方案"},
        "gap_rules": ["缺少角色或结果时保留知识缺口"],
        "assumptions": ["未提供 JD"],
        "generator_id": "reporting-test",
        "prompt_contract_version": "reporting-test-v1",
    }


def _dict(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _list(value: object) -> list[object]:
    assert isinstance(value, list)
    return cast(list[object], value)


def _prepare_and_analyze(
    database: Database,
    workspace: Path,
    receipt_id: str,
    *,
    scan_run_id: str | None = None,
    role_name: str = "应用软件工程师 </script><img onerror=alert(1)> \u2028\u2029\u202e",
    knowledge_gap_severity: str | None = None,
    claim_statement_prefix: str | None = None,
    verified_semantic_key: str | None = None,
    technology_identifiers: list[str] | None = None,
) -> tuple[str, str]:
    if scan_run_id is None:
        scan = WorkspaceScanner(database).scan(
            workspace_path=str(workspace),
            config_revision="reporting-scan-v1",
            authorization_receipt_id=receipt_id,
        )
        scan_run_id = scan.scan_run_id
    validated = _dict(
        validate_job_input(
            {
                "contract_version": "job-input-v1",
                "target_role": role_name,
                "jd_input": {"kind": "none"},
            }
        )["job_input"]
    )
    prepared = PreparationService(database).start(
        workspace_path=workspace,
        authorization_receipt_id=receipt_id,
        request_value={
            "contract_version": "preparation-request-v1",
            "request_id": str(uuid.uuid4()),
            "scan_run_id": scan_run_id,
            "config_revision": "reporting-prepare-v1",
            "target_role": role_name,
            "jd_input": {"kind": "none"},
            "job_input_validation_sha256": validated["validation_sha256"],
            "requested_exports": [],
            "evidence_limit_per_project": 100,
            "role_lens": _lens(),
        },
    )
    run = _dict(prepared["preparation_run"])
    lens = _dict(prepared["role_lens"])
    evidence_bundle = _dict(prepared["evidence_bundle"])
    coverage = [_dict(value) for value in _list(evidence_bundle["coverage"])]
    project_id = cast(str, next(item["project_id"] for item in coverage if item["eligible"]))
    evidence = [_dict(value) for value in _list(evidence_bundle["evidence_items"])]
    by_kind: dict[str, dict[str, object]] = {}
    for item in evidence:
        by_kind.setdefault(cast(str, item["evidence_kind"]), item)
    implementation_id = cast(str, by_kind["implementation"]["evidence_id"])
    test_id = cast(str, by_kind["test_definition"]["evidence_id"])
    statement_tokens = [
        {
            "kind": "text",
            "value": claim_statement_prefix
            or "我可以解释该项目如何通过可测试的 Python 函数边界实现核心流程，并安全展示 ",
        },
        {
            "kind": "text",
            "value": "</script><img onerror=alert(1)> \u2028\u2029\u202e 与超长输入：" + "x" * 320,
        },
        {"kind": "inert_url", "value": "javascript:alert(1)"},
    ]
    review_semantic_projection: dict[str, object]
    if verified_semantic_key is None:
        review_semantic_projection = {
            "concept_keys": ["calculation-boundary"],
            "mechanism_keys": ["function-call"],
            "behavior_contract_keys": ["input-to-output"],
            "tradeoff_keys": [],
            "technology_identifiers": technology_identifiers or ["python"],
        }
    else:
        review_semantic_projection = {
            "concept_keys": [verified_semantic_key],
            "mechanism_keys": [],
            "behavior_contract_keys": [],
            "tradeoff_keys": [],
            "technology_identifiers": [],
            "verification_anchors": {verified_semantic_key: [implementation_id]},
        }
    gap_drafts: list[dict[str, object]] = []
    gap_refs: list[str] = []
    if knowledge_gap_severity is not None:
        gap_drafts.append(
            {
                "draft_id": "gap-owner-context",
                "scope_kind": "project",
                "project_id": project_id,
                "dimension": "implementation_depth",
                "stable_gap_concept_key": "owner-context",
                "gap_contract_version": "reporting-context-gap-v1",
                "description_tokens": [{"kind": "text", "value": "尚未补充个人角色与学习上下文。"}],
                "severity": knowledge_gap_severity,
                "resolution_kind": "owner_follow_up",
                "status": "open",
            }
        )
        gap_refs.append("gap-owner-context")
    AnalysisService(database).record_analysis(
        workspace_path=workspace,
        authorization_receipt_id=receipt_id,
        request_value={
            "contract_version": "analysis-commit-v1",
            "request_id": str(uuid.uuid4()),
            "preparation_run_id": run["preparation_run_id"],
            "role_lens_id": lens["role_lens_id"],
            "evidence_drafts": [],
            "claim_drafts": [
                {
                    "draft_id": "claim-main",
                    "claim_key": "core-calculation",
                    "category": "implementation_method",
                    "scope_kind": "project",
                    "project_id": project_id,
                    "section": "project_story",
                    "statement_tokens": statement_tokens,
                    "facets": ["implemented", "test_defined"],
                    "support_level": "cross_checked",
                    "personal_attribution": "capability",
                    "review_semantic_projection": review_semantic_projection,
                    "evidence_relations": [
                        {
                            "evidence_ref": implementation_id,
                            "relation": "supports",
                            "supported_facets": ["implemented"],
                        },
                        {
                            "evidence_ref": test_id,
                            "relation": "supports",
                            "supported_facets": ["test_defined"],
                        },
                    ],
                }
            ],
            "project_assessments": [
                {
                    "project_id": project_id,
                    "dimension_scores_milli": {
                        "implementation_depth": 800,
                        "verification": 700,
                    },
                    "coverage_bps": 10000,
                    "evidence_refs": [implementation_id, test_id],
                    "gap_refs": gap_refs,
                    "rationale_tokens": [{"kind": "text", "value": "实现和测试定义均有当前证据。"}],
                }
            ],
            "knowledge_gaps": gap_drafts,
        },
    )
    return cast(str, run["preparation_run_id"]), scan_run_id


def _latest(paths: DataPaths) -> dict[str, object]:
    return cast(
        dict[str, object],
        json.loads(paths.latest_artifact_file.read_text(encoding="utf-8")),
    )


def test_embedded_json_escape_keys_match_independent_expected_set() -> None:
    expected = {
        "&",
        "<",
        ">",
        "=",
        "\u2028",
        "\u2029",
        "\u202a",
        "\u202b",
        "\u202c",
        "\u202d",
        "\u202e",
        "\u2066",
        "\u2067",
        "\u2068",
        "\u2069",
    }
    actual = set(_EMBEDDED_JSON_ESCAPES)

    assert expected <= actual, f"missing embedded JSON escapes: {expected - actual!r}"
    assert actual <= expected, f"unexpected embedded JSON escapes: {actual - expected!r}"


@pytest.mark.parametrize(
    "unsafe_data",
    (
        "&",
        "<",
        ">",
        "=",
        "\u2028",
        "\u2029",
        "\u202a",
        "\u202b",
        "\u202c",
        "\u202d",
        "\u202e",
        "\u2066",
        "\u2067",
        "\u2068",
        "\u2069",
    ),
)
def test_embedded_json_validation_rejects_unescaped_data(unsafe_data: str) -> None:
    with pytest.raises(InvalidInputError):
        _validate_embedded_json(unsafe_data)


def test_embedded_json_escape_mapping_drives_every_required_escape() -> None:
    unsafe_characters = "".join(_EMBEDDED_JSON_ESCAPES)
    bundle: dict[str, object] = {
        "contract_version": "report-bundle-v1",
        "value": unsafe_characters,
    }
    bundle["bundle_sha256"] = report_bundle_sha256(bundle)

    embedded_data = _embedded_json(bundle)

    for character, escape in _EMBEDDED_JSON_ESCAPES.items():
        assert character not in embedded_data
        assert escape in embedded_data


def test_dashboard_render_enforces_embedded_json_postcondition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle: dict[str, object] = {"contract_version": "report-bundle-v1"}
    bundle["bundle_sha256"] = report_bundle_sha256(bundle)
    monkeypatch.setattr(reporting, "_embedded_json", lambda _: '{"unsafe":"<x=>"}')

    with pytest.raises(InvalidInputError, match="unescaped character"):
        render_dashboard_html(bundle)


@pytest.mark.parametrize(
    "token_value",
    (
        '<div style="color:red">hi</div>',
        "style=",
        "<img src=x onerror=alert(1)>",
    ),
)
def test_dashboard_renders_markup_like_code_tokens_as_inert_data(token_value: str) -> None:
    bundle: dict[str, object] = {
        "contract_version": "report-bundle-v1",
        "tokens": [{"kind": "code", "value": token_value}],
    }
    bundle["bundle_sha256"] = report_bundle_sha256(bundle)

    html = render_dashboard_html(bundle)
    assert html == render_dashboard_html(bundle)
    marker = '<script id="report-data" type="application/json">'
    embedded_data = html.split(marker, maxsplit=1)[1].split("</script>", maxsplit=1)[0]
    for character, escape in (("<", "\\u003c"), (">", "\\u003e"), ("=", "\\u003d")):
        assert character not in embedded_data
        if character in token_value:
            assert escape in embedded_data
    document_structure = html.replace(f"{marker}{embedded_data}</script>", "", 1)
    assert " style=" not in document_structure.lower()
    assert "<img" not in document_structure.lower()


def test_dashboard_embedding_preserves_canonical_bundle_digest() -> None:
    bundle: dict[str, object] = {
        "contract_version": "report-bundle-v1",
        "x": [{"kind": "code", "value": '<div style="color:red">hi</div>'}],
    }
    expected_digest = "ba683a8d440b45447ddaef2ff3bc8d5bbc9794b1cee91f33440906450e6e200e"
    assert report_bundle_sha256(bundle) == expected_digest

    bundle["bundle_sha256"] = expected_digest
    render_dashboard_html(bundle)
    assert report_bundle_sha256(bundle) == expected_digest


def test_report_bundle_and_snapshot_are_deterministic_safe_and_idempotent(
    tmp_path: Path,
    data_paths: DataPaths,
) -> None:
    workspace = _workspace(tmp_path)
    database = Database(data_paths)
    receipt_id = _authorize(database, workspace)
    run_id, _ = _prepare_and_analyze(database, workspace, receipt_id)
    workspace.rename(tmp_path / "workspace-moved-after-analysis")

    builder = ReportBundleBuilder(database)
    first_bundle = builder.build(run_id)
    second_bundle = builder.build(run_id)
    assert canonical_report_bundle(first_bundle) == canonical_report_bundle(second_bundle)
    assert first_bundle["bundle_sha256"] == second_bundle["bundle_sha256"]
    claims = [_dict(value) for value in _list(first_bundle["claims"])]
    assert claims[0]["personal_attribution"] == "capability"
    assert _dict(first_bundle["export_projection"])["projection_sha256"]

    html = render_dashboard_html(first_bundle)
    assert html == render_dashboard_html(second_bundle)
    assert '<meta http-equiv="Content-Security-Policy"' in html
    assert "connect-src 'none'" in html
    assert "unsafe-inline" not in html
    assert "unsafe-eval" not in html
    assert "<img onerror=alert(1)>" not in html
    assert "\\u003cimg onerror\\u003dalert(1)\\u003e" in html
    assert "\\u2028" in html
    assert "\\u2029" in html
    assert "\\u202e" in html
    assert "\u202e" not in html
    assert "x" * 320 in html
    assert "overflow-wrap: anywhere" in html
    assert 'href="javascript:' not in html
    markdown = render_report_markdown(first_bundle)
    assert "&lt;/script&gt;" in markdown
    assert "Evidence" in markdown
    assert "capability" in markdown
    assert "\\[U\\+2028\\]\\[U\\+2029\\]\\[U\\+202E\\]" in markdown
    assert "#### 学习要点" in markdown
    assert "#### 如何实现" in markdown
    assert "### STAR 素材" in markdown
    assert "**T · 任务与职责**：未冻结可用 Claim，不补造内容。" in markdown
    assert "**R · 结果**：未冻结可用 Claim，不补造内容。" in markdown

    service = ArtifactSnapshotService(database)
    rendered = service.render(run_id)
    snapshot = _dict(rendered["artifact_snapshot"])
    paths = [
        Path(cast(str, snapshot["report_markdown_path"])),
        Path(cast(str, snapshot["resume_markdown_path"])),
        Path(cast(str, snapshot["html_path"])),
        Path(cast(str, snapshot["manifest_path"])),
    ]
    assert all(path.is_file() for path in paths)
    assert all(_is_read_only(path) for path in paths)
    assert _is_read_only(paths[0].parent, directory=True)
    manifest = cast(dict[str, object], json.loads(paths[-1].read_text(encoding="utf-8")))
    assert manifest["report_bundle_sha256"] == first_bundle["bundle_sha256"]
    assert _latest(data_paths)["artifact_snapshot_id"] == snapshot["artifact_snapshot_id"]

    repeated = service.render(run_id)
    assert (
        _dict(repeated["artifact_snapshot"])["artifact_snapshot_id"]
        == snapshot["artifact_snapshot_id"]
    )
    connection = sqlite3.connect(data_paths.database_file)
    assert connection.execute("SELECT COUNT(*) FROM render_attempts").fetchone() == (1,)
    assert connection.execute("SELECT COUNT(*) FROM artifact_snapshots").fetchone() == (1,)
    assert connection.execute("SELECT personal_attribution FROM claim_revisions").fetchone() == (
        "capability",
    )
    connection.close()


def test_review_lineage_projects_only_equivalent_subjects_into_new_snapshots(
    tmp_path: Path,
    data_paths: DataPaths,
) -> None:
    workspace = _workspace(tmp_path)
    database = Database(data_paths)
    receipt_id = _authorize(database, workspace)
    first_run, scan_run_id = _prepare_and_analyze(
        database,
        workspace,
        receipt_id,
        knowledge_gap_severity="medium",
        claim_statement_prefix="我可以解释 Python 的同一可测试函数边界，换一种措辞并安全展示 ",
        verified_semantic_key="python",
    )
    review_service = ReviewService(database)
    target_request = {
        "contract_version": "interview-input-v1",
        "mode": "mock_review",
        "action": "list_targets",
        "preparation_run_id": first_run,
    }
    with pytest.raises(InvalidInputError, match="published ArtifactSnapshot"):
        review_service.interview(
            workspace_path=workspace,
            authorization_receipt_id=receipt_id,
            request_value=target_request,
        )
    snapshot_service = ArtifactSnapshotService(database)
    first_snapshot = _dict(snapshot_service.render(first_run)["artifact_snapshot"])
    first_html = Path(cast(str, first_snapshot["html_path"]))
    first_html_bytes = first_html.read_bytes()
    first_bundle = ReportBundleBuilder(database).build(first_run)
    first_bundle_hash = first_bundle["bundle_sha256"]
    first_claim_revision_id = _dict(_list(first_bundle["claims"])[0])["claim_revision_id"]

    listed = review_service.interview(
        workspace_path=workspace,
        authorization_receipt_id=receipt_id,
        request_value=target_request,
    )
    mock_review = _dict(listed["mock_review"])
    questions = [_dict(value) for value in _list(mock_review["questions"])]
    assert len(questions) == 2
    claim_question = next(question for question in questions if "claim_id" in question)
    gap_question = next(question for question in questions if "gap_id" in question)
    assert claim_question["continuity_status"] == "new"
    assert gap_question["continuity_status"] == "new"
    with database.read_connection() as connection:
        subject_projections = [
            str(row["subject_projection"])
            for row in connection.execute(
                """
                SELECT subject_projection FROM review_target_bindings
                WHERE preparation_run_id = ?
                """,
                (first_run,),
            ).fetchall()
        ]
    assert all("claim_revision_id" not in value for value in subject_projections)
    assert all("gap_id" not in value and "statement" not in value for value in subject_projections)

    claim_request_id = str(uuid.uuid4())
    claim_request = {
        "contract_version": "interview-input-v1",
        "request_id": claim_request_id,
        "mode": "mock_review",
        "action": "record_review",
        "preparation_run_id": first_run,
        "review_target_binding_id": claim_question["review_target_binding_id"],
        "question_id": claim_question["question_id"],
        "review": {
            "summary": "能够讲清主流程，但异常路径还需要再练习。",
            "mastery_level": "solid",
            "weak_points": ["异常路径", "替代方案"],
            "next_review_at": "2026-08-15",
        },
    }
    first_review = review_service.interview(
        workspace_path=workspace,
        authorization_receipt_id=receipt_id,
        request_value=claim_request,
    )
    assert (
        review_service.interview(
            workspace_path=workspace,
            authorization_receipt_id=receipt_id,
            request_value=claim_request,
        )
        == first_review
    )
    changed_retry = cast(dict[str, object], json.loads(json.dumps(claim_request)))
    changed_review = _dict(changed_retry["review"])
    changed_review["summary"] = "同一 request_id 的不同内容"
    with pytest.raises(InvalidInputError, match="another InterviewReview"):
        review_service.interview(
            workspace_path=workspace,
            authorization_receipt_id=receipt_id,
            request_value=changed_retry,
        )
    gap_review = review_service.interview(
        workspace_path=workspace,
        authorization_receipt_id=receipt_id,
        request_value={
            "contract_version": "interview-input-v1",
            "request_id": str(uuid.uuid4()),
            "mode": "mock_review",
            "action": "record_review",
            "preparation_run_id": first_run,
            "review_target_binding_id": gap_question["review_target_binding_id"],
            "question_id": gap_question["question_id"],
            "review": {
                "summary": "知道缺口是什么，但还没有补充角色上下文。",
                "mastery_level": "developing",
                "weak_points": ["角色上下文"],
                "next_review_at": None,
            },
        },
    )
    assert _dict(gap_review["interview_review"])["mastery_level"] == "developing"
    with pytest.raises(InvalidInputError, match="unsupported fields: transcript"):
        review_service.interview(
            workspace_path=workspace,
            authorization_receipt_id=receipt_id,
            request_value={
                **claim_request,
                "request_id": str(uuid.uuid4()),
                "review": {**_dict(claim_request["review"]), "transcript": "不得落库"},
            },
        )
    assert _latest(data_paths)["artifact_snapshot_id"] == first_snapshot["artifact_snapshot_id"]

    second_run, _ = _prepare_and_analyze(
        database,
        workspace,
        receipt_id,
        scan_run_id=scan_run_id,
        knowledge_gap_severity="medium",
        claim_statement_prefix="换一种纯展示措辞，仍然解释 Python 的同一机制，并安全展示 ",
        verified_semantic_key="python",
    )
    second_bundle = ReportBundleBuilder(database).build(second_run)
    second_claim_revision_id = _dict(_list(second_bundle["claims"])[0])["claim_revision_id"]
    assert second_claim_revision_id != first_claim_revision_id
    second_bindings = [_dict(value) for value in _list(_dict(second_bundle["review"])["bindings"])]
    assert {binding["continuity_status"] for binding in second_bindings} == {"continued"}
    assert {binding["mastery_level"] for binding in second_bindings} == {
        "solid",
        "developing",
    }
    assert any(binding["weak_points"] == ["异常路径", "替代方案"] for binding in second_bindings)
    second_snapshot = _dict(snapshot_service.render(second_run)["artifact_snapshot"])
    assert second_snapshot["artifact_snapshot_id"] != first_snapshot["artifact_snapshot_id"]
    assert first_html.read_bytes() == first_html_bytes
    assert ReportBundleBuilder(database).build(first_run)["bundle_sha256"] == first_bundle_hash

    third_run, _ = _prepare_and_analyze(
        database,
        workspace,
        receipt_id,
        scan_run_id=scan_run_id,
        knowledge_gap_severity="high",
        claim_statement_prefix="这次改为解释 Python3 的另一套语义机制，并安全展示 ",
        verified_semantic_key="python3",
    )
    third_bundle = ReportBundleBuilder(database).build(third_run)
    third_bindings = [_dict(value) for value in _list(_dict(third_bundle["review"])["bindings"])]
    assert {binding["continuity_status"] for binding in third_bindings} == {"reassess_required"}
    assert all(binding["mastery_level"] is None for binding in third_bindings)
    historical_reviews = [_dict(binding["historical_review"]) for binding in third_bindings]
    assert {history["mastery_level"] for history in historical_reviews} == {
        "solid",
        "developing",
    }
    assert {history["summary"] for history in historical_reviews} == {
        "能够讲清主流程，但异常路径还需要再练习。",
        "知道缺口是什么，但还没有补充角色上下文。",
    }
    assert any(history["weak_points"] == ["异常路径", "替代方案"] for history in historical_reviews)
    assert _dict(third_bundle["review"])["status"] == "reassessment_required"
    third_snapshot = _dict(snapshot_service.render(third_run)["artifact_snapshot"])
    third_markdown = Path(cast(str, third_snapshot["report_markdown_path"])).read_text(
        encoding="utf-8"
    )
    third_html = Path(cast(str, third_snapshot["html_path"])).read_text(encoding="utf-8")
    assert "需要重评 · 当前掌握度：未评估" in third_markdown
    assert "上次复盘（仅供历史参考，不代表当前掌握度）" in third_markdown
    assert "历史薄弱点" in third_markdown
    assert '"historical_review"' in third_html
    assert "\\u4e0a\\u6b21\\u590d\\u76d8" in third_html.lower()


@pytest.mark.parametrize("damage", ["missing", "tampered", "linked"])
def test_mock_review_rejects_invalid_artifact_snapshot(
    tmp_path: Path,
    data_paths: DataPaths,
    damage: str,
) -> None:
    workspace = _workspace(tmp_path)
    database = Database(data_paths)
    receipt_id = _authorize(database, workspace)
    run_id, _ = _prepare_and_analyze(database, workspace, receipt_id)
    snapshot = _dict(ArtifactSnapshotService(database).render(run_id)["artifact_snapshot"])
    review_service = ReviewService(database)
    list_request = {
        "contract_version": "interview-input-v1",
        "mode": "mock_review",
        "action": "list_targets",
        "preparation_run_id": run_id,
    }
    listed = review_service.interview(
        workspace_path=workspace,
        authorization_receipt_id=receipt_id,
        request_value=list_request,
    )
    question = _dict(_list(_dict(listed["mock_review"])["questions"])[0])
    record_request = {
        "contract_version": "interview-input-v1",
        "request_id": str(uuid.uuid4()),
        "mode": "mock_review",
        "action": "record_review",
        "preparation_run_id": run_id,
        "review_target_binding_id": question["review_target_binding_id"],
        "question_id": question["question_id"],
        "review": {
            "summary": "损坏快照不得接受这条复盘。",
            "mastery_level": "developing",
            "weak_points": [],
            "next_review_at": None,
        },
    }
    html_path = Path(cast(str, snapshot["html_path"]))
    html_path.parent.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    html_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    if damage == "missing":
        html_path.unlink()
    elif damage == "tampered":
        html_path.write_text("tampered", encoding="utf-8")
    else:
        outside = tmp_path / "outside.html"
        outside.write_text("outside", encoding="utf-8")
        html_path.unlink()
        html_path.symlink_to(outside)

    for request in (list_request, record_request):
        with pytest.raises(InvalidInputError):
            review_service.interview(
                workspace_path=workspace,
                authorization_receipt_id=receipt_id,
                request_value=request,
            )
    with database.read_connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM interview_reviews").fetchone()[0] == 0


def test_review_sequence_breaks_equal_timestamp_ties_and_freezes_run_cutoff(
    tmp_path: Path,
    data_paths: DataPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)
    database = Database(data_paths)
    receipt_id = _authorize(database, workspace)
    first_run, scan_run_id = _prepare_and_analyze(database, workspace, receipt_id)
    ArtifactSnapshotService(database).render(first_run)
    review_service = ReviewService(database)
    listed = review_service.interview(
        workspace_path=workspace,
        authorization_receipt_id=receipt_id,
        request_value={
            "contract_version": "interview-input-v1",
            "mode": "mock_review",
            "action": "list_targets",
            "preparation_run_id": first_run,
        },
    )
    question = _dict(_list(_dict(listed["mock_review"])["questions"])[0])
    tied_at = "2026-07-27T12:00:00.123400Z"
    monkeypatch.setattr("goodjob.review._now", lambda: tied_at)

    def record(summary: str, mastery_level: str) -> None:
        review_service.interview(
            workspace_path=workspace,
            authorization_receipt_id=receipt_id,
            request_value={
                "contract_version": "interview-input-v1",
                "request_id": str(uuid.uuid4()),
                "mode": "mock_review",
                "action": "record_review",
                "preparation_run_id": first_run,
                "review_target_binding_id": question["review_target_binding_id"],
                "question_id": question["question_id"],
                "review": {
                    "summary": summary,
                    "mastery_level": mastery_level,
                    "weak_points": [],
                    "next_review_at": None,
                },
            },
        )

    record("同一微秒内的第一条复盘。", "developing")
    record("同一微秒内的第二条复盘。", "solid")
    monkeypatch.setattr("goodjob.preparation._now", lambda: tied_at)
    second_run, _ = _prepare_and_analyze(
        database,
        workspace,
        receipt_id,
        scan_run_id=scan_run_id,
    )
    second_bundle = ReportBundleBuilder(database).build(second_run)
    second_binding = _dict(_list(_dict(second_bundle["review"])["bindings"])[0])
    assert second_binding["summary"] == "同一微秒内的第二条复盘。"
    assert second_binding["mastery_level"] == "solid"
    frozen_hash = second_bundle["bundle_sha256"]

    record("PreparationRun 冻结后写入的同时间复盘。", "mastered")
    repeated_bundle = ReportBundleBuilder(database).build(second_run)
    repeated_binding = _dict(_list(_dict(repeated_bundle["review"])["bindings"])[0])
    assert repeated_binding["summary"] == "同一微秒内的第二条复盘。"
    assert repeated_bundle["bundle_sha256"] == frozen_hash
    with database.read_connection() as connection:
        sequences = [
            (int(row["review_sequence"]), str(row["created_at"]))
            for row in connection.execute(
                """
                SELECT review_sequence, created_at
                FROM interview_reviews ORDER BY review_sequence
                """
            ).fetchall()
        ]
        cutoff = connection.execute(
            """
            SELECT review_cutoff_sequence FROM preparation_runs
            WHERE preparation_run_id = ?
            """,
            (second_run,),
        ).fetchone()[0]
    assert sequences == [(1, tied_at), (2, tied_at), (3, tied_at)]
    assert cutoff == 2


@pytest.mark.parametrize("fault_at", ["after_temp", "after_publish"])
def test_render_failure_preserves_latest_and_retries_the_same_bundle(
    tmp_path: Path,
    data_paths: DataPaths,
    fault_at: str,
) -> None:
    workspace = _workspace(tmp_path)
    database = Database(data_paths)
    receipt_id = _authorize(database, workspace)
    first_run, scan_run_id = _prepare_and_analyze(database, workspace, receipt_id)
    service = ArtifactSnapshotService(database)
    first_snapshot = _dict(service.render(first_run)["artifact_snapshot"])
    first_latest = _latest(data_paths)

    second_run, _ = _prepare_and_analyze(
        database,
        workspace,
        receipt_id,
        scan_run_id=scan_run_id,
        role_name="应用软件工程师二次准备",
    )
    with pytest.raises(InvalidInputError, match="artifact rendering failed"):
        service.render(second_run, _fault_at=fault_at)
    assert _latest(data_paths) == first_latest
    assert not any(data_paths.artifact_tmp_dir.iterdir())
    connection = sqlite3.connect(data_paths.database_file)
    assert connection.execute(
        "SELECT status FROM preparation_runs WHERE preparation_run_id = ?",
        (second_run,),
    ).fetchone() == ("render_failed",)
    assert connection.execute(
        "SELECT status FROM render_attempts WHERE preparation_run_id = ?",
        (second_run,),
    ).fetchone() == ("failed",)
    connection.close()

    second_snapshot = _dict(service.render(second_run)["artifact_snapshot"])
    assert second_snapshot["artifact_snapshot_id"] != first_snapshot["artifact_snapshot_id"]
    assert _latest(data_paths)["artifact_snapshot_id"] == second_snapshot["artifact_snapshot_id"]
    connection = sqlite3.connect(data_paths.database_file)
    hashes = connection.execute(
        "SELECT DISTINCT report_bundle_sha256 FROM render_attempts WHERE preparation_run_id = ?",
        (second_run,),
    ).fetchall()
    statuses = connection.execute(
        "SELECT status FROM render_attempts WHERE preparation_run_id = ? ORDER BY started_at",
        (second_run,),
    ).fetchall()
    connection.close()
    assert len(hashes) == 1
    assert statuses == [("failed",), ("succeeded",)]


def test_committed_snapshot_repairs_latest_without_duplicate_render(
    tmp_path: Path,
    data_paths: DataPaths,
) -> None:
    workspace = _workspace(tmp_path)
    database = Database(data_paths)
    receipt_id = _authorize(database, workspace)
    run_id, _ = _prepare_and_analyze(database, workspace, receipt_id)
    service = ArtifactSnapshotService(database)

    with pytest.raises(InvalidInputError, match="artifact rendering failed"):
        service.render(run_id, _fault_at="after_database_commit")
    assert not data_paths.latest_artifact_file.exists()
    connection = sqlite3.connect(data_paths.database_file)
    assert connection.execute("SELECT status FROM render_attempts").fetchone() == ("succeeded",)
    assert connection.execute("SELECT COUNT(*) FROM artifact_snapshots").fetchone() == (1,)
    connection.close()

    rendered = service.render(run_id)

    snapshot = _dict(rendered["artifact_snapshot"])
    assert _latest(data_paths)["artifact_snapshot_id"] == snapshot["artifact_snapshot_id"]
    connection = sqlite3.connect(data_paths.database_file)
    assert connection.execute("SELECT COUNT(*) FROM render_attempts").fetchone() == (1,)
    assert connection.execute("SELECT COUNT(*) FROM artifact_snapshots").fetchone() == (1,)
    connection.close()


def test_repeated_render_rejects_tampered_or_linked_snapshot_files(
    tmp_path: Path,
    data_paths: DataPaths,
) -> None:
    workspace = _workspace(tmp_path)
    database = Database(data_paths)
    receipt_id = _authorize(database, workspace)
    run_id, _ = _prepare_and_analyze(database, workspace, receipt_id)
    service = ArtifactSnapshotService(database)
    snapshot = _dict(service.render(run_id)["artifact_snapshot"])
    html_path = Path(cast(str, snapshot["html_path"]))
    snapshot_directory = html_path.parent

    snapshot_directory.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    unexpected = snapshot_directory / "unregistered.txt"
    unexpected.write_text("not in manifest", encoding="utf-8")
    with pytest.raises(InvalidInputError, match="file set does not match"):
        service.render(run_id)
    unexpected.unlink()

    html_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    html_path.write_text("tampered", encoding="utf-8")
    with pytest.raises(InvalidInputError, match="digest does not match"):
        service.render(run_id)

    html_path.unlink()
    linked_target = snapshot_directory / "manifest.json"
    html_path.symlink_to(linked_target.name)
    with pytest.raises(InvalidInputError, match="unavailable or linked"):
        service.render(run_id)
    assert linked_target.is_file()


def test_render_writer_busy_creates_no_attempt_or_artifact(
    tmp_path: Path,
    data_paths: DataPaths,
) -> None:
    workspace = _workspace(tmp_path)
    database = Database(data_paths)
    receipt_id = _authorize(database, workspace)
    run_id, _ = _prepare_and_analyze(database, workspace, receipt_id)

    with (
        ExclusiveWriterLock(data_paths.writer_lock_file),
        pytest.raises(WriterBusyError),
    ):
        ArtifactSnapshotService(database).render(run_id)

    connection = sqlite3.connect(data_paths.database_file)
    assert connection.execute("SELECT COUNT(*) FROM render_attempts").fetchone() == (0,)
    assert connection.execute(
        "SELECT status FROM preparation_runs WHERE preparation_run_id = ?",
        (run_id,),
    ).fetchone() == ("ready",)
    connection.close()
    assert not any(data_paths.artifact_tmp_dir.iterdir())


def test_carried_forward_evidence_keeps_snapshot_worktree_provenance(
    tmp_path: Path,
    data_paths: DataPaths,
) -> None:
    workspace = _workspace(tmp_path)
    database = Database(data_paths)
    receipt_id = _authorize(database, workspace)
    run_id, old_scan_run_id = _prepare_and_analyze(database, workspace, receipt_id)
    current_scan_run_id = "current-scan-with-different-branch"
    with database.write_transaction() as connection:
        observation = connection.execute(
            """
            SELECT wo.worktree_id, srp.project_id, srp.project_snapshot_id
            FROM worktree_observations AS wo
            JOIN worktrees AS wt ON wt.worktree_id = wo.worktree_id
            JOIN scan_run_projects AS srp
              ON srp.scan_run_id = wo.scan_run_id AND srp.project_id = wt.project_id
            WHERE wo.scan_run_id = ?
            LIMIT 1
            """,
            (old_scan_run_id,),
        ).fetchone()
        assert observation is not None
        worktree_id = str(observation["worktree_id"])
        project_id = str(observation["project_id"])
        snapshot_id = str(observation["project_snapshot_id"])
        connection.execute(
            """
            UPDATE worktree_observations
            SET branch = 'old-frozen-branch', head_commit = ?, dirty_state = 'modified'
            WHERE scan_run_id = ? AND worktree_id = ?
            """,
            ("a" * 40, old_scan_run_id, worktree_id),
        )
        connection.execute(
            """
            INSERT INTO scan_runs(
                scan_run_id, workspace_id, authorization_receipt_id,
                owner_process_identity, mode, change_detection_mode,
                config_revision, started_at, finished_at, status
            )
            SELECT ?, workspace_id, authorization_receipt_id, 'provenance-test',
                   'refresh', 'fast', config_revision,
                   '2026-07-27T01:00:00Z', '2026-07-27T01:00:01Z', 'partial'
            FROM scan_runs WHERE scan_run_id = ?
            """,
            (current_scan_run_id, old_scan_run_id),
        )
        connection.execute(
            """
            INSERT INTO worktree_observations(
                worktree_id, scan_run_id, branch, head_commit, dirty_state,
                history_basis, external_git_dir, external_common_dir,
                external_metadata_receipt_id, external_metadata_confirmed_at,
                external_metadata_read_fields, observed_at
            )
            SELECT worktree_id, ?, 'new-current-branch', ?, 'clean',
                   history_basis, external_git_dir, external_common_dir,
                   external_metadata_receipt_id, external_metadata_confirmed_at,
                   external_metadata_read_fields, '2026-07-27T01:00:00Z'
            FROM worktree_observations
            WHERE scan_run_id = ? AND worktree_id = ?
            """,
            (current_scan_run_id, "b" * 40, old_scan_run_id, worktree_id),
        )
        connection.execute(
            """
            INSERT INTO scan_run_projects(
                scan_run_id, project_id, snapshot_disposition, project_snapshot_id
            ) VALUES (?, ?, 'carried_forward', ?)
            """,
            (current_scan_run_id, project_id, snapshot_id),
        )
        connection.execute(
            """
            UPDATE preparation_runs SET scan_run_id = ? WHERE preparation_run_id = ?
            """,
            (current_scan_run_id, run_id),
        )
        connection.execute(
            """
            UPDATE preparation_run_projects
            SET snapshot_disposition = 'carried_forward'
            WHERE preparation_run_id = ? AND project_id = ?
            """,
            (run_id, project_id),
        )

    bundle = ReportBundleBuilder(database).build(run_id)
    evidence = [_dict(value) for value in _list(bundle["evidence"])]
    assert evidence
    worktrees = [_dict(item["worktree"]) for item in evidence]
    assert {
        (
            worktree["branch"],
            worktree["head_commit"],
            worktree["dirty_state"],
            worktree["observed_scan_run_id"],
        )
        for worktree in worktrees
    } == {("old-frozen-branch", "a" * 40, "modified", old_scan_run_id)}
    projects = [_dict(value) for value in _list(bundle["projects"])]
    assert projects[0]["snapshot_disposition"] == "carried_forward"


def test_open_context_gap_is_visible_and_downgrades_the_package(
    tmp_path: Path,
    data_paths: DataPaths,
) -> None:
    workspace = _workspace(tmp_path)
    database = Database(data_paths)
    receipt_id = _authorize(database, workspace)
    run_id, _ = _prepare_and_analyze(
        database,
        workspace,
        receipt_id,
        knowledge_gap_severity="medium",
    )

    bundle = ReportBundleBuilder(database).build(run_id)
    limitations = [_dict(value) for value in _list(_dict(bundle["coverage"])["limitations"])]

    assert bundle["package_status"] == "partial"
    assert any(
        limitation["kind"] == "knowledge_gap" and limitation["severity"] == "medium"
        for limitation in limitations
    )
    assert "尚未补充个人角色与学习上下文" in render_report_markdown(bundle)
    snapshot = _dict(ArtifactSnapshotService(database).render(run_id)["artifact_snapshot"])
    assert snapshot["preparation_run_id"] == run_id
    assert _latest(data_paths)["package_status"] == "partial"


def test_scan_issue_limitation_retains_its_affected_path(
    tmp_path: Path,
    data_paths: DataPaths,
) -> None:
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("print('outside')\n", encoding="utf-8")
    (workspace / "outside.py").symlink_to(outside)
    (workspace / "app-icon.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00binary-image")
    database = Database(data_paths)
    receipt_id = _authorize(database, workspace)
    run_id, _ = _prepare_and_analyze(database, workspace, receipt_id)

    bundle = ReportBundleBuilder(database).build(run_id)
    coverage = _dict(bundle["coverage"])
    limitations = [_dict(value) for value in _list(coverage["limitations"])]
    limitation = next(
        value
        for value in limitations
        if value["kind"] == "scan_issue:symlink_outside_authorized_root"
    )
    message = "".join(
        cast(str, _dict(token)["value"]) for token in _list(limitation["message_tokens"])
    )
    excluded = _dict(coverage["excluded_by_category"])
    markdown = render_report_markdown(bundle)
    project = _dict(_list(bundle["projects"])[0])
    project_section = markdown.split(f"- Project ID：`{project['project_id']}`", 1)[1]
    snapshot = _dict(ArtifactSnapshotService(database).render(run_id)["artifact_snapshot"])
    manifest = _dict(json.loads(Path(cast(str, snapshot["manifest_path"])).read_text("utf-8")))
    manifest_coverage = _dict(manifest["coverage_summary"])

    assert "outside.py" in message
    assert limitation["project_id"] == project["project_id"]
    assert coverage["excluded_by_category_available"] is True
    assert excluded["binary_or_undecodable"] == 1
    assert _dict(manifest_coverage["excluded_by_category"])["binary_or_undecodable"] == 1
    assert "binary_or_undecodable" in markdown
    assert "outside.py" in project_section


def test_issue_path_mapping_uses_every_frozen_worktree_and_prefers_deepest_project() -> None:
    projects: list[dict[str, object]] = [
        {"project_id": "multi-worktree", "workspace_relative_location": "linked"},
        {"project_id": "nested", "workspace_relative_location": "primary/nested"},
    ]
    locations = {
        "multi-worktree": ("linked", "primary"),
        "nested": ("primary/nested",),
    }

    assert (
        ReportBundleBuilder._project_for_issue_path(
            projects,
            "primary/outside.py",
            locations,
        )
        == "multi-worktree"
    )
    assert (
        ReportBundleBuilder._project_for_issue_path(
            projects,
            "primary/nested/outside.py",
            locations,
        )
        == "nested"
    )


def test_dead_render_owner_is_interrupted_and_only_registered_paths_are_cleaned(
    tmp_path: Path,
    data_paths: DataPaths,
) -> None:
    workspace = _workspace(tmp_path)
    database = Database(data_paths)
    receipt_id = _authorize(database, workspace)
    run_id, _ = _prepare_and_analyze(database, workspace, receipt_id)
    bundle = ReportBundleBuilder(database).build(run_id)
    bundle_hash = cast(str, bundle["bundle_sha256"])
    old_attempt_id = "dead-render-attempt"
    snapshot_id = _artifact_snapshot_id(run_id, bundle_hash)
    temp_relative = f"artifacts/.tmp/{old_attempt_id}"
    latest_temp_relative = f"artifacts/.tmp/{old_attempt_id}.latest.tmp"
    final_relative = f"artifacts/{snapshot_id}"
    with database.write_transaction() as connection:
        connection.execute(
            """
            INSERT INTO render_attempts(
                render_attempt_id, preparation_run_id, owner_process_identity,
                report_bundle_sha256, generator_version, temp_relative_path,
                latest_temp_relative_path, final_relative_path, started_at, status
            ) VALUES (?, ?, 'pid:999999;started:Thu Jan  1 00:00:00 1970', ?,
                      'interrupted-test', ?, ?, ?, '2026-07-27T00:00:00Z', 'running')
            """,
            (
                old_attempt_id,
                run_id,
                bundle_hash,
                temp_relative,
                latest_temp_relative,
                final_relative,
            ),
        )
        connection.execute(
            "UPDATE preparation_runs SET status = 'rendering' WHERE preparation_run_id = ?",
            (run_id,),
        )
    protected_marker = data_paths.root / "must-survive-cleanup.txt"
    protected_marker.write_text("keep-root", encoding="utf-8")
    old_temp = data_paths.root / temp_relative
    old_temp.symlink_to(data_paths.root, target_is_directory=True)
    old_latest_temp = data_paths.root / latest_temp_relative
    old_latest_temp.write_text("partial", encoding="utf-8")
    old_final = data_paths.root / final_relative
    old_final.mkdir()
    (old_final / "orphan.tmp").write_text("partial", encoding="utf-8")
    unrelated = data_paths.artifacts_dir / "owner-file.txt"
    unrelated.write_text("keep", encoding="utf-8")

    rendered = ArtifactSnapshotService(database).render(run_id)
    assert _dict(rendered["artifact_snapshot"])["artifact_snapshot_id"] == snapshot_id
    assert protected_marker.read_text(encoding="utf-8") == "keep-root"
    assert data_paths.database_file.is_file()
    assert not old_temp.is_symlink()
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert not (old_final / "orphan.tmp").exists()
    assert not old_latest_temp.exists()
    connection = sqlite3.connect(data_paths.database_file)
    old_status = connection.execute(
        "SELECT status FROM render_attempts WHERE render_attempt_id = ?",
        (old_attempt_id,),
    ).fetchone()
    new_statuses = connection.execute(
        "SELECT status FROM render_attempts WHERE preparation_run_id = ? ORDER BY started_at",
        (run_id,),
    ).fetchall()
    connection.close()
    assert old_status == ("interrupted",)
    assert new_statuses == [("interrupted",), ("succeeded",)]


def test_render_recovery_probe_failure_preserves_attempt_and_registered_paths(
    tmp_path: Path,
    data_paths: DataPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)
    database = Database(data_paths)
    receipt_id = _authorize(database, workspace)
    run_id, _ = _prepare_and_analyze(database, workspace, receipt_id)
    bundle = ReportBundleBuilder(database).build(run_id)
    bundle_hash = cast(str, bundle["bundle_sha256"])
    attempt_id = "unproven-render-attempt"
    snapshot_id = _artifact_snapshot_id(run_id, bundle_hash)
    temp_relative = f"artifacts/.tmp/{attempt_id}"
    latest_temp_relative = f"artifacts/.tmp/{attempt_id}.latest.tmp"
    final_relative = f"artifacts/{snapshot_id}"
    with database.write_transaction() as connection:
        connection.execute(
            """
            INSERT INTO render_attempts(
                render_attempt_id, preparation_run_id, owner_process_identity,
                report_bundle_sha256, generator_version, temp_relative_path,
                latest_temp_relative_path, final_relative_path, started_at, status
            ) VALUES (?, ?, 'pid:321;started:100', ?, 'probe-failure-test',
                      ?, ?, ?, '2026-08-16T00:00:00Z', 'running')
            """,
            (
                attempt_id,
                run_id,
                bundle_hash,
                temp_relative,
                latest_temp_relative,
                final_relative,
            ),
        )
        connection.execute(
            "UPDATE preparation_runs SET status = 'rendering' WHERE preparation_run_id = ?",
            (run_id,),
        )
    temp_path = data_paths.root / temp_relative
    latest_temp_path = data_paths.root / latest_temp_relative
    final_path = data_paths.root / final_relative
    temp_path.mkdir()
    (temp_path / "partial.tmp").write_text("partial", encoding="utf-8")
    latest_temp_path.write_text("partial", encoding="utf-8")
    final_path.mkdir()
    (final_path / "partial.tmp").write_text("partial", encoding="utf-8")

    def fail_owner_probe(_identity: str) -> bool:
        raise OSError("injected owner liveness failure")

    monkeypatch.setattr(reporting, "owner_process_stopped", fail_owner_probe)
    service = ArtifactSnapshotService(database)

    with (
        database.exclusive_writer_connection() as connection,
        pytest.raises(OSError, match="owner liveness failure"),
    ):
        service._recover_interrupted_attempts(connection)

    connection = sqlite3.connect(data_paths.database_file)
    try:
        attempt_state = connection.execute(
            """
            SELECT status, finished_at, error_summary
            FROM render_attempts WHERE render_attempt_id = ?
            """,
            (attempt_id,),
        ).fetchone()
        preparation_state = connection.execute(
            "SELECT status, finished_at FROM preparation_runs WHERE preparation_run_id = ?",
            (run_id,),
        ).fetchone()
    finally:
        connection.close()
    assert attempt_state == ("running", None, None)
    assert preparation_state == ("rendering", None)
    assert (temp_path / "partial.tmp").read_text(encoding="utf-8") == "partial"
    assert latest_temp_path.read_text(encoding="utf-8") == "partial"
    assert (final_path / "partial.tmp").read_text(encoding="utf-8") == "partial"


@pytest.mark.parametrize("old_status", ["failed", "interrupted"])
def test_terminal_unsnapshotted_attempt_cleanup_is_retried(
    tmp_path: Path,
    data_paths: DataPaths,
    old_status: str,
) -> None:
    workspace = _workspace(tmp_path)
    database = Database(data_paths)
    receipt_id = _authorize(database, workspace)
    run_id, _ = _prepare_and_analyze(database, workspace, receipt_id)
    bundle = ReportBundleBuilder(database).build(run_id)
    bundle_hash = cast(str, bundle["bundle_sha256"])
    old_attempt_id = f"terminal-{old_status}"
    snapshot_id = _artifact_snapshot_id(run_id, bundle_hash)
    temp_relative = f"artifacts/.tmp/{old_attempt_id}"
    latest_temp_relative = f"artifacts/.tmp/{old_attempt_id}.latest.tmp"
    final_relative = f"artifacts/{snapshot_id}"
    with database.write_transaction() as connection:
        connection.execute(
            """
            INSERT INTO render_attempts(
                render_attempt_id, preparation_run_id, owner_process_identity,
                report_bundle_sha256, generator_version, temp_relative_path,
                latest_temp_relative_path, final_relative_path, started_at,
                finished_at, status, error_summary
            ) VALUES (?, ?, 'pid:999999;started:Thu Jan  1 00:00:00 1970', ?,
                      'terminal-cleanup-test', ?, ?, ?, '2026-07-27T00:00:00Z',
                      '2026-07-27T00:00:01Z', ?, 'cleanup was interrupted')
            """,
            (
                old_attempt_id,
                run_id,
                bundle_hash,
                temp_relative,
                latest_temp_relative,
                final_relative,
                old_status,
            ),
        )
        connection.execute(
            """
            UPDATE preparation_runs
            SET status = 'render_failed', status_reason = 'cleanup interrupted'
            WHERE preparation_run_id = ?
            """,
            (run_id,),
        )
    old_temp = data_paths.root / temp_relative
    old_temp.mkdir()
    (old_temp / "partial.tmp").write_text("partial", encoding="utf-8")
    old_latest_temp = data_paths.root / latest_temp_relative
    old_latest_temp.write_text("partial", encoding="utf-8")
    old_final = data_paths.root / final_relative
    old_final.mkdir()
    (old_final / "partial.tmp").write_text("partial", encoding="utf-8")

    snapshot = _dict(ArtifactSnapshotService(database).render(run_id)["artifact_snapshot"])

    assert snapshot["artifact_snapshot_id"] == snapshot_id
    assert not old_temp.exists()
    assert not old_latest_temp.exists()
    connection = sqlite3.connect(data_paths.database_file)
    statuses = connection.execute(
        "SELECT status FROM render_attempts WHERE preparation_run_id = ? ORDER BY started_at",
        (run_id,),
    ).fetchall()
    connection.close()
    assert statuses == [(old_status,), ("succeeded",)]
