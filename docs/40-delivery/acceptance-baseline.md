# GoodJob 验收基线

> 状态：待 Owner 核对  
> 权威范围：定义文档与运行时变更“什么算通过”；不以代码存在、单项测试或安装副本替代完整验收  
> 上游：[产品目标](../00-product/vision-and-goals.md)、[产品需求](../10-product/product-requirements.md)、[系统设计](../20-architecture/system-design.md)、[决策账本](../30-decisions/decision-log.md)  
> 下游：实现任务、代码审查、发布候选与用户级安装更新

## 1. 验收纪律

- 计划、文档、单元测试通过和真实工作区验收是四种不同证据，不得互相替代。
- 每个实现任务或行为变更必须引用本基线中的验收项，并补充当时真实存在的门禁命令。
- 仓库已包含私有首版 Skill、扫描器、SQLite schema、离线前端和自动化测试；任何“已通过”的结论仍必须指向同一份代码状态和对应的可复现证据。代码存在、局部单测或安装副本都不等于全部 `IMP-*`、真实工作区或发布验收已经通过。
- 部分失败可以通过，但必须在 Coverage/ScanIssue 中显式呈现；静默漏扫不能通过。
- 门禁本身也要成立。任何校验、构建或渲染门禁必须在任意真实工作区内容下有效：只在固定夹具下成立的门禁不算门禁；把结构化数据扁平化成字符串后再模式匹配的门禁不算门禁；以源码文本匹配代替行为断言的门禁不算门禁（`D-043`、`D-044`）。发现由用户内容触发的确定性拒绝时，按缺陷处理，不按“输入不合法”处理。
- Owner 核对文档不等于授权额外范围。实现任务、依赖和公共契约的变更仍须显式审查。

## 2. 文档阶段 DoD

| ID | 验收项 | 判定方法 |
| --- | --- | --- |
| DOC-01 | 文档地图完整 | docs/index.md 能链接产品、架构、决策、验收全部权威文档，链接目标存在 |
| DOC-02 | 决策无遗漏 | [决策账本](../30-decisions/decision-log.md) 覆盖访谈中已确认的产品、扫描、岗位、证据、产物和交付选择 |
| DOC-03 | 单一事实源 | 相同契约只在一个权威文档定义，其他文档使用链接和编号引用 |
| DOC-04 | 三区一致性 | 目标、产品需求、架构模块和本基线之间可追溯，无范围、接口与验收互相冲突 |
| DOC-05 | 状态诚实 | 候选权威设计文档标记“待 Owner 核对”，已决硬决策 ADR 标记“已接受”；项目如实区分已实现运行时、未完成验收和已发布版本，不把文档、代码、CI、安装副本或发布证据互相冒充 |
| DOC-06 | 无阻塞占位 | 不存在未决占位项，也不把影响实现的选择留给后续开发者自行决定 |
| DOC-07 | 非首版隔离 | 多岗位比较、额外语言深读、主动提醒、公开发布和常驻服务明确不进入首版 |

## 3. 实现验收矩阵

### 3.1 需求追溯

| 需求 | 主要验收项 |
| --- | --- |
| FR-01、FR-02 | IMP-01、IMP-23 |
| FR-03 | IMP-02、IMP-03、IMP-04、IMP-06、IMP-10 |
| FR-04 | IMP-08、IMP-12 |
| FR-05 | IMP-11 |
| FR-06 | IMP-07、IMP-12、IMP-13 |
| FR-07 | IMP-14 |
| FR-08、FR-09 | IMP-13 |
| FR-10 | IMP-15 |
| FR-11 | IMP-07、IMP-12、IMP-16、IMP-28 |
| FR-12 | IMP-17、IMP-18、IMP-28 |
| FR-13 | IMP-16、IMP-27 |
| FR-14 | IMP-19、IMP-25、IMP-28 |
| FR-15 | IMP-20、IMP-28 |
| FR-16、NFR-09 | IMP-29 |
| FR-18 | IMP-31 |
| NFR-01、NFR-02 | IMP-05、IMP-07、IMP-12、IMP-21、IMP-23 |
| NFR-03 | IMP-18、IMP-28 |
| NFR-04 | IMP-08、IMP-12、IMP-17、IMP-25 |
| NFR-05 | IMP-20、IMP-24、IMP-27 |
| NFR-06 | IMP-21、IMP-26 |
| NFR-07 | IMP-10、IMP-11 |
| NFR-08 | IMP-07、IMP-14、IMP-22、IMP-23、IMP-28 |
| NFR-11 | IMP-31 |

### 3.2 场景矩阵

| ID | 能力 | 必须验证的场景 | 通过条件 |
| --- | --- | --- | --- |
| IMP-01 | Skill 入口与 JD | 用户显式调用 Skill，分别提供无 JD、有效文本/文件 JD、不可读/目录/不可解码 JD | 参数缺失时只追问缺失项；无 JD 可继续并显示假设；坏 JD 在更正或明确 `continue_without_jd` 前不创建 JobInput、RoleLens、ScanRun/PreparationRun |
| IMP-02 | 项目发现 | Git 根、.git 指针、嵌套 Git、manifest 非 Git 项目混合存在；另测一个可读空工作区 | 每个真实项目均有稳定 identity；空目录不成为项目，并返回空覆盖、下一步提示和 failed ScanRun，不生成准备快照 |
| IMP-03 | 工作树归并 | 同一 common-dir 有多个 linked worktree，其中一个 common-dir 位于授权根外；另含根内 `.git` 目录指向根外 common-dir；工作树间既有相同内容也有分支差异 | 根内候选检查、候选绑定关系授权、路径/身份绑定元数据授权和双向校验全部通过后只生成一个项目；相同内容不重复解析但可展开全部来源；差异形成 worktree-scope/冲突 Claim；根外只出现关系、HEAD/ref，index/dirty 与源码 commit state 明示不可用，非法外部 config 不影响扫描 |
| IMP-04 | 嵌套 ignore 与忽略语义边界 | 父仓库 ignore 内层 Git 仓库；另测一条已知不支持的 ignore 模式语法，以及一条命中的 Owner 项目级排除规则 | 内层仓库仍独立扫描，并应用自己的 ignore；不支持的模式产生 `ignore_pattern_unsupported` ScanIssue 并说明实际近似语义，不静默生效；被排除项目记为 `excluded` 并在覆盖摘要中与 `failed_no_baseline` 分开呈现 |
| IMP-05 | 路径与敏感排除 | 存在指向根外源码的普通 symlink、symlink 环、多个别名指向同一目录、显式根外 JD、.env、密钥、依赖、构建和缓存目录 | 普通 symlink 不跟随；环不会递归/挂死；别名按规范实路径去重；JD 只作输入；秘密/生成物不读取、不存储、不输出；覆盖说明排除类别 |
| IMP-06 | 当前状态 | 同时存在已提交、已修改和未跟踪代码/文档 | 全部非忽略内容可成为证据，并正确标记来源状态 |
| IMP-07 | 证据真实性 | 分别提交：计划文档→implemented、测试定义→test_verified、匹配 revision 的通过结果→test_verified | 前两类整批拒绝且不半提交；计划可改为 documented/planned，测试定义只可 test_defined；匹配通过结果可 test_verified |
| IMP-08 | 增量刷新 | 首扫后无变化、改单文件、删除/移动文件；伪造保持大小/mtime 不变的内容变化；分别运行 `fast` 与 `verify_content` | fast 明示仅为检测结果且重哈希 dirty/untracked；verify_content 对全部合资格文件重算 SHA-256；相同 hash 复用，变化项刷新，旧定位失效，不重新深读全仓 |
| IMP-09 | Git 历史与基线 | 当前代码需要解释、近期历史不足；默认分支分别为唯一 remote HEAD、main/master、均不存在和 detached HEAD；根内/根外 common-dir 各测并提交伪造 candidate | 不 fetch/checkout；按契约选择基线并记录来源；默认只索引 180 天；根内选中 candidate 可有界深读且正文不落库；根外永不读历史/object/blob/diff；伪造项使整批失败 |
| IMP-10 | 语言覆盖 | TS/TSX、Python、Rust、Dart、SQL 与一种未知语言共存 | 首批语言生成模块/符号级证据；未知语言仍有结构、依赖和文档档案 |
| IMP-11 | RoleLens 与定点评分 | 同一扫描快照以两个差异岗位运行，覆盖有/无 JD、职级覆盖、未内置岗位、权重和为 9999/10001、边界分数和并列 | 基础事实不变；合法权重总和恰为 10000；非法值在创建 PreparationRun 前拒绝且不归一化；Repository 按规定整数公式重算分数、连续稳定排名，两岗位差异可解释 |
| IMP-12 | 按证据深读与三阶段校验 | 大工作区含不相关项目；分别在 preflight 后、文件读取前、record_analysis 提交前修改/删除文件或收回权限 | host agent 先读 EvidenceBundle 再打开选定原文件；三阶段逐项记录；任一 mismatch 令运行终态 `refresh_required`，不隐式 refresh、零 Evidence/Claim/Assessment/Artifact 半提交，旧 latest 不变 |
| IMP-13 | 完整准备包与项目资格 | ScanRun 同时含 fresh、carried-forward、failed-no-baseline、excluded、低分和证据不足项目 | 只为 fresh/carried-forward 创建 Assessment 和连续 rank；低分合资格项目仍有基础章节；failed/excluded 只在 Coverage 列原因与补救；合资格集合为空时失败 |
| IMP-14 | 能力叙事 | 证据来自团队模块，且无个人学习/角色上下文；另有项目结果；尝试提交过去式学习或强个人归因 ClaimDraft | 可生成“能讲解/可复习”、候选学习和客观结果；record_analysis 拒绝“我当时学到/实现/负责/主导/取得指标”，且不半提交 |
| IMP-15 | 项目级访谈 | 业务目标、指标或角色证据缺失 | 在 prepare 阶段一次性批量提问；答案独立持久化且后续运行复用 |
| IMP-16 | 简历与英文导出 | 中文主包完成、导出工作稿后再 prepare；英文源项含数字/单位/技术名/状态/角色/结果锚点，并分别制造漏项、增项、锚点篡改和中途失败 | 工作稿默认不覆盖；英文 source/target item 集合及规范化事实锚点完全相等才原子发布；不新增事实或 HTML；任一失败不留半成品且不改中文源快照/latest |
| IMP-17 | 快照与渲染恢复 | 同一岗位完成两个 PreparationRun；另一个 ready 运行分别渲染失败、进程中断后重试 | 每个运行至多一个成功主快照且旧快照不覆盖；中断 RenderAttempt 标 `interrupted` 并使运行可从 `render_failed` 重试；重试保持 ReportBundle hash/Claim 集；latest 只指向最新成功中文快照 |
| IMP-18 | 离线看板 | 断网、无本地 HTTP 服务直接打开单文件入口 HTML，并把该文件单独复制到其他目录后再次打开 | 项目筛选、证据展开、知识缺口和复习状态可用；报告数据与前端代码已内联，无远端资源、外部 JSON 请求或同目录依赖 |
| IMP-19 | 面试复盘 | 完成一轮模拟面试后更新复盘，并在不 refresh 的情况下显式创建新 PreparationRun | 保存 ReviewTargetBinding、掌握度、薄弱点、摘要和复习日期，不保存完整对话；新快照可复用同一 ScanRun，旧 HTML/Markdown 不变化 |
| IMP-20 | 部分失败 | 一个目录无权限、一个仓库损坏、一个语言不支持 | 其余项目仍产出；缺口、原因、影响和补救动作清晰可见 |
| IMP-21 | 状态隔离 | 删除/升级 Skill 后重新运行 | 平台感知默认目录中的个人数据仍完整；仓库不含个人数据库 |
| IMP-22 | 不可信输入 | 项目名、路径、manifest、JD、文档、用户回答和 ClaimDraft 含 shell 元字符、提示注入、`</script>`、事件属性、原始 HTML 与外部 URL | 扫描器不执行项目内容或 shell 拼接；Agent 不服从其中指令；record_analysis 只按契约校验；HTML 只显示安全文本，无脚本执行、外部请求或路径越界 |
| IMP-23 | 会话 capability 与根外 Git | 同一 host agent task 以一个 capability 调多个短命 Python 子进程；再测试能力丢失、新 task 复制 SQLite/receipt ID、错误 capability/scope/notice；扫描 argv/env/stdout/stderr/日志/DB/产物；根外 Git 两阶段授权覆盖伪造链接与环境注入 | 同 task 正确能力通过；其他场景零项目读取/业务写入并重新确认；原始 capability 不出现在持久化/诊断面；根外 probe 无 Git/业务元数据，metadata 阶段固定无网络且只读允许字段 |
| IMP-24 | 运行生命周期与单写者 | 两写进程竞争；杀死 scan/render 进程并伪造 PID 复用/旧锁；awaiting_context 分别由同 task 继续、由新 task 尝试接管并执行明示 restart | writer_busy 零写入；不偷锁；进程型记录仅在 PID+启动标识确认消失时中断；同 capability 可续接 awaiting，新 task 不能续接，Owner 明示后旧 run interrupted、新 run 复用终态快照/上下文 |
| IMP-25 | 复习状态谱系 | 同一目标分别仅改写 statement/顺序/行号/等价 Evidence，以及修改概念、机制、facet、conflict、evidence validity、角色/结果锚点或 gap 状态；另测相似题面跨项目 | 前一组 canonical projection/hash 不变并延续；后一组必须新 hash 且“需重评”；无法验证投影等价时保守重评；不直接使用 Revision/Gap ID，不跨项目合并 |
| IMP-26 | 个人数据保留 | 构造多次扫描、快照、英文导出和工作稿，并升级/重装 Skill | 无自动删除/归档；每次 scan/prepare 显示 SQLite/artifacts/exports/drafts 字节数和快照数量；状态完整保留且仓库不含个人数据 |
| IMP-27 | 英文导出中断恢复 | 候选生成阶段确认零文件；发布阶段同时存在成功导出、未知目录和新 ExportAttempt，分别在写 temp、原子改名后、DB 提交前杀进程，并伪造 PID 复用后重试 | 首次写盘前 attempt 已记录 PID+启动标识；只在确认 owner 消失后标 interrupted；只清理预登记 temp/无 DerivedExport 的 final；不碰成功/未知目录；重试新 attempt，latest 不变 |
| IMP-28 | 看板呈现与安全边界 | 对同一冻结 `ReportBundle` 执行 `DASH-01` 至 `DASH-12`：断网双击、Chromium/WebKit 双引擎全视图交互、注入语料（含 `style="…"` 片段）、375px 视口、`partial` 首屏、混合时效证据、打印、纯键盘、`forced-colors`/灰度、双快照与跨版本深链、复习三态、Markdown/HTML 逐条比对 | 两个引擎网络面板零请求、控制台零 CSP 违规与零脚本错误，且“注入 `style` 属性触发违规”的阳性对照成立；注入语料全部为文本、无可点击外部链接，且不导致渲染被拒；无横向滚动；`partial` 首屏降级带非空不可折叠；两次交互内到完整证据指针；打印展开全部折叠与 locator；键盘全流程可达；状态在无色通道下仍可辨；跨版本深链明确报错；无写状态控件；Markdown 与 HTML 对同一 Claim 呈现一致 |
| IMP-29 | 跨平台 Git 沙箱 | macOS 与 Linux（含 WSL2）分别运行完整 scan/prepare 流程；macOS 检查 sandbox-exec 命令结构、Linux 检查 bwrap 命令结构（`--unshare-net`、`--unshare-pid`、`--proc` 顺序、`--ro-bind` 授权根、`--die-with-parent`）；模拟 bwrap/sandbox-exec 不存在时 fail-closed；进程身份在 macOS 用 BSD ps、Linux 用 `/proc` stat | 两平台分别全绿；bwrap `--proc` 在 `--unshare-pid` 之后；沙箱缺失时零 Git 执行且报错明确；同一进程 marker 稳定、PID 重用时 marker 变化；macOS 现有行为零回归 |
| IMP-30 | Host Agent 无关会话 | DB v11 迁移在含存量数据的数据库上可正确执行且可回滚；issuer_kind CHECK 约束已移除；`--agent-runtime` 完整传递；`--preflight-only` 在 macOS/Linux/unsupported 上逐项覆盖 ready/not-ready、严格 mutation、stdout/stderr、`0/2`、零 broker/个人状态/源码读取及普通成功静默；Skill 只写通用 flag；macOS、Ubuntu 和进入支持矩阵的宿主在同一冻结 commit 上有门禁证据 |
| IMP-31 | 原生 Windows 安全运行时 | 在受支持的 GA Windows + NTFS 真机上覆盖 WFP/Job/真实 Git、NT handle-relative 全操作、direct launcher/capability、bounded-output、句柄所有权与异常清理；新 v1 无损映射同一次旧 prerequisite 结果，旧 flag 的两类报告保持内容、顺序、通道与 `0/2` 兼容；预检完成、超时和中断均无进程泄漏 | 逐项满足 3.3 矩阵且证据绑定同一候选 commit；mock/monkeypatch 只能补充，不能替代 Windows 新旧协议进程实测、WFP、NTFS/reparse、Job、pipe/queue 和关闭竞态证据；全部通过前原生 Windows 保持 unsupported 并推荐 WSL2 |

### 3.3 后续原生 Windows 准入门

`IMP-31` 是原生 Windows 的独立聚合准入项，不由现有 macOS/Linux 门禁或原语 spike 自动满足。只有下表全部在同一实现候选上通过，才能把原生 Windows 标记为 supported；任一项缺失或失败都必须保持 unsupported / fail-closed，并提示改用 WSL2。唯一允许的安全降级是 Git 子进程缺少文件系统读隔离，且必须在运行前提示和证据报告中可见；Git 网络隔离、扫描器授权根、进程树回收和 capability 隔离不得降级。

| 子项 | 真机阳性证据 | 真机负向/对抗证据 | 通过条件 |
| --- | --- | --- | --- |
| `IMP-31A` WFP 生命周期与失败关闭 | 在有 IPv4/IPv6 出口的 GA Windows 上，以真实 `mingw64\bin\git.exe`/egress probe application ID 安装并逐条回读 V4/V6 `AUTH_CONNECT`、`AUTH_RECV_ACCEPT` filters；会话关闭后 filter 自动消失且阳性连通恢复 | 非管理员/权限不足、BFE 不可用、filter 安装或回读不完整；IPv4/IPv6 的 TCP、UDP、DNS 在 filters 生效时均失败 | filters 全部回读后才可 resume；任何准备失败零子进程入口执行；关闭前后证明无清理网络窗口。IPv6 必须是真实出口，不以本来无路由或仅 loopback 代替 |
| `IMP-31B` 真实 Git + Job | 直接启动 `mingw64\bin\git.exe`，本地 `rev-parse`、`log` 在 `KILL_ON_JOB_CLOSE + ACTIVE_PROCESS=1` Job 中成功；`CREATE_NO_WINDOW` 下 headless `conhost.exe` 的 accounting 可观察 | `cmd\git.exe` shim 被拒作入口；远程 HTTP/SSH Git 和恶意 config/helper 派生均失败，Job completion port 记录 active-process limit，未出现可执行 helper；关闭 Job 回收整棵进程树 | WFP scope 与实际入口二进制一致；conhost 不成为提高 active-process 限额的理由；WFP 不是 helper 拦截的唯一证据，Job 不是网络隔离的替代 |
| `IMP-31C` NT handle-relative FS | NTFS root 上逐组件打开并以 volume serial/file ID 校验；`read_regular/list_directory/write_new_file_at/write_new/open_parent/replace_file/publish_directory/remove`、scanner readlink/枚举均用同一已验证 handle 完成；同一 retained root/目录 handle 连续两次独立枚举得到相同完整结果，多页调用序列每次均为 `11 -> 10...`；非 Git manifest 工作区真实走过 `_walk_directories -> _non_git_manifest -> _discover/scan` 后产生项目且 scan 非 `failed`；组件长度边界值阳性对照正常 | 拒绝绝对、drive/UNC/device prefix、空、`.`、`..`、分隔符、ADS `:` 与超过后端明示上限的超长组件；覆盖 junction/symlink/reparse tag、大小写别名、UNC root、跨卷、目标替换；在 open/rename/delete 各阶段并发换父目录；上述每个拒绝/对抗用例均断言根外哨兵未读/未写/未删；非 NTFS fail-closed | 授权仅来自 root/parent handle + identity；无 `GetFileAttributesW -> CreateFileW`、`DeleteFileW`、`RemoveDirectoryW` 等 pathname 降级；cursor、分页和 discovery 链按 [ADR-0012](../30-decisions/adrs/ADR-0012-windows-nt-rename-and-directory-enumeration-correction.md) 绑定同一候选在 Windows 真机验证，mock 只能补充；UNC/跨卷未证明安全时只能拒绝；组件长度按实际文件系统/后端上限验证，不以 `MAX_PATH` 猜测替代真机边界证据 |
| `IMP-31D` direct launcher 与 capability | `CreateProcessW(CREATE_SUSPENDED, bInheritHandles=TRUE) -> AssignProcessToJobObject -> ResumeThread`，resume 前 `IsProcessInJob=true`；最小 `PROC_THREAD_ATTRIBUTE_HANDLE_LIST` 只含显式 inheritable 的 capability/payload/stdio handles，子进程接管并关闭；业务子进程入口立即派生的获准后代仍在同一 Job，关闭 Job 可整树回收 | assign/resume/attribute-list/pipe 构造任一步注入失败，入口零执行；列表外 sentinel handle 不可见；错误 handle/capability、重复关闭、父子同时关闭均 fail-closed；禁止派生的 policy 拒绝后代，允许派生的 policy 下后代也不能逃逸 Job | 不调用 `subprocess.Popen`；原始 capability 不进 argv/env/日志/DB/输出，argv 只有数值 handle；每次启动使用独立 Job，只有 Git 固定 `ACTIVE_PROCESS=1`，业务子进程按自身 policy 配置；失败终止 process/Job 且不泄漏继承能力 |
| `IMP-31E` bounded-output 与关闭竞态 | stdout/stderr 并发输出在小于预算时完整、有序映射到协议结果；正常完成后全部 reader/pipe/process/thread/Job/WFP/attribute-list resources 归零 | 单流和双流同时越界、queue 满、超时、协议错误、child/parent 提前关闭、取消与 WFP/Job/pipe 关闭竞态；输出持续写入时触发终止 | pipe + 有界 queue + accumulator 共用一个原子累计预算，超预算尾部丢弃，禁止 `communicate()`；有限 join 不挂死，不死锁、不超预算 |
| `IMP-31F` 句柄所有权与逆序清理 | 正常成功、子进程非零退出和 Owner 取消后，以进程 handle 计数/系统观察证明回到基线 | 在每个资源获取点注入异常并重复执行；验证先停止/回收进程、再关闭 pipe/join readers、释放 process/thread/transfer/attribute-list、关闭 Job、最后关闭 WFP session；每步 close 失败仍继续清理 | borrowed handle 从不误关，owned handle 恰好关闭一次；无持续 handle/filter/process 泄漏；任何路径都不会在活进程前先撤 WFP |
| `IMP-31G` 支持状态与提示 | 实现候选上完整 scan/prepare 真机 E2E 通过，运行前明确显示 Git FS 读隔离是唯一降级 | 在 `IMP-31A-F` 任一证据缺失/失败、非支持文件系统或后端不可用环境运行 | supported 状态只由完整矩阵得出；失败时零受保护 Git/扫描执行并推荐 WSL2，不把 mock、计划或 spike 冒充准入证据 |

每份 `IMP-31` 证据必须记录 Windows edition/version/build、文件系统、Python/Git 版本、权限/BFE/IPv4/IPv6 环境、候选 commit、运行命令和原始结果摘要。早期 ctypes ABI 错误造成的 `FwpmEngineOpen0 ERROR_NOT_SUPPORTED (50)` 已撤回，不得作为 WFP 失败模式或替代正确 ABI 的负测。

## 4. 真实工作区只读验收

### 只读证明（适用于全部真实工作区）

GoodJob 对真实工作区只读是产品级承诺，验收必须逐条证明而非从状态推断。判据分三条，各自独立成立：

**(a) GoodJob 未写入。** 机器可验，不接受"未执行写操作"的声称：

- 全程未向目标工作区发出任何 Git 写命令（`add`/`commit`/`checkout`/`reset`/`clean`/`gc`/`fetch`/`stash` 等）；
- 全程未以写模式打开目标工作区内任何文件描述符；
- 目标工作区 `.git` 的 inode 与 mtime 前后不变。

**(b) 分析基线自洽。**

- 目标工作区 HEAD 全程不变；
- 进入 `SourceRevision` 的每个文件，其内容哈希自 `before_read` 校验至分析冻结全程不变。

**(c) 外部漂移记为观察，不判 fail。** 首读前与末读后各采一次 `git status --porcelain=v2 --untracked-files=all`，差集即漂移路径集，按是否进入本次分析分类：

- **影响分析基线的漂移**（漂移路径集 ∩ `source_artifacts` 相对路径集 ≠ ∅）：必须为空。不为空时 (b) 判红，且**必须点名具体路径**——此时路径不再是隐私问题，而是"该报告不可信"的必要信息；
- **未进入分析的漂移**：只记数量与 Git 状态类别，不记路径。

全量漂移清单落在仓库外的临时 data directory，与产物同处，供 Owner 自行查阅；**仓库内证据文档与信道只出现计数与分类**，以守住第 6 节条件 5。

> **为什么不用"前后状态摘要全等"**：该判据所证为"扫描期间无人写入"，强于所需证明的"GoodJob 未写入"，且在 Owner 活跃使用的仓库上不可达——而活跃仓库正是本产品的目标场景。一条只能在静止仓库上通过的判据，验证的不是产品的真实使用形态，并且会用外部噪声掩盖真实缺陷。（Owner 裁定 `OWN-04`，2026-08-06；源案例见协作信道 #65、#70）

### CodeRoute

真实验收对象为 Owner 指定的真实工作区；本节不固定已清理的过渡工作树或任何一次性路径。

- 必须识别 pnpm workspace、Tauri/Rust、React/TypeScript、内容工具和课程内容的不同模块角色。
- 文档中的计划服务端不能在缺少实现证据时写成已交付能力。
- node_modules、dist 和 Rust target 不进入证据。

多工作树归并、根外 Git 授权和工作树差异保真继续按 `IMP-03` 与第 5 节由合成工作区验证，不再要求为真实 CodeRoute 验收重建已清理的过渡状态。

### SliverShield

- 必须区分 Flutter 移动端、Python API、数据库迁移和基础设施模块。
- 已修改和未跟踪的非忽略代码/文档可被索引，但必须标为 working-tree evidence。
- .venv、Flutter build、.dart_tool、本地运行数据和环境配置不进入证据。
- 文档、测试、迁移与运行代码必须分别标记证据类型，不互相冒充。

## 5. 质量与安全门禁

- Python：单元测试、类型检查、lint、格式检查和 SQLite migration 测试全部通过；当前命令以根目录 README 与 runtime manifest 为准，变更时同步更新。macOS 与 Linux（含 WSL2）均须在真实环境中通过 `make gate`（不是 mock/monkeypatch 替代）；Linux 须安装 bwrap（`dependencies = []` 不破坏--bwrap 是系统二进制，与 sandbox-exec 同性质）。原生 Windows 进入支持矩阵前还必须在真实 GA Windows 上通过 `make gate` 与 `IMP-31`，两者缺一不可。
- TypeScript 前端：类型检查、lint、单元测试和可复现构建通过；构建产物不得引用远端 CDN、远端字体或同目录静态资源。构建门禁还必须静态检出 `ADR-0008` 决策 6 列出的禁用 API（含 `element.style` 的任何属性赋值），校验 CSP meta 中的哈希与实际内联内容一致，并验证同一 `ReportBundle` 重复渲染产出逐字节相同的入口文件。静态门禁不得以“源码中必须出现某段字符串或某句 UI 文案”代替行为断言（`ADR-0008` 决策 8、`DASH-INV-11`）。
- 看板行为：在真实渲染产物上跨 Chromium 与 WebKit 各执行一次可机检核对，断言干净加载零控制台错误、零外部请求、多宽度 × 全视图零横向溢出、打印分支生效，并以“注入 `style` 属性必须触发 CSP 违规”为阳性对照。核对脚本随运行时前端一起维护，不以设计原型中的同类脚本充当证据。
- 门禁的反向用例：至少覆盖一条含 `style=` 的 `code` token 能正常渲染，一条引用命令行片段（如 `rg -i`）的非个人化 Claim 能通过归因校验。二者任一失败即为门禁缺陷。
- 端到端：合成工作区覆盖嵌套仓库、symlink 环/别名、linked worktree 与根外伪造指针、dirty/untracked、三阶段失效哈希、未知语言、进程中断和部分失败。
- 视觉：桌面与窄窗口截图不存在遮挡、横向溢出、不可读对比度或无反馈交互。
- 隐私：数据库、argv、环境变量、stdout/stderr、诊断、产物和仓库扫描均不包含原始 SessionCapability、环境变量值、密钥、完整源码快照或完整面试对话；回执只含 scope/notice/session binding digest，根外 Git 不含历史/object/blob/diff/config。
- 注入安全：子进程使用参数数组且不经 shell；SQLite 写入参数化；报告字段进行上下文安全编码并通过 CSP/无网络请求检查；仓库/JD 指令不改变 Agent 控制流。
- 性能：`fast` 无变化 refresh 只处理候选元数据与必要哈希，`verify_content` 可重哈希全部合资格文件但不得重新深读全部源码；两者分别建立基线。具体时间预算由真实首扫数据在任务卡固化，不能预先伪造数字。

## 6. 发布条件

只有同时满足以下条件才允许创建或更新可安装私有版本和用户级安装；仓库中已有代码或文档本身不构成发布：

1. DOC-01 至 DOC-07 已由 Owner 核对；
2. 当前支持矩阵要求 IMP-01 至 IMP-30 有可复现的本地证据；原生 Windows 只有在 IMP-31 全部通过后才能加入该矩阵；
3. CodeRoute 与 SliverShield 只读验收通过，未通过项均有明确 ScanIssue；
4. 离线 HTML 已完成视觉验收，且 DASH-01 至 DASH-12 全部通过；
5. 仓库不存在个人数据、扫描缓存、密钥或真实项目源码副本；
6. 安装后显式 Skill 调用可复现同一版本的报告契约。

## 7. Owner 核对边界

Owner 核对聚焦于：目标是否正确、决策是否完整、验收是否足以证明产品有效。任务顺序、工作量、依赖版本和精确命令应以当前代码、manifest 与可复现门禁为准，不在本文件中预先猜测。
