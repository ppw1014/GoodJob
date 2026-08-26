# GoodJob 权威文档索引

> 状态：待 Owner 核对  
> 权威范围：文档地图、阅读顺序、单一事实源和文档冲突处理规则  
> 上游：Owner 已确认的产品构想与本次方案固化指令  
> 下游：本仓库全部设计、实现、验收与发布变更

## 当前阶段

GoodJob 已进入私有首版的实现与验证阶段：仓库包含可安装的 host agent Skill、Python 扫描/SQLite 运行时、离线 HTML 看板和自动化测试。Phase 1 覆盖 macOS 和 Linux（含 WSL2）平台，各平台使用等价的 Git 沙箱与进程身份安全模型（[ADR-0009](30-decisions/adrs/ADR-0009-cross-platform-runtime-security.md)）；真实 Ubuntu 24.04 的完整 `make gate` 已通过，ADR-0009 已接受，WSL2 复用同一 Linux 后端。原生 Windows 的基础安全契约由 [ADR-0011](30-decisions/adrs/ADR-0011-native-windows-security-contract.md) 接受，其 §3 的 Win32 rename/publish primitive 已由 [ADR-0012](30-decisions/adrs/ADR-0012-windows-nt-rename-and-directory-enumeration-correction.md) 基于真机取证替代；ADR-0012 同时在既有 handle-relative 枚举边界上新增 cursor/reset 操作细化。原生 Windows 发布门已启用，但每次运行仍须通过 [IMP-31](40-delivery/acceptance-baseline.md#33-后续原生-windows-准入门) 固化的九项 prerequisite 后才能启动 broker；缺失 Git、NTFS、BFE、管理员或 WFP 能力时继续 fail-closed 并给出准确修复动作或 WSL2 建议。用户运行不依赖 GNU Make，唯一已接受的限制仍是 Git 子进程缺少文件系统读隔离。文档仍是产品与架构契约的权威来源；本页及各设计文档的“待 Owner 核对”状态表示**当前文档修订**仍可被 Owner 复核，不表示运行时不存在。

当前阶段的边界如下：

- 产品、架构、ADR 与验收契约必须和运行时代码一起演进，冲突仍是阻塞性缺陷。
- 已有代码、单项测试绿灯或已安装副本都不能替代某一 `IMP-*`/`DASH-*` 的可复现验收证据。
- Skill 版本资产与 Owner 个人数据保持分离；仓库不保存个人数据库、扫描缓存或真实项目源码副本。
- 自 2026-07-29 起启用双 agent 协作：Architect 出卡与验收、Implementer 按卡实现、Owner 决策，通过仓库内只追加信道异步协作。协作文档位于 [docs/collab/](collab/)，任务状态以 [任务池](40-delivery/backlog.md) 为准。协作运行区不是产品契约，与权威文档冲突时权威文档优先。

## 阅读顺序

第一次了解项目时，按以下顺序阅读：

1. [产品愿景与目标](00-product/vision-and-goals.md)：为什么做、服务谁、首版边界和成功定义。
2. [产品需求](10-product/product-requirements.md)：用户如何输入、系统必须产生什么可观察结果，以及 `FR-*`/`NFR-*` 契约。
3. [系统设计](20-architecture/system-design.md)：Skill、扫描器、个人数据、RoleLens 和静态产物如何协作。
4. [证据模型](20-architecture/evidence-model.md)：事实、证据、Claim、用户上下文和快照的语义边界。
5. [扫描与分析设计](20-architecture/scanning-and-analysis.md)：工作区发现、Git/worktree、忽略、增量和语言分析规则。
6. [产物与学习闭环](20-architecture/artifacts-and-learning.md)：准备包、Markdown、离线 HTML、面试复盘和复习状态。
7. [看板呈现契约](20-architecture/dashboard-design.md)：离线 HTML 看板的信息架构、状态编码、布局交互和呈现层安全规则。
8. [决策账本](30-decisions/decision-log.md) 与 [ADR 目录](30-decisions/adrs/)：已接受与待接受选择的状态及其理由。
9. [验收基线](40-delivery/acceptance-baseline.md)：当前与后续实现必须满足的门禁、测试和真实工作区验收。

## 文档地图与权威性

| 文档 | 状态 | 唯一权威范围 | 不承担的职责 |
| --- | --- | --- | --- |
| [本文档](index.md) | 待 Owner 核对 | 文档关系、权威边界、阅读和变更规则 | 不重复产品或技术契约 |
| [产品愿景与目标](00-product/vision-and-goals.md) | 待 Owner 核对 | 问题、目标用户、`G-*`、范围、非目标、术语 | 不规定内部实现或数据库字段 |
| [产品需求](10-product/product-requirements.md) | 待 Owner 核对 | 可观察行为、输入输出、`FR-*`、`NFR-*`、叙事边界 | 不规定算法、表结构或目录实现细节 |
| [系统设计](20-architecture/system-design.md) | 待 Owner 核对 | 系统边界、组件责任、数据流、运行位置 | 不重写扫描算法和实体字段 |
| [证据模型](20-architecture/evidence-model.md) | 待 Owner 核对 | 实体、状态、来源和证据可追溯性契约 | 不定义界面内容或扫描遍历顺序 |
| [扫描与分析设计](20-architecture/scanning-and-analysis.md) | 待 Owner 核对 | 发现、忽略、增量、Git、语言适配和降级行为 | 不定义简历文案或面试交互 |
| [产物与学习闭环](20-architecture/artifacts-and-learning.md) | 待 Owner 核对 | 准备包、快照、Markdown/HTML 必需内容、访谈与复习产物 | 不定义扫描器内部实现，也不定义看板呈现与交互 |
| [看板呈现契约](20-architecture/dashboard-design.md) | 待 Owner 核对 | 看板信息架构、首屏顺序、状态视觉编码、图表形式、布局交互、呈现层安全规则、`DASH-*` | 不定义 ReportBundle 字段、产物文件集合或简历文案口径 |
| [决策账本](30-decisions/decision-log.md) | 待 Owner 核对 | 已接受、待接受、未来扩展与已否决事项的状态索引 | 不替代 ADR 的论证或设计文档的完整契约 |
| [ADR-0001](30-decisions/adrs/ADR-0001-skill-and-state-isolation.md) 至 [ADR-0008](30-decisions/adrs/ADR-0008-single-file-dashboard-and-structured-token-embedding.md) | 已接受 | 难以逆转决策的理由、后果和替代方案 | 不作为功能需求清单 |
| [ADR-0009](30-decisions/adrs/ADR-0009-cross-platform-runtime-security.md) | 已接受 | macOS/Linux/WSL2 沙箱后端选择、进程身份、fail-closed 边界和平台等价性 | 不定义 agent 无关化或原生 Windows 支持 |
| [ADR-0010](30-decisions/adrs/ADR-0010-host-agent-neutral-session.md) | 已接受 | Host Agent 无关会话：解除 Codex 硬编码绑定，宿主兼容性探针准入矩阵 | 不定义平台沙箱后端选择或原生 Windows 支持 |
| [ADR-0011](30-decisions/adrs/ADR-0011-native-windows-security-contract.md) | 已接受（2026-08-14；ADR-0011 被部分替代：其 §3 的 Win32 rename/publish primitive 由 ADR-0012 替代） | 原生 Windows 的 WFP/Job、NT handle-relative FS、direct launcher/capability handle、所有权与 fail-closed 基础契约 | rename/publish primitive 以 ADR-0012 为准；cursor/reset 是 ADR-0012 对既有枚举边界的新增细化 |
| [ADR-0012](30-decisions/adrs/ADR-0012-windows-nt-rename-and-directory-enumeration-correction.md) | 已接受（2026-08-16） | Windows NT class 10 rename/publish，以及对既有 handle-relative 枚举边界新增的 cursor/reset 操作契约 | 除 rename/publish primitive 外不替代 ADR-0011；IMP-31 未通过前仍 unsupported |
| [ADR-0013](30-decisions/adrs/ADR-0013-platform-neutral-launcher-preflight.md) | 已接受（2026-08-18） | 平台无关 `launcher-preflight-v1`、输出/退出码、零副作用预检及 Windows 旧协议兼容边界 | 不替代 session 的安全检查或改变 broker 业务协议 |
| [验收基线](40-delivery/acceptance-baseline.md) | 待 Owner 核对 | `G-*`、`FR-*`、`NFR-*` 的测试与交付门槛 | 不重新解释产品意图 |
| [任务池](40-delivery/backlog.md) | 协作运行区 | 任务状态、归属与裁决引注 | 不定义任何产品或技术契约 |
| [协作运行区](collab/) | 协作运行区 | 双 agent 协作协议、角色手册、反模式池、信道与任务卡 | 不定义任何产品或技术契约；与上表任一权威文档冲突时权威文档优先 |

## 单一事实源规则

1. 一个契约只在上表指定的权威文档完整定义一次；其他文档只能链接、引用编号或说明采用关系。
2. `G-*` 只在产品愿景中定义，`FR-*`/`NFR-*` 只在产品需求中定义；架构和验收文档不得另起平行需求编号体系。
3. 接受的技术方向由 ADR 决定，决策账本只作为索引。某项 ADR 变更时，受影响的权威设计文档和验收基线必须在同一变更中同步。
4. 产品行为或范围的变更必须先更新产品愿景/产品需求；ADR 不能在没有 Owner 产品决策的情况下悄然扩大产品承诺。
5. 若文档互相矛盾，任何实现变更必须将其视为阻塞性设计缺陷，停止自行解释；Owner 的最新明确决定优先，其次是最新已接受 ADR，再其次是该领域的权威文档。
6. 任务卡、实现说明、测试报告和生成材料只能引用本集合，不能复制后形成第二份契约。
7. 设计文档、计划、原型和测试绿灯都不是“功能已完成”的证据；完成状态必须由验收基线定义的实现与运行证据支持。

## 需求追溯链

实现和验收遵循固定链路：

```text
产品目标 G-*  ->  产品需求 FR-*/NFR-*  ->  架构契约/ADR  ->  验收基线 DoD
```

- 任何实现工作必须能指出它覆盖的 `FR-*` 或 `NFR-*`。
- 任何验收项必须能回指至少一个 `G-*`、`FR-*` 或 `NFR-*`，避免只验证技术活动而没有验证用户价值。
- 任何新增需求都必须先进入产品需求并获得新的稳定编号，再修改下游文档；禁止在实现中隐式加范围。

## 决策覆盖

下表只索引[决策账本](30-decisions/decision-log.md)中的编号，不在此重复完整契约：

| 决策域 | 决策编号与状态 | 硬决策/下游契约 |
| --- | --- | --- |
| 产品形态、Skill 与个人状态 | D-001 至 D-006、D-034、D-035 | ADR-0001、产品需求、系统设计 |
| 动态岗位与职级 | D-007 至 D-010 | ADR-0004、RoleLens 模型 |
| 项目发现、Git、忽略、刷新与语言 | D-011 至 D-018、D-021、D-034、D-036 | ADR-0005、ADR-0006、ADR-0007、扫描与分析设计 |
| 证据保存、深读、叙事与派生保真 | D-002、D-019、D-020、D-028、D-031、D-039 | ADR-0003、ADR-0006、ADR-0007、证据模型、产物与学习闭环 |
| 技术栈与离线产物 | D-022 至 D-027、D-030、D-040、D-042、D-044 | ADR-0002、ADR-0008、产物与学习闭环、看板呈现契约、验收基线 |
| 项目访谈、模拟面试与复习 | D-029、D-032、D-033 | ADR-0002、ADR-0007、产物与学习闭环、验收基线 |
| 运行恢复与个人数据保留 | D-037、D-038 | 系统设计、证据模型、验收基线 |
| 不可信输入与安全呈现 | D-036、D-041、D-043 | ADR-0008、系统设计、扫描与分析设计、证据模型、看板呈现契约 |
| 跨平台与 host agent 会话 | D-045 至 D-048 已接受；统一 launcher 预检，原生 Windows 九项 prerequisite 逐次 fail-closed | ADR-0009 至 ADR-0013、系统设计、扫描与分析设计、验收基线 |
| 明确延后能力 | F-001 至 F-009 | 决策账本“明确的非首版能力” |

## Owner 核对清单

Owner 可复核以下边界是否符合真实意图；它们约束已有实现和后续变更：

1. 产品以显式 host agent Skill 为入口，一次运行只聚焦一个工作区和一个主岗位。
2. “我实现/负责/主导”需要项目级角色上下文，“我取得结果”还需要可信结果证据，“我当时学到”需要项目级学习上下文；不足时只保留客观实现、客观项目结果、能力叙事与候选学习要点。
3. 动态 RoleLens 能接受任意岗位、可选 JD 与职级覆盖，并允许不同岗位复用同一证据图谱。
4. 扫描保持项目内容只读，个人状态与 Skill/Git 分离；根外只允许扫描契约定义的受限 Git 元数据。
5. 每个 host agent task 用不落库的易失 SessionCapability 绑定授权；SQLite receipt ID 不能跨 task 复用，能力丢失就重新确认。这不是逐条 Claim 确认，也不改变 host agent 平台数据边界。
6. 根外 Git 先对候选做精确关系探测授权，再对解析后的 git-dir/common-dir 做精确元数据授权；任一阶段拒绝或绑定失败都只形成可见缺口。
7. 首版输出中文完整准备包、可编辑 Markdown 工作稿和离线 HTML；英文按需且每次有 ExportAttempt 恢复账本；复习状态按结构化语义而非 Revision ID 延续，纯文案改写不断档。
8. 多岗位比较、额外语言深读、主动提醒、公开发布、常驻服务和独立桌面程序均不进入首版。
9. 工作区/JD 中的指令、脚本和 HTML 只作为数据，不得驱动 Agent、命令或离线看板执行。富文本只以封闭 token 集合进入呈现层，看板不含 Markdown/HTML 解析器。
10. 离线看板是单个全内联 HTML 文件，双击可读、只读不可写；首屏先呈现覆盖限制与降级，再呈现岗位叙事与项目排序。
11. 校验与门禁按结构判定而不是按扁平字符串判定，并且必须在任意真实工作区内容下成立；不可信内容既不能获得执行语义，也不能靠触发规则阻断一次本身合法的分析或渲染。

## 文档状态与变更规则

| 状态 | 含义 | 后续动作 |
| --- | --- | --- |
| 待 Owner 核对 | 已形成可实施候选契约，尚待 Owner 判断是否完整且符合意图 | 不得据此声称已获最终产品验收；可进行只读审阅 |
| 已接受 | Owner 或既定决策流程已确认，可作为下游设计约束 | 修改需记录原因；难以逆转的技术选择新增或替代 ADR |
| 已替代 | 已被新文档或新 ADR 明确替换 | 保留历史链接，不再作为实现依据 |

Owner 核对聚焦于：这些文档是否完整、互不矛盾且真实反映已确认的方向。该核对不是逐条确认代码结论；每个实现结论仍必须有对应的测试、真实工作区或发布验收证据。
