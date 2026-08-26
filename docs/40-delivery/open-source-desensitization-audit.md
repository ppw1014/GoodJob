# GJ-22 开源前脱敏审计报告

> 状态：已终审（信道 #123）
>
> Owner 已在信道 #124/#125 执行条件 1、明确跳过条件 2/3，并将仓库切换为 public。
>
> 审计主体：`5af141340d72233b95e4c1c59de9fb7e73e6330b`
>
> 审计日期：2026-08-11
>
> 上游：[`docs/collab/tasks/GJ-22.md`](../collab/tasks/GJ-22.md)、[验收基线](acceptance-baseline.md)

本报告只回答“当前仓库历史与工作树是否具备公开前提”，不改变仓库可见性、不改写历史，也不替 Owner 作最终 public 决策。报告中的定位只保留类别、commit、路径和行号；不复述已核查的身份、路径或任何疑似秘密原文。

## 1. 结论摘要

| 维度 | 结论 | 覆盖范围 | 关键结果 |
| --- | --- | --- | --- |
| 密钥/凭据 | ⚪ | 149 个 commit、476 个唯一 blob | 敏感文件名、私钥、云 token、提交消息秘密均为零；8 个宽口径赋值命中折叠为 3 种非凭据形状 |
| PII | ⚪ | 文件内容、author/committer、提交消息 | 未发现未获授权的可定位自然人 PII；Owner 已在 #115/#117/#119 精确裁定的身份和历史路径实例按白名单保留 |
| 真实扫描内容 | ⚪ | 全历史 blob、当前工作树、合成 fixture | 没有 CodeRoute/SliverShield 真实源码、真实 JD 或 Owner 回答原文 |
| 叙事文档 | ⚪ | 7,153 行人工通读 | 内容是 GoodJob 自身设计、协作与合成验收；未发现额外真实生产代码或未授权身份线索 |
| 全部 branch/ref | ⚪ | 24 个 branch 输出、4 个内部 tree ref | 所有 commit 已被审计主体包含；tree ref 没有 commit 图之外的 blob |
| 前瞻性防护 | 🟡 | 根与 runtime/frontend 忽略规则 | 常见缓存已覆盖，但 build、egg-info、repo-local 数据和 `.env` 等仍需开源前补窄规则 |

**总体建议：有条件建议开源。** 在改 public 前，先提交窄范围 `.gitignore` 防护、确认运行时 data directory 位于仓库外，再以最终候选 SHA 重跑本报告中的全历史扫描。已核查并获 Owner 裁定的历史身份/路径实例不要求历史改写；这不扩展到未来新出现的 PII。

## 2. 审计基线与方法

`audit_subject_head` 固定为 `5af1413`。本报告提交不属于被审计主体，避免把自身提交混入计数。审计主体包含 149 个 `git rev-list --all` commit、118 个历史路径和 476 个唯一 blob；二进制与超过 5 MB 的 blob 均为 0，因此没有因大小或编码跳过内容。

独立扫描使用一次性标准库脚本，对每个唯一 blob 做校准后的模式扫描，并另用 `git show` 对每个 commit 的 author、committer 和提交消息扫描：

```text
python3 /private/tmp/goodjob-gj22.SIITOR/audit_history.py
python3 /private/tmp/goodjob-gj22.SIITOR/audit_commits.py
```

脚本 SHA-256 分别为 `28999d31e0a968a081b0b540deb23ac7ded04b1b646beea95d9eacf23bdfa133` 与 `454fc22ed3883c1b1bcbe93c9764e1e09e651c272d6591b1a236754bc7f84d2f`。两份脚本的正、负校准均通过。评审发现初始通用赋值模式带有最小值长度过滤后，又对同一 476 个 blob 增跑一次**值长度不限**的宽口径扫描；PII 另增跑姓名/地址标记扫描。除脚本输出外，本报告还用 `git log -S`、逐 commit `git grep`、`git ls-tree` 与 `git cat-file` 做定点复核。

## 3. 六维度审计

### 3.1 密钥与凭据

检查了私钥头、AWS/GitHub/Slack token 形状、通用凭据赋值形状及敏感文件名（包括 `.env`、credentials、私钥扩展名、SQLite/DB 等）。结果：

- `sensitive_paths=[]`；118 个历史路径中没有命中敏感文件名。
- 私钥、AWS、GitHub、Slack 模式均为 0；提交消息中的秘密模式也是 0。
- 值长度不限的宽口径扫描得到 8 个历史 blob 命中，折叠为 3 种逻辑形状：`analysis.py` 的局部解析变量（5 个历史 blob，代表定位 `02395ac6:279`）、`exporting.py` 的 `InlineToken` 局部对象（2 个历史 blob，`02395ac6:235`、`8169113:269`），以及合成 fixture 的 `example.invalid` URL 查询参数（1 个 blob，`37871de:prototypes/dashboard/fixture/report-bundle.json:486`）。逐项复核均不是认证凭据。
- 扫描覆盖全部 149 个 commit 的唯一 blob，没有跳过二进制或大文件。

结论：⚪ 已核查确认干净。

### 3.2 PII 与 Owner 例外

文件内容独立扫描得到 250 个邮箱形状：168 个示例地址、49 个 agent no-reply 地址、33 个 Owner 已批准身份的历史 blob 命中；未分类邮箱为 0。手机号形状为 0。author 与 committer 的姓名和邮箱元数据在 149/149 个 commit 中均为同一项 Owner 已批准身份，author/committer name 各只有 1 个唯一值；提交消息中该身份出现 1 次（`261d96c`，消息第 3 行）。

姓名/地址另做全历史标记扫描：姓名标记共 81 个历史 blob 行，折叠为 13 条唯一文本；地址标记共 6 个历史 blob 行，折叠为 2 条唯一文本。逐条人工复核后，前者都是 runtime 的 author/committer 字段名或 GJ-22 的审计措辞，后者是 GJ-22 的 PII 类别说明与看板原型的“地址栏”操作，不含自然人姓名值或物理地址。

为避免在公开报告中复制身份原文，具体文件定位只列路径与行号：

- 当前 `docs/40-delivery/backlog.md:306`、`docs/collab/tasks/GJ-22.md:47`、`docs/collab/channel.md:3406,3700,3750` 含该已批准身份的披露；该身份**经 Owner 2026-08-11 裁定同意公开**。
- Owner 本机目录段的当前实例仅在 `docs/collab/channel.md:2266,2290,2453,3320`、`docs/collab/protocol.md:204`、`docs/collab/implementer-bootstrap.md:10`、`docs/collab/tasks/GJ-16B.md:15`、`docs/collab/tasks/GJ-18.md:31`；这些属于 #117 精确列举的协作记录白名单。
- `README.md` 与 `docs/40-delivery/real-workspace-acceptance.md` 当前使用占位符（`d72752f`）；更早 blob 中的同类实例仍随完整历史可见，范围已由 #117 按文件精确裁定。
- `docs/40-delivery/acceptance-baseline.md` 的同类实例只存在于历史版本：`25b7b93b` 引入、`4c61aebee` 移除，149 个可达 commit 中 91 个快照包含；当前工作树为零命中。#119 已将这一明确区间纳入白名单。

除上述 #115/#117/#119 记录的 Owner 身份与路径线索外，没有发现其他可定位自然人的 PII。结论：⚪ 已核查确认干净（例外均为 Owner 明确裁定范围，不代表跳过 PII 检查）。

### 3.3 真实扫描内容泄漏

使用当前工作树 `rg`、全历史逐 commit `git grep` 和路径名审计交叉检查：

- 当前非 `docs/` 路径没有 `CodeRoute` 或 `SliverShield` 命中；历史命中全部位于仓库自身的叙事/验收文档，另有一处测试 fixture 的合成包名。
- `CodeRoute` 的唯一非文档历史命中是 commit `c5dfddd6` 的 `tests/test_packaged_app_exclusion.py:48`，只用于验证 `.app` 根目录排除；不是外部仓库源码。
- `prototypes/dashboard/fixture/report-bundle.json` 使用合成项目标签、示意路径和示意 Claim，不含真实源码正文或真实工作区文件。
- 未发现真实 JD 文件、Owner context card 回答原文、数据库/导出包中的外部项目内容；真实工作区验收文档只保留 locator、hash、计数和短结论。

结论：⚪ 已核查确认干净。

### 3.4 叙事文档语义通读

我在审计主体上逐文件通读下表范围，并对新增的 #118/#119、GJ-22 卡面和 backlog 增量重新阅读。通读不以正则命中代替语义判断：重点核对真实公司/JD 原文、生产代码片段、Owner 额外身份线索和外部项目源码。

| 文件/集合 | 通读行数 |
| --- | ---: |
| `docs/collab/channel.md` | 3,801 |
| `docs/40-delivery/backlog.md` | 322 |
| `docs/40-delivery/acceptance-evidence.md` | 139 |
| `README.md` | 318 |
| `docs/index.md` | 113 |
| `opus-review.md` | 114 |
| `docs/40-delivery/real-workspace-acceptance.md`（补充验收记录） | 231 |
| `docs/collab/tasks/GJ-01.md` | 72 |
| `docs/collab/tasks/GJ-02.md` | 67 |
| `docs/collab/tasks/GJ-03.md` | 72 |
| `docs/collab/tasks/GJ-04.md` | 94 |
| `docs/collab/tasks/GJ-05.md` | 72 |
| `docs/collab/tasks/GJ-06.md` | 91 |
| `docs/collab/tasks/GJ-07.md` | 94 |
| `docs/collab/tasks/GJ-08.md` | 96 |
| `docs/collab/tasks/GJ-09.md` | 73 |
| `docs/collab/tasks/GJ-11.md` | 102 |
| `docs/collab/tasks/GJ-12.md` | 100 |
| `docs/collab/tasks/GJ-13.md` | 70 |
| `docs/collab/tasks/GJ-14.md` | 150 |
| `docs/collab/tasks/GJ-15.md` | 130 |
| `docs/collab/tasks/GJ-16A.md` | 87 |
| `docs/collab/tasks/GJ-16B.md` | 103 |
| `docs/collab/tasks/GJ-18.md` | 74 |
| `docs/collab/tasks/GJ-19.md` | 74 |
| `docs/collab/tasks/GJ-22.md` | 83 |
| `docs/collab/tasks/GJ-23.md` | 62 |
| `docs/30-decisions/adrs/ADR-0001-skill-and-state-isolation.md` | 38 |
| `docs/30-decisions/adrs/ADR-0002-python-and-offline-typescript-dashboard.md` | 39 |
| `docs/30-decisions/adrs/ADR-0003-evidence-pointers-without-source-snapshots.md` | 37 |
| `docs/30-decisions/adrs/ADR-0004-dynamic-role-lens.md` | 39 |
| `docs/30-decisions/adrs/ADR-0005-local-first-discovery-and-degradation.md` | 42 |
| `docs/30-decisions/adrs/ADR-0006-authorized-codex-analysis-and-external-git-metadata.md` | 47 |
| `docs/30-decisions/adrs/ADR-0007-review-state-lineage-and-snapshot-integrity.md` | 49 |
| `docs/30-decisions/adrs/ADR-0008-single-file-dashboard-and-structured-token-embedding.md` | 58 |
| **合计** | **7,153** |

任务卡与 ADR 的行数由逐文件 `wc -l` 取得，未跳过任何卡。内容均围绕 GoodJob 的产品契约、实现协作、测试证据和合成 fixture；外部项目只作为验收边界的名称/locator 出现。结论：⚪ 已核查确认干净。

### 3.5 全部分支与 ref

固定主体的 `git rev-list --all --not 5af1413 --count` 为 0；相对 `main` 为 5，全部来自当前 GJ-22 分支的裁决记录提交。除当前任务分支外，其余 branch/ref 没有 `main` 之外的独有 commit。完整 `git branch -a` 输出如下：

```text
  codex/analysis-core
  codex/dashboard-scope-link-layout
  codex/evidence-bundle-diversity
  codex/evidence-path-quality
  codex/scanner-binary-quality
  codex/scanner-evidence
  codex/skill-core
  main
  task/GJ-06-scanner-git-split
  task/GJ-07-gate-entrypoint
  task/GJ-09-escape-set-anchor
  task/GJ-11-doc-link-checker-tests
  task/GJ-12-m1-loose-ends
* task/GJ-22-open-source-desensitization-audit
  task/GJ-B-m1-remainder
  remotes/origin/HEAD -> origin/main
  remotes/origin/codex/analysis-core
  remotes/origin/codex/dashboard-scope-link-layout
  remotes/origin/codex/evidence-bundle-diversity
  remotes/origin/codex/evidence-path-quality
  remotes/origin/codex/scanner-binary-quality
  remotes/origin/codex/scanner-evidence
  remotes/origin/codex/skill-core
  remotes/origin/main
```

另外 4 个 `refs/codex/turn-diffs/...` 是内部 tree ref。其根 tree 为 `17025a7`、`979c8b5`、`b15c71a`、`17025a7`；逐 tree `git ls-tree -r` 后，所有 blob 均已存在于 commit 图，没有额外可达内容。结论：⚪ 已核查确认干净。

### 3.6 `.gitignore` 前瞻性防护

`git check-ignore --no-index` 实测结果：`.venv`、mypy/pytest/ruff 缓存、`__pycache__`、前端 `node_modules`/`verify-out`、原型 `out`/`node_modules` 均被忽略；当前历史 tracked sensitive path 数为 0。

下列常见 repo-local 路径目前未被提交的根规则覆盖：`runtime/build/`、`runtime/src/*.egg-info/`、`goodjob.sqlite3`、`artifacts/`、`exports/`、`drafts/`、`.env`、`config.toml`。其中 SQLite、三类产物目录与配置文件会在误把 `--data-dir` 指向仓库时生成；`.env` 是独立的本地秘密文件风险。固定 `.gitignore` 无法覆盖任意自定义 data directory。本地 `runtime/dist/` 目前靠生成目录内的未跟踪 `.gitignore` 隔离，fresh clone 不会得到这层保护。

结论：🟡 建议级。开源前应提交窄范围规则并在干净 clone 中用 `git check-ignore` 验证；运行时数据继续放在仓库外，不把任意用户目录写入仓库。

## 4. 独立复核、零原文与限制

独立结果与派卡时 Architect 的最终分类在“无秘密、无未授权 PII、无真实外部源码”三个方向一致，但原始命中数并不相同：#105 的通用赋值扫描报告 0，本次先由带最小长度的模式得到 2，再经评审要求的不限长度扫描得到 8 个 blob 命中；差异来自 #105 的带引号值模式与初始脚本的长度过滤，8 项已全部按上文三种非凭据形状复核。#105 的手机号探针是宽松的任意 11 位数字，得到 9 个哈希子串误报；本次使用带数字边界的中国手机号形状，在 476 个唯一 blob 中为 0。其余数字变化来自审计主体从早期 120/147 个 commit 增长到本次固定的 149 个 commit，以及按唯一 blob 去重的统计口径；#118/#119 的 acceptance-baseline 历史区间也已重新计入。

本报告已经完成类别扫描：没有私钥片段、认证 token、电话号码、Owner 身份原文或本机真实目录段；报告只保留白名单引用的 commit/path/line。正式交付信道消息在追加后按同一规则与本报告联合检查，并在消息中记录结果。扫描范围与结果可由 `git diff --check`、敏感模式扫描和人工逐段复核重现。

本报告的限制：

1. 它证明的是固定 `audit_subject_head`，远程 ref、工作树或历史在此后变化时必须重新审计。
2. `.gitignore` 只能降低未来误提交风险，不能替代 data directory 的仓库外约束。
3. 语义通读是工程审计，不是法律、雇佣或隐私合规意见。

## 5. 交付建议

**有条件建议开源**：

1. 先补窄范围 `.gitignore` 规则，覆盖上节列出的 repo-local 构建、数据和凭据文件，并在 fresh clone 验证。
2. 确认发布候选不包含 ignored/untracked 运行产物，运行时 data directory 位于仓库外。
3. 在真正切换 public 前，以最终候选 SHA 重跑六维度扫描，并把结果与本报告的 `audit_subject_head` 对账。

Owner 已明确裁定保留的身份与历史路径实例不在本报告中重新扩大；任何新文件、新身份或新历史区间仍按 GJ-22 契约 3 走阻断级 L1。
