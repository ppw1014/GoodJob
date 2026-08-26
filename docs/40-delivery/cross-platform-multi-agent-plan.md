# 跨平台 + 多 Agent 适配计划：goodjob-career-review

> 状态：终审后修订 v4，待复审（review by ds + Sol v1/v2/final + glm-plus, 2026-08-12）
>
> 日期：2026-08-11
>
> 作者：glm-plus（基于代码独立探索）
>
> 用户裁定：原生 Windows 仅接受 Git 文件系统读隔离缺口；网络隔离和扫描器核心 FS guard 不降级；uv 保留 + python 回退
>
> 治理定位：本文件是交付路线与技术 spike 输入，不是产品/架构契约。下文的候选原语必须先经真机验证并进入带稳定编号的 FR/NFR、superseding ADR 与权威设计文档；后续任务卡只引用这些权威编号，不直接引用本计划建立新契约。

---

## 1. 背景与问题

goodjob-career-review 技能当前仅支持 macOS + Codex + uv。用户朋友在 Windows + OpenCode 环境下遇到三个阻断：

1. **macOS-only**：`git_metadata.py:1180` 的 `sys.platform != "darwin"` 守卫使 Git 元数据读取在非 macOS 上显式失败；`sandbox-exec` Seatbelt 沙箱和 BSD `ps` 均无跨平台实现。
2. **Codex 专属**：SKILL.md 全篇围绕 Codex 调用模型设计；`db.py:40` 的 schema CHECK 约束 `issuer_kind = 'codex_task_runtime'`、`paths.py:8` 的 `~/.codex/` 默认目录均为硬编码。
3. **uv 未安装**：SKILL.md:17 硬性要求 `uv run`，无 python 回退。

### 1.1 用户目标

路线图目标是支持 Linux、macOS、WSL2、原生 Windows（WSL1 不支持用户命名空间，fail-closed），并逐宿主适配 OpenCode、Codex、ClaudeCode、ZCode、MimoCode。当前仓库以 macOS + Codex 为现有支持基线；Phase 2 必须让 Codex 也按 §5.5 的同一套 5 项探针回归，任何宿主在探针、真机 E2E 和权威契约闭合前都不以“已验证”身份进入支持矩阵。

### 1.2 已确认的设计裁定

| 决策点 | 用户选择 | 含义 |
|---|---|---|
| 原生 Windows 安全边界 | 只接受 Git 文件系统读隔离缺口 | Git 仍须有可证明的网络隔离；扫描器核心 FS guard 仍须保持授权根边界；任一边界无法建立时原生 Windows fail-closed。WSL2 仍享完整沙箱 |
| uv 启动器 | 保留 uv，加 python 回退 | uv 为推荐启动器；检测到 uv 不存在时自动回退到 `python3.12 -I -B` |

---

## 2. 现状架构分析（独立探索结论）

### 2.1 安全模型的 5 层平台耦合

| 层 | 模块 | macOS 原语 | 安全作用 | Linux 可用？ | Windows 可用？ |
|---|---|---|---|---|---|
| Git 沙箱 | `git_metadata.py:57-75,1180` | `sandbox-exec` Seatbelt profile | 拒网络、只读授权根、禁 hooks | ❌ 需 bwrap | ⚠️ 候选为 WFP 网络隔离 + Job 单进程限制；真机探针通过前不承诺支持 |
| 进程身份 | `process_identity.py:30` | BSD `ps -o lstart=` | 防 PID 重用（崩溃恢复） | ⚠️ 用 `/proc` | ❌ 需 `GetProcessTimes` |
| FS 安全 | `safe_fs.py`, `source_io.py`, `git_metadata.py` | `dir_fd=` openat + `O_NOFOLLOW` + `os.fstat` | 路径穿越 + 符号链接防御 | ✅ 原样可用 | ❌ `dir_fd` raise `NotImplementedError` |
| 能力传递 | `session.py:1440-1475` | `os.pipe()` + `pass_fds` | 256-bit token 经继承 FD 传给子进程 | ✅ 原样可用 | ❌ `pass_fds` POSIX-only（Python 文档明确标注）；`os.fpathconf` Unix-only |
| 写入锁 | `locks.py:25` | `fcntl.flock` | 单写互斥 | ✅ 原样可用 | ❌ 需 `msvcrt.locking` |

**关键发现：**

- **Linux 共享全部 POSIX API**——dir_fd、fcntl、os.pipe、pass_fds、O_NOFOLLOW 在 Linux 上原样可用。只有 sandbox-exec 和 BSD ps 需要替换。Phase 1 改动量小；这不改变当前 macOS + Codex 现有支持基线的产品状态，Codex 仍须在 Phase 2 按统一宿主探针回归。
- **session.py 的 JSONL broker 协议已 agent 无关**——无 "codex" 字符串，纯 stdin/stdout JSONL。Codex 耦合仅在 SKILL.md 措辞、db.py CHECK 约束、paths.py 默认目录、auth.py INSERT 字面量。
- **session.py 的 `subprocess.Popen` 在 Windows 上不可直接复用**：`pass_fds` 是 POSIX-only（Python 官方文档明确标注），`os.fpathconf` 是 Unix-only（Windows 上函数不存在，抛 `AttributeError` 而非 `OSError`，`except OSError -> 512` 回退接不住）。`preexec_fn`/`start_new_session` 也是 POSIX-only。Windows 需完整重新设计子进程启动、FD 传递和进程树回收。
- **已有注入接口**：`GitMetadataReader.__init__` 接受 `workspace_git_command: Callable` 回调。测试已通过 monkeypatch `scanner._git_command` 绕过沙箱。
- **`generator_id` 已是自由文本**——代码层面 agent 传入什么就存什么，只有 SKILL.md 示例和测试硬编码 `"codex"`。

### 2.2 Codex 耦合点清单

**硬编码（需改代码）：**

| 位置 | 当前值 | 性质 |
|---|---|---|
| `db.py:40` | `CHECK (issuer_kind = 'codex_task_runtime')` | schema 约束 |
| `auth.py:150` | `'codex_task_runtime'` INSERT 字面量 | 应用层硬编码 |
| `paths.py:8` | `~/.codex/goodjob-career-review` | 默认数据目录 |
| `cli.py:95` | help 文本 `~/.codex/...` | 文档 |
| `pyproject.toml:8` | `"GoodJob Codex Skill"` | 包描述 |

**文档级（已接受的架构决策）：**

- README.md:3,32,47-81,85,145,205,227,233 —— Codex 为唯一支持 agent
- docs/00-product/vision-and-goals.md —— 产品愿景围绕 Codex
- docs/10-product/product-requirements.md —— NFR/FR 契约绑定 Codex
- docs/20-architecture/system-design.md, evidence-model.md, scanning-and-analysis.md —— 架构绑定 Codex
- docs/30-decisions/decision-log.md D-001/D-002/D-019 —— 决策记录
- ADR-0001, ADR-0006 —— ADR 绑定 Codex
- docs/collab/protocol.md §8 平台行 —— "运行时当前是 macOS-only…不引入新的平台假设"

**已 agent 无关（无需改）：**

- `session.py` —— JSONL stdin 循环，无 "codex" 字符串
- `generator_id` 字段 —— `preparation.py:394,441` 已是自由文本
- 能力/收据模型 —— 概念上 task-scoped，但不调 Codex API

### 2.3 uv 耦合点清单

- SKILL.md:17 —— 生产启动器（唯一硬编码点）
- Makefile:9-12,24 —— 开发门禁（`uv run ruff/mypy/pytest`、`uv build`）
- README.md:33-34,258,280-283,291 —— 环境要求 + 开发指南
- docs/collab/protocol.md:336 —— 门禁命令表
- runtime/uv.lock —— 锁文件
- **无 python 回退**：`python3` 仅用于无关的开发脚本（Makefile:19-20, README:294-295,306）

### 2.4 测试与构建基础设施

- **15 个测试文件，174 个测试函数**
- **无平台条件跳转**：零 `skipif`、零 `sys.platform` 引用
- **E2E 测试隐式 macOS-only**：`test_history.py`、`test_cli.py`、`test_installation.py` 跑真实 broker -> 真实 sandbox-exec
- **无 CI**：`.github/` 目录不存在
- **前端平台无关**：除 `verify.mjs` 需 Playwright 浏览器二进制
- **DB 迁移系统**：10 个迁移（v1-v10），下一个为 v11
- **protocol.md §8**：平台为 macOS-only 是已接受契约；`dependencies = []` 是供应链边界（红区 L1）；安全边界是红区 L1

---

## 3. 架构设计

### 3.1 平台后端选择

在 `runtime/src/goodjob/platform/` 新建轻量平台抽象层，import 时选择实现：

```
platform/
├── __init__.py          # detect_platform() -> Platform 枚举
├── sandbox_macos.py     # SeatbeltSandbox（现有代码提取）
├── sandbox_linux.py     # BwrapSandbox（新）
├── sandbox_windows.py   # WfpGitSandbox（新，Phase 3）
├── process_posix.py     # macOS/Linux 进程身份（现有代码 + Linux 分支）
├── process_windows.py   # Windows 进程身份（新，Phase 3）
├── launcher_windows.py  # Win32 CreateProcessW + Job + pipe（新，Phase 3）
├── fs_posix.py           # dir_fd 操作（现有代码提取，Phase 3）
├── fs_windows.py         # Win32 路径解析（新，Phase 3）
├── lock_posix.py         # fcntl（现有代码提取，Phase 3）
└── lock_windows.py       # msvcrt（新，Phase 3）
```

**设计原则：**

- POSIX 代码在 macOS + Linux 上原样复用（Phase 1 不动 FS/锁/能力层）
- Windows 实现在 Phase 3 独立开发
- 沙箱是唯一需要在 macOS/Linux 间切换的层（sandbox-exec vs bwrap）
- `dependencies = []` 不破坏——bwrap/sandbox-exec/ps 是系统二进制（与 sandbox-exec 同性质），不是 Python 包依赖

---

## 4. Phase 1：Linux/WSL2 平台支持（完整安全模型）

**目标：** macOS + Linux + WSL2 均享完整沙箱安全模型。

**改动量小的原因：** Linux 共享全部 POSIX API。只有 sandbox-exec（-> bwrap）和 BSD ps（-> /proc）需替换，其余 dir_fd/fcntl/os.pipe/pass_fds 原样可用。

### 4.0 Phase 1 契约门（代码前置、同变更闭合）

Phase 1 已扩大平台产品承诺，不能先合代码、等 Phase 2 再补文档。第一张实现卡必须先取得新的稳定 `FR-*` / `NFR-*`，并在 **Phase 1 同一变更** 中完成：

- 更新 `vision-and-goals.md`、`product-requirements.md` 的 Linux/WSL2 产品范围；
- 新增 `ADR-0009-cross-platform-runtime-security.md`，记录 macOS/Linux/WSL2 沙箱与 fail-closed 边界；需要改变 ADR-0001/ADR-0006 的平台限定时，在 ADR-0009 中逐条声明 supersede，不改写旧 ADR 的历史决策正文；
- 同步 `docs/index.md` 的平台状态与目标路线（包括 `:10`），并在文档地图中加入 ADR-0009；同步 `system-design.md`、`scanning-and-analysis.md`、`acceptance-baseline.md`、`decision-log.md`、README 和 `docs/collab/protocol.md`；`docs/index.md:93,97` 及 Codex-only 会话条款留在 Phase 2 的完整 Agent 契约闭包中，不得在 Phase 1 提前改成“多 Agent 已支持”；
- Phase 1 开卡同时建立全量影响清单，至少点名 `docs/index.md:10,93,97`、`artifacts-and-learning.md:12`、`ADR-0003`、`ADR-0007`，为每个条目标注“本 Phase 同步 / Phase 2 同步及阻塞理由 / 历史保留”；清单未闭合前不得称 Phase 独立可交付；
- 文档契约、实现和真实 Linux/WSL2 验收一起合入后，Phase 1 才能宣称独立交付。仅 `make gate-docs` 通过不构成功能完成证据。

以下 §4.1-4.6 是 ADR-0009 的候选实现输入；在 ADR 和权威设计接受前不具规范性，也不能直接据此出实现卡。

### 4.1 平台后端选择器

**新增文件：** `runtime/src/goodjob/platform/__init__.py` + `platform/detect.py`

- `detect_platform()` 返回 `Platform.MACOS` / `Platform.LINUX` / `Platform.WINDOWS`
- 基于 `sys.platform`（`darwin` / `linux*` / `win32`）
- `select_git_sandbox(git_executable, authorized_root, git_args)` 工厂方法

### 4.2 bwrap 沙箱后端

**新增文件：** `runtime/src/goodjob/platform/sandbox_linux.py`

- `BwrapSandbox.build_command(git_executable, authorized_root, git_args) -> list[str]`
- bwrap profile 等价于现有 Seatbelt：
  - `--unshare-net`（禁网络，等价 `(deny network*)`）
  - `--unshare-pid`（私有 PID namespace，防止沙箱内进程看到宿主进程列表）
  - `--ro-bind <authorized_root> <authorized_root>`（只读授权根）
  - `--ro-bind /usr /usr` + `--ro-bind /lib /lib` + `--ro-bind-try /lib64 /lib64`（系统库 + git 二进制可读）
  - `--ro-bind-try /etc /etc`（git 需 `/etc/passwd`、`/etc/group` 做身份解析，`/etc/resolv.conf`、`/etc/nsswitch.conf` 等）
  - `--dev /dev`（挂全新 devtmpfs，含 null/zero/random 等，等价 `(allow file-write-data (literal "/dev/null"))`）
  - `--proc /proc`（procfs，需在 `--unshare-pid` 之后挂载，否则暴露宿主进程 cmdline）
  - `--tmpfs /tmp`（空 tmpfs，避免宿主 `/tmp` 泄漏）
  - `--die-with-parent`（父进程退出时杀死子进程）
- 检测 bwrap 是否存在（`shutil.which("bwrap")`），不存在则 fail-closed
- **安全权衡说明：** bwrap 使 `/usr`、`/etc` 可读，比 macOS Seatbelt 的"仅 AUTHORIZED_ROOT + GIT_EXECUTABLE"更宽。但 `/usr`、`/etc` 是系统文件不含用户数据；用户 home 目录和其他工作区不可达是关键安全属性。
- **注意：** "bwrap 等价 Seatbelt、改动量小"是设计目标，Phase 1 必须在真实 Linux 上跑通全部 E2E 验证。

### 4.3 修改 `git_metadata.py`

| 改动 | 位置 | 说明 |
|---|---|---|
| 替换平台守卫 | `_git_command():1180` | `sys.platform != "darwin"` -> `select_git_sandbox()` 后端选择 |
| macOS 路径 | 现有 `SANDBOX_EXECUTABLE` + `GIT_SANDBOX_PROFILE` | 提取到 `platform/sandbox_macos.py` |
| Linux 路径 | 新增 | 调用 `BwrapSandbox.build_command()` |
| Git 可执行文件发现 | `GIT_EXECUTABLE_CANDIDATES:45-50` | 增加 Linux 路径：`/usr/bin/git`、`/usr/local/bin/git`；用 `shutil.which("git")` 兜底 |
| `GIT_ENV["PATH"]` | `:78` | 保持 `/usr/bin:/bin`（Linux 也可用） |
| `workspace_git_command` 回调 | `GitMetadataReader.__init__:417-431` | 不变——沙箱选择在 scanner 装配时决定 |

### 4.4 修改 `process_identity.py`

| 改动 | 位置 | 说明 |
|---|---|---|
| macOS 分支 | `process_start_marker():29-40` | 保持现有 `/bin/ps -o lstart=` |
| Linux 分支 | 新增 | 读取 `/proc/<pid>/stat` 第 22 字段（starttime jiffies）——无需子进程，格式稳定 |
| `os.kill(pid, 0)` | `owner_process_stopped():55` | Linux 上同样可用，不变 |
| 返回格式 | `process_identity():11-15` | 格式不重要（`pid:NNN;started:MMM`），只要同进程一致、PID 重用时变化 |

### 4.5 测试

- macOS sandbox-exec 专属测试加 `@pytest.mark.skipif(sys.platform != "darwin")`
- 新增 bwrap 沙箱测试，加 `@pytest.mark.skipif(sys.platform == "darwin" or not shutil.which("bwrap"))`
- E2E broker 测试（`test_history.py`、`test_cli.py`、`test_installation.py`）在两个平台各跑一次真实沙箱
- `conftest.py` 增加 `git_sandbox_available` fixture

### 4.6 交付物

- macOS（现有）+ Linux + WSL2 均可运行，安全模型完整
- `make gate` 在 Linux 上全绿

---

## 5. Phase 2：Agent 无关打包（与平台正交）

**目标：** 为 Codex 保持兼容，并让 ZCode / ClaudeCode / OpenCode / MimoCode 逐个通过发现与调用探针；只有通过 §5.5 全部前置门的宿主才进入本 Phase 支持矩阵。

### 5.1 DB 迁移 v11：解除 issuer_kind CHECK 约束

**修改文件：** `db.py`

- 新增 `Migration(version=11, ...)`
- 移除 `authorization_receipts.issuer_kind` 的 `CHECK (issuer_kind = 'codex_task_runtime')`
- **外键安全迁移流程**（`authorization_receipts` 被 `scan_runs`、`worktree_observations`、`preparation_runs` 外键引用，`db.py:1088` 始终开启 `PRAGMA foreign_keys = ON`，直接 `DROP TABLE` 会因隐式 DELETE 触发 `FOREIGN KEY constraint failed`）：
  1. **迁移前备份**：`VACUUM INTO` 生成一致性快照到临时文件（在获取排他锁后、FK 操作前）；备份失败则中止迁移
  2. 迁移引擎在事务外执行 `PRAGMA foreign_keys = OFF`（SQLite 不允许事务内切换）
  3. `BEGIN` 事务
  4. `CREATE TABLE authorization_receipts_new`（无 CHECK 约束）
  5. `INSERT INTO authorization_receipts_new SELECT * FROM authorization_receipts`
  6. `DROP TABLE authorization_receipts`（FK 已关闭，安全）
  7. `ALTER TABLE authorization_receipts_new RENAME TO authorization_receipts`
  8. 重建索引（`authorization_receipts_scope_idx`）
  9. `PRAGMA foreign_key_check`：**查询返回违规行时（非空），立即 `ROLLBACK` 并报错**
  10. `COMMIT`
  11. 事务外执行 `PRAGMA foreign_keys = ON`
- **失败闭包**：以上任何步骤抛异常时 `ROLLBACK`；`finally` 块确保 `PRAGMA foreign_keys = ON` 在所有退出路径上执行（连接不复用 FK 关闭状态）；崩溃后下次连接自动恢复 FK=ON（`db.py:1088` 每次 `_connect` 都设置）
- **恢复流程**：迁移失败时从步骤 1 的备份恢复（`VACUUM INTO` 快照是完整 DB 副本，可直接替换）
- 应用层（`auth.py`）负责校验合法值
- **向后兼容：** 现有数据 `issuer_kind='codex_task_runtime'` 不变
- **验收**：存量 DB 迁移测试 + 故障注入测试（在步骤 6/9 注入失败，验证回滚和 FK 恢复）

### 5.2 参数化 Codex 硬编码值

| 文件 | 行 | 当前值 | 改为 |
|---|---|---|---|
| `auth.py` | `:150` | `'codex_task_runtime'` 字面量 | 从 `--agent-runtime` 参数取值（默认 `codex_task_runtime` 保持兼容） |
| `paths.py` | `:8` | `~/.codex/goodjob-career-review` | 平台感知：若 `~/.codex/goodjob-career-review` 已存在则沿用（兼容）；否则 Linux 用 `~/.local/share/goodjob-career-review`，Windows 用 `%LOCALAPPDATA%/goodjob-career-review`，macOS 保持 `~/.codex/` |
| `cli.py` | `:95` | help 文本 `~/.codex/...` | 更新为平台感知描述 |
| `session.py` | - | 无 agent 标识 | 接收 `--agent-runtime` 参数，传给子进程 |
| `pyproject.toml` | `:8` | `"GoodJob Codex Skill"` | `"GoodJob Career Review Skill"` |

### 5.3 启动器脚本（uv + python 回退）

**新增文件：** `runtime/scripts/launch_broker.py`

逻辑：

1. 检测 `uv` 是否在 PATH 上（`shutil.which("uv")`）
2. 有 uv：`uv run --isolated --no-project --no-config --offline --no-python-downloads --python 3.12 python -I -B <runtime_dir>/scripts/session.py --agent-runtime <runtime>`
3. 无 uv：查找 `python3.12`（或 `python3` >= 3.12），直接 `python3.12 -I -B <runtime_dir>/scripts/session.py --agent-runtime <runtime>`
4. agent 工具只需调用 `python3 scripts/launch_broker.py --agent-runtime <runtime>`

### 5.4 SKILL.md 通用化

| 改动 | 位置 | 说明 |
|---|---|---|
| "Codex" -> "host agent" | `:15,17,28` | "source files opened by Codex" -> "source files opened by the host agent" |
| 启动器指令 | `:17` step 4 | 改为调用 `launch_broker.py` |
| `generator_id` 示例 | `:52` | `"codex"` -> `"<agent-runtime>"` |
| JSONL 协议描述 | `:18-31` | 不变（已 agent 无关） |

### 5.5 各 Agent 清单与兼容性探针

> **重要：** "薄清单即可支持"是未验证断言。SKILL.md 的真实宿主契约不只是发现文件，而是"同一 task 启动一次 broker、持续保持 stdin、跨多次操作复用同一易失 capability、task 结束以 EOF 回收"。每个宿主必须先做兼容性探针，确认以下能力后才可进入支持矩阵：
> 1. 发现路径（清单文件格式与位置）
> 2. 长驻 stdin / 进程句柄保持能力
> 3. 交互确认（JSONL 往返）
> 4. 退出清理与失败关闭行为
> 5. 真机 E2E

| Agent | 发现机制 | 清单文件 | 状态 |
|---|---|---|---|
| Codex | `agents/openai.yaml` + SKILL.md | 现有 | 现有支持基线；Phase 2 须按同一 5 项探针回归，通过前不标“已验证” |
| ZCode | `.agents/skills/` 下 SKILL.md frontmatter | 已可发现 | SKILL.md 通用化后需探针验证 |
| ClaudeCode | `.claude/commands/` 或 skills 机制 | 待探针 | **未验证**--探针通过前不列入支持矩阵 |
| OpenCode | 待探针 | 待探针 | **未验证**--探针通过前不列入支持矩阵 |
| MimoCode | 待探针 | 待探针 | **未验证**--探针通过前不列入支持矩阵 |

每个清单是薄包装：指向同一个 `launch_broker.py` + SKILL.md JSONL 协议。核心运行时代码只有一份。但薄包装可用性取决于宿主是否满足上述 5 项契约，未通过者明确留在待支持状态。

### 5.6 文档更新

> **Phase 2 契约门（`docs/index.md:55-57`）：** Agent 产品范围和会话边界必须在 Phase 2 代码前或同一变更中同步。Phase 1 只闭合平台契约，不得提前声称多 Agent 已交付；Phase 2 也不得复用“更新旧 ADR”来改变已接受决策。

| 文件 | 改动 |
|---|---|
| `docs/index.md` | ADR-0009 已由 Phase 1 加入文档地图；本 Phase 更新 `:93,97` 等 Codex-only 入口、task capability 和 Agent 相关摘要，并只新增 ADR-0010 索引 |
| `docs/00-product/vision-and-goals.md` | 产品愿景从 Codex 专属改为“仅通过统一探针的 host agent 可进入支持矩阵”；SessionCapability 和模型处理链提示改为 host-scoped |
| `docs/10-product/product-requirements.md` | 先新增稳定的多 Agent `FR-*` / `NFR-*`，再供设计、实现和验收引用 |
| `docs/20-architecture/system-design.md` | SessionCapability、运行位置、入口绑定 Codex 的段落改为多 agent + 多平台 |
| `docs/20-architecture/evidence-model.md` | `issuer_kind` 绑定 Codex 的段落改为自由文本 |
| `docs/20-architecture/scanning-and-analysis.md` | 平台绑定 macOS 的段落改为多平台，注明安全模型差异 |
| `docs/20-architecture/artifacts-and-learning.md` | 更新 `:12` 等 Codex-only 会话授权与处理链表述 |
| `docs/40-delivery/acceptance-baseline.md` | 新增多平台验收项；Windows 安全降级写入验收门 |
| 新增 `ADR-0010-host-agent-neutral-session.md` | 正式 supersede ADR-0001/0003/0006/0007 中绑定 Codex 的入口、深读、SessionCapability 和 awaiting-context 条款；逐条列出被替代条款及新契约 |
| `ADR-0001/0003/0006/0007` | 只在状态/关系头标注“部分由 ADR-0010 替代”并链接新 ADR；保留原正文作为历史，不直接重写已接受决策 |
| `docs/30-decisions/decision-log.md` | 新增 ADR-0010 决策并更新 D-001、D-002、D-019 等受影响索引的权威来源；不得原地改判而无 supersede 链 |
| `README.md` | 环境要求从 "macOS + Codex + uv" 改为多平台 + 多 agent + uv 可选 |
| `docs/collab/protocol.md §8` 平台行 | 从 "macOS-only" 改为多平台，注明各平台安全模型差异 |

Phase 2 开卡前对 `docs/`、README、SKILL 清单做一次 `Codex|codex_task|\.codex` 全量审计：每个命中必须分类为“应通用化”“兼容保留”或“历史文本”。本表是最低集合，不是允许遗漏其他权威命中的白名单。

§5.1-5.5 的迁移与宿主适配细节同样是 ADR-0010/权威设计的候选输入；只有稳定 FR/NFR 和 ADR supersede 链建立后，才能转成实现验收条款。

### 5.7 交付物

- 朋友在 WSL2 + OpenCode 上可用（**前置：OpenCode 5 项兼容性探针 + 真机 E2E 通过**；未通过则交付物收窄为已验证宿主）
- 已验证的 agent 工具可发现并调用本技能
- uv 不存在时自动回退到 python3.12

---

## 6. Phase 3：原生 Windows 平台支持（仅 Git FS 读隔离降级）

> 当前状态（2026-08-16）：Phase 3-A/B 已闭合，最终发布候选启用原生 Windows 持久 release gate。每次运行仍须在 broker 前通过九项 prerequisite，失败时给出准确 remediation 或 WSL2 建议；用户运行不依赖 GNU Make。候选只有在同一冻结 head/tree 上通过 Windows `make gate` 与 `IMP-31A-G`，且最终 merge tree 与候选 tree 完全一致时才能合入；下文保留早期 spike 与实现步骤作为交付追溯。

**目标：** 原生 Windows（非 WSL）可运行；只保留 Owner 已接受的 Git 文件系统读隔离缺口，网络隔离、扫描器授权根边界和进程树回收不降级。

**改动量大的原因：** Windows 缺乏 `dir_fd`、`fcntl`、`sandbox-exec`、BSD `ps`，且 `pass_fds`（POSIX-only）、`os.fpathconf`（Unix-only）、`os.killpg`（POSIX-only）均不可用。整个文件系统安全层、能力传递链、子进程树回收和 bounded-output 循环均需 Win32 重新实现。

### 6.0 Phase 3 安全前置门

Phase 3 先以真机 spike 验证 §6.1 的 WFP、NT handle-relative FS 和 direct `CreateProcessW` 原语；三者任一不可用时，原生 Windows 保持 **unsupported / fail-closed**，不得回退到路径型检查、`subprocess.Popen` 或仅用 Git 环境变量“禁网”。若将来希望接受“无网络隔离”的新降级，必须另取 Owner 明确裁定，并先更新产品 NFR、superseding ADR、风险和验收基线；本计划不代替 Owner 作该裁定。

§6.1 的 API、所有权和时序是为验证终审可实现性而写的 **候选 spike 假设**，不是已经接受的 Windows 架构。spike 产物必须记录最小复现、支持的 Windows/文件系统范围、失败模式和负向证据；通过后再将被证实的接口写入 superseding ADR/权威设计，未通过则回到 Owner 裁定而不是在任务卡内另选弱化方案。

### 6.1 Windows 模块（使用 §3.1 平铺布局）

> **包结构统一：** Windows 模块遵循 §3.1 的平铺布局（`platform/sandbox_windows.py`、`platform/fs_windows.py` 等），不使用 `platform/windows/` 子包。

#### `sandbox_windows.py` - WFP 网络隔离 + Git FS 降级

- **网络安全边界是 WFP，不是 Git 环境变量：** 用 `FwpmEngineOpen0` 打开 dynamic session，在创建 Git 进程前，按规范化 `git.exe` application ID 在 `ALE_AUTH_CONNECT_V4/V6` 与 `ALE_AUTH_RECV_ACCEPT_V4/V6` 安装高权重 block filters；提交后枚举/读取过滤器确认均已生效，最后才允许进程执行。dynamic session 句柄关闭或 broker 崩溃时过滤器由 BFE 自动清理。
- 为防 Git 通过任意 helper 绕过按 application ID 的过滤器，Git Job 设置 `JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 1`。本功能仅允许不需要 helper 的本地元数据命令白名单；任何派生进程请求都失败。WFP/BFE 不可用、无权安装过滤器、过滤器未能回读确认，或命令需要 helper 时均 fail-closed，并提示改用 WSL2。
- `GIT_NO_LAZY_FETCH=1`、`GIT_TERMINAL_PROMPT=0` 只作为纵深防御，文档和测试不得把它们记为网络隔离证据。另固定 `GIT_ALLOW_PROTOCOL=file`，清空 hooks、system/global config、pager、credential helper、fsmonitor、external diff 和交互入口；命令由参数数组和 allowlist 构建，不执行 shell。
- **唯一已接受降级：不做 Git 文件系统读隔离。** Git 进程可能读取授权根外的本机文件；这一限制必须进入 NFR、superseding ADR、运行前提示、风险和验收产物。Python 扫描器自身仍使用下述 handle-relative FS guard。
- 网络真机负测必须同时证明：受 launcher 管理的 egress probe 对 IPv4/IPv6 TCP、UDP 和 DNS 均失败；真实 Git 对本地/受控 HTTP 与 SSH endpoint 均无法连接；恶意 config 尝试派生 helper 时因 Job active-process limit 失败。测试须以可连接的阳性对照证明 endpoint 和测试环境有效。

#### `fs_windows.py` - Win32 路径解析与文件操作

- **根句柄与名称契约：** 授权根只在入口按绝对路径打开一次，立即用 `GetFinalPathNameByHandleW`、`GetFileInformationByHandleEx(FileIdInfo)` 固定 volume serial + file ID；授权提示和 receipt 绑定规范路径与该身份。后续接口只接受该 root handle 和单个相对组件，不接受绝对路径、drive/UNC/device prefix、`.`、`..`、空组件、分隔符或 alternate data stream `:`。
- **逐组件打开：** 用 `NtCreateFile(OBJECT_ATTRIBUTES.RootDirectory=<verified directory handle>)` + `FILE_OPEN_REPARSE_POINT` 相对打开每一组件，打开后以 `FileAttributeTagInfo`/`FileIdInfo` 校验类型、reparse tag、volume 与身份；reparse point 一律不跟随。所有后续动作都作用于返回的句柄，不再把验证后的字符串交给 pathname API。
- **句柄所有权：** 平台层使用唯一的 `OwnedHandle` wrapper；入参 root/parent 是 borrowed，成功返回值是 owned，所有异常路径 `CloseHandle`。rename/disposition 成功后按句柄重新读取身份并更新上层对象；禁止缓存 pathname 作为授权依据。
- **必须覆盖 `safe_fs.py` 全部 8 个操作：**
  - `read_regular`：`NtCreateFile` 相对打开 + `ReadFile` 同句柄读取；打开前后均拒绝 reparse/directory，读取大小受限。
  - `list_directory`：独立枚举的 cursor 与分页采用 [ADR-0012](../30-decisions/adrs/ADR-0012-windows-nt-rename-and-directory-enumeration-correction.md) 和[扫描与分析设计](../20-architecture/scanning-and-analysis.md#34-原生-windows-文件系统与-git-边界)的权威契约；本计划不再复制 API class 细节。
  - `write_new_file_at` / `write_new`：从已验证 parent handle 调 `NtCreateFile(FILE_CREATE | FILE_NON_DIRECTORY_FILE | FILE_OPEN_REPARSE_POINT)`；若同名对象、大小写别名或 reparse 已存在则失败。
  - `open_parent`：逐组件 `NtCreateFile`，每层验证后只把新目录 handle 传给下一层。
  - `replace_file` / `publish_directory`：handle-relative rename primitive 与证据边界采用 [ADR-0012](../30-decisions/adrs/ADR-0012-windows-nt-rename-and-directory-enumeration-correction.md) 和[扫描与分析设计](../20-architecture/scanning-and-analysis.md#34-原生-windows-文件系统与-git-边界)的权威契约；本计划只保留交付追溯。
  - `remove`：从 parent handle 相对打开目标（含 `DELETE` 权限），验证后在该对象 handle 上调用 `SetFileInformationByHandle(FileDispositionInfoEx/FileDispositionInfo)`；不调用 `DeleteFileW`/`RemoveDirectoryW` 重新解析路径。
- **scanner.py 委托：** `readlink` 对相对打开的 reparse handle 调 `DeviceIoControl(FSCTL_GET_REPARSE_POINT)`，只返回数据、不跟随目标；目录枚举复用上述 handle API，不使用 `FindFirstFileExW` pathname 枚举。
- **对抗验收：** 在每个 open/rename/delete 阶段并发把父目录换成 junction/symlink，均不得越出 root identity；另覆盖大小写别名、ADS、跨卷、UNC root、目标替换、reparse tag、超长组件和关闭竞态。每项都要有“根外哨兵未读/未写/未删”的断言。
- 用户裁定只接受 **Git 文件系统沙箱**缺口，不接受扫描器核心 FS guard 的 TOCTOU。若当前 Windows/文件系统不支持上述原语，扫描器 fail-closed，不能退回 `GetFileAttributesW -> CreateFileW` 的 check-then-use。
- `safe_fs.py`、`source_io.py`、`git_metadata.py`、`scanner.py` 中的 dir_fd 操作委托给平台 `fs_windows`

#### `process_windows.py` - GetProcessTimes

- 用 `ctypes` 调 `GetProcessTimes` 获取进程创建时间（FILETIME，100ns 间隔）
- 存活检查：以 `SYNCHRONIZE` 打开进程后执行零超时 `WaitForSingleObject`；对象已 signaled 才证明原 owner 已停止，timeout 表示仍运行，等待或 handle 关闭失败则 fail-closed。PID 与 `GetProcessTimes` creation marker 继续共同防止 PID 复用冒充原 owner。

#### `lock_windows.py` - msvcrt.locking

- Python stdlib `msvcrt` 模块提供 `locking(fd, mode, nbytes)`
- 或用 `ctypes` 调 `LockFileEx`（更接近 fcntl 语义）

#### `capability_windows.py` - 能力传递（Windows 专属设计）

- **`pass_fds` 是 POSIX-only**（Python 官方文档明确标注），Windows 上不可用
- **`os.fpathconf` 是 Unix-only**（Windows 上函数不存在，抛 `AttributeError` 而非 `OSError`，`except OSError -> 512` 回退接不住）
- Windows 能力传递需完整重新设计：
  - 用 Win32 匿名管道（`CreatePipe`）创建 inheritable handle
  - 通过 `STARTUPINFOEXW` 的 `PROC_THREAD_ATTRIBUTE_HANDLE_LIST` 限定继承集合（**该列表只限制继承，不供子进程枚举**）；`CreateProcessW(bInheritHandles=TRUE)`，列表外 handle 均设为 non-inheritable
  - **handle 值通过命令行参数传递**（如 `--capability-handle <N>`），子进程用 `int()` 解析后调 `msvcrt.open_osfhandle(handle, 0)` 转换为 C runtime FD，再 `os.read(fd, ...)` 读取
  - **关闭所有权**：子进程读完数据后 `os.close(fd)`（`msvcrt` 会调 `CloseHandle`）；父进程关闭写端
  - 用固定 chunk size（如 4096）替代 `os.fpathconf(fd, "PC_PIPE_BUF")`
- `session.py:1440-1479` 的 `os.pipe()` + `pass_fds` + `os.fpathconf` 逻辑需平台分支

#### `launcher_windows.py` - direct CreateProcessW 生命周期

- Windows 后端 **不调用 `subprocess.Popen`**。用 `ctypes` 直接调用 `CreateProcessW`，flags 固定包含 `EXTENDED_STARTUPINFO_PRESENT | CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW`，并保留 `PROCESS_INFORMATION.hProcess` 与 `hThread`。
- `Win32Child` 返回对象拥有 `process_handle`、`primary_thread_handle`、`job_handle`、stdout/stderr read handles、stdin/capability parent handles、WFP dynamic-session handle 与 attribute-list storage；类型逐项规定谁关闭，构造失败按逆序清理。argv 用 `list2cmdline` 等 Windows 规则编码，环境从白名单映射构造，不走 shell。
- **无逃逸时序：** 建立并回读 WFP filters -> `CreateProcessW(CREATE_SUSPENDED)` -> `AssignProcessToJobObject`（Job 预先设 `KILL_ON_JOB_CLOSE`、内存/时间/active-process limits）-> 关闭 child-side parent copies -> `ResumeThread(hThread)` -> 关闭 primary thread handle。assign/resume 任一步失败都 `TerminateProcess`/`TerminateJobObject`，不得执行子进程入口。
- stdout/stderr 使用两个 reader 线程与共享累计字节预算；每次读取在放入 `queue.Queue(maxsize=N)` 前先原子预留预算，不能让 pipe + queue + accumulator 任一层无界。超限、超时或协议错误统一：`TerminateJobObject` -> 关闭父端不再使用的 pipe handles -> 有限时 `join` readers -> 丢弃超预算尾部 -> 关闭 process/job/WFP handles。禁止 `communicate()`。
- capability 与 Git 两条子进程链共用该 launcher；Git 额外安装 WFP filters 和 active-process limit，业务子进程按各自 policy 配置 Job。真机测试在子进程入口第一条指令派生后代，断言在 resume 前已入 Job 且后代不能逃逸。

### 6.2 修改现有模块使用平台后端

| 模块 | 改动 |
|---|---|
| `git_metadata.py` | 根据平台选择沙箱后端 + 子进程启动模式 |
| `session.py` / `cli.py` / `auth.py` | capability/payload handle argv 适配，消费端统一转换并落实关闭所有权 |
| `safe_fs.py` | dir_fd 操作委托给 `platform.fs_windows` |
| `source_io.py` | 同上 |
| `scanner.py` | `os.readlink(dir_fd=)` 和 `os.scandir(os.dup(fd))` 委托给平台 |
| `locks.py` | 委托给 `platform.lock_windows` |
| `process_identity.py` | 委托给 `platform.process_windows` |

### 6.3 Windows 测试

- Windows 专属测试（`@pytest.mark.skipif(sys.platform != "win32")`）
- POSIX 专属测试加 skipif
- §6.0-6.1 每个安全边界都必须有 Windows 真机阳性/负向 E2E；mock/monkeypatch 测试只能补充，不能替代 WFP、NTFS/reparse、Job 与 pipe 行为证据
- 无管理员/WFP 权限、BFE 停止、非 NTFS/不支持原子句柄操作等环境明确 fail-closed；测试断言没有静默进入 pathname 或无网络隔离后端
- 安全模型差异写入权威 NFR、superseding ADR、验收基线和运行前提示

### 6.4 交付物

- 原生 Windows 发布门已启用；每次运行只有在 WFP、NT handle-relative FS、direct launcher 所需的九项 prerequisite 全部通过后才启动 broker，否则 fail-closed 并给出准确修复动作或 WSL2 建议
- 唯一安全降级是 Git 无文件系统读隔离，且已在权威契约和运行前提示中可见
- WSL2 用户仍走 Linux bwrap（完整沙箱）

---

## 7. 测试策略

- `@pytest.mark.skipif(sys.platform == ...)` 按平台跳过
- macOS sandbox-exec 测试仅 darwin 跑
- Linux bwrap 测试仅 linux 跑（且 bwrap 存在）
- Windows 测试仅 win32 跑
- E2E broker 测试在各平台各跑一次真实沙箱
- `make gate` 在三个平台均全绿
- **CI 不在本计划范围**——protocol.md §6 标记 "CI/workflow 结构" 为红区 L1，建议另出任务卡

---

## 8. 风险

| # | 风险 | 影响 | 缓解 |
|---|---|---|---|
| 1 | bwrap 可用性 | 部分 Linux 发行版默认未装 | fail-closed + 清晰报错 + 安装指引；不回退到无沙箱 |
| 1b | bwrap 已安装但无法运行 | 非特权用户命名空间在大量发行版默认受限：Ubuntu 23.10+ AppArmor 限制、Debian `kernel.unprivileged_userns_clone`、容器/CI 环境常禁 userns；**WSL1 不支持用户命名空间（WSL2 才支持）** | 运行时捕获 bwrap 启动失败并给明确报错 + 开启指引；文档明确"完整沙箱 = WSL2"；不回退到无沙箱 |
| 2 | Windows Git FS 读隔离缺口 | Git 进程可读取授权根外的本机文件；网络、hooks/config 入口和扫描器核心 FS guard 仍必须受保护 | WFP + Job active-process limit + Git allowlist；运行前明确提示唯一降级；不满足前置门就 fail-closed；WSL2 用户可获完整沙箱 |
| 3 | DB 迁移 FK 约束 | `authorization_receipts` 被外键引用，`DROP TABLE` 在 FK 开启时失败 | 迁移引擎级 FK 关闭/重建/`foreign_key_check` 非空 rollback/`finally` 恢复 FK=ON（§5.1）；`VACUUM INTO` 备份 + 故障注入测试 |
| 4 | 向后兼容 | 现有用户数据在 `~/.codex/`、`issuer_kind='codex_task_runtime'` | 数据目录回退检测 + 迁移不破坏 |
| 5 | Agent 兼容性未验证 | Codex 尚需按统一探针回归；ZCode/ClaudeCode/OpenCode/MimoCode 的发现契约、长驻 stdin、capability 复用、退出清理均未完整验证 | 逐宿主兼容性探针（§5.5），通过后才以“已验证”身份进入支持矩阵；未通过者留在现有基线或待支持状态 |

---

## 9. 工作量估计

| Phase | 内容 | 估计会话数 | 交付平台覆盖 |
|---|---|---|---|
| Phase 1 | Linux/WSL2（bwrap + /proc + 守卫泛化） | ~1 | macOS + Linux + WSL2 |
| Phase 2 | Agent 无关（DB 迁移 + 参数化 + 启动器 + 清单 + 权威文档同步） | ~2-3 | Codex 为现有支持基线但须按同一 5 项探针回归；ZCode/ClaudeCode/OpenCode/MimoCode 均待各自探针，通过者才加入 |
| Phase 3 | 原生 Windows（WFP + NT handle-relative FS + capability 链重写 + direct launcher/Job Object + 锁 + 进程身份 + bounded-output 重写） | ~4-5 | 前置门全部通过后 + 原生 Windows（仅 Git FS 读隔离缺口） |

**建议执行顺序：** Phase 1 -> Phase 2 -> Phase 3。Phase 1 + 2 完成后，若 OpenCode 兼容性探针通过，朋友即可在 WSL2 + OpenCode 上使用；未通过则交付物收窄为已验证宿主。Phase 3 补齐原生 Windows。每个 Phase 独立可交付、可验证。

---

## 10. 明确不在范围

- CI/GitHub Actions 矩阵（红区 L1，另出任务卡）
- Git 进程的完整文件系统读隔离（用户已接受该单一缺口）；扫描器核心 FS guard、网络隔离和进程树回收不在此豁免内
- 重写现有 `issuer_kind='codex_task_runtime'` 历史数据（向后兼容保留）
- 修改 JSONL 协议本身（已 agent 无关，无需改）
- 前端 dashboard 跨平台适配（已平台无关，除 Playwright verify 需浏览器二进制）

---

## 附录 A：平台耦合点完整清单（file:line）

### Git 沙箱

- `git_metadata.py:57` —— `SANDBOX_EXECUTABLE = Path("/usr/bin/sandbox-exec")`
- `git_metadata.py:59-75` —— `GIT_SANDBOX_PROFILE`（Seatbelt DSL）
- `git_metadata.py:1180` —— `sys.platform != "darwin"` 守卫（唯一的平台条件）
- `git_metadata.py:1198-1206` —— sandbox-exec 命令组装
- `git_metadata.py:1260` —— `preexec_fn=partial(os.fchdir, worktree_fd)`（POSIX-only）
- `git_metadata.py:1261` —— `start_new_session=True`（POSIX-only）
- `git_metadata.py:1302` —— `os.killpg(process.pid, signal.SIGKILL)`（POSIX-only）

### 进程身份

- `process_identity.py:8` —— `_PROCESS_ENV = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}`
- `process_identity.py:30-31` —— `["/bin/ps", "-o", "lstart=", "-p", str(pid)]`
- `process_identity.py:55` —— `os.kill(pid, 0)`（POSIX-only）

### FS 安全（dir_fd / O_NOFOLLOW / fstat）

- `git_metadata.py:111-201` —— `_open_directory`、`_open_absolute_directory`、`_open_regular_file_at`、`_safe_lstat`、`_directory_identity`
- `source_io.py:13-63` —— `open_regular_file`、`open_absolute_regular_file`
- `safe_fs.py:15-289` —— 全部文件操作（write_new_file_at、open_parent、read_regular、list_directory、write_new、replace_file、publish_directory、remove）
- `scanner.py:1407` —— `os.readlink(entry.name, dir_fd=directory_fd)`
- `scanner.py:1386` —— `os.scandir(os.dup(directory_fd))`

### 锁

- `locks.py:5` —— `import fcntl`
- `locks.py:25` —— `fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)`
- `locks.py:39` —— `fcntl.flock(self._fd, fcntl.LOCK_UN)`

### 能力传递

- `session.py:377` —— `os.fpathconf(file_descriptor, "PC_PIPE_BUF")`（Unix-only；`except OSError` 回退接不住 Windows 上的 `AttributeError`）
- `session.py:1440,1447` —— `os.pipe()`
- `session.py:1456-1475` —— `pass_fds`（POSIX-only，Python 官方文档明确标注）

### Git 可执行文件发现

- `git_metadata.py:45-50` —— `GIT_EXECUTABLE_CANDIDATES`（macOS 路径）
- `git_metadata.py:52-55` —— `GIT_EXECUTABLE` 回退 `/usr/bin/git`
- `git_metadata.py:77-88` —— `GIT_ENV`

### Codex 硬编码

- `db.py:40` —— `CHECK (issuer_kind = 'codex_task_runtime')`
- `auth.py:150` —— `'codex_task_runtime'` INSERT 字面量
- `paths.py:8` —— `DEFAULT_DATA_DIR = ~/.codex/goodjob-career-review`
- `cli.py:95` —— help 文本
- `pyproject.toml:8` —— description
- `SKILL.md:15,17,28,52` —— Codex 措辞 + generator_id 示例

### uv 耦合

- `SKILL.md:17` —— 生产启动器
- `Makefile:9-12,24` —— 开发门禁
- `README.md:33-34,258,280-283,291` —— 环境要求
- `docs/collab/protocol.md:336` —— 门禁命令表
- `runtime/uv.lock` —— 锁文件
