## SWO-127 扫描问题分组摘要与完整审计记录取证

在 `goodjob.scanner.WorkspaceScanner.overview` 中升级了 `scan-overview-v2` 契约：

### 契约特性与保障

- **全量审计保留**：底层 `scan_issues` SQLite 表完整保存每条诊断记录（包括 1000+ 条符号链接跳过诊断），全量查询与排障能力不受任何影响。
- **确定性分组摘要**：在 `scan_overview.issue_groups` 中按 `(severity, kind, remediation)` 稳定维度聚合，返回分组 `count`、最多 3 条代表性样本（`samples`）以及省略数量（`omitted_count`）。
- **严重性配额优先**：在 `scan_overview.issues` 原始问题列表中，优先保障 `error` 与 `warning` 全部可见，剩余配额再填充 `info` 级诊断，防止被大量重复 info 挤占。
- **边界统计**：在 `limits` 中补充 `group_count`、`error_count`、`warning_count` 与 `info_count`。

### v1 到 v2 迁移

`scan-overview-v2` 保留了 v1 已有的 `issues`、`limits.issue_limit`、`limits.available_issues` 和 `limits.issues_truncated` 字段及其含义。只依赖这些字段的 host 可以继续读取同一终态 ScanRun；不需要 SQLite 数据迁移，因为原始 `scan_issues` 记录没有改写。

迁移后的 host 应根据 `contract_version` 识别 v2，并在可用时优先展示 `issue_groups`：每个分组的 `severity`、`kind`、`remediation`、`count`、`samples` 和 `omitted_count` 共同说明覆盖范围和省略原因。`issues` 仍是确定性、有严重性优先级的有界列表，不能被当作完整审计记录。

实现通过数据库聚合、窗口样本和带 `LIMIT` 的原始问题查询构建摘要；Python 进程不再把全部 `scan_issues` 行载入后才截断。SQLite 中的完整审计记录仍保留给受控排障查询。

### 可重复验证

在 runtime 目录执行：

```text
uv run pytest -q tests/test_scanner.py -k 'test_scan_overview_groups_repetitive_issues_and_retains_audit_records'
```

补充边界验证：

```text
uv run pytest -q tests/test_scanner.py -k 'test_scan_overview_small_quota_is_deterministic_and_preserves_v1_fields'
```
