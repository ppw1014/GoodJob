## SWO-127 扫描问题分组摘要与完整审计记录取证

在 `goodjob.scanner.WorkspaceScanner.overview` 中升级了 `scan-overview-v2` 契约：

### 契约特性与保障

- **全量审计保留**：底层 `scan_issues` SQLite 表完整保存每条诊断记录（包括 1000+ 条符号链接跳过诊断），全量查询与排障能力不受任何影响。
- **确定性分组摘要**：在 `scan_overview.issue_groups` 中按 `(severity, kind, remediation)` 稳定维度聚合，返回分组 `count`、最多 3 条代表性样本（`samples`）以及省略数量（`omitted_count`）。
- **严重性配额优先**：在 `scan_overview.issues` 原始问题列表中，优先保障 `error` 与 `warning` 全部可见，剩余配额再填充 `info` 级诊断，防止被大量重复 info 挤占。
- **边界统计**：在 `limits` 中补充 `group_count`、`error_count`、`warning_count` 与 `info_count`。

### 可重复验证

在 runtime 目录执行：

```text
uv run pytest -q tests/test_scanner.py -k 'test_scan_overview_groups_repetitive_issues_and_retains_audit_records'
```
