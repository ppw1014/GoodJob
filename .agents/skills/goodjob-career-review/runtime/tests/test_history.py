from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

RUNTIME_DIR = Path(__file__).resolve().parents[1]


def _git(
    repository: Path,
    *arguments: str,
    extra_env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    if extra_env is not None:
        environment.update(extra_env)
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )


def _commit(repository: Path, message: str, committed_at: datetime) -> str:
    timestamp = committed_at.isoformat()
    environment = {
        "GIT_AUTHOR_DATE": timestamp,
        "GIT_COMMITTER_DATE": timestamp,
    }
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", message, extra_env=environment)
    return _git(repository, "rev-parse", "HEAD").stdout.strip()


def _broker(data_dir: Path) -> subprocess.Popen[str]:
    command = [sys.executable, "scripts/session.py", "--data-dir", str(data_dir)]
    if sys.platform == "win32":
        workspace = data_dir.parent / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        command.extend(["--preflight-workspace", str(workspace)])
    return subprocess.Popen(
        command,
        cwd=RUNTIME_DIR,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": str(RUNTIME_DIR / "src")},
    )


def _send_json(process: subprocess.Popen[str], payload: dict[str, object]) -> dict[str, object]:
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(json.dumps(payload) + "\n")
    process.stdin.flush()
    response = json.loads(process.stdout.readline())
    assert isinstance(response, dict)
    return cast(dict[str, object], response)


def _close_broker(process: subprocess.Popen[str]) -> None:
    assert process.stdin is not None
    process.stdin.close()
    assert process.wait(timeout=5) == 0
    assert process.stderr is not None
    stderr = process.stderr.read()
    if sys.platform == "win32":
        assert json.loads(stderr)["can_start_broker"] is True
    else:
        assert stderr == ""


def _object(payload: dict[str, object], key: str) -> dict[str, object]:
    value = payload[key]
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def test_targeted_history_is_bounded_transient_and_session_scoped(
    tmp_path: Path, git_sandbox_available: None
) -> None:
    workspace = tmp_path / "workspace"
    repository = workspace / "repository"
    repository.mkdir(parents=True)
    _git(repository, "init")
    _git(repository, "config", "user.name", "Test Author")
    _git(repository, "config", "user.email", "author@example.test")
    (repository / "pyproject.toml").write_text(
        "[project]\nname='history-query'\n", encoding="utf-8"
    )
    historical_source = "HISTORY_ONLY_SECRET = 'never-persist-this-source'\n"
    (repository / "main.py").write_text(historical_source, encoding="utf-8")
    magic_path = ":(glob)*"
    historical_magic_source = "MAGIC_PATH_SECRET = 'literal-path-only'\n"
    (repository / magic_path).write_text(historical_magic_source, encoding="utf-8")
    old_commit = _commit(
        repository,
        "legacy implementation",
        datetime.now(UTC) - timedelta(days=365),
    )
    (repository / "main.py").write_text(
        "def current_implementation() -> str:\n    return 'current'\n",
        encoding="utf-8",
    )
    (repository / magic_path).write_text("MAGIC_PATH_CURRENT = True\n", encoding="utf-8")
    recent_commit = _commit(
        repository,
        "current implementation",
        datetime.now(UTC) - timedelta(days=5),
    )

    data_dir = tmp_path / "data"
    broker = _broker(data_dir)
    authorized = _send_json(
        broker,
        {"op": "authorize_source_analysis", "workspace": str(workspace), "confirmed": True},
    )
    receipt = _object(authorized, "receipt")
    receipt_id = receipt["authorization_receipt_id"]
    assert isinstance(receipt_id, str)
    validated = _send_json(
        broker,
        {
            "op": "validate_job_input",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
            "job_input": {
                "contract_version": "job-input-v1",
                "target_role": "系统工程师",
                "jd_input": {"kind": "none"},
            },
        },
    )
    validated_job_input = _object(validated, "job_input")
    validation_sha256 = validated_job_input["validation_sha256"]
    assert isinstance(validation_sha256, str)
    scanned = _send_json(
        broker,
        {
            "op": "scan",
            "job_input_validation_sha256": validation_sha256,
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
        },
    )
    scan_run = _object(scanned, "scan_run")
    scan_run_id = scan_run["scan_run_id"]
    assert isinstance(scan_run_id, str)
    prepared = _send_json(
        broker,
        {
            "op": "prepare_start",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
            "preparation_request": {
                "contract_version": "preparation-request-v1",
                "request_id": str(uuid.uuid4()),
                "scan_run_id": scan_run_id,
                "config_revision": "history-test-v1",
                "target_role": "系统工程师",
                "jd_input": {"kind": "none"},
                "job_input_validation_sha256": validation_sha256,
                "requested_exports": [],
                "evidence_limit_per_project": 20,
                "role_lens": {
                    "contract_version": "role-lens-v1",
                    "dimensions": [
                        {
                            "key": "system_depth",
                            "display_name": "系统深度",
                            "weight_bps": 10000,
                            "evaluation_criteria": "评价系统实现和演进证据",
                            "required_evidence_kinds": ["implementation"],
                        }
                    ],
                    "evidence_requirements": ["implementation"],
                    "ranking_rules": ["系统相关性优先"],
                    "output_sections": ["系统能力"],
                    "question_strategy": {"primary": "追问技术演进"},
                    "gap_rules": ["缺少证据则记录缺口"],
                    "assumptions": ["未提供岗位 JD"],
                    "generator_id": "history-test",
                    "prompt_contract_version": "history-test-v1",
                },
            },
        },
    )
    preparation_run = _object(prepared, "preparation_run")
    preparation_run_id = preparation_run["preparation_run_id"]
    assert isinstance(preparation_run_id, str)
    role_lens = _object(prepared, "role_lens")
    role_lens_id = role_lens["role_lens_id"]
    assert isinstance(role_lens_id, str)

    connection = sqlite3.connect(data_dir / "goodjob.sqlite3")
    identifiers = connection.execute(
        """
        SELECT p.project_id, w.worktree_id
        FROM projects AS p
        JOIN worktrees AS w ON w.project_id = p.project_id
        WHERE w.canonical_root = ?
        """,
        (str(repository),),
    ).fetchone()
    assert identifiers is not None
    project_id, worktree_id = map(str, identifiers)
    persisted_history = "\n".join(
        str(row[0])
        for row in connection.execute(
            "SELECT locator FROM evidence WHERE origin_kind = 'git_commit'"
        )
    )
    connection.close()
    assert recent_commit in persisted_history
    assert old_commit not in persisted_history

    rejected_fake_lens = _send_json(
        broker,
        {
            "op": "query_history_candidates",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
            "preparation_run_id": preparation_run_id,
            "scan_run_id": scan_run_id,
            "role_lens_id": "forged-role-lens",
            "project_id": project_id,
            "worktree_id": worktree_id,
            "relative_paths": ["main.py"],
            "query_reason": "伪造岗位镜头不得读取历史",
            "maximum_candidates": 5,
        },
    )
    assert rejected_fake_lens["status"] == "error"
    assert rejected_fake_lens["code"] == "invalid_input"

    queried = _send_json(
        broker,
        {
            "op": "query_history_candidates",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
            "preparation_run_id": preparation_run_id,
            "scan_run_id": scan_run_id,
            "role_lens_id": role_lens_id,
            "project_id": project_id,
            "worktree_id": worktree_id,
            "relative_paths": ["main.py"],
            "query_reason": "验证旧实现的技术演进",
            "maximum_candidates": 5,
        },
    )
    history_query = _object(queried, "history_query")
    candidates = history_query["candidates"]
    assert isinstance(candidates, list)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert isinstance(candidate, dict)
    candidate = cast(dict[str, object], candidate)
    assert candidate["commit"] == old_commit
    candidate_id = candidate["candidate_id"]
    assert isinstance(candidate_id, str)

    forged = _send_json(
        broker,
        {
            "op": "read_history_candidate",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
            "candidate_id": "0" * 64,
            "selected_path": "main.py",
        },
    )
    assert forged["status"] == "error"
    assert forged["code"] == "invalid_input"

    selected = _send_json(
        broker,
        {
            "op": "read_history_candidate",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
            "candidate_id": candidate_id,
            "selected_path": "main.py",
        },
    )
    candidate_read = _object(selected, "history_candidate_read")
    assert candidate_read["persisted"] is False
    assert candidate_read["blob_text"] == historical_source
    assert isinstance(candidate_read["diff_sha256"], str)
    assert isinstance(candidate_read["blob_sha256"], str)

    magic_query = _send_json(
        broker,
        {
            "op": "query_history_candidates",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
            "preparation_run_id": preparation_run_id,
            "scan_run_id": scan_run_id,
            "role_lens_id": role_lens_id,
            "project_id": project_id,
            "worktree_id": worktree_id,
            "relative_paths": [magic_path],
            "query_reason": "验证 Git pathspec magic 文件名按字面量处理",
            "maximum_candidates": 5,
        },
    )
    magic_candidates = _object(magic_query, "history_query")["candidates"]
    assert isinstance(magic_candidates, list)
    assert len(magic_candidates) == 1
    magic_candidate = magic_candidates[0]
    assert isinstance(magic_candidate, dict)
    magic_candidate = cast(dict[str, object], magic_candidate)
    assert magic_candidate["changed_paths"] == [magic_path]
    magic_candidate_id = magic_candidate["candidate_id"]
    assert isinstance(magic_candidate_id, str)
    magic_read = _send_json(
        broker,
        {
            "op": "read_history_candidate",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
            "candidate_id": magic_candidate_id,
            "selected_path": magic_path,
        },
    )
    magic_content = _object(magic_read, "history_candidate_read")
    assert magic_content["blob_text"] == historical_magic_source
    assert "main.py" not in str(magic_content["diff_text"])

    evidence_bundle = _object(prepared, "evidence_bundle")
    evidence_items = evidence_bundle["evidence_items"]
    assert isinstance(evidence_items, list)
    current_implementation = next(
        item
        for item in evidence_items
        if isinstance(item, dict)
        and item["project_id"] == project_id
        and item["evidence_kind"] == "implementation"
    )
    assert isinstance(current_implementation, dict)
    analysis = _send_json(
        broker,
        {
            "op": "record_analysis",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
            "analysis_commit_request": {
                "contract_version": "analysis-commit-v1",
                "request_id": str(uuid.uuid4()),
                "preparation_run_id": preparation_run_id,
                "role_lens_id": role_lens_id,
                "evidence_drafts": [
                    {
                        "draft_id": "legacy-history",
                        "origin_kind": "git_commit",
                        "project_id": project_id,
                        "worktree_id": worktree_id,
                        "evidence_kind": "git_history",
                        "locator": {},
                        "summary": (
                            "The selected historical change explains an older implementation."
                        ),
                        "commit_state": "historical",
                        "candidate_id": candidate_id,
                        "selected_path": "main.py",
                        "query_reason": "验证旧实现的技术演进",
                        "commit": old_commit,
                        "metadata_sha256": candidate["metadata_sha256"],
                        "diff_sha256": candidate_read["diff_sha256"],
                        "blob_sha256": candidate_read["blob_sha256"],
                    }
                ],
                "claim_drafts": [
                    {
                        "draft_id": "history-claim",
                        "claim_key": "implementation-evolution",
                        "category": "implementation_method",
                        "scope_kind": "project",
                        "project_id": project_id,
                        "section": "project_story",
                        "statement_tokens": [
                            {
                                "kind": "text",
                                "value": "该项目",
                            },
                            {
                                "kind": "text",
                                "value": "的当前实现可结合受限历史证据解释技术演进。",
                            },
                        ],
                        "facets": ["implemented"],
                        "support_level": "single_source",
                        "personal_attribution": "none",
                        "review_semantic_projection": {
                            "concept_keys": ["implementation-evolution"],
                            "mechanism_keys": ["bounded-history"],
                            "behavior_contract_keys": ["current-to-legacy"],
                            "tradeoff_keys": [],
                            "technology_identifiers": ["git"],
                        },
                        "evidence_relations": [
                            {
                                "evidence_ref": current_implementation["evidence_id"],
                                "relation": "supports",
                                "supported_facets": ["implemented"],
                            },
                            {
                                "evidence_ref": "legacy-history",
                                "relation": "contextualizes",
                                "supported_facets": [],
                            },
                        ],
                    }
                ],
                "project_assessments": [
                    {
                        "project_id": project_id,
                        "dimension_scores_milli": {"system_depth": 850},
                        "coverage_bps": 10000,
                        "evidence_refs": [
                            current_implementation["evidence_id"],
                            "legacy-history",
                        ],
                        "gap_refs": [],
                        "rationale_tokens": [
                            {
                                "kind": "text",
                                "value": "当前实现和受限历史定位共同支持演进讲解。",
                            }
                        ],
                    }
                ],
                "knowledge_gaps": [],
            },
        },
    )
    assert analysis["status"] == "ok"
    assert analysis["run_status"] == "ready"

    database_bytes = (data_dir / "goodjob.sqlite3").read_bytes()
    assert historical_source.encode() not in database_bytes
    assert historical_magic_source.encode() not in database_bytes
    _close_broker(broker)

    fresh_broker = _broker(data_dir)
    other_workspace = tmp_path / "other-workspace"
    other_workspace.mkdir()
    other_authorized = _send_json(
        fresh_broker,
        {
            "op": "authorize_source_analysis",
            "workspace": str(other_workspace),
            "confirmed": True,
        },
    )
    other_receipt = _object(other_authorized, "receipt")
    rejected_cross_workspace = _send_json(
        fresh_broker,
        {
            "op": "query_history_candidates",
            "workspace": str(other_workspace),
            "authorization_receipt_id": other_receipt["authorization_receipt_id"],
            "preparation_run_id": preparation_run_id,
            "scan_run_id": scan_run_id,
            "role_lens_id": role_lens_id,
            "project_id": project_id,
            "worktree_id": worktree_id,
            "relative_paths": ["main.py"],
            "query_reason": "不得借用另一工作区的冻结快照",
            "maximum_candidates": 5,
        },
    )
    assert rejected_cross_workspace["status"] == "error"
    assert rejected_cross_workspace["code"] == "invalid_input"

    fresh_authorized = _send_json(
        fresh_broker,
        {"op": "authorize_source_analysis", "workspace": str(workspace), "confirmed": True},
    )
    fresh_receipt = _object(fresh_authorized, "receipt")
    rejected_reuse = _send_json(
        fresh_broker,
        {
            "op": "read_history_candidate",
            "workspace": str(workspace),
            "authorization_receipt_id": fresh_receipt["authorization_receipt_id"],
            "candidate_id": candidate_id,
            "selected_path": "main.py",
        },
    )
    assert rejected_reuse["status"] == "error"
    assert rejected_reuse["code"] == "invalid_input"
    _close_broker(fresh_broker)
