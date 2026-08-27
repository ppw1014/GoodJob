# GoodJob 扫描与岗位化分析契约

> 状态：待 Owner 核对  
> 权威范围：定义授权工作区的发现、索引、增量刷新、Git 历史读取、语言适配和 host agent 按证据深读的行为；不定义 SQLite 实体字段或最终报告章节
> 上游：[产品需求](../10-product/product-requirements.md)、[系统设计](system-design.md)、[证据模型](evidence-model.md)、[ADR-0003](../30-decisions/adrs/ADR-0003-evidence-pointers-without-source-snapshots.md)、[ADR-0005](../30-decisions/adrs/ADR-0005-local-first-discovery-and-degradation.md)、[ADR-0006](../30-decisions/adrs/ADR-0006-authorized-codex-analysis-and-external-git-metadata.md)、[ADR-0007](../30-decisions/adrs/ADR-0007-review-state-lineage-and-snapshot-integrity.md)、[ADR-0009](../30-decisions/adrs/ADR-0009-cross-platform-runtime-security.md)、[ADR-0011](../30-decisions/adrs/ADR-0011-native-windows-security-contract.md)、[ADR-0012](../30-decisions/adrs/ADR-0012-windows-nt-rename-and-directory-enumeration-correction.md)
> 下游：[产物与学习闭环](artifacts-and-learning.md)、[验收基线](../40-delivery/acceptance-baseline.md)

## 1. 目标与边界

扫描的目标不是让 host agent 无边界地阅读全部源码，而是把 Owner 在当前显式会话中确认可分析的工作区转换成可增量维护、可定位、可按岗位重排的证据目录。扫描器只做确定性观察：发现项目和模块、提取结构化证据、记录覆盖范围及问题。岗位价值、个人经历和面试叙事由 `RoleLens` 与准备流程在此基础上形成。

- `workspace_path` 的本机可读性不足以授权源码分析。`scan`、`refresh` 或向 host agent 返回源码衍生 EvidenceBundle 前，必须用当前 host agent task 的易失 SessionCapability 通过 `AuthorizationReceipt(source_analysis)` digest/scope/notice 校验；receipt ID 或 SQLite 记录本身不授权任何读取。
- 首版只扫描 Owner 明确提供、在本机可读的工作区根；不递归扫描未授权位置，不触发网络抓取或 Git fetch。工作区内 `.git` 指针只是 3.2 的不可信候选，不能自行授权任何根外读取（`FR-02`、`NFR-01`）。
- 扫描产物写入证据模型中的 `ScanRun`、`ProjectSnapshot`、`SourceRevision`、`Evidence` 与 `ScanIssue`；字段、身份和状态以[证据模型](evidence-model.md)为唯一事实源。
- 扫描器不保存源码正文、完整函数、完整 diff 或大段文档。原文件仍是实现事实源，数据库只保存指针、哈希、短摘要和结构化状态（`NFR-02`）。
- 文件内容、manifest、Git 文本和路径名一律是不可信数据。适配器只能解析字节/文本并调用参数化的只读 Git 子进程，不得 import 或执行项目模块，不得运行构建脚本、包管理器或 shell 拼接，也不得把仓库中的指令当作 Skill 指令（`NFR-08`）。Git 子进程在平台原生安全后端中运行：macOS `sandbox-exec` / Linux 与 WSL2 `bwrap` 由已接受的 [ADR-0009](../30-decisions/adrs/ADR-0009-cross-platform-runtime-security.md) 定义；原生 Windows 的 WFP + Job 契约由已接受的 [ADR-0011](../30-decisions/adrs/ADR-0011-native-windows-security-contract.md) 定义，Phase 3-B 运行时候选已合入，在 `IMP-31` 全部通过前保持 unsupported。任一已支持后端不可用时 fail-closed。
- 本文中的“项目”指证据模型的 `Project`，“工作树”指 `Worktree`，“快照”指不可变 `ProjectSnapshot`；不得以目录名或远端 URL 替代其稳定身份。

## 2. 运行契约

### 2.1 输入与终态

| 操作 | 必需输入 | 成功输出 | 不能建立时的行为 |
| --- | --- | --- | --- |
| `scan` | `workspace_path`、`config_revision`、`SessionAuthorizationEnvelope` | 一个终态 `ScanRun`、Workspace/Project 身份、覆盖摘要与 ScanIssue | capability/receipt/path 任一无效时零项目读取、零业务写入，不产生可用快照 |
| `refresh` | `workspace_id`、`config_revision`、`change_detection_mode=fast\|verify_content`、`SessionAuthorizationEnvelope` | 一个新的终态 `ScanRun`；未变内容复用既有 revision，变化内容新增 revision | 工作区不可访问或会话绑定无效时不产生新快照；历史快照和 `latest` 保持不变 |
| 岗位证据查询 | `scan_run_id`、`role_lens_id`、`SessionAuthorizationEnvelope`、可选项目/模块/类型过滤 | 有界 `EvidenceBundle`，带项目排序、时效、覆盖与深读建议 | 会话绑定无效时不返回项目衍生数据；无可用 ProjectSnapshot 时返回明确空结果 |

`scan` 是首次登记或强制全量索引，`refresh` 是 Owner 显式触发的增量操作（`FR-03`、`FR-04`）。首版没有文件监听、定时扫描或后台服务。空工作区可以完成一次零项目扫描，但空目录本身不得被登记为项目。`fast` 的“无变化”表示未检测到变化；`verify_content` 才表示本次对全部合资格文件重新核验了内容身份。

### 2.2 事务、快照与降级

每个项目必须在独立 SQLite 事务中处理，并遵守[证据模型 §6.2](evidence-model.md)的 `ScanRunProject.snapshot_disposition`：

- 项目成功索引，创建新 `ProjectSnapshot`，记为 `fresh`。
- 项目本次失败但已有成功基线，引用最近基线，记为 `carried_forward`，同时写入本次 `ScanIssue`；该数据不得在输出中称为 fresh。
- 项目本次失败且没有基线，记为 `failed_no_baseline`，不创建空快照。
- Owner 在个人配置中明确排除的项目记为 `excluded`，并记录命中的规则来源（规则形态见 §4.3）。该分类只能由真实生效的排除规则产生；没有排除机制时它不得存在于任何输出、schema 取值或验收场景中。

至少一个项目存在可用快照时，扫描可终态为 `completed` 或 `partial`；有任一 `carried_forward`、`failed_no_baseline` 或未解决覆盖问题时必须为 `partial`。无可用项目快照或工作区无法建立时为 `failed`。进程异常结束且未完成发布的运行在下一写会话中标为 `interrupted`；它不能成为 PreparationRun 基线，即使其中已有按项目提交的中间快照。失败或中断项目不得删除、回写或污染上一份成功快照（`FR-15`、`NFR-04`、`NFR-05`）。

## 3. 项目发现与隔离

### 3.1 探索顺序

1. 将工作区根解析为规范化实路径；普通文件和目录符号链接一律不跟随。遍历器用 descriptor-relative `O_NOFOLLOW` 打开实际目录和文件：根内链接记录 `symlink_skipped` alias 覆盖信息，可能逃逸根外的链接记录 `symlink_outside_authorized_root`，二者都不产生项目、模块或 Evidence。`.repo` 是 repo/manifest 的 Git 元数据目录，始终从普通源码遍历和逐链接诊断中排除；需要纳入链接目标时，Owner 应把目标的真实目录作为新的显式工作区运行；工作区内 `.git` 普通文件对受限 Git 元数据的例外按 3.2 执行。
2. 在普通 ignore 生效前发现 `.git` 目录与 `.git` 指针文件。硬安全排除仍优先，避免进入依赖、构建和密钥区域。
3. 对每个候选 Git 根读取本地 Git 元数据；先确定独立仓库，再按各自仓库规则扫描。父仓库的 `.gitignore` 不得吞掉内层 Git 仓库。
4. 用规范化 `git common-dir` 归并同一 Git 项目的主工作树和 linked worktree；每个实际工作树保留单独的根、分支、HEAD 与 dirty observation。
5. 对不处于已发现 Git 项目内的剩余目录，识别可靠的非 Git 项目根；已发现项目内的 manifest 用于划分 `Module`，不重复变成第二个 `Project`。

Git 元数据损坏时，该候选项目产生 `broken_repository` 类 `ScanIssue`；扫描器不得把其目录悄悄降格为非 Git 项目，也不得中止其他项目。

### 3.2 Git 项目、嵌套仓库与工作树

- Git 项目的身份是 `git common-dir` 的规范化实路径。同一 common-dir 只生成一个 `Project`，即使工作区中存在多个 linked worktree（`FR-03`）。
- `WorktreeObservation` 记录每个工作树在本次 `ScanRun` 的 branch、HEAD 和 dirty state。项目归并只去重 Project 身份；相同内容可按 `content_equivalence_key` 复用解析并折叠展示，但必须保留各自来源。不同分支的当前状态绝不混写为同一份实现事实。外部元数据模式无法证明 dirty state 时必须记录 `not_applicable`，对应源码 Evidence 的 `commit_state` 也只能是 `not_applicable`，不得猜成 `committed`。
- 若 `.git` 文件的 `git_dir`/common-dir 或 `.git` 目录的 common-dir 位于授权根外，扫描器先执行仅限根内标记的 candidate inspection：有限长度解析 `.git` 标记，显示 `marker_kind`、词法规范化的 `git_dir_candidate` 和可在根内得出的 `common_dir_candidate`，此时绝不打开根外候选。Owner 授予绑定同一 SessionCapability、该根内标记和这些精确候选的 `AuthorizationReceipt(external_git_relation_probe)` 后，扫描器才可用 descriptor-bound 直接文件读取打开候选目录、读取 `gitdir/commondir` 关系文件并验证回指。该阶段不得启动 Git 子进程、读取 HEAD/ref/index/config/object 或持久化 Project 身份。
- 关系探测解析出规范化 `git_dir/common_dir`，并返回两个目录的 device/inode 身份。扫描器必须把精确路径、身份、拟读取字段和边界再次展示给 Owner，取得当前会话同时绑定路径与身份的 `AuthorizationReceipt(external_git_metadata)`。随后仍只能通过 `O_NOFOLLOW` 目录描述符直接读取白名单字段，并在读取前后复核根内标记、双向关系、路径与目录身份；任何候选替换、路径变化、符号链接、格式错误或回指不匹配都形成 `untrusted_git_pointer` 或 `external_git_relation_mismatch` ScanIssue。外部阶段绝不启动 Git，因此也不会隐式读取 repository config。
- 验证成功后，首版只读取绑定关系与 HEAD/ref；不读取 index/dirty 状态，并把相关覆盖明确标为不可用。不得读取根外 Git 历史、对象库、作者、标题、路径范围、blob、diff、其他 worktree、源码、配置或模块；需要此类信息时形成可见知识缺口，Owner 可显式扩大工作区后重新运行。覆盖报告必须列出工作树、已确认 git-dir/common-dir、回执时间和实际读取字段。
- 内层 Git 根是独立项目。它的子树从父项目源码遍历中排除，避免同一文件同时归属父、子两个项目。
- repo/manifest 工作区的 `.repo` 目录不作为 Project 或 Module 遍历；扫描器会在启动任何 Git 子进程前检查候选 `.git` 的顶层元数据链接、`gitdir` 和 `commondir`。目标仍在 Owner 当前授权根内时，沿既有 `O_NOFOLLOW` 绑定继续按子仓库扫描；目标越过授权根或链接链无法安全闭合时，只产生一个 repository-level `unsupported_repository_layout` 诊断，提示重新选择并授权包含完整元数据的 manifest 根，不自动扩大根、不读取根外目标，也不把 manifest 聚合体建模成一个 Project。

### 3.3 非 Git 项目与模块

首版的非 Git 项目发现以以下根级标记为准：`package.json`、`pyproject.toml`、`Cargo.toml`、`pubspec.yaml`、`go.mod`、`pom.xml`、`build.gradle`/`build.gradle.kts`、`*.sln`、`*.csproj`、`Package.swift`、`CMakeLists.txt`。必须至少存在一个标记文件，普通目录、文档目录和空目录不能成为项目。

模块边界优先由 Git 项目的 workspace manifest、语言 workspace 配置、服务/应用 manifest、数据库迁移根和明确的 build/test 配置给出。无法由这些证据确认的目录只作为文件集合，不创建虚假的 `Module`。模块记录本次 `ProjectSnapshot` 的边界，随快照版本化（`FR-03`、`NFR-07`）。

### 3.4 POSIX 工作区文件系统能力预检

macOS/Linux launcher 在收到 `--workspace` 时，在启动 broker 或 Git 前对工作区路径做只读文件系统能力探测。macOS 读取系统 `statfs` 的 filesystem type 与 local flag；Linux 读取 `statfs.f_type` 并只识别明确的本地 filesystem magic。APFS/HFS、ext、XFS、Btrfs、tmpfs、overlay 等已知本地类型可继续；sshfs、macFUSE/FUSE、NFS、SMB、WebDAV 及其他明确非本地类型返回 `workspace_filesystem` failed check 和 `unsupported_capability` remediation。未知类型或探测失败在两个平台都 fail-closed，并保留稳定的 type/flags 诊断字段，不解析本地化 stderr。Linux 的 bwrap 仍是实际访问隔离权威，但不构成对未知或远程文件系统的默认放行。

同一能力探测也在 scanner 直接入口执行，根路径失败会在已建立的终态 `ScanRun` 中写入一个 `workspace_filesystem_unsupported` error，随后返回结构化 `failed` 结果；因此绕过 launcher 不会把挂载问题泛化成 Git boundary issue。对每个候选工作树，scanner 在 descriptor-bound 解析出工作树根、`git_dir` 和 `common_dir` 后、任何 Git 子进程前再次探测这些必需路径；已授权的外部 Git 元数据也在打开外部目录前遵守相同规则。非 supported 结果阻断该工作树并产生 `git_metadata_filesystem_unsupported`，不读取外部元数据、不执行 Git。探测只调用本地系统 API，不通过 SSH 远程执行、不自动复制工作区、不自动换根。完整 issue 仍写入 SQLite，overview 的摘要展示规则见 §8。

### 3.5 原生 Windows 文件系统与 Git 边界

原生 Windows 入口只允许把一个绝对路径解析为授权 root handle 一次；入口立即记录 `GetFinalPathNameByHandleW` 的显示路径及 `FileIdInfo` 的 volume serial/file ID。后续 discovery、index、source read、rename、publish、remove、scanner readlink 与目录枚举只经 `ARCH-I12` 传递 borrowed root/parent handle 和单个名称组件，不能把缓存 pathname 当作授权。

名称组件必须在调用 NT API 前拒绝绝对路径、drive/UNC/device prefix、空组件、`.`、`..`、`/`、`\` 和 ADS `:`。每层用 `NtCreateFile(OBJECT_ATTRIBUTES.RootDirectory=<verified parent>)` 与 `FILE_OPEN_REPARSE_POINT` 打开，随后以 `FileAttributeTagInfo`/`FileIdInfo` 验证类型、reparse tag、volume 与身份；reparse point 不跟随。任何时候都禁止 `GetFileAttributesW -> CreateFileW`、`DeleteFileW`、`RemoveDirectoryW` 或其他重新按路径解析的授权/操作。

`safe_fs.py` 的八项操作采用以下稳定语义：

| 操作 | Windows handle 契约 |
| --- | --- |
| `read_regular` | 相对打开同一 owned file handle，验证非目录/非 reparse 后有界 `ReadFile`；不重新打开 |
| `list_directory` | 从 directory handle 枚举名称；每次调用首个查询使用 `FileIdBothDirectoryRestartInfo` (11) 重置 cursor，后续分页使用 `FileIdBothDirectoryInfo` (10)；每个待访问 entry 仍从该 parent handle 相对打开并验证 |
| `write_new_file_at` / `write_new` | 从已验证 parent 用 `FILE_CREATE | FILE_NON_DIRECTORY_FILE | FILE_OPEN_REPARSE_POINT` 创建；同名、大小写别名或 reparse 已存在即失败 |
| `open_parent` | 每个组件相对打开并验证，新 owned directory handle 成为下一层 borrowed parent |
| `replace_file` / `publish_directory` | 固定 source 与 target parent handles，使用 `NtSetInformationFile(FileRenameInformation=10)` 的 `RootDirectory`；`ReplaceIfExists` 按调用语义取值，名称为不含终止 NUL 的 UTF-16LE 相对组件；只允许同 volume 且原子语义可证明 |
| `remove` | 相对打开目标并取得 `DELETE` 权限，在同一 object handle 上设置 disposition information |

scanner `readlink` 从 reparse handle 调 `FSCTL_GET_REPARSE_POINT`，只返回 reparse 数据而不跟随；枚举不使用 pathname `FindFirstFileExW`。非 NTFS、UNC root、跨卷或当前文件系统不能证明上述语义时先 fail-closed，只有新增对应真机证据和契约后才能扩大支持范围。

Git 是唯一允许缺少文件系统读隔离的子进程，但它仍只能直接启动 `mingw64\bin\git.exe`：为同一二进制 application ID 安装/回读 WFP V4/V6 filters，并在 `ACTIVE_PROCESS=1` Job 中 suspended assign 后 resume。`cmd\git.exe` shim、helper 派生、WFP/BFE 失败或过滤器未回读都使 Git 操作失败。该降级不改变扫描器本身的 root-handle 授权边界。

## 4. 文件选择、安全排除与事实优先级

### 4.1 忽略规则

发现嵌套 Git 根后，扫描器对每个独立项目分别应用其 `.gitignore`、项目工具 ignore 和 Owner 配置的普通忽略规则。忽略只影响该项目中的普通文件纳入，不影响已经确认的内层仓库身份。

以下类别在任何普通扫描中硬排：

- Git 内部对象与 hooks、依赖和包缓存目录；
- 构建产物、测试覆盖产物、临时目录和运行时缓存；
- 实际环境变量文件、私钥、证书私钥、凭据存储和常见密钥命名文件；
- 二进制、大型生成物或无法安全解码的文件。

扫描器实现的 ignore 语义是 `.gitignore` 的确定性子集，不承诺与 Git 逐条等价。子集的支持范围必须在实现中显式列举，并满足两条约束：命中不确定时偏向排除而不是纳入；遇到已知不支持的模式语法（例如需要精确锚定或需要在已排除目录下重新纳入的写法）必须产生 `ignore_pattern_unsupported` 类 `ScanIssue`，说明该模式被按何种近似语义处理，而不是静默按近似结果继续。覆盖摘要必须能让 Owner 看出“哪些内容因为哪条规则没有进入证据”（`NFR-01`、`FR-15`）。

首版没有“把被 ignore 排除的文件重新纳入”的机制。被普通 ignore 或硬安全排除误伤的示例、fixture 或源码，只能通过调整项目自身的 ignore 规则或把目标目录作为新的显式工作区来纳入。精确到相对文件路径的安全例外是 `F-009`，不进入首版：它是唯一会让扫描读到本来被排除内容的入口，需要独立设计秘密拒绝校验、例外命中审计和覆盖摘要呈现，不能作为忽略规则的附带能力顺手加入。排除类别与未读原因仍必须进入覆盖摘要。

### 4.2 当前工作树是实现事实源

事实强度按来源区分，具体 `Evidence`、`ClaimRevision.facets` 与 `EvidenceValidity` 定义以[证据模型 §5](evidence-model.md)为准：

- 当前可读工作树中的实现、迁移或可执行定义可以支持 `implemented`；测试源码/配置在同时存在实现支持时只支持 `test_defined`。只有能关联相关 revision/commit 的通过结果元数据才支持 `test_verified`；GoodJob 不通过运行测试来创造该状态。
- manifest 只能说明声明的工具或依赖；只有 import、调用、配置接线或运行入口等额外证据才能写成“实际使用”。
- 文档、设计和任务计划只能支持 `documented` 或 `planned`，不得自动升级为 `implemented`、`test_defined` 或 `test_verified`。
- 已修改和未跟踪的非忽略文件同样可入证据库，但其 `commit_state` 必须分别为 `modified` 或 `untracked`，不得伪装成已交付历史。
- Git 作者和提交范围只提供检索线索，不能单独推出 Owner 的负责、主导或结果。
- “当前工作树优先”表示所有授权范围内的当前 worktree 都优先于 Git 历史描述，不表示任意挑一个分支覆盖其他分支。多 worktree 事实一致时可形成 project-scope Claim；不一致时必须按 worktree 分开或显式标冲突。

每个源码型 Evidence 必须持有相对工作树路径、`SourceRevision`、内容哈希和定位器；摘要只说明为什么该位置有意义，不能复制源码正文。准备新材料前，使用者必须按当前 `ScanRun` 解析 `current`、`stale` 或 `missing` 时效；旧摘要不得静默证明当前实现（`FR-11`、`NFR-02`）。

### 4.3 项目级排除与配置边界

Owner 可在个人数据目录的 `config.toml` 中登记项目级排除规则，命中的项目在本次 `ScanRun` 中记为 `excluded`（§2.2），不创建快照、不产生 Evidence，也不进入合资格集合。

- 规则按已发现项目的稳定身份或工作区相对位置匹配，不是文件通配；它只减少读取范围，不会让扫描读到任何本来读不到的内容。
- 每条命中必须记录规则来源，并在覆盖摘要中与 `failed_no_baseline` 分开呈现——前者是 Owner 的选择，后者是失败，二者不得混为一类。
- 配置不可读、格式错误或规则指向不存在的项目时，扫描继续进行并产生 `ScanIssue`，不静默忽略整份配置，也不因为配置问题使扫描失败。
- 排除规则本身是个人配置，不进入仓库；`config_revision` 变更必须使受影响项目在下一次运行中重新评估。

配置文件的职责边界到此为止。工作区注册、项目身份和角色信息由 SQLite 持有，不在 `config.toml` 中重复定义（`D-034` 已据此收窄）。

## 5. 全量索引、增量刷新与 Git 历史

### 5.1 初始索引与 refresh

首次 `scan` 对全部合资格文件建立内容身份。`refresh(mode=fast)` 先比较项目发现结果、Git HEAD/dirty 状态、文件路径集合、文件元数据和分析器/配置版本；候选变化文件以及所有已报告 `modified`/`untracked` 文件重新计算 `content_sha256` 与 `analysis_fingerprint`。`fast` 的路径、大小和 `mtime_ns` 只是性能筛选条件，不能被表述为全量内容验证。

`refresh(mode=verify_content)` 对全部合资格文件重算 `content_sha256`；相同哈希、适配器版本和配置版本的 Evidence 仍复用既有不可变 revision，不因此重新深读。两种模式都在 ScanRun 和覆盖摘要中持久化；当 Owner 需要排除保留 mtime/大小的内容变更时必须选择 `verify_content`。

删除路径在新快照中标为 `missing`。同项目内移动文件优先使用本地 Git rename 信息；无法获得 rename 信息时，仅在内容哈希相同的情况下写入 `supersedes_artifact_id` 作为移动线索，仍保留旧路径的历史引用。没有变化时不得重新解析全部源码（`FR-04`、`NFR-04`）。

每次 refresh 都必须重新检查敏感排除和嵌套 Git 根，因为 ignore、目录结构或配置可能已变。分析器或配置版本变更导致的重分析必须在覆盖摘要中可见。

### 5.2 近期 Git 历史

初始 Git 历史窗口固定为扫描开始时刻向前 180 天。查询范围始终包含每个发现工作树的 HEAD；本地默认分支只在无需 fetch 即可唯一解析时加入并集：优先本地存在的唯一 `refs/remotes/*/HEAD` 目标，否则依次尝试本地 `refs/heads/main`、`refs/heads/master`。无 remote、default ref 缺失/歧义或 detached HEAD 时使用 `HEAD-only`，在 WorktreeObservation 与覆盖摘要中标记原因；不猜测、不 fetch、不 checkout、不修改工作树。索引内容限于根内 Git 数据库的 commit 定位、时间、作者元数据、标题和已变更路径范围，不保存完整 diff 正文。

当某个具体 Claim 需要解释当前代码的演进而近期窗口无法回答时，host agent 可以通过 `EvidenceQuery` 针对该 Claim 的证据路径、模块或 commit 发起一次有理由的更早历史查询。查询先返回受限 commit 元数据和路径 candidate；对选中的单个 candidate，若 Git 数据库位于授权根内，host agent 可在当前会话有界读取相关 path 的 diff/blob，`EvidenceDraft` 只保存 commit/object/diff hash、locator、理由和短摘要，不保存正文。若 git-dir 或 common-dir 位于授权根外，则不查询历史，直接形成可见知识缺口；Owner 只有显式扩大工作区后才能重新运行该历史分析。采用的 EvidenceDraft 在 record_analysis 中校验并落为 preparation-scope Evidence，不回写 ProjectSnapshot，不做隐式全历史重索引，也不把历史作者自动转换为个人贡献（`FR-06`、`NFR-02`）。

## 6. 语言适配与基础档案

所有项目至少获得通用基础档案：项目/模块根、manifest、依赖声明、入口与构建配置、测试配置、部署/运行配置、数据库或接口线索、领域名词、文档和文件覆盖。这些条目都是候选证据，不能替代实际实现证据。

首版深读适配器如下（`FR-03`、`NFR-07`）：

| 语言/文件 | 深读范围 | 明确产出 |
| --- | --- | --- |
| TypeScript / TSX | package/workspace、import/export、应用/服务入口、路由/UI 接线、构建与测试配置 | 模块依赖、实际技术使用、入口/边界与测试证据 |
| Python | `pyproject`/requirements、包结构、import、CLI/Web 入口、异步/任务与测试配置 | 服务/工具模块、依赖使用、运行和测试证据 |
| Rust | Cargo workspace/crate、module、feature、bin/lib、错误与异步边界、测试 | crate 关系、编译入口、能力边界与测试证据 |
| Dart | `pubspec`、package、Flutter 应用入口、路由/状态/平台接线与测试 | 移动端模块、依赖使用、UI/平台与测试证据 |
| C / C++ | CMakeLists、include 依赖、类/结构体/枚举/函数定义、main 入口点与能力边界 | 头文件包含、模块依赖、符号定义、入口与技术边界证据 |
| SQL | migration/schema、表/关系、约束/索引、view/trigger、查询文件 | 数据模型、演进和查询能力证据；迁移与计划文档必须区分 |

支持的结构化解析器失败时，该文件仍保留 `SourceArtifact/SourceRevision` 身份与内容哈希，但不生成 `implementation`、`manifest` 或其他成功分析 Evidence；扫描器持久化有界 `analysis_diagnostics`，并创建使项目覆盖降为 `partial` 的 `ScanIssue`。单文件事实达到上限时同样记录 `analysis_truncated`，不得把截断结果呈现为完整理解。诊断只包含运行时定义的状态与补救提示，不保存源码、解析异常原文或项目数据片段。

其他语言仍生成基础档案，并在覆盖中标为“基础分析，未做语言深读”。它们可以支持结构、依赖、文档和配置层面的 Claim；不得生成需要深层调用分析才成立的技术断言。新增深读语言必须作为新的适配器和验收项进入后续决策，不能在首版隐式扩大范围。

## 7. 按证据的岗位化深读

`RoleLens` 先从岗位、JD、职级和覆盖规则构造有限查询。Python 核心返回 `EvidenceBundle`，其中含项目排序、模块、证据定位器、短摘要、时效、覆盖问题和深读建议，但没有源码正文。

host agent 按以下顺序工作：

1. `prepare_start` 对候选 SourceRevision 做预检，确认其仍匹配冻结 ProjectSnapshot；不符时返回 `refresh_required`，不隐式扫描；
2. 读取岗位相关 EvidenceBundle，选择能回答岗位维度的高优先级项目和模块；
3. 打开被选中 Evidence 指向的本地原文件前再次核对内容哈希与当前时效；
4. 只在证据不足、相互矛盾、需要解释实现方式或出现岗位关键追问时，沿 import、调用、配置或测试链路扩展阅读；
5. `record_analysis` 对每个 EvidenceDraft 最后一次校验哈希和 locator。任一所用文件在读取后变化时整批标记 `refresh_required`，不写入 Evidence、Claim 或 Assessment；不能核验的内容作为知识缺口或不确定性，而不是补写为事实。

因此，扫描器索引全量模块，host agent 深读有界证据链。系统不得把全仓源码复制进 EvidenceBundle、数据库或最终报告（`FR-05`、`FR-06`、`FR-11`、`NFR-02`）。

## 8. 覆盖报告与可判定验收规则

每个终态扫描必须在 `EvidenceBundle` 和下游产物中呈现：发现项目数、fresh/carried-forward/failed-no-baseline/excluded 数量、工作树数、模块数、纳入/排除文件类别、`fast`/`verify_content` 检测模式、history basis、深读与基础分析语言、外部 Git 授权例外、每项 `ScanIssue` 的路径范围、原因、影响和补救动作。`coverage_status=complete` 只表示当次配置下合资格输入均被处理，不等于理解全部业务语义。

`scan-overview-v2` 保留有界的 `issues` 和原有 `limits.issue_limit`、`limits.available_issues`、`limits.issues_truncated` 字段；额外以 `(severity, kind, remediation)` 提供确定性 `issue_groups`，每组包含总数、最多三个样本和省略数量。`limits.group_limit` 与 `limits.groups_truncated` 明确分组摘要的边界。error/warning 优先占用 `issues` 配额；二者合计超限时，配额在两个非空严重级别间轮转，避免任一级别被另一方完全挤出。完整审计记录仍只存在 `scan_issues`。摘要必须由数据库聚合、窗口样本和有界原始问题查询生成，不能先把所有问题载入 host 进程后截断。

| ID | 可判定输入 | 必须输出 | 失败或降级行为 | 需求映射 |
| --- | --- | --- | --- | --- |
| `SCAN-01` | 一个可读工作区根或一个无效/不可读根 | 前者产生 `Workspace` 与终态 `ScanRun`；后者不产生可用快照 | 无效根为 `failed`，历史数据不被覆盖 | `FR-02`、`FR-03`、`NFR-01` |
| `SCAN-02` | Git 根、根内 symlink loop/alias、`.git` 指针、嵌套 Git 与 manifest 非 Git 项目混合 | 稳定 Project/Worktree/Module 发现结果；空目录不是项目；循环停止且 alias 不重复解析 | 损坏 Git 根或循环记 `ScanIssue`，不降格、不阻断其他项目 | `FR-03`、`NFR-01`、`NFR-05` |
| `SCAN-03` | 同一 common-dir 的多个 linked worktree，既有相同文件也有分支差异 | 一个 Project、多份 worktree observation；相同内容复用分析但保留来源；差异事实按 worktree 分开 | 不能读取某个工作树时问题可见；不得把不同分支拼成一个当前项目状态 | `FR-03`、`FR-11`、`FR-15` |
| `SCAN-04` | 父项目 ignore 内层 Git；依赖、构建、缓存、`.env` 与密钥文件；一条 Owner 项目级排除规则；一条已知不支持的 ignore 模式语法 | 内层仓库被独立发现；敏感和硬排项不读、不存、不输出；被排除项目记为 `excluded` 并注明规则来源；不支持的模式产生 `ignore_pattern_unsupported` | 普通 ignore 不覆盖内层 Git；硬排项只报告类别，不泄露内容；`excluded` 与 `failed_no_baseline` 在覆盖摘要中分开呈现；近似匹配不得静默生效 | `FR-03`、`FR-15`、`NFR-01` |
| `SCAN-05` | 已提交、modified 与 untracked 的合资格文件、测试定义/结果元数据，以及只含计划的文档 | Evidence 记录正确 commit state/facet；测试定义与通过结果分开；计划不显示为实现 | 无实现不得生成 implemented；无匹配通过结果不得生成 test_verified | `FR-11`、`NFR-02` |
| `SCAN-06` | 首扫后无变化、修改、删除、移动、分析器/配置变更，以及 size/mtime 被保留的内容变化 | fast 运行明确“未检测变化”；verify_content 发现所有内容变化；旧定位出现 stale/missing | 项目事务失败保留旧快照为 `carried_forward`，不冒充 fresh | `FR-04`、`FR-15`、`NFR-04`、`NFR-05` |
| `SCAN-07` | 当前实现与超过/未超过 180 天的 Git 历史；正常默认分支、无默认分支和 detached HEAD | 默认只索引近期本地根内提交；history basis 可解释；按 Claim 可进行有记录的定向追溯 | default 不可解析时使用 HEAD-only；无法读取历史产生 ScanIssue，不阻断当前工作树索引 | `FR-06`、`NFR-02` |
| `SCAN-08` | TS/TSX、Python、Rust、Dart、SQL 和未知语言并存 | 首批语言提供深读证据；未知语言提供明确标注的基础档案 | 语言不支持记覆盖缺口，不阻断其他语言/项目 | `FR-03`、`FR-15`、`NFR-07` |
| `SCAN-09` | 大型多项目工作区与一个岗位 RoleLens | 先返回有界 EvidenceBundle，再按证据打开本地原文件 | 证据失效或不足时要求 refresh 或形成知识缺口，不通读全仓或编造结论 | `FR-05`、`FR-06`、`FR-11`、`NFR-02` |
| `SCAN-10` | 无权限目录、损坏仓库和超限/解析失败文件 | 其余可用项目的快照、结构化 ScanIssue 与覆盖影响 | 有可用快照时为 `partial`；没有可用快照时为 `failed` | `FR-15`、`NFR-05` |
| `SCAN-11` | 文件名、manifest、文档或 Git 标题含 shell 元字符、提示注入或 HTML/脚本文本 | 内容仅作为证据数据与安全短摘要，项目代码和指令均不执行 | 解析失败形成 ScanIssue；不得改变授权、运行命令、访问网络或扩大写入范围 | `NFR-01`、`NFR-08` |
| `SCAN-12` | 合法根外 linked worktree、分别拒绝 relation-probe/metadata 回执、伪造 `.git` 指针、回指不匹配和确认后路径替换 | 合法场景经两阶段精确回执后只读取绑定/HEAD/ref/index 元数据并记录字段；无效场景不越过对应阶段 | 未授权、路径逃逸、关系不匹配或外部历史请求均形成 ScanIssue/知识缺口；probe 阶段不得读取业务 Git 元数据，任何阶段不得持久化未授权目标派生数据 | `FR-03`、`FR-15`、`NFR-01`、`NFR-08` |
| `SCAN-13` | Windows NTFS root；绝对/drive/UNC/device/空/`.`/`..`/分隔符/ADS/超长组件名称；junction/symlink/reparse；大小写别名；跨卷；在 open/rename/delete 每阶段并发替换 parent | 所有授权从 root/parent handle + volume/file identity 导出；合法对象由同一 handle 读写/rename/delete；超长组件有边界值阳性对照，且每个拒绝/并发用例均证明根外哨兵未读/未写/未删 | 非 NTFS/未知原子语义、任何非法或超过后端明示长度上限的组件、reparse、身份/volume 变化和关闭竞态均 fail-closed；不得调用 pathname 降级；mock 只能补充，真机正负证据由 IMP-31 聚合 | `FR-18`、`NFR-01`、`NFR-11` |

## 9. 非首版边界

首版不做 Go、Java、C#、Swift 等语言的深层调用分析，不做全历史默认索引，不监听文件变化，不启动本地服务，也不因 Git 作者自动生成个人贡献结论。这些限制不妨碍之后新增适配器或更强分析，但必须先更新相应决策与验收契约。
