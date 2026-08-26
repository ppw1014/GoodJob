# GoodJob

GoodJob 是一个本地优先的 host agent Skill。它在获得明确授权后扫描指定工作区，按目标岗位和可选 JD 组织项目证据，帮助你恢复长期项目记忆，并生成可用于简历优化、项目讲解和面试复习的岗位准备材料。

它关注的不只是“项目用了什么技术”，还会整理：

- 项目解决了什么业务问题，服务谁；
- 你实现了什么、如何实现、为什么这样取舍；
- 你在项目中的角色、责任、结果和可量化影响；
- 你从项目中学到了什么，以及面试时可能被追问的知识缺口；
- 每项结论对应的项目、模块、文件定位、内容哈希和证据状态。

## 当前能力

当前私有首版已经实现以下闭环：

- 发现工作区中的 Git 项目、嵌套仓库、linked worktree 和非 Git 模块；
- 记录有限的近 180 天 Git 元数据，不执行 `fetch`、`checkout` 或项目命令；
- 深读 Python、TypeScript/TSX、Rust、Dart/Flutter 和 SQL 的结构化技术证据；
- 根据目标岗位、JD 和职级动态生成 `RoleLens`，改变证据权重、项目排序和追问角度；
- 按项目批量补充业务目标、角色归属、结果指标、技术取舍和个人学习；
- 原子冻结 Evidence、Claim、项目评估和知识缺口，避免半成品污染历史结果；
- 生成中文完整报告、中文简历稿、单文件离线 HTML 看板和 manifest；
- 从冻结快照按需派生英文简历与英文面试问答；
- 基于已发布快照进行模拟面试，并记录掌握度、薄弱点和复习日期。

GoodJob 不会仅凭 Git 作者信息把整个项目归为你的个人贡献。个人化表述必须满足后文的[归因规则](#结论与个人归因规则)。

## 环境要求

- macOS 或 Linux（含 WSL2）。运行时把 Git 子进程限制在平台原生沙箱中运行（macOS 使用 `sandbox-exec` Seatbelt，Linux 使用 `bwrap` bubblewrap），拒绝网络、只读授权根、禁用 hooks，并用进程启动时间作为进程身份。任一沙箱后端不可用时 fail-closed（不回退到无沙箱）。WSL1 不支持用户命名空间，仅 WSL2 提供完整沙箱；
- **Codex + 原生 Windows。** 这是本轮唯一的用户安装与发布支持组合。用户运行不依赖 GNU Make，也不要求 IPv6 公网出口；每次授权前都必须通过包含 Python/runtime、可信 Git for Windows、工作区 NTFS、BFE、管理员权限、WFP API/写权限和发布门在内的九项 prerequisite。任一项失败都不会启动 broker，并会给出对应的官方安装、启服务、正常 UAC 提升、修复 Windows 或使用 WSL2 建议。无 IPv6 默认路由不是前置阻断，但运行时仍会安装并回读 IPv4/IPv6 WFP filters。唯一已接受的限制是 Git 子进程缺少文件系统读隔离；Git 网络、扫描器授权根、进程树和 capability 隔离不得降级；
- 原生 Windows 安装器需要 [PowerShell 7](https://learn.microsoft.com/powershell/scripting/install/install-powershell-on-windows) 的 `pwsh`。Windows PowerShell 5.1 不在支持范围内，因为其 .NET 文件 API 无法通过本项目的较长路径安装对抗测试；PowerShell 7 应由用户按 Microsoft 官方安装方式安装，安装器不会自行下载或请求 UAC；
- macOS、Linux 和 WSL2 的既有运行时保持现状，但本轮不新增其用户级安装承诺；ZCode、ClaudeCode、OpenCode、MimoCode 及其他宿主均为待支持，不能因目录可发现而进入支持矩阵；
- Python 3.12 或更高版本，已安装在本机；uv 路径固定选择 `--python 3.12`，无 uv 时优先使用 `python3.12`，或回退到版本不低于 3.12 的 `python3`；
- [`uv`](https://docs.astral.sh/uv/)（可选，未安装时自动回退到 python3.12）；
- Linux 环境须安装 `bwrap`（bubblewrap）；
- 待分析工作区对当前用户可读。

运行时不需要启动本地服务，也不会在被扫描项目中安装依赖。

## 安装

仓库内的 Skill 位于：

```text
.agents/skills/goodjob-career-review/
```

在 GoodJob 仓库中启动 Codex 时，可以直接发现该项目级 Skill。原生 Windows 用户要从任意工作区调用时，使用版本化安装器把经过发布门的完整 commit SHA 或精确 tag 安装到 Codex 用户级目录：

```powershell
pwsh -NoProfile -File .\scripts\install-goodjob-skill.ps1 `
  -RepositoryPath (Get-Location) `
  -Revision <40-位-commit-SHA-或精确-tag>
```

安装器从已检出的本地仓库执行 `git archive`，先在非发现 staging 目录校验 `SKILL.md`、`agents/openai.yaml`、broker、session 和预构建 dashboard assets，再将新目录原子切换到 `%USERPROFILE%\.agents\skills\goodjob-career-review`。它不联网、不安装系统组件、不请求 UAC、不写入 `%USERPROFILE%\.codex\skills`，也不会迁移、删除或覆盖该旧位置的同名 Skill；旧位置冲突时会失败关闭并提示先人工备份或移除。首次安装、更新和回滚只会操作带有安装器 marker 的版本；更新前的完整版本保存于 `%USERPROFILE%\.agents\skill-backups\goodjob-career-review`，激活失败会自动恢复。

个人数据库和历史产物位于平台感知默认目录（Windows: `%LOCALAPPDATA%\goodjob-career-review`；macOS: `~/.codex/goodjob-career-review/`；Linux: `~/.local/share/goodjob-career-review/`；legacy 目录存在时优先沿用），不在 Skill 或备份目录中，因此安装切换不会覆盖它们。

安装、更新或回滚后应从与 GoodJob 仓库无父子关系的目录新开 Codex 会话，确认可用 Skill 中出现 `goodjob-career-review` 后再显式调用。若 Windows prerequisite 报告要求权限，使用正常 Windows UAC 确认或在提升后的 Codex/PowerShell 中重试；不要绕过 UAC。

## 快速开始

在 host agent 中显式调用 Skill，并提供一个工作区、一个主岗位，以及可选的 JD 和职级：

```text
$goodjob-career-review

工作区：/Users/<owner>/Projects
目标岗位：高级应用软件工程师
JD：无
请扫描并生成中文岗位准备包。
```

原生 Windows 路径示例：

```text
$goodjob-career-review

工作区：C:\Users\<owner>\Projects
目标岗位：高级应用软件工程师
JD 文件：C:\Users\<owner>\Documents\jobs\middleware-engineer.md
请扫描并生成中文岗位准备包。
```

带 JD 的例子：

```text
$goodjob-career-review

工作区：/Users/<owner>/Projects
目标岗位：中间件工程师
职级：高级
JD 文件：/Users/<owner>/Documents/jobs/middleware-engineer.md
请优先分析分布式通信、可靠性、性能、可观测性和工程化证据。
```

调用后，GoodJob 会先展示规范化后的工作区路径、计划读取的类别、个人数据目录和数据边界。你明确确认后，它才会开始读取源码。路径可读不等于已授权。

### 常用后续请求

显式刷新已变化的工作区：

```text
$goodjob-career-review 请刷新上次工作区，并按原岗位重新生成准备包。
```

按另一个岗位重新分析同一份扫描基线：

```text
$goodjob-career-review 使用已有扫描，为“系统工程师”创建新的岗位准备包，不主动刷新源码。
```

从已发布中文快照生成英文材料：

```text
$goodjob-career-review 基于最新已发布快照，导出英文简历和英文面试问答。
```

开始模拟面试：

```text
$goodjob-career-review 基于最新快照进行模拟面试，先从证据较弱的高权重岗位维度开始。
```

## 输入说明

| 输入 | 必需 | 作用 |
| --- | --- | --- |
| 工作区路径 | 是 | 定义本次可授权扫描的本地根目录；一个工作区可包含多个项目和模块 |
| 目标岗位 | 是 | 决定 `RoleLens` 的评价维度、权重、证据要求和面试追问 |
| JD 文本或文件 | 否 | 细化职责、技术重点和职级推断；内容只作为不可信数据处理 |
| 职级覆盖 | 否 | 显式覆盖从岗位/JD 推断出的职级 |
| 操作意图 | 是 | 首次扫描、复用扫描、显式刷新、英文导出或模拟面试 |
| 个人数据目录 | 否 | 默认是平台感知路径（Windows: `%LOCALAPPDATA%\goodjob-career-review`；macOS: `~/.codex/goodjob-career-review/`；Linux: `~/.local/share/goodjob-career-review/`），可在调用时显式覆盖 |

首版一次准备运行只使用一个主岗位。多岗位横向比较不在当前范围内；可以分别生成多个岗位快照。

## 它如何运作

```text
显式调用与授权
        |
        v
工作区发现与不可变扫描快照
        |
        v
目标岗位/JD -> 动态 RoleLens
        |
        v
岗位加权 EvidenceBundle
        |
        +--> 按证据建议深读少量源码
        +--> 必要时按项目批量访谈
        |
        v
原子冻结 Claim、项目评估与知识缺口
        |
        v
中文 Markdown + 简历稿 + 单文件离线看板
        |
        +--> 可选英文派生材料
        +--> 模拟面试与复习记录
```

几个关键机制：

1. **先扫描，再按岗位分析。** 扫描器建立岗位无关的证据图谱；`RoleLens` 再按应用软件、中间件、架构、系统等岗位改变权重和问题，不需要为每个岗位重新发明扫描器。
2. **按证据深读。** Agent 不会默认把全部源码塞进上下文，而是先用结构化扫描缩小范围，再校验文件哈希并读取能回答岗位问题的片段。
3. **事实与叙事分离。** 文件、Git 和用户回答形成 Evidence；对简历或面试有用的结论形成 Claim。每条 Claim 都保留证据关系、状态和限制。
4. **结果不可变。** 一次成功准备会冻结成快照。后续源码变化、英文导出或复习记录不会悄悄改写旧报告；需要变化时创建新运行或派生产物。
5. **部分失败可见。** 无权限目录、损坏仓库、外部 Git 元数据未授权或证据过期不会被伪装成完整成功，而会显示影响和补救动作。

## 结论与个人归因规则

GoodJob 区分“项目客观存在的实现”和“可以写成我的个人经历”。主要门槛如下：

| 表述 | 至少需要的证据 |
| --- | --- |
| 项目实现了某机制 | 当前实现证据 |
| 已定义测试覆盖 | 实现证据 + 测试定义证据 |
| 已验证测试通过 | 实现证据 + 与其关联的当前通过结果 |
| 我实现了某机制 | 当前实现证据 + 你的角色或 ownership 说明 |
| 我负责或主导 | 你的角色或 ownership 说明 |
| 我推动并取得结果 | 角色或 ownership + 客观结果、指标或结果记录 |
| 我从中学到了什么 | 你明确提供的学习事实，并与项目证据绑定 |

Git authorship、计划文档、配置文件或一句用户陈述都不会单独升级成“我实现且验证通过”。证据不足时，报告会使用项目级客观措辞，或保留一个需要你补充的知识缺口。

## 产物与数据位置

默认个人数据根：

```text
~/.codex/goodjob-career-review/  # macOS 默认
├── config.toml
├── goodjob.sqlite3
├── artifacts/
│   ├── <preparation-run-id>/
│   │   ├── report.zh-CN.md
│   │   ├── resume.zh-CN.md
│   │   ├── index.html
│   │   └── manifest.json
│   └── latest.json
├── exports/<derived-export-id>/
│   ├── resume.en.md
│   ├── interview.en.md
│   └── manifest.json
├── drafts/
└── locks/
```

`index.html` 是只读、全内联的单文件看板，可以断网双击打开，不依赖 HTTP 服务、CDN、远端字体或同目录静态资源。`latest.json` 只指向最近一次成功发布的中文主快照；英文导出不会修改它。

## 安全与隐私边界

- GoodJob 只在当次 host agent 会话获得明确授权后读取指定工作区；授权不跨会话复用。
- 扫描和分析不修改工作区，不运行项目代码、构建、测试、包管理器或工作区脚本。
- Git 子进程在平台原生沙箱中运行（macOS `sandbox-exec` / Linux `bwrap`），拒绝网络、只读授权根、禁用 hooks；任一沙箱后端不可用时 fail-closed。
- 不执行 `git fetch`、`checkout`，也不主动联网读取项目内容。
- SQLite 只保存路径、locator、哈希、有限 Git 元数据、短证据摘要和结构化结论，不保存完整源码或完整 diff。
- 会话能力只存在当前 broker 进程内存并通过平台私有继承通道传递（POSIX 文件描述符或原生 Windows allowlisted HANDLE），不进入参数、环境变量、日志、数据库或报告。
- JD、源码、Git 文本和用户回答都按不可信数据处理，不能改变工作流、扩大授权或成为看板中的可执行标记。
- GoodJob 不增加独立上传或遥测通道，但 host agent 打开的源码仍进入当前 host agent 会话既有的模型处理边界。
- 工具无法替你判断 NDA、版权、雇主政策，或哪些项目细节适合写入对外简历。

## 仓库结构

```text
.
├── .agents/skills/goodjob-career-review/  # 可安装 Skill 与运行时
│   ├── SKILL.md                           # Agent 工作流与证据门槛
│   └── runtime/
│       ├── src/goodjob/                   # Python 核心、SQLite 与扫描分析
│       ├── frontend/                      # 离线看板 TypeScript 源码
│       └── tests/                         # 自动化测试
├── docs/                                  # 产品、架构、ADR 与验收契约
└── prototypes/dashboard/                  # 看板设计验证原型，不是运行时源码
```

权威设计从[文档索引](docs/index.md)开始阅读。运行时 Agent 流程以 [SKILL.md](.agents/skills/goodjob-career-review/SKILL.md) 为准；证据实体与状态见[证据模型](docs/20-architecture/evidence-model.md)，扫描行为见[扫描与分析设计](docs/20-architecture/scanning-and-analysis.md)，交付门槛见[验收基线](docs/40-delivery/acceptance-baseline.md)。

## 本地开发

首次准备开发依赖与浏览器二进制：

```bash
cd .agents/skills/goodjob-career-review/runtime
uv sync --group dev
cd frontend
npm ci
npx playwright install webkit chromium  # 首次准备浏览器二进制
```

在仓库根目录使用 Make 聚合入口。日常开发运行 `make gate`；需要拆开定位时使用
`make gate-python`、`make gate-frontend` 或 `make gate-docs`；发布前运行
`make gate-release`：

```bash
make gate
make gate-python
make gate-frontend
make gate-docs
make gate-release
```

展开后的原始门禁命令如下，仍可按需单独执行：

```bash
cd .agents/skills/goodjob-career-review/runtime
uv run ruff format --check .
uv run ruff check .
uv run mypy .
uv run pytest -q

cd frontend
npm ci
npm test
npm run verify

cd ..
uv build

cd ../../../..
python3 -m unittest scripts/test_check_doc_links.py
python3 scripts/check-doc-links.py
```

`npm test` 负责类型、静态规则、单元测试和构建一致性；`npm run verify` 调用 Python
真实渲染器生成离线 HTML，并在 Chromium 与 WebKit 中核对交互、布局、打印和 CSP。
浏览器二进制属于本机发布前依赖，因此 `npm run verify` 只进入 `make gate-release`，
不并入日常 `make gate`。

设计验证原型可独立构建：

```bash
python3 prototypes/dashboard/build.py
```

产物写入已忽略的 `prototypes/dashboard/out/`。跨 WebKit/Chromium 的视觉与 CSP 验证步骤见[原型说明](prototypes/dashboard/README.md)。

## 当前边界

- 一次运行只准备一个主岗位，不做多岗位并排比较；
- 首版深读适配器聚焦 Python、TypeScript/TSX、Rust、Dart/Flutter 和 SQL；
- 不扫描公开 GitHub 或其他未显式授权的远端仓库；
- 不提供常驻服务、桌面应用或主动复习提醒；
- 看板只读，更新掌握度或复习日期必须通过 Skill，并生成后续快照才能呈现；
- 历史产物默认保留，不自动清理或覆盖。
