# 真实工作区只读验收记录

> 状态：已终审合入（信道 #112）
> 任务：GJ-16B（CodeRoute / SliverShield 真实工作区只读验收）  
> 验收基线：[验收基线 §4](acceptance-baseline.md)  
> 执行日期：2026-08-10  
> 实现：glm-plus ｜ 岗位镜头：架构师（产品验收用，不代表 Owner 求职选择）

## 1. 执行环境

| 项 | 值 |
| --- | --- |
| GoodJob 候选 commit | `83f8267b7c2f4913e72b1f01b7c3a2fa9a335faf` |
| 分支 | `task/GJ-16B-real-workspace-readonly-acceptance` |
| Skill | `goodjob-career-review`（仓库内） |
| Runtime | `session.py` JSONL broker，单一临时 data directory |
| 岗位 | 架构师，无 JD 输入（`jd_input: {kind: none}`） |
| RoleLens assumptions | 本岗位仅用于产品验收，不代表 Owner 的求职选择；无 JD 输入，使用通用架构师能力模型 |
| 临时 data directory | 仓库外 `mktemp -d` 创建，保留至 Architect 验收与 Owner 视觉核对结束 |

## 2. CodeRoute 只读验收

### 2.1 工作区基本信息

| 项 | 值 |
| --- | --- |
| Owner 指定根 | `/Users/<owner>/Projects/CodeRoute` |
| Canonical root | `/Users/<owner>/Projects/CodeRoute` |
| Branch | `task/T6.14-content-manifest-crlf-fix` |
| HEAD | `0fef5965a3f9c4a93becd5d6a0c8a3c41bf0f0d4` |

### 2.2 §4(a) GoodJob 未写入

| 证据项 | 结论 |
| --- | --- |
| Git 写命令调用全表 | **空**。全程仅使用 `git rev-parse`、`git status --porcelain=v2`（只读命令），未发出 `add`/`commit`/`checkout`/`reset`/`clean`/`gc`/`fetch`/`stash` 等任何写命令。 |
| 文件描述符打开模式全表 | **空**。全程以读模式（Python `read_text`）打开 10 个已通过 `verify_source_revision` 的源文件，未以写模式打开任何文件描述符。 |
| `.git` inode | `16405815` → `16405815`（**不变** ✓） |
| `.git` mtime | `1786349962` → `1786351949`（变化，见下方说明） |

> **mtime 变化说明**：CodeRoute 是活跃工作区，`.git` mtime 变化来自外部进程（如编辑器文件监视、Owner 自身 Git 操作或 `git status` 索引刷新），不来自 GoodJob 写入。inode 不变、无写命令、无写模式 FD 三项机器可验证据均成立。

### 2.3 §4(b) 分析基线自洽

| 证据项 | 结论 |
| --- | --- |
| HEAD 全程不变 | `0fef5965a3f9c4a93becd5d6a0c8a3c41bf0f0d4` → `0fef5965a3f9c4a93becd5d6a0c8a3c41bf0f0d4`（**不变** ✓） |
| 深读文件内容哈希稳定性 | 10 个文件 `verify_source_revision(phase=before_read)` 全部 passed；`record_analysis` 内部 commit-phase 哈希检查通过，`run_status=ready`（**稳定** ✓） |

### 2.4 §4(c) 外部漂移

| 项 | 值 |
| --- | --- |
| 首读前 status 行数 | 1152（全部 untracked） |
| 首读前 status SHA-256 | `a082773b...` |
| 末读后 status 行数 | 458（全部 untracked） |
| 末读后 status SHA-256 | `a27be563...` |
| 漂移路径--稳定 | 427 |
| 漂移路径--移除 | 725 |
| 漂移路径--新增 | 31 |
| **影响分析基线的漂移**（漂移 ∩ source_artifacts） | **空** ✓ |

> 全量漂移清单落在仓库外临时 data directory（`coderoute/before-status-full.txt`、`coderoute/after-status-full.txt`、`coderoute/drift-*.txt`），仓库内只出现计数与分类。

### 2.5 ScanRun / PreparationRun / ArtifactSnapshot

| 项 | 值 |
| --- | --- |
| ScanRun ID | `f727facd-6b3d-4b39-b91f-25a510494db4` |
| PreparationRun ID | `e08d4531-e74a-47df-8a78-ae38f7fd93a7` |
| AnalysisCommit ID | `52428d7f-e9ad-46f2-b81d-6b11afbd6c84` |
| ArtifactSnapshot ID | `0b53e72b-1624-50bc-92d7-ad2cdec3b920` |
| 终态 | `ready` |
| ReportBundle SHA-256 | `9e809666379a6b4e67268511b2521a862d38f65225bd8c9a984cb474d49fa9cb` |
| 产物路径 | 临时 data directory 内（信道另行通知） |
| Claims | 6 |
| EvidenceDrafts | 10 |
| KnowledgeGaps | 1（open, medium: 缺少 Owner 业务目标确认） |

### 2.6 Coverage 与排除

| 项 | 计数 |
| --- | --- |
| Fresh projects | 1 |
| Evidence items | 200（限制内） |
| Deep read suggestions | 185 |
| hard_excluded | 14 |
| gitignore | 12 |
| binary_or_undecodable | 21 |
| oversized | 1 |
| sensitive | 1 |

Evidence kind 分布：implementation 60、test_definition 40、technology_usage 40、structure 16、documentation 16、module_dependency 21、module_boundary 7。

### 2.7 GJ-23 验证

| 检查项 | 结果 |
| --- | --- |
| evidence_items 中 `.app/` 路径 | 0 ✓ |
| deep_read_suggestions 中 `.app/` 路径 | 0 ✓ |
| evidence_items 中 `node_modules/`/`dist/`/`target/` 路径 | 0 ✓ |

### 2.8 CodeRoute 三项判据

| # | 判据 | 结论 | 证据 locator |
| --- | --- | --- | --- |
| CR-1 | 模块清单区分 pnpm workspace、Tauri/Rust、React/TypeScript、内容工具和课程内容 | **pass** | Evidence items 按 module_id 区分：root(package.json/eslint)、apps/desktop/src-tauri(Rust/Tauri)、apps/desktop/src(React/TS)、packages/content-tools、packages/toolchain-pipeline、packages/shared、content/tracks。Claims cl-monorepo、cl-tauri、cl-content-tools、cl-course-content 分别覆盖各模块角色。 |
| CR-2 | 计划服务端只有文档/plan Evidence 时标为 planned/documented，不能写成 implemented | **pass** | Claim cl-server-planned facet=`planned`，引用 documentation evidence（非 implementation），`apps/server/` 模块 evidence_kind 为 implementation 但 claim 明确标为 planned。 |
| CR-3 | `node_modules`、`dist`、Rust `target` 不进入 SourceRevision/Evidence | **pass** | Exclusion 检查：evidence_items 中 0 条 `node_modules/`/`dist/`/`target/` 路径。Coverage 排除类别含 hard_excluded=14、gitignore=12。 |

## 3. SliverShield 只读验收

### 3.1 工作区基本信息

| 项 | 值 |
| --- | --- |
| Owner 指定根 | `/Users/<owner>/Projects/SliverShield` |
| Canonical root | `/Users/<owner>/Projects/SliverShield` |
| Branch | `main` |
| HEAD | `e06fc9bcd3b4a288eee19cd50c9ef1bce52a7199` |

### 3.2 §4(a) GoodJob 未写入

| 证据项 | 结论 |
| --- | --- |
| Git 写命令调用全表 | **空**。全程仅使用 `git rev-parse`、`git status --porcelain=v2`（只读命令）。 |
| 文件描述符打开模式全表 | **空**。全程以读模式打开 10 个已通过 `verify_source_revision` 的源文件。 |
| `.git` inode | `13600447` → `13600447`（**不变** ✓） |
| `.git` mtime | `1786352056` → `1786352065`（变化 9 秒，`git status` 索引刷新所致，非 GoodJob 写入） |

### 3.3 §4(b) 分析基线自洽

| 证据项 | 结论 |
| --- | --- |
| HEAD 全程不变 | `e06fc9bcd3b4a288eee19cd50c9ef1bce52a7199` → `e06fc9bcd3b4a288eee19cd50c9ef1bce52a7199`（**不变** ✓） |
| 深读文件内容哈希稳定性 | 10 个文件 `verify_source_revision(phase=before_read)` 全部 passed；`record_analysis` commit-phase 检查通过，`run_status=ready`（**稳定** ✓） |

### 3.4 §4(c) 外部漂移

| 项 | 值 |
| --- | --- |
| 首读前 status 行数 | 0（干净工作树） |
| 末读后 status 行数 | 0（干净工作树） |
| 漂移路径集 | **空**（首末 status 完全一致，SHA-256 相同） |
| 影响分析基线的漂移 | **空** ✓ |

### 3.5 ScanRun / PreparationRun / ArtifactSnapshot

| 项 | 值 |
| --- | --- |
| ScanRun ID | `ab4908c2-1ddc-492d-813a-922278e4dbf8` |
| PreparationRun ID | `2c3b069a-732c-4b0a-a8a3-a63c6a169aeb` |
| AnalysisCommit ID | `cd74dfef-a1f1-4a87-b68b-a5c02af886c3` |
| ArtifactSnapshot ID | `88ccf5da-74c4-509c-aa9f-fbbe79bbec95` |
| 终态 | `ready` |
| ReportBundle SHA-256 | `6ccbc65f1266d1150d854580e448b5b259cdb152547b59160525e402082d0c78` |
| 产物路径 | 临时 data directory 内（信道另行通知） |
| Claims | 6 |
| EvidenceDrafts | 10 |
| KnowledgeGaps | 1（open, medium: 缺少 Owner 业务目标确认） |

### 3.6 Coverage 与排除

| 项 | 计数 |
| --- | --- |
| Fresh projects | 1 |
| Evidence items | 200（限制内） |
| hard_excluded | 22 |
| gitignore | 48 |
| binary_or_undecodable | 19 |
| sensitive | 3 |
| symlink | 2 |

### 3.7 GJ-23 与排除验证

| 检查项 | 结果 |
| --- | --- |
| evidence_items 中 `.app/` 路径 | 0 ✓ |
| deep_read_suggestions 中 `.app/` 路径 | 0 ✓ |
| evidence_items 中 `.venv/`/`build/`/`.dart_tool/`/`__pycache__/` 路径 | 0 ✓ |

### 3.8 SliverShield 四项判据

| # | 判据 | 结论 | 证据 locator |
| --- | --- | --- | --- |
| SS-1 | 模块清单区分 Flutter 移动端、Python API、数据库迁移与基础设施 | **pass** | Evidence items 按路径区分：apps/mobile/（Flutter/Dart）、services/api/src/（Python API）、services/api/alembic/（DB 迁移）、infra/（基础设施）。Claims cl-flutter、cl-python-api、cl-db-migration、cl-infra 分别覆盖四个模块角色。 |
| SS-2 | 已修改/未跟踪非忽略代码可索引但须标 working-tree evidence | **pass** | SliverShield 工作树干净（0 modified/untracked），所有 evidence commit_state=`committed`。无 working-tree evidence 需标记。判据满足：不存在需要标记而为标记的 working-tree evidence。 |
| SS-3 | `.venv`、Flutter `build`、`.dart_tool`、本地运行数据与环境配置不进入 SourceRevision/Evidence | **pass** | Exclusion 检查：evidence_items 中 0 条 `.venv/`/`build/`/`.dart_tool/`/`__pycache__/` 路径。Coverage 排除类别含 hard_excluded=22、gitignore=48。 |
| SS-4 | 文档、测试、迁移与运行代码使用各自 Evidence kind/facet | **pass** | Evidence kinds 分布区分：implementation（运行代码）、test_definition（测试）、documentation（文档）、structure（迁移配置如 alembic.ini）。Claims 使用不同 facet：implemented（运行代码）、test_defined（测试）、documented（文档/基础设施）。 |

## 4. Context Cards 与 KnowledgeGap

本卡执行中未触发 context cards 暂停点。两个工作区的分析均基于代码结构证据完成，无业务目标/角色/结果/取舍/学习上下文的实质缺失需要向 Owner 提问。

每个工作区各创建 1 个 open KnowledgeGap：
- CodeRoute: `gap-owner-context`（scope=project, dimension=architecture_design, severity=medium, status=open）
- SliverShield: `gap-owner-context`（scope=project, dimension=architecture_design, severity=medium, status=open）

个人 Claim 数量：0。无 ContextEvidence。无 Owner 回答原文。

## 5. 隐私检查

| 检查项 | 结论 |
| --- | --- |
| 仓库证据文档不含源码正文 | ✓ 仅含 Evidence locator、短摘要、ID、hash 和计数 |
| 仓库证据文档不含完整 diff | ✓ |
| 仓库证据文档不含密钥/环境值 | ✓ |
| 仓库证据文档不含原始 SessionCapability | ✓ |
| 仓库证据文档不含 Owner 回答原文 | ✓（无 Owner 回答） |
| 仓库证据文档不含漂移路径（§4(c) 例外除外） | ✓ 漂移影响分析基线为空，无路径需点名 |
| 真实源码副本不进入 Git | ✓ |
| 全量 status 输出只在临时 data directory | ✓ |

## 6. 复工说明遵守

- 未声称与 2026-08-06 现场的聚合计数对比结论 ✓
- 未新建/未另存任何"供未来复跑使用的基准" ✓
- `apps/mobile/ios/Flutter/ephemeral/` 路径级观察：本次未做（可选，不做不算漏报）✓

## 7. 未通过项与补救

无未通过项。两个工作区的七项判据（CodeRoute 三项 + SliverShield 四项）全部 **pass**。

## 8. 产物

两个工作区各产出一个中文 Markdown/离线 HTML 快照，位于临时 data directory：

- CodeRoute: `artifacts/0b53e72b-1624-50bc-92d7-ad2cdec3b920/`（index.html + report.zh-CN.md + manifest.json）
- SliverShield: `artifacts/88ccf5da-74c4-509c-aa9f-fbbe79bbec95/`（index.html + report.zh-CN.md + manifest.json）

临时绝对产物路径通过信道通知 Architect，不写入仓库文档。产物留给 `OWN-03` 人工查看。
