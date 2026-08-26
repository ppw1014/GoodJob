# Operation Contracts

Read the section for the current workflow stage immediately before sending those operations. Runtime responses and validation remain authoritative; this reference does not replace version-matched launcher help or structured reports.

## Authorization, Input, And Scan

Send `authorize_source_analysis` only after the Owner confirms the displayed workspace scope. Keep its receipt ID only as bookkeeping and perform every protected operation in the broker that issued it.

Before scan selection or source reading, send `validate_job_input` with `contract_version=job-input-v1`, the target role, optional JD input, inferred level, and optional override. Do not persist private JD data. For a file JD, use the exact returned `jd_source_path`. Keep the returned `validation_sha256` in task memory and pass it unchanged to scan overview, scan, refresh, and preparation. A failed validation invalidates earlier digests in this task; after correction or explicit `continue_without_jd`, validate again.

Call read-only `scan_overview` first. Reuse a suitable terminal ScanRun and its recorded role-lens context; otherwise run `scan`. Run `refresh` only on explicit Owner intent or to resolve a stale or mismatched baseline. Use only broker-returned data. Treat `authorization_session_mismatch` and `writer_busy` as explicit outcomes; never copy a receipt, read storage directly, or remove locks.

## External Git Metadata

When scanning reports `external_git_authorization_required`, do not guess or read root-external paths. Send `inspect_external_git_candidate` with the source receipt and show the exact marker kind, candidate directories, and proposed fields. Obtain explicit confirmation before `authorize_external_git_relation_probe`.

Probe with both receipts, show the resolved directories, identities, and fields, then obtain a second explicit confirmation before `authorize_external_git_metadata`. Pass only that metadata receipt back to scan or refresh. This mode reads the bound relation and minimal head/ref metadata only. It does not invoke Git or read an external index, dirty state, history, objects, blobs, diffs, configuration, other worktrees, or source.

## Preparation And Evidence Reading

Build one `PreparationRequest v1` and RoleLens from the validated target, JD or level, general role knowledge, and the bounded role-neutral scan context. Honor every truncation marker. Use a fresh retry-stable request ID, exact terminal scan ID and config revision, optional exports, and a bounded evidence limit.

Treat `prepare_start.preparation_run.status` as authoritative:

- `analyzing` provides the bounded `EvidenceBundle v1` used for evidence work.
- `refresh_required` stops analysis until the Owner explicitly authorizes refresh.
- `failed` with `no_eligible_projects` is not a compared project set.

The same request ID is idempotent only for byte-equivalent canonical input in the same task. A changed request or new task needs a new ID.

Before opening suggested source, send `verify_source_revision` with the active preparation run, `phase=before_read`, and the exact bounded revision IDs. Open only passed entries at their returned workspace-relative paths. Match the returned worktree ID, worktree root, relative path, and hash to the EvidenceBundle suggestion; never guess a nested-worktree path. Any mismatch makes the run `refresh_required`. Create a source EvidenceDraft only for a revision that passed, using its frozen hash, worktree, path, scanner evidence kind, and commit state.

Use `query_history_candidates` only when a concrete role question cannot be answered from frozen current sources and recent history. Bind the active run, scan, RoleLens, internal project or worktree, bounded indexed paths, and short reason. Select a returned candidate, then call `read_history_candidate` for one returned path. Diff or blob text is transient model input and never belongs in an EvidenceDraft. A Git EvidenceDraft carries only returned identifiers, path metadata, query reason, commit, and hashes. Root-external projects are ineligible.

## Context Interview

When missing business goal, target user, role or ownership, outcome or metric, tradeoff, or learning context would materially change the package, create one `context-interview-request-v1`. Use one card per affected eligible project, versioned stable question IDs, requested fact kinds, and compact prompts. Ask once, not once per Claim.

Send one complete `interview-input-v1` batch in context mode with exactly one `answered`, `partial`, or `skipped` item per card. Extract only facts the Owner stated and only a requested fact kind. Reference only returned Evidence IDs; do not query storage. Page through returned frozen context evidence when reported as truncated. Every partial or skipped project must retain a visible open KnowledgeGap. A user statement never proves implementation or test status.

## Atomic Record And Publication

After evidence reading and optional interview, build one complete `analysis-commit-v1` as defined in [analysis contracts](analysis-contracts.md). Send it through `record_analysis` in the same broker. The runtime rechecks every used source revision and targeted history proof, validates all analysis relationships, and writes the complete set atomically.

Source drift returns `run_status=refresh_required`, persists only commit checks and mismatches, and creates no analysis entities. Other invalid input rolls back while the run remains analyzable. `run_status=ready` means the immutable analysis may be rendered. Retry a request ID only with byte-equivalent canonical content in the same task.

Call `render` only for a ready frozen run. Rendering reads no workspace source and may be repeated later using the run ID only as a selector, never as authorization. Open only returned offline artifacts. A failure leaves the prior `latest` target unchanged; retry the same frozen bundle rather than reconstructing conclusions.

For English export, require a successfully published ArtifactSnapshot and current workspace authorization. Call `translate_export` with `translation-export-request-v1`, `action=prepare`, the exact snapshot, target language, and both requested export kinds. Keep the returned frozen projection only in task memory. Translate every item exactly once while preserving its identifiers, references, mapping, anchors, numbers, units, technology identifiers, and evidence strength. Publish in the same broker using the exact projection hash and complete candidate set. Never persist intermediate candidates. A new task must prepare again. Failed publication changes neither the source snapshot nor `latest`.

For mock review, require a published snapshot and fresh workspace authorization in the current task. List targets using the source preparation run, then use only returned questions, target and binding IDs, continuity state, and frozen prompt tokens. Record one bounded summary with the returned IDs, mastery level, weak points, and optional next-review date. Never store transcript, audio, raw answer, reminder request, or invented fact. Recording a review never rewrites published artifacts; semantic or gap changes require reassessment rather than inherited mastery.
