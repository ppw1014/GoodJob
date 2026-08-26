# 任务卡 GJ-16A：补齐多工作树合成证据

- 状态：✅ 已验收合入（信道 #62，merge `b7be588`） ｜ 实现：Sol-Impl ｜ 出卡/评审：Sol
- 对应 backlog：里程碑 M2 · 批次 G · GJ-16A
- 前置任务：GJ-14、GJ-15 已验收合入；Owner 已于 2026-08-05 裁定 `OWN-01`
- 分支：`task/GJ-16A-worktree-synthetic-evidence`，从派卡消息指定的本地 `main` 基线拉出
- **验收强度**：对抗（[protocol §2.2](../protocol.md)）——Git 工作树元数据、dirty 状态与分支差异会直接决定证据作用域；错误折叠会把某一分支的实现冒充为整个项目的当前事实
- **体量预授权**：合成 fixture 与联合断言预计 240 行，手写 gross 参考上限 320 行；超出时按 protocol §7 如实披露，不得以压缩断言换取行数

> 本卡只有一个目标：让已从真实 CodeRoute 验收移除的多工作树保真能力拥有完整合成证据。它不扫描 CodeRoute/SliverShield，不修改产品行为，也不把 `IMP-03` 的其他非法 config 组合顺手塞进来。

## 背景与出卡审计

Owner 裁定不再重建已清理的 CodeRoute 临时工作树，原三项能力由合成测试承担。Architect 在出卡前打开并实际运行以下六个节点，结果为 `6 passed`：

- `test_root_internal_linked_worktree_is_grouped_without_external_authorization`：根内 linked worktree 归并为一个 Project；
- `test_root_external_linked_worktree_requires_two_stage_authorization_and_never_reads_history`：根外 `.git` 指针的候选绑定、两阶段授权和禁读历史；
- `test_git_directory_with_external_commondir_uses_the_same_candidate_bound_protocol`：根内 `.git` 目录指向根外 common-dir 的同一协议；
- `test_same_content_worktrees_reuse_analysis_and_keep_expandable_sources`：相同内容只分析一次但保留两个来源；
- `test_module_claim_requires_worktree_scope_without_equivalent_branch_coverage`：跨工作树提升必须具有等价覆盖，限定到单工作树的 Claim 可接受；
- `test_carried_forward_evidence_keeps_snapshot_worktree_provenance`：冻结报告保留证据所属旧 branch/HEAD，而非误取当前观察。

缺口是：没有一个由真实 Git 命令构造的合成场景同时证明三个工作树的 branch、HEAD、dirty 状态、相同内容复用和分支差异来源隔离；报告测试也没有断言 `dirty_state` 随冻结 provenance 输出。发布账本因此仍把 `IMP-03` 记为 `partial`。

## 目标

用一个三工作树合成集成场景和两个最小既有测试加强项，证明扫描层不会丢失工作树状态、不会重复分析等价内容、不会把分支独有内容扩散到其他工作树；同时证明分析层只能把完整等价覆盖提升到 module/project scope，并且冻结 `ReportBundle` 保留 dirty provenance。

## 涉及范围

- **允许**：
  - `.agents/skills/goodjob-career-review/runtime/tests/test_scanner.py`
  - `.agents/skills/goodjob-career-review/runtime/tests/test_analysis.py`
  - `.agents/skills/goodjob-career-review/runtime/tests/test_reporting.py`
  - `docs/collab/channel.md` 物理 EOF 追加协作消息（元协议豁免）
- **禁止**：其余一切文件。特别禁止修改 `src/goodjob/`、broker、schema/migration、前端、Skill 说明、依赖/lockfile、Makefile、权威文档、证据账本、backlog、真实工作区和个人数据目录
- **依赖**：零新增依赖；只使用现有 pytest helper、Python 标准库与本机 Git，不访问网络、不设置 remote、不运行合成项目代码

## 接口契约

### 契约 1：三工作树真实 Git fixture

在 `tmp_path` 授权根内建立一个 primary worktree 与两个 linked worktree，三者共享同一 common-dir：

1. primary 分支名固定为 `main`；两个 linked 分支使用稳定且互异的名字。
2. primary 与 linked-A 指向同一个 base HEAD；linked-B 在自己的分支增加一个已提交的独有实现文件并形成不同 HEAD。
3. 三者都包含一个字节完全相同的公共实现文件；另有一个已提交的跟踪文件只在 linked-A 留下未提交修改；linked-B 再增加一个未跟踪源码文件。预期 dirty 映射必须明确为 primary=`clean`、linked-A=`modified`、linked-B=`untracked`。
4. fixture 只通过参数数组调用 Git，不设置 remote、hook 或外部 common-dir；不得 mock Git 输出、直接预填 SQLite 或复制扫描器状态。

核心测试名固定为 `test_three_worktrees_preserve_branch_state_and_divergent_evidence`，便于账本精确引用。

### 契约 2：扫描与来源保真

测试通过现有真实 scanner 路径扫描一次，并至少断言：

1. coverage 为一个 Project、三个 Worktree，无根外 Git 授权问题；数据库中的三个 `worktree_observations` 按 canonical root 精确对应预期 branch、HEAD 与 dirty 状态。
2. 对公共实现文件的 `analyze_file` 调用恰好一次；其扫描 Evidence 具有同一个非空 `content_equivalence_key`，但保留三个不同 worktree/source 来源。
3. linked-B 的分支独有已提交实现 Evidence 只绑定 linked-B；linked-A 的 modified Evidence 与 linked-B 的 untracked Evidence 各自具有正确 `commit_state`，不得出现在其他工作树来源集合中。
4. 断言使用结构化 SQL 行与公开 coverage 字段，不通过对整个 JSON/源码做模糊字符串搜索判绿。

### 契约 3：Claim 作用域与冻结 provenance

1. 加强既有 `test_module_claim_requires_worktree_scope_without_equivalent_branch_coverage`：项目含三个 worktree 时，缺少任一工作树等价 Evidence 的 module/project 提升仍被拒绝；三者同一 equivalence key 的完整覆盖可提升；分支独有 Evidence 只能形成带精确 `worktree_id` 的 worktree 或 module-worktree Claim。不得把“设置了 worktree_id 但未核 scope”冒充 worktree-scope 证据。
2. 加强既有 `test_carried_forward_evidence_keeps_snapshot_worktree_provenance`：为旧观察设置一个非默认 `dirty_state`，为当前观察设置不同值，最终 `ReportBundle.evidence[].worktree` 必须同时取旧 branch、旧 HEAD、旧 dirty 与旧 scan run ID。
3. 本卡不要求自动生成 Claim；证明 Repository 的失败关闭与允许边界即可。若公共校验实际无法表达 worktree-scope，按 L1 保留最小失败现场，不修改产品。

## 验收标准（DoD）

- [ ] 三工作树核心测试使用真实 Git/真实 scanner，精确断言一个 Project、三个 Worktree、三个 branch/HEAD/dirty 映射。
- [ ] 公共文件只分析一次但有三个可追溯来源；branch-only、modified、untracked Evidence 均不跨工作树污染。
- [ ] 分析测试同时证明不完整提升被拒、完整等价覆盖可提升、工作树独有差异只能限定到精确 worktree。
- [ ] 报告测试证明 frozen branch/HEAD/dirty/scan-run provenance 一起保留。
- [ ] 出卡审计的六个既有节点与本卡新增/加强节点统一运行，命令非空且全绿；交付报告列出实际测试数。
- [ ] `make gate-release` 在候选 HEAD 明确退出 0，并报告动态 Python、前端、文档、双引擎和构建计数。
- [ ] `git diff --stat` 只有三个允许测试文件与信道追加；产品、schema、依赖、权威文档、账本及真实工作区零改动。
- [ ] Implementer 不自行更新 `acceptance-evidence.md` 或把 `IMP-03` 标为 `verified`；由 Architect 验收后逐子句裁定。

## 边界与停工条件

- 不创建、删除或修改任何 CodeRoute/SliverShield worktree；所有 Git 状态只存在于 pytest `tmp_path`。
- 不补 `IMP-03` 中“全部非法 config 组合”这一既有剩余缺口；该子句与 OWN-01 移除的真实验收场景无关，继续留在账本。
- 现有产品行为若不能满足上述测试，属于 L1：报告 `现象 / 影响 / 最小复现 / 建议拆卡`，不得在 test-only 卡内修 runtime。
- 允许范围内的 helper 命名与 fixture 排列属于绿区；交付报告仍需在“自主决策”节披露。

## 交付物

信道交付报告须包含：候选 commit 与固定基线、三工作树现场映射、等价/差异 Evidence 行摘要、Claim 正反边界、frozen provenance、聚焦测试与完整门禁结果、gross、范围核验、自主决策、全部 L1/L2/L3（无则明确写“无”）。
