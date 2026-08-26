# GoodJob 决策账本

> 状态：待 Owner 核对  
> 权威范围：记录截至 2026-08-16 已接受、待接受、未来扩展与已否决的产品、架构和交付决策状态；硬决策的完整理由以对应 ADR 为准
> 上游：[产品目标](../00-product/vision-and-goals.md)、[产品需求](../10-product/product-requirements.md)  
> 下游：[系统设计](../20-architecture/system-design.md)、[验收基线](../40-delivery/acceptance-baseline.md)

## 使用规则

- 本表是决策状态索引，不复制 ADR 的完整论证；待接受项不是已接受架构。
- 决策按“已接受”“待接受”“未来扩展”“已否决”分区；待接受项不得作为已接受架构实施。修改已接受决策必须新增 ADR 或替代原 ADR。
- 设计文档和后续任务卡只引用决策编号，不另写不同口径。
- “未来扩展”不进入首版 DoD，不得在实现中顺手加入。

## 已接受决策

| ID | 决策 | 结果 | 权威来源 |
| --- | --- | --- | --- |
| D-001 | 产品形态 | 采用 host agent Skill 对话 + 本地确定性扫描器 + SQLite 证据库，而非纯提示词或独立桌面程序。ADR-0010 部分替代入口绑定 | [ADR-0001](adrs/ADR-0001-skill-and-state-isolation.md)、[ADR-0010](adrs/ADR-0010-host-agent-neutral-session.md) |
| D-002 | 源码访问与授权 | 当前 host agent task 易失持有原始 SessionCapability，SQLite 只存 digest；受保护请求须证明同 task 能力。根外 Git 另走两阶段精确回执；GoodJob 不新增会话外上传/遥测。ADR-0010 部分替代 SessionCapability 与 issuer_kind 绑定 | [ADR-0006](adrs/ADR-0006-authorized-codex-analysis-and-external-git-metadata.md)、[ADR-0010](adrs/ADR-0010-host-agent-neutral-session.md) |
| D-003 | 持久化 | SQLite 保存结构化状态，Markdown/HTML 作为人类阅读产物 | [ADR-0001](adrs/ADR-0001-skill-and-state-isolation.md) |
| D-004 | Skill 与个人数据 | 版本化 Skill 只带流程、脚本、参考和前端资源；个人数据放在独立用户目录 | [ADR-0001](adrs/ADR-0001-skill-and-state-isolation.md) |
| D-005 | 开发与安装 | GoodJob 已建立私有 GitHub 文档基线；只有实现和发布验收通过后才安装到用户级 Skill 目录，当前仓库状态不等于可安装 Skill | [ADR-0001](adrs/ADR-0001-skill-and-state-isolation.md) |
| D-006 | 调用方式 | 通过显式 Skill 对话发起；用户提供工作区、主岗位和可选 JD | [产品需求](../10-product/product-requirements.md) |
| D-007 | 岗位模式 | 每次以一个主岗位为中心；JD 可细化岗位，未来可做多岗位对比 | [ADR-0004](adrs/ADR-0004-dynamic-role-lens.md) |
| D-008 | 职级 | 优先从 JD 推断职级，用户可显式覆盖 | [ADR-0004](adrs/ADR-0004-dynamic-role-lens.md) |
| D-009 | 无 JD 降级 | 模型根据岗位名生成带假设的候选 RoleLens，不阻塞准备流程 | [ADR-0004](adrs/ADR-0004-dynamic-role-lens.md) |
| D-010 | 岗位模板 | 模板仅提供分析框架，不能成为固定枚举；模型必须能推导测试、底软等未知岗位 | [ADR-0004](adrs/ADR-0004-dynamic-role-lens.md) |
| D-011 | 项目发现 | 指定工作区后自动发现 Git、嵌套 Git、工作树及可识别的非 Git 项目 | [ADR-0005](adrs/ADR-0005-local-first-discovery-and-degradation.md) |
| D-012 | 工作树身份 | 同一 Git common-dir 的多个工作树合并为一个项目；相同内容复用分析但保留来源，分支差异保持 worktree scope | [扫描设计](../20-architecture/scanning-and-analysis.md) |
| D-013 | 嵌套仓库 | 先发现独立 Git 根，再应用各自忽略规则；外层 ignore 不得吞掉内层仓库 | [ADR-0005](adrs/ADR-0005-local-first-discovery-and-degradation.md) |
| D-014 | 忽略与敏感项 | 尊重各仓库 ignore，并硬排依赖、构建、缓存、环境变量和密钥文件；ignore 语义按确定性子集实现，不支持的模式必须可见而非静默近似。精确安全例外推迟为 `F-009`，其“不能重新纳入实际秘密”的约束在恢复时仍然成立 | [ADR-0005](adrs/ADR-0005-local-first-discovery-and-degradation.md)、[扫描设计 §4.1](../20-architecture/scanning-and-analysis.md) |
| D-015 | 扫描节奏 | 首次全量索引，之后由用户显式触发增量 refresh；不运行后台监听 | [扫描设计](../20-architecture/scanning-and-analysis.md) |
| D-016 | 事实与快照优先级 | 当前可读工作树是实现事实主来源，文档计划不得升级为已实现；准备阶段三次哈希校验，漂移时显式 refresh，不混用源码版本 | [ADR-0007](adrs/ADR-0007-review-state-lineage-and-snapshot-integrity.md) |
| D-017 | Git 历史 | 初始读取最近 180 天；只有具体结论需要时才向更早历史追溯 | [扫描设计](../20-architecture/scanning-and-analysis.md) |
| D-018 | 语言深读 | 首版深读 TS/TSX、Python、Rust、Dart 和 SQL；其他语言仍生成可靠基础档案 | [扫描设计](../20-architecture/scanning-and-analysis.md) |
| D-019 | Host agent 阅读策略 | 扫描器索引全量模块；host agent 先读岗位相关证据包，再按问题钻入本地原文件。ADR-0010 部分替代 Codex 深读策略措辞 | [ADR-0003](adrs/ADR-0003-evidence-pointers-without-source-snapshots.md)、[ADR-0010](adrs/ADR-0010-host-agent-neutral-session.md) |
| D-020 | 证据保存 | 只保存位置、行范围、哈希、状态和短摘要，不保存源码全文 | [ADR-0003](adrs/ADR-0003-evidence-pointers-without-source-snapshots.md) |
| D-021 | 失败策略 | 权限、损坏仓库或不支持语言不使全局失败；保留部分结果并显式列缺口 | [ADR-0005](adrs/ADR-0005-local-first-discovery-and-degradation.md) |
| D-022 | 技术栈 | Python 负责扫描、SQLite 和产物编排；TypeScript 前端负责离线静态看板 | [ADR-0002](adrs/ADR-0002-python-and-offline-typescript-dashboard.md) |
| D-023 | HTML 运行形态 | 看板离线打开，不依赖常驻本地服务；报告数据随产物嵌入 | [ADR-0002](adrs/ADR-0002-python-and-offline-typescript-dashboard.md) |
| D-024 | 产物组合 | 每次生成完整岗位准备包：岗位总览、项目/模块章节、简历、面试与知识缺口 | [产物设计](../20-architecture/artifacts-and-learning.md) |
| D-025 | 跨项目组织 | 先给岗位能力地图和项目排序，再保留每个项目与模块的可追溯章节 | [产物设计](../20-architecture/artifacts-and-learning.md) |
| D-026 | 简历产物 | 主快照保存不可变简历 Markdown 源稿，可显式导出不被后续运行覆盖的工作稿；每条 bullet 可追溯项目和证据 | [产物设计](../20-architecture/artifacts-and-learning.md) |
| D-027 | 语言 | 中文主快照；英文简历和英文问答作为独立不可变派生物按需导出，不更新 latest | [产品需求](../10-product/product-requirements.md) |
| D-028 | 叙事口径 | 全量代码可转为“能讲解/可复习”的能力与候选学习；“我当时学到”需 learning 上下文，“我实现/负责/主导”需角色信息，“我取得结果”还需结果证据 | [产品需求](../10-product/product-requirements.md) |
| D-029 | 人工上下文 | 准备材料发现业务、指标或角色缺口时，发起项目级批量访谈；不逐条确认 Claim | [产物设计](../20-architecture/artifacts-and-learning.md) |
| D-030 | 产物版本 | 每次运行保留不可变中文主快照，并维护只指向成功主快照的 latest；人工回答和英文派生导出不覆盖源快照 | [产物设计](../20-architecture/artifacts-and-learning.md) |
| D-031 | 证据呈现 | 关键结论在 Markdown 与 HTML 中直接关联模块、文件、状态和证据摘要 | [ADR-0003](adrs/ADR-0003-evidence-pointers-without-source-snapshots.md) |
| D-032 | 学习闭环 | 复盘绑定稳定 ReviewTarget；指纹由结构化复习语义而非 Revision/Gap ID 生成，纯文案变化延续、实质语义变化重评 | [ADR-0007](adrs/ADR-0007-review-state-lineage-and-snapshot-integrity.md) |
| D-033 | 面试隐私 | 不保存完整面试对话，只保存结构化复盘；不创建主动提醒 | [产物设计](../20-architecture/artifacts-and-learning.md) |
| D-034 | 配置位置 | 个人中心 `config.toml` 保存 `config_revision`、默认岗位与项目级排除规则；工作区注册、项目身份与角色信息由 SQLite 持有。文件级安全例外收窄为 `F-009`，不进入首版 | [系统设计](../20-architecture/system-design.md)、[扫描设计 §4.3](../20-architecture/scanning-and-analysis.md) |
| D-035 | 当前治理阶段 | 权威文档先行、实现按卡推进。自 2026-07-29 起部署双 agent 协作（Architect 出卡/裁决/验收、Implementer 按卡实现、Owner 决策），信道与任务卡位于 [docs/collab/](../collab/)，任务状态以[任务池](../40-delivery/backlog.md)为准；协作运行区不产生产品契约 | 本决策账本、[协作协议](../collab/protocol.md) |
| D-036 | 不可信输入 | 工作区、`.git`/Git 配置、JD、用户上下文和模型文本只作为数据；不得驱动命令、生成授权或扩大路径，进入 HTML 前必须安全编码并受 CSP 约束 | [系统设计](../20-architecture/system-design.md)、[扫描设计](../20-architecture/scanning-and-analysis.md)、[产物设计](../20-architecture/artifacts-and-learning.md) |
| D-037 | 个人数据保留 | 首版不自动删除、归档或淘汰 SQLite、快照、导出和工作稿；每次运行显示分类用量，清理作为未来显式能力 | [证据模型](../20-architecture/evidence-model.md) |
| D-038 | 运行恢复与单写者 | 所有写操作使用 OS 非阻塞排他锁；运行与 ExportAttempt 中断由恢复账本终止并只清理预登记归属路径，不超时偷锁或续跑模型内存 | [系统设计](../20-architecture/system-design.md) |
| D-039 | 英文派生事实保真 | 英文材料按冻结 source item 一一派生并机检事实锚点；每次导出先建 ExportAttempt，只有成功 attempt 可发布 DerivedExport | [证据模型](../20-architecture/evidence-model.md)、[产物设计](../20-architecture/artifacts-and-learning.md) |
| D-040 | 看板打包形态 | 看板入口为单个全内联 HTML 文件，字体只用系统字体栈，CSP 以 `<meta>` 按内联哈希施加；同一快照的 Markdown、manifest 与派生导出仍是独立文件 | [ADR-0008](adrs/ADR-0008-single-file-dashboard-and-structured-token-embedding.md)、[看板呈现契约](../20-architecture/dashboard-design.md) |
| D-041 | 富文本进入呈现层的形态 | `ReportBundle` 只以 `ReportInlineToken` 封闭集合传递富文本，不传 Markdown/HTML 字符串；看板不含解析器，未知 kind 整批拒绝 | [ADR-0008](adrs/ADR-0008-single-file-dashboard-and-structured-token-embedding.md)、[证据模型](../20-architecture/evidence-model.md) |
| D-042 | 看板呈现顺序与只读出口 | 总览首屏固定 `L0→L4`，覆盖限制与降级先于叙事；看板不提供写状态控件，只给可复制的 Skill 调用出口 | [看板呈现契约](../20-architecture/dashboard-design.md) |
| D-043 | 校验与门禁的判定层级 | 对结构化数据的校验必须按结构判定：token 序列的散文级规则只作用于 `text`/`emphasis`，入口文档的属性检查不作用于内联数据区。禁止先扁平化成字符串再模式匹配；任何门禁必须在任意真实用户内容下成立 | [ADR-0008](adrs/ADR-0008-single-file-dashboard-and-structured-token-embedding.md)、[证据模型](../20-architecture/evidence-model.md)、[看板呈现契约](../20-architecture/dashboard-design.md) |
| D-044 | 呈现层动态几何与行为验收 | 呈现层禁用全部 `.style` 运行时写入，动态几何用内联 SVG 几何属性配合整数 `viewBox`；看板行为由跨 Chromium/WebKit 的真实文档核对验收，并以「注入 `style` 属性必须触发违规」为阳性对照，不以源码字符串匹配代替行为断言 | [ADR-0008](adrs/ADR-0008-single-file-dashboard-and-structured-token-embedding.md)、[看板呈现契约](../20-architecture/dashboard-design.md)、[验收基线](../40-delivery/acceptance-baseline.md) |
| D-045 | 跨平台运行时安全 | macOS 与 Linux（含 WSL2）使用等价的 Git 沙箱后端（sandbox-exec / bwrap）与进程身份（BSD ps / /proc）；任一后端不可用时 fail-closed，不回退到无沙箱。ADR-0009 部分替代 ADR-0001/ADR-0006 的 macOS-only 平台限定 | [ADR-0009](adrs/ADR-0009-cross-platform-runtime-security.md) |
| D-046 | Host Agent 无关会话 | 解除 `issuer_kind` CHECK 约束（DB v11），参数化宿主运行时，新增 uv+python 回退启动器，并以五项探针管理宿主准入。ADR-0010 部分替代 ADR-0001/0003/0006/0007 中绑定 Codex 的条款 | [ADR-0010](adrs/ADR-0010-host-agent-neutral-session.md) |
| D-047 | 原生 Windows 安全运行时 | 接受 WFP ALE dynamic filters + `ACTIVE_PROCESS=1` Git Job、NT handle-relative FS、direct `CreateProcessW`、最小 capability handle 继承与逆序清理契约。ADR-0012 基于真机反证，以 NT class 10 rename/publish primitive 替代 ADR-0011 §3 的 Win32 primitive，并在既有 handle-relative 枚举边界上新增 cursor/reset 操作细化；其余决策及唯一 Git FS 读隔离降级继续有效。原生 Windows 发布门已启用；launcher、preflight、session 与受保护子进程必须读取同一持久状态，且九项 prerequisite 任一失败都保持零 broker/受保护执行并提供准确 remediation 或 WSL2 建议 | [ADR-0011](adrs/ADR-0011-native-windows-security-contract.md)、[ADR-0012](adrs/ADR-0012-windows-nt-rename-and-directory-enumeration-correction.md) |
| D-048 | 平台无关 launcher 预检 | 所有平台以 `--preflight-only` 输出封闭的 `launcher-preflight-v1`；producer/parser 共用检查与 remediation 注册表，预检零 broker/源码读取/个人状态/安装/提权/网络副作用。普通成功静默，启动前失败只输出 accepted v1。Windows 新协议无损包装同一次旧 prerequisite 报告，旧 flag 与两类旧报告继续兼容 | [ADR-0013](adrs/ADR-0013-platform-neutral-launcher-preflight.md) |

## 待接受决策

当前无待接受决策。

## 明确的非首版能力

| ID | 能力 | 首版处理 |
| --- | --- | --- |
| F-001 | 多岗位并排比较 | 数据模型保留扩展空间，但首版每次只处理一个主岗位 |
| F-002 | Go、Java、C#、Swift 等语言的深层调用分析 | 首版仅做通用发现与基础档案，真实需求出现后新增适配器 |
| F-003 | 后台文件监听 | 不实现；使用显式 refresh |
| F-004 | 主动复习提醒或 Codex 自动任务 | 不实现；只记录日期 |
| F-005 | 公开 GitHub 发布 | 首次只建私有仓库，稳定后另作决策 |
| F-006 | 需要本地服务的交互式 Dashboard | 不实现；首版为离线静态看板 |
| F-007 | 独立桌面可执行程序 | 不实现；先验证 Skill 工作流 |
| F-008 | 自动清理或压缩个人数据 | 不实现；首版只显示用量，未来需设计显式预览、引用保护与可恢复策略 |
| F-009 | 精确到文件路径的忽略安全例外 | 不实现；首版只有项目级排除（`D-034`）。它是唯一会让扫描读到本来被排除内容的入口，需独立设计秘密拒绝校验、例外命中审计与覆盖摘要呈现 |

## Owner 核对

本账本不包含待实现任务清单。Owner 核对的对象是：决策是否完整、是否与产品目标一致、是否存在需要改判的已接受项。核对通过后，才能依据权威文档建立实现任务清单。
