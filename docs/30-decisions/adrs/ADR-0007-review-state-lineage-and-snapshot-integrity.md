# ADR-0007：复习状态谱系与冻结快照完整性

> 状态：已接受（Codex task awaiting-context 条款部分已由已接受的 [ADR-0010](ADR-0010-host-agent-neutral-session.md) 替代）
> 日期：2026-07-24  
> 权威范围：PreparationRun 对源码版本的绑定、复习状态跨快照连续性、静态看板更新与派生导出完整性  
> 上游：[产品需求](../../10-product/product-requirements.md)  
> 下游：[系统设计](../../20-architecture/system-design.md)、[证据模型](../../20-architecture/evidence-model.md)、[产物设计](../../20-architecture/artifacts-and-learning.md)

## 背景

GoodJob 的分析会跨越多个文件读取和模型回合；期间源码可能变化。只在最终写入时比较部分哈希会产生“同一报告混用两个代码状态”的风险。另一方面，模拟面试的题面会随岗位镜头和材料变化；若按文本相似度延续掌握度，会把不同知识主题混在一起。离线 HTML 又必须保持不可变，不能靠浏览器直接回写数据库。

## 决策

1. PreparationRun 在 preflight、每次原文件读取前和 record_analysis 提交事务内三次校验实际使用的 SourceRevision。任一文件 missing、unreadable 或 SHA-256 mismatch 时，运行转为终态 `refresh_required`。
2. `refresh_required` 不隐式执行 scan/refresh，不创建新的 Evidence、ClaimRevision、ProjectAssessment、ArtifactSnapshot 或 DerivedExport，不改变旧基线与 `latest`。Owner 显式 refresh 后启动新的运行。
3. 每个复习主题使用稳定 `ReviewTarget`：锚定逻辑 `Claim.claim_id` 或版本化 `topic_key`，不使用问题文本或摘要作为身份。
4. 每个运行创建 `ReviewTargetBinding`。Repository 从 canonical `ReviewSubjectProjection` 计算 fingerprint：稳定 `claim_id + review_semantic_sha256`、稳定 `gap_key + 结构化状态`、topic/question contract version；不得直接使用 ClaimRevision/KnowledgeGap ID、题面或 statement。
5. 纯文案改写、展示顺序、行号变化或等价 Evidence 替换保持同一 review semantic hash；概念、机制、行为、取舍、实现/测试/冲突/证据时效、角色/结果锚点或缺口状态实质变化时必须改变。无法验证结构化投影时标 `unverified`，把 normalized statement + fact/evidence anchor hashes 的 fallback hash 纳入投影并保守重评，不用 Revision ID 代替。
6. 只有 review target 与 fingerprint 均相同，才能把截止当前运行创建时间前的最新复盘投影为 `continued`；目标相同但 fingerprint 变化时只显示历史并标 `reassess_required`；不同项目、作用域或 topic key 不合并。
7. 离线看板保持只读。新复盘通过 Skill/Python 持久化，再由 Owner 显式创建新的 PreparationRun；仅更新复习状态时可以复用同一 ScanRun，但仍产生新的不可变 ArtifactSnapshot，旧快照不改写。
8. `awaiting_context` 持久化、不持锁且不按时间过期，但只允许持有原 SessionCapability 的同一 Codex task 继续。task/capability 丢失后，新 task 经 Owner 重新授权只能显式 `abandon_and_restart`：旧 PreparationRun 标 interrupted，新 run 复用终态 ScanRun/持久上下文，不恢复模型内存。ScanRun/RenderAttempt 只有 owner PID+启动标识确认不存在时才自动中断。
9. 翻译候选只留在当前 task 内存。首次文件写入前，单个持锁发布子进程创建 `ExportAttempt`，记录 PID+启动标识并预登记 attempt-scoped temp/final path。只有校验、原子改名和数据库提交完成后才创建 DerivedExport；确认 owner 进程已不存在后，中断恢复才按账本清理 temp 或无 DerivedExport 的 final path，不扫描其他 exports。

## 影响

- 一个已发布快照只描述一个可验证源码版本集合，不会发生隐式半刷新。
- 复习连续性既避免题面误合并，也避免纯措辞改写造成频繁假重评；实质变化才要求重评。
- 英文导出中断有可审计 attempt 和精确清理边界，不产生不可归属孤儿目录。
- 新复盘会产生新快照，因此历史更完整但占用更多本地空间；首版显示存储用量，不自动删除。
- 静态看板无需常驻服务或浏览器写权限。

## 否决方案

- 只在准备开始时校验哈希：无法覆盖长时间分析中的文件变化。
- 发现变化后自动 refresh 并继续：会在用户不知情时改变分析基线。
- 按题目文本相似度迁移掌握度：身份不稳定，容易跨项目和主题串线。
- 直接 hash ClaimRevision/KnowledgeGap ID：任何改写都会失去连续性，产生过多 false negative。
- 不记录导出尝试、只依赖原子改名：崩溃后无法证明临时或孤儿 final 目录的归属。
- 让 HTML 直接写 SQLite：需要服务或高风险本地桥接，违背离线静态边界。
- 修改旧快照中的复习状态：破坏不可变报告与可重放性。

## 验证

- 三个校验阶段任一修改、删除或权限收回都会得到 `refresh_required`，且数据库无分析半提交、`latest` 不变。
- 同一稳定目标中，纯 statement 改写与等价 Evidence 替换保持连续；修改概念/机制/facet/conflict/evidence validity/角色结果锚点或 gap 状态后变为“需重评”。
- 相似题面但不同项目/作用域/topic key 不共享掌握度。
- 完成新复盘并复用原 ScanRun 生成新快照后，旧 HTML 的状态与文件 hash 保持不变。
- 分别在英文导出的写 temp、原子改名后、数据库提交前终止进程；恢复只清理该 ExportAttempt 的预登记路径，既有 DerivedExport 不受影响。
