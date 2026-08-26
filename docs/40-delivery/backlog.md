# GoodJob 任务池（backlog）

> 状态：协作运行区，非产品契约
> 权威范围：任务状态的唯一事实源（谁在做、做到哪、裁决落在哪）
> 上游：[验收基线](acceptance-baseline.md)、[决策账本](../30-decisions/decision-log.md)
> 维护者：Architect。Implementer 只读，状态变更走[信道](../collab/channel.md)。
> **2026-08-02 起 Architect 由 Sol 担任**（信道 #36 交接）；Claude Opus 5 退出日常，仅做最终验收。

任务卡在 [docs/collab/tasks/](../collab/tasks/)。本表只记状态与归属，不复制卡面内容。

## 里程碑 M1 · 首版实现缺陷收敛

评审基线 commit `a3fac9f`。该基线上 Python 与前端全部门禁为绿；以下任务指向门禁**没有覆盖到**的地方，不是回归。评审记录见 [opus-review.md](reviews/2026-07-24-opus-design-review.md)（文档层，2026-07-24）与 2026-07-29 实现层评审。

### 批次 A · 门禁判定层级（`D-043`）

| 任务 | 卡面 | 状态 | 验收项 |
| --- | --- | --- | --- |
| GJ-01 · 渲染门禁被工作区内容触发 | [GJ-01](../collab/tasks/GJ-01.md) | ✅ 已验收合入（信道 #5，merge `0e75a78`） | `IMP-28`、`DASH-03` |
| GJ-02 · 归因校验按 token kind 分层 | [GJ-02](../collab/tasks/GJ-02.md) | ✅ 已验收合入（信道 #5，merge `0e75a78`） | `IMP-14`、`IMP-22` |

批量模式：统一分支 `task/GJ-A-structural-gates`，从 `ead37b0` 拉出，按卡独立 commit（`3131465`、`5cebbc1`），统一交付统一验收。

验收裁决要点（信道 #5）：契约与 DoD 逐条成立，范围零越界，体量 64/200 与 84/260。变异测试确认新增用例在缺陷回归时变红；产物在 Chromium 151 与 WebKit 26.5 上零控制台错误、零外部请求、内联 JSON 正常解析。验收中发现的两处**出卡侧疏漏**（非实现缺陷）已转 GJ-08。

### 批次 B · M1 剩余（顺序实施，统一交付）

| 序 | 任务 | 卡面 | 状态 | 体量 | 验收项 |
| --- | --- | --- | --- | --- | --- |
| 1 | GJ-08 · 让分层判定各自守得住 | [GJ-08](../collab/tasks/GJ-08.md) | ✅ 已验收合入（信道 #12，merge `711cb33`） | 197/260 | `IMP-28`、`DASH-03`、`IMP-14`、`IMP-22` |
| 2 | GJ-04 · 跨引擎行为门禁进入运行时前端 | [GJ-04](../collab/tasks/GJ-04.md) | ✅ 已验收合入（信道 #12，merge `711cb33`） | 560/600 | `IMP-28` |
| 3 | GJ-03 · 项目级排除与 `excluded` 生产者 | [GJ-03](../collab/tasks/GJ-03.md) | ✅ 已验收合入（信道 #12，merge `711cb33`） | 586/650 | `IMP-04`、`IMP-13` |
| 4 | GJ-05 · ignore 子集显式化并可见 | [GJ-05](../collab/tasks/GJ-05.md) | ✅ 已验收合入（信道 #12，merge `711cb33`） | 254/450 | `IMP-04`、`SCAN-04` |

批量模式：统一分支 `task/GJ-B-m1-remainder`，**严格按上表顺序**逐卡实施，按卡独立 commit，四卡全部完成后一次性交付统一验收。合计体量上限 1900 行。

顺序约束：GJ-05 必须在 GJ-03 之后（同改 `scanner.py` 覆盖摘要）。其余三卡文件范围互不重叠（GJ-08 碰 `reporting.py`/`analysis.py`，GJ-04 碰 `frontend/`，GJ-03 碰 `scanner.py`/`paths.py`），排此序是为让上下文由热到冷推进。

停工点：任一卡触发 L1 即**停止整条链**并立即上报，不得带着未裁决的问题进入下一张。

派卡前自查修正：GJ-04 契约 7 原本要删 5 条源码文本检查却未在契约 3 中给出等价行为断言，属净损失覆盖，派卡前已补齐一一对应关系与反向验证要求，体量上限相应由 520 提到 600。

验收裁决要点（信道 #12）：四卡契约与 DoD 逐条成立，范围零越界，七个提交文件白名单互不重叠，合计体量 1597/1900。Architect 独立复跑全部门禁，并独立复现 GJ-08 三次归因变异、GJ-04 八条行为反证；另做两项卡面未要求的加验——176 组单调性枚举（0 降级、0 漏检、8 例升级为更强归因，严格超集成立）与一次 GJ-04 源码级反证（改 `dashboard.ts` 的 `<a>` 为 `<span>` 后重新构建，`npm run verify` 红 2 断言 × 2 引擎，证明门禁守到源码而非夹具）。验收中发现的一处**出卡侧疏漏**（非实现缺陷）已转 GJ-09。

裁决记录：**#9 L1 → #10 裁决**。GJ-08 原契约 4「散文投影插入换行分隔」会击穿依赖字面相邻的中文 `_PERSONAL_PATTERNS`，使 `implemented`/`responsible`/`led`/`personal_learning` 四类强归因全部降级为 `personal_assertion`；因 `_attribution_covers` 中 `responsible` 覆盖 `personal_assertion` 却不覆盖 `implemented`，实际会放宽个人归因校验。裁决采纳 Implementer 建议的双投影并集方案，新增单调性硬约束与对应测试、变异自检增至 3 次，体量上限 200 → 260。属出卡侧契约缺陷，非实现缺陷。

### 批次 C · 验收回流

| 任务 | 卡面 | 状态 | 体量 | 验收项 |
| --- | --- | --- | --- | --- |
| GJ-09 · 把转义集合的内容本身锚住 | [GJ-09](../collab/tasks/GJ-09.md) | ✅ 已验收合入（信道 #14，merge `9f06b8f`） | 45/80 | `DASH-03` |

验收裁决要点（信道 #14）：契约 1-4 逐条成立，产品代码零差异，单文件范围。Architect 独立复现 DoD 要求的五次变异，每次均由新锚点点名到具体字符；另加一次卡面未要求的加验——删除 `U+202E`（双向覆写符，本组中唯一具实际欺骗价值的字符，可令看板上的路径或函数名视觉反向显示），批次 B 时同样静默，现已被接住。空洞闭合范围超出 #12 点名的 `&`。

出卡缘由（信道 #12 §4）：GJ-08 契约 3 要求转义集合与后置断言同源——这是对的，但同源必然意味着删掉映射里任一条目会让转义与断言**一起**消失。防线强度因此完全取决于是否有独立于该映射的测试锚住集合内容。验收变异实测：删 `=` → 5 failed，删 `&` → **167 全绿**，删 U+2028 → 1 failed（且只因某夹具恰好含它，非针对性断言）。`=` 被接住是因为参数表硬编码为 `("<", ">", "=")`，恰好等于 GJ-08 的 DoD 点名的三个字符。契约 1 点了七类字符、DoD 只要求直接覆盖三类，差额即此洞。属出卡侧疏漏，GJ-08 实现覆盖范围与要求范围逐字相等。

出卡侧措辞修订（不出卡，记账）：GJ-08 契约 6 写的"必须检出同一个归因"过窄，本意是其后两条 `不得` 子句（不得降级、不得变为不检出）。per-pattern 并集会把部分 `personal_assertion` 升级为 `led`/`implemented`（176 组枚举中 8 例），方向更严，契约 7 明确认可。实现无误，措辞待下次改卡时收紧。

### 待 Owner 裁决（不出卡，观察记录）

| 观察 | 出处 | 说明 |
| --- | --- | --- |
| ~~全部项目被排除时终态为 `failed`~~ | GJ-03 验收 | **Owner 2026-08-02 裁定：改。**关键证据 `history.py:244` 的 `status IN ('completed','partial')` 会把 `failed` 运行整个过滤出下游，Owner 的配置意图被当成故障。已转 [GJ-12](../collab/tasks/GJ-12.md) 契约 1-3 |
| ~~ignore 原始模式行非独立字段~~ | GJ-05 验收 | **Owner 2026-08-02 裁定：做。**已转 [GJ-12](../collab/tasks/GJ-12.md) 契约 4-5 |
| ~~文档门禁静默依赖 `python3` 解析到哪个解释器~~ | GJ-11 #26 裁决时实测 | **Owner 2026-08-02 裁定：不固定解释器。**Architect 实测 Python 3.9.6 / 3.12.13 / 3.14.3 三版本结果完全一致（均 `39 files`、测试 `OK`），脚本只用 walrus 等 3.8+ 特性；运行期版本守卫拦不住 `SyntaxError`（解析期即失败），加了是假防护。仅要求在 docstring 写明「只用 3.9+ 语法」，已转 [GJ-12](../collab/tasks/GJ-12.md) 契约 6。原观察： `make gate-docs` 跑 `python3 scripts/check-doc-links.py`；本机 `python3` 解析到 **Python 3.9.6**（Xcode 自带），而 runtime 要求 3.12。检查器在 3.9.6 上工作正常、门禁为绿，故非缺陷。但门禁行为取决于 `python3` 恰好指向什么，是否固定解释器需 Owner 定。GJ-11 明确不处理此项 |
| ~~protocol §8「路径代码」条款的适用范围~~ | GJ-07 验收 | **Owner 2026-08-02 裁定：收窄到运行时。**`O_NOFOLLOW`/descriptor-relative 只约束 `runtime/src/`（会被安装、会读 Owner 任意工作区）；仓库根 dev 工具（`scripts/`、`prototypes/`）只读本仓库自身内容，不受此条约束，已写的加固保留不回退。**已直接改入 [protocol §8](../collab/protocol.md)，不出卡。**原观察： §8 平台行写「涉及路径/进程/文件的代码仍按 `O_NOFOLLOW`、descriptor-relative 的既有写法」。Architect 撰写时指的是**运行时**（会被安装、会读用户任意工作区），但字面覆盖到了仓库根上只读自己文档的 dev 脚本，GJ-07 据此写了 40 行加固。多守一层无害，但条款适用范围应由 Owner 裁定，非 Architect 单方收窄。GJ-11 契约 7 明确该 40 行保留不动 |

## 里程碑 M1 后 · 门禁入口与结构债

### 批次 D（顺序实施，分卡交付）

| 序 | 任务 | 卡面 | 状态 | 体量上限 | 验收项 |
| --- | --- | --- | --- | --- | --- |
| 1 | GJ-07 · 门禁聚合入口 | [GJ-07](../collab/tasks/GJ-07.md) | ✅ 已验收合入（信道 #20，merge `40491ed`） | 422/180（超额，裁量接受） | `SCAN-04` 门禁可复现性 |
| 2 | GJ-06 · 剥离 Git 元数据与沙箱调用 | [GJ-06](../collab/tasks/GJ-06.md) | ↪ 已转批次 E 并验收合入（信道 #31，merge `ed53ff2`） | 净新增 194/200（移动行单列） | `SCAN-04` |

批量模式：**与批次 B 不同，本批分卡交付分卡验收**。GJ-06 是纯重构且风险集中在红区，必须先拿到 GJ-07 的 `make gate` 作为等价性证据入口，且要在独立的验收窗口里单独评审，不与其他卡混在一次交付里看。

**GJ-07 出卡依据（2026-07-31 Architect 实测）**：改 `frontend/src/dashboard.ts` 一句 UI 文案而不重新构建，`uv run pytest -q`（180 用例）与 `npm run verify`（跨引擎行为门禁）**双双为绿**，仅 `npm run build:check` 变红——Python 侧与 verify 读的都是已提交的 `dashboard_assets/dashboard.js` 产物，不感知 TS 源码。全套门禁中**只有一条命令**知道源码与产物是否一致，且它挂在需要浏览器准备步骤的链子上。

**GJ-06 出卡依据与范围收窄**：`scanner.py` 4189 行，`WorkspaceScanner` 一类 3317 行 66 个方法。按方法名归簇实测：Git / 历史 / 外部仓库沙箱 **28 个方法 1048 行**，遍历 / ignore / 分类 5 个方法 427 行，其余 33 个方法约 1840 行。红区（子进程调用、超时与输出上限、外部仓库授权、路径校验）全部埋在此类中。**本卡只剥第一簇**——纯重构的评审成本随 diff 体积超线性增长，一次搬 1500 行会让「逐条核对是不是纯移动」失效，而那是本卡唯一的安全网。遍历 + ignore 簇留待后续卡。

裁决记录：**#17 L1 → #18 裁决**。GJ-07 原契约 2 按族手工枚举门禁命令，把前端族写成 `npm test`，与 [protocol §8](../collab/protocol.md) 门禁命令表原文 `npm ci && npm test` 不一致，致使契约 2 自身构成「改门禁命令」，与契约 5 直接冲突，Implementer 无法在不违反其一的前提下交卡。裁决采纳 Implementer 建议保留 `npm ci`。**Architect 核查时另发现同源第二处**：协议表为三族，原契约 2 整个漏掉文档族（全仓 markdown 相对链接检查），而该族是三族中唯一连可执行命令都不存在、纯靠人手工执行的一族——恰好被漏在"消灭靠人记性"这张卡之外。契约 2 因此重写为「以协议表为唯一事实源，逐条一致，不增不减不改写」，不再手工枚举；新增 `make gate-docs` 与契约 8（`scripts/check-doc-links.py`，仅标准库）；契约 5 增加冲突消解规则「契约与 protocol §8 冲突时以协议为准」。**契约 9 系撰写裁决时实测撞出**：临时链接检查器把卡面中描述链接语法的示例文本误判为断链，故要求检查器跳过围栏代码块与行内代码，并配双向 DoD 自检；改用代码区感知版复检，34 个 md 断链为零。体量上限 120 → 180。均属出卡侧契约缺陷，非实现缺陷；Implementer 候选提交 `8f67765` 除被拦下的一条外全部成立，无需返工。

GJ-07 验收裁决要点（信道 #20）：契约 1-9 逐条成立，`runtime/` 与锁文件零改动。Architect 独立复跑 `make gate` 并独立复现全部三项漂移自检（产物漂移红在 `build:check`；依赖漂移删除 `node_modules/typescript` 后由 `npm ci` 2 秒装回；断链与代码区跳过双向成立）。`make -n gate` 展开为协议表三族七条、无 `verify`；检查器仅标准库。**体量 422/180，超授权 2.3 倍**，按 [protocol §7](../collab/protocol.md)「是否重构由 review 裁量」——**裁量结果：接受不返工**（披露流程合规，超额源自 Implementer 自身双轴 review 的发现，重写成本高于收益）。实测分支分布：契约明文核心 176 行、容器围栏与完整链接闭合解析 118 行（**本仓库零实例**）、`O_NOFOLLOW`/descriptor walk 40 行。存疑点裁决：「全仓 = Git 已跟踪 + 未忽略未跟踪」语义确认成立，98 → 38 份的收敛正确，不改契约。

### 批次 E（顺序实施，分卡交付）

| 序 | 任务 | 卡面 | 状态 | 体量上限 | 验收项 |
| --- | --- | --- | --- | --- | --- |
| 1 | GJ-11 · 给文档门禁补测试并按覆盖收敛 | [GJ-11](../collab/tasks/GJ-11.md) | ✅ 已验收合入（信道 #28，merge `110315f`） | 318/320 | `SCAN-04` |
| 2 | GJ-06 · 剥离 Git 元数据与沙箱调用 | [GJ-06](../collab/tasks/GJ-06.md) | ✅ 已验收合入（信道 #31，merge `ed53ff2`） | 净新增 194/200 | `SCAN-04` |

裁决记录：**#23 L1 → #24 裁决**。GJ-11 的 DoD 把检查器输出份数写死为 `38 files`（取自 #20 验收 GJ-07 时 `40491ed` 的观测值），而卡面指定的基线 `2f0bbbd` 正是新增 `GJ-11.md` 自身、把计数变为 39 的那个提交——卡面在它自己的基线上即自相矛盾。采纳 Implementer 的第二方案改为动态口径（报告份数 == `git ls-files --cached --others --exclude-standard -- '*.md' | wc -l` 在当次 HEAD 的结果），而非把 38 改成 39。**同因扫描**（出卡门禁第 3 条）发现 GJ-06 同病：DoD 写死「既有 180 个 Python 用例」，已改为动态；契约 2 的导入面名字表加注「以实际代码为准，自行重新 grep 核对，不一致按 L2 报告」。**病因回流**：`architect.md` §1.3 出卡门禁新增第 8 条「派生量不写死」与第 9 条「枚举优先指向上游事实源」；`anti-patterns.md` 出卡侧新增对应两条，源案例分别为 GJ-11 与 GJ-07。均属出卡侧缺陷，非实现缺陷；Implementer 已完成的测试与收敛实现不受影响，无需返工。

裁决记录：**#25 L1 → #26 裁决**。GJ-11 契约 1 要求测试可由裸 `python3 -m unittest` 运行，但允许范围不含使 discovery 生效所需的 `scripts/__init__.py`。**采纳 Implementer 的备选方案而非首选**，依据是 Architect 实测——测试文件消失时：裸 `unittest` 与 `discover -s scripts` 均输出 `Ran 0 tests / OK` 并退出 0（假绿），仅显式命名测试模块会硬错非零。`unittest` 在零测试时报 OK，故门禁不得使用任何 discovery 形式。契约 1 改为固定 `python3 -m unittest scripts/test_check_doc_links.py`，不新增 `__init__.py`；新增 DoD「零测试假绿自检」（改名测试文件后 `make gate-docs` 必须变红）。附带发现：`discover -s scripts` 无需 `__init__.py` 即可发现测试（`-t .` 才需要），故首选方案要解决的问题另有无新增文件的解法，但两种 discovery 都留假绿，均不采用。**同因扫描**发现 GJ-06 变异对等自检存在同型空集假绿（两侧均零失败也满足「集合相同」，实则证明该变异点无覆盖），已补「每次变异两侧均须非空失败集合」。L3：`1ea32f8` 提交标题缺 `(Sol)` 执行者尾注（protocol §8 要求，审计必需），下次提交时补齐即可，不单独 amend。

GJ-11 验收裁决要点（信道 #28）：契约 1-7 逐条成立，产品代码仅 +1/-1（删除 `inline_link_targets` 入口处与 `matching_bracket` 重复的 `is_escaped` 判断），`runtime/` 与 `protocol.md` 零改动，体量 318/320。Architect 抽验三处变异（移除 `--cached`、移除 `FileNotFoundError` 容错、禁用围栏遮罩）均独立复现且命中测试名与交付表一致；**#26 裁决的直接产物单独验证**——测试文件移走后 `make gate-docs` 硬错非零，恢复后 `Ran 20 tests / OK`，假绿路径封死。CLI 输出格式与退出码逐字不变，动态计数口径 39 == 39。契约 5 的取舍（118 行「本仓库零实例」分支选择保留并测试而非删除）在授权范围内且理由落在 Architect 给的误红/误绿判据上，认可。

**记账（不出卡）**：文档链接检查功能现总量为检查器 357 行 + 测试 315 行 = 672 行。本卡是净改善（从「357 行没人管」变为「672 行全被守住」），但根因是 GJ-07 契约 8 定的「零依赖、只用标准库」——Python 标准库无 markdown 解析器，该约束直接导致必须手写。将来此处若再出问题，正确的追问方向是「零依赖约束对一个 dev 脚本是否成立」，而非继续在实现上打补丁。

GJ-06 验收裁决要点（信道 #31）：契约 1-7 逐条成立。`runtime/tests/` diff 为空，`cli.py`/`history.py`/`db.py` 零改动，提交仅含 `scanner.py` 与新增 `git_metadata.py`。行数守恒 `scanner.py` 4189 → 3031（−1158）、新模块 1352、净新增 194，落在 −50..+200。对外导入面 14 个名字（卡面表 13 + L2 补报 `ScanResult`）实测全部保留且签名一致。Architect 自选变异点独立复现对等（`GIT_COMMAND_TIMEOUT_SECONDS` 10.0 → 0.0，两侧失败集合 11 = 11 逐条相同），并机械比对逐字性（标注「仅移动」的方法 15/16 字节相同，`_bind_internal_git` 为契约 5 允许的类限定改写，仅表格标注小疏漏）。自主决策认可：28 个候选只搬 25 个，`_discover`/`_history_evidence`/`_resolve_history_evidence_validity` 因混着遍历或持久化而留下；reader 持有五个协作者而非整个 `WorkspaceScanner`（后者会让新模块反向依赖旧类）。产出：红区代码（Git 子进程、超时、输出上限、外部仓库授权、描述符路径校验）现集中于单一文件并有边界 docstring，可单独审阅。

**Implementer L2 成立（出卡侧疏漏）**：卡面契约 2 的导入面表漏了 `tests/test_preparation.py` 依赖的 `ScanResult`，因 Architect 建表时只 grep 了 `test_scanner.py`。未造成损失——#24 裁决为契约 2 补入的「开工前自己重新 grep 核对，以实际代码为准，不一致按 L2 报」正好在此生效，名字原地保留、调用方零改动、全程未停工。这是出卡门禁第 8/9 条第一次产生实际收益。

**Architect 附带发现（不影响验收，记账）**：`HISTORY_WINDOW_DAYS` 的权威定义随簇迁入 `git_metadata.py`，`scanner.py` 仅余别名 `HISTORY_WINDOW_DAYS = _git_metadata.HISTORY_WINDOW_DAYS`。把**两侧 `scanner.py` 暴露的该名字**改为 0：base 失败 2 条、head 失败 1 条（少的一条是原走 `scanner.py` 内部使用点、现改读 `git_metadata` 自有常量的路径）。产品行为不受影响（两处均为 180，运行期无写入，`history.py` 取值一致），当前 180 条测试无一依赖该缝隙；对照之下 `GIT_COMMAND_TIMEOUT_SECONDS` 的缝隙由闭包 `lambda: GIT_COMMAND_TIMEOUT_SECONDS` 保留完好。**方法论盲区**：变异对等要求把变异打在语义等价的**权威位置**，故在构造上看不见「同一名字在两侧可写性改变」这类差异；覆盖这类需把变异打在**对外暴露的名字**上。属出卡侧设计遗漏，后续同类拆分卡的 DoD 应补「对外暴露名字的可写性对等」一条。

排序理由（信道 #21 §3）：两卡文件范围零重叠（GJ-11 碰 `scripts/` 与 `Makefile`，GJ-06 碰 `runtime/src/goodjob/`），技术上无依赖。GJ-11 在先有两个理由——其一，GJ-06 的等价性证据要跑在 `make gate` 上，那就该先让 `make gate` 自己被守住；其二，GJ-06 是纯重构、零行为变更、风险集中在红区，是队列中最难验的一张，需要一个台面上没有别的卡在飞的评审窗口。

**主干推送**：2026-07-31 首次 `git push origin main`（`a3fac9f..2f0bbbd`，29 个提交，纯快进，未使用 force）。此前批次 A / B / C / D 全程仅本地。推送不改变形态 A 的任何纪律。

出卡缘由（信道 #20 §3）：源自 GJ-07 交付的 L3，Architect 采纳并扩了一半。`scripts/check-doc-links.py` 是 357 行手写 markdown 解析器，**在门禁路径上且零测试**，开发期间被双轴 review 抓出 6 个缺陷。关键是**失败方向不对称**：解析过严 → 门禁误红（可见，会被立刻修）；解析过松 → 门禁**误绿**，即断链存在而门禁报告没有——正是 GJ-07 立卡要消灭的失败类。故本卡不只补测试，还带收敛：无测试覆盖的分支须补测试或删除，不允许两者皆非。取舍依据是失败方向而非行数，Architect 不预设结论。

**GJ-06 的核心约束**：零行为变更、`runtime/tests/` 一行不改（需要改测试即为停工信号）、逐字移动、对外导入面逐字不变（含 `history.py` 依赖的 `_safe_history_path` 与测试依赖的 `_open_regular_file` 两个跨模块私有名）。等价性证据为**变异对等自检**：同一组 5 次变异在重构前后两侧的失败用例集合必须逐个名字相同。

### 批次 F（已验收）

| 任务 | 卡面 | 状态 | 体量上限 | 验收项 |
| --- | --- | --- | --- | --- |
| GJ-12 · M1 遗留小项收口 | [GJ-12](../collab/tasks/GJ-12.md) | ✅ 已验收合入（信道 #35，merge `9dfdd09`） | 123/220 | `IMP-04`、`IMP-13`、`SCAN-04` |

出卡缘由：Owner 2026-08-02 一次性裁定「待 Owner 裁决」区的三条观察，并要求合并为一张卡。三项为——(1) 全部项目被排除时终态不再 `failed`（`available == 0` 且 `excluded == 0` 才判 `failed`，不新增枚举、不做 migration）；(2) `ignore_pattern_unsupported` 的原始模式行提为独立字段；(3) `check-doc-links.py` docstring 写明只用 3.9+ 语法。

验收裁决要点（信道 #35）：契约 1-6 逐条成立，体量 123/220，禁区文件零改动，无 migration、无新增依赖、无状态枚举变化。`_final_status` 确认只改一个条件表达式、其后判定链一行未动；`check-doc-links.py` diff 仅 docstring。Architect 独立复现两次变异自检，均单条命中；`make gate` 全绿 `182 passed`（180 既有 + 2 新增）。三处实现判断认可：`IgnorePatternIssueDraft` 用专用子类型守住契约 5；保留未 `strip()` 的 `raw_line` 并以 `"  /build/  "` 带空白样例证明逐字性（Spec 首轮抓到原实现取了 `.strip()`）；测试从 `result.coverage` 改读 `overview`，证明字段熬过冻结与重新加载。

**Architect 实测发现（不影响验收，记账）**：`_overview_coverage` 在 `scan_run_overviews` 无行时（扫描中断、未走到 `_finish_run`）走重建分支，`_coverage(..., {})` 传空字典，导致 `ignore_pattern_issues` 条目上 **`raw_pattern` 键整个缺失**（其余六个键均在），消费方写 `issue["raw_pattern"]` 会 KeyError。已在临时库上实测复现。**非实现缺陷**——契约 4 只写了「进入覆盖摘要时成为独立字段」，未提降级路径，且 GJ-03 的 `project_exclusions` 在同一路径上同样为空并已被接受。**留待下一张触及覆盖摘要形状的卡一并定「降级路径字段一致性」；明确不得并入 GJ-10（纯重构卡，卡面禁止顺手改对）。**

**观察（不出卡）**：`_unsupported_issue` 内联复制了 `_issue()` 的构造步骤，两份已有一处不同——`_issue` 为 `relative_path=_short(relative_path) if relative_path else None`，副本为 `relative_path=_short(source)`。`source` 恒非空故今日无行为差异，但 `_issue` 将来变更时副本不会跟随。属「抄一份 vs 指向事实源」同族，暂不处理。

**本卡违反 `anti-patterns.md` 的「一张任务卡塞多个目标」，系 Owner 明确要求的合并。**允许理由：三项互不依赖、各自可独立验收、均不在红区、合计体量小于一张常规卡。卡面已写明：任一项长出超预期复杂度即按 L2 上报，由 Architect 拆卡，不得为「一张卡装得下」而压缩测试。

## 发布验收缺口盘点（2026-08-02，Architect 交接前实测）

> 本节是**交接给下一任 Architect 的工作面**，数据全部实测，非估计。
> 权威的「做完」定义是[验收基线 §6 发布条件](acceptance-baseline.md)，共六条。

### 结论先行

- **产品功能 FR-01~15：15/15 实现完成。**每条功能需求都有对应命令；2026-07-28 真实产出过 3 份中文包（报告 + 简历 + 141 KB 离线看板）与 1 份英文导出。
- **自动化测试 334 条**：Python 182 + 跨引擎行为 132 + 文档检查器 20。
- **发布条件 6 条只满足 1 条。**
- 一句话：**代码基本写完了，验收这道闸门一步没走。**M1（批次 A–F）修的是评审查出的实现缺陷，功能在 M1 开始前就已造完。

### 发布条件逐条状态

| 条件 | 内容 | 状态 | 依据 |
| --- | --- | --- | --- |
| 1 | DOC-01~07 由 Owner 核对 | ❌ 未做 | `index.md` 中 13 项「待 Owner 核对」，5 项已接受。**只有 Owner 能做** |
| 2 | IMP-01~28 有可复现本地证据 | ⚠️ 5/28 | 测试文件中**零处 IMP 标注**，无法逐条指认。可追溯的 5 项来自 M1 卡面：IMP-04、IMP-13、IMP-14、IMP-22、IMP-28 |
| 3 | CodeRoute 与 SliverShield 只读验收 | ❌ 未做 | 从未执行。**且基线 §4 已过期**——点名的 `CodeRoute-t30`/`CodeRoute-t55` 工作树在本机已不存在，实际为主仓 + 一个位于 `/private/tmp/cr-t61` 的 linked worktree，「三工作树归并」场景无法按原文复现 |
| 4 | DASH-01~12 全部通过 + 视觉验收 | ⚠️ 6/12 | 见下表；视觉验收需 Owner 人工 |
| 5 | 仓库无个人数据 / 密钥 / 真实源码副本 | ✅ 满足 | 批次 A–F 每轮 diff 均核，零泄漏 |
| 6 | 安装后调用可复现同一版本报告契约 | ❌ 未做 | 从未验证 |

> 本节是 2026-08-02 交接快照。表内 CodeRoute 临时工作树与“待裁定”描述已由 2026-08-05 的 `OWN-01` 取代；当前权威状态见下方 M2 任务池与[验收基线 §4](acceptance-baseline.md)。

### DASH-01~12 逐条（6 有 / 3 部分 / 3 空白）

证据来源为 `runtime/frontend/scripts/verify.mjs`（132 断言 × Chromium + WebKit）与 Python 侧测试。

| ID | 场景 | 状态 | 证据 |
| --- | --- | --- | --- |
| DASH-01 | 断网零外部请求 | ✅ | `clean-external-requests`，2 引擎 × 9 视图 |
| DASH-02 | 双引擎零 CSP 违规 + 阳性对照 | ✅ | `clean-console-errors`/`clean-page-errors`/`csp-style-positive-control`/`csp-connect-probe`/`probe-errors-separated` |
| DASH-03 | 注入语料以文本呈现 | ✅ | Python 侧参数化用例 + GJ-09 转义集合独立锚点 |
| DASH-04 | 多宽度无横向滚动 | ✅ | `no-horizontal-overflow`，5 宽 × 9 视图 = 45 切点 |
| DASH-05 | `partial` 快照首屏降级带 | ❌ | **无任何断言** |
| DASH-06 | 两次交互内到达完整证据 | ❌ | **无任何断言** |
| DASH-07 | 打印分支 | ✅ | `print-controls-hidden`/`print-details-expanded`/`print-full-locator` |
| DASH-08 | 纯键盘全流程 | ⚠️ | 有 `coverage-scope-link-focusable`/`deferred-search-focus`/`focused-project-enter-activation`；未覆盖「切视图 → 检索 → 筛选 → 展开证据 → 复制 locator」全链 |
| DASH-09 | `forced-colors` 五组状态可辨 | ✅ | 5 条 `forced-colors-*` 状态断言 + `forced-colors-media-active` |
| DASH-10 | 双快照身份条 + 跨版本深链 | ⚠️ | `version-mismatch` 视图已覆盖；两份快照并存的身份条区分未验 |
| DASH-11 | 复习三态可区分 | ⚠️ | `review-target` 视图与 `continued`/`developing` 夹具存在，无专门断言 |
| DASH-12 | Markdown 与 HTML 逐条一致 | ❌ | **无任何断言** |

### 端到端实测（2026-08-02，当前主干）

M1 全部工作完成后**首次**真实端到端运行，用合成工作区 + 临时数据目录，未触碰 Owner 真实状态：

```text
bootstrap           -> schema_version 10
authorize           -> receipt
validate_job_input  -> validation_sha256
scan                -> status completed / fresh_projects 1 / modules 1 / indexed_files 3 / role_lens_context 存在
scan（加排除规则）  -> status completed / fresh_projects 0 / excluded_projects 1 / indexed_files 0
```

第二次运行同时验证了两件刚合入的事：**GJ-03 的项目排除真实生效**（未读源码、未建快照），以及 **GJ-12 的终态修复首次在真实运行中兑现**（全排除时终态为 `completed` 而非 `failed`，修复前该运行会被 `history.py` 的 `status IN ('completed','partial')` 整个过滤掉）。

**后半条链未验**：`prepare_start` → 上下文访谈 → `record_analysis` → `render` → 英文导出，自 2026-07-28 起未在真实环境跑过重构后的形态。它有 182 个单测与跨引擎门禁覆盖，但单测证明的是部件行为不变，端到端证明的是串起来还对——两者不等价。

### 建议的推进顺序（供下一任 Architect 参考，非指令）

1. **IMP 追溯盘点**——性价比最高。把 28 项逐条对到现有测试并标注。大概率能把条件 2 从 5/28 推到 20+/28，剩下真缺的再出卡。**这不是缺功能，是缺一层标注。**
2. **补 DASH 三条空白（05/06/12）与两条部分（08/10/11）**——条件 4 的机检部分。
3. **裁定基线 §4 怎么办**——`t30`/`t55` 已不存在，需 Owner 定：重建工作树复现原场景，还是改为描述当前实际形态。这是**权威文档变更，需 Owner 核对**。
4. **完整端到端跑通**（含后半条链）——它同时是条件 3 的前置。
5. **安装后可复现性验证**（条件 6）。
6. **Owner 核对 13 份文档**（条件 1）。

## 里程碑 M2 · 私有首版发布实证与候选发布

> 2026-08-02 由接任 Architect（Sol）建立。M2 不默认新增产品功能；先把“已有行为”与“缺少证据/缺少实现”分开。每张后续卡的精确范围以 GJ-13 证据账本为上游，不复制交接快照中的动态数量。

### 关键路径

```text
GJ-13 证据盘点
  ├─ GJ-14 合成全链路验收 ─┐
  └─ GJ-15 看板缺口闭合 ───┼─ GJ-16A 多工作树合成证据 ─ GJ-16B 真实工作区只读验收
OWN-01 CodeRoute 场景裁定 ✅ ┘                                      ├─ Owner 视觉/文档核对
                                                                     └─ GJ-17 安装副本可复现
```

GJ-14 与 GJ-15 在 GJ-13 后技术上可并行；单 Implementer 模式下一次只派一张，避免共享工作区内两个实现现场互相污染。真实工作区与安装验收发现产品缺陷时，按 L1 停止验收并另开缺陷卡，不在验收卡内顺手修。

### 批次 G · 证据先行

| 任务 | 卡面 | 状态 | 前置 | 验收强度 | 发布条件 |
| --- | --- | --- | --- | --- | --- |
| GJ-13 · 建立发布验收证据账本 | [GJ-13](../collab/tasks/GJ-13.md) | ✅ 已验收合入（信道 #43，merge `682a66d`） | M1 已合入 | 常规 | 条件 2、4 的事实基线 |
| GJ-14 · 合成工作区全链路验收 | [GJ-14](../collab/tasks/GJ-14.md) | ✅ 已验收合入（信道 #58，merge `5aa2504`） | GJ-13 | 对抗 | 条件 2；条件 3 前置 |
| GJ-15 · 看板机检缺口闭合 | [GJ-15](../collab/tasks/GJ-15.md) | ✅ 已验收合入（信道 #52，merge `319ccd6`） | GJ-13 | 对抗 | 条件 4 机检部分 |
| GJ-16A · 多工作树合成证据补齐 | [GJ-16A](../collab/tasks/GJ-16A.md) | ✅ 已验收合入（信道 #62，merge `b7be588`） | GJ-14、GJ-15、OWN-01 | 对抗 | 条件 2；条件 3 前置 |
| GJ-16B · CodeRoute/SliverShield 真实只读验收 | [GJ-16B](../collab/tasks/GJ-16B.md) | ✅ 已验收合入（终审 [#112](../collab/channel.md)） | GJ-16A、GJ-18、GJ-19、GJ-23 均已合入 | 对抗 | 条件 3 满足；Owner 视觉输入待 `OWN-03` |
| GJ-17 · 隔离安装副本与报告契约复现 | 待前置完成后出卡 | 阻塞 | GJ-16B、Owner 文档/视觉核对 | 对抗 | 条件 6；发布候选收口 |

GJ-13 验收裁决要点（信道 #43）：证据账本按上游动态提取并覆盖 28 个 `IMP` 与 12 个 `DASH`，当前结论为 `IMP = verified 1 / partial 27`、`DASH = partial 10 / missing 2`。首轮验收将缺少完整快照不可变与中断状态转换断言的 `IMP-17` 从 `verified` 退回为 `partial`，修订后独立发布门禁全绿并合入。#40 的 L2“全绿测试不等于完整发布实证”成立，缺口归宿以账本逐行为准：先由 GJ-15 闭合两个 `missing` 看板语义，再按账本最小目标规划 GJ-14，不把全部 `partial` 塞入一张卡。

GJ-15 由 Architect 在信道 #44 正式派发，随后以 #45 修订限制 parity 的可观察锚点：限定闭合 `DASH-10` 双快照/跨版本深链与 `DASH-12` Markdown/HTML 同源投影；不吸收其他 `partial` 看板项。经 #48、#50 两轮裁决收紧可见语义后，最终候选 `63592b7` 于 #52 通过对抗档验收并以 `319ccd6` 合入；账本现为 `DASH = verified 2 / partial 10 / missing 0`。下一张候选为 GJ-14，仍按账本最小目标出卡。

GJ-14 由 Architect 在信道 #54 正式派发；#55 发现 JD 持久化断言与 `EVID-E22` 冲突后，#56 裁定保留个人 SQLite 中的 Owner JD、收窄禁止泄漏面并复工。候选 `1ddaa28` 于 #58 通过对抗档验收并以 `5aa2504` 合入：真实 broker 主链、双项目批量访谈、中文不可变快照、英文失败关闭、跨任务复用和结构化复盘均由独立门禁复验。账本据此将 `IMP-15`、`IMP-19` 更新为 `verified`，其余六项只追加精确子句并保持 `partial`；当前汇总为 `IMP = verified 3 / partial 25`、`DASH = verified 2 / partial 10`。

`OWN-01` 于 2026-08-05 解除：CodeRoute 已清理的临时工作树属于过渡状态，不再作为真实验收对象；验收基线 §4 改为 Owner 指定真实工作区，只保留模块角色、计划不冒充实现、构建产物排除三项 CodeRoute 判据，SliverShield 不变。Architect 出卡前实际运行六个相关测试节点，结果 `6 passed`：现有证据已覆盖根内归并、根外两阶段授权、相同内容复用、工作树 Claim 提升门槛及冻结 branch/HEAD；尚缺同一合成场景对三工作树 branch/HEAD/dirty 与分支差异来源隔离的联合断言。因此 GJ-16 拆为 test-only 的 GJ-16A 和真实验收 GJ-16B，先闭合前者，不重建真实临时工作树。

GJ-16A 由 Architect 在信道 #60 正式派发，候选 `8f05c43` 于 #62 通过对抗档验收并以 `b7be588` 合入：真实 Git 三工作树联合证明 branch/HEAD/dirty、等价内容单次分析与全部来源、分支独有 Evidence 隔离、module/project 提升边界、精确 worktree scope 及冻结 provenance。独立聚焦节点为 `7 passed`，完整门禁为 184 个 Python 测试与 Chromium/WebKit `152/152`；账本只闭合上述子句，`IMP-03` 因全部非法 external config/candidate 组合仍缺证据而保持 `partial`。GJ-16B 前置现已满足。

GJ-16B 因 `IgnoreMatcher`/`_is_sensitive` 两项缺陷于 #73 停工，GJ-18/GJ-19 合入后经 #104 二次修订复工。复工第一次真实扫描（CodeRoute）即撞出第三个缺陷（信道 [#106](../collab/channel.md) L1）：未跟踪的 `.agent_context/.../CodeRoute.app/...` 应用打包产物内部资源被建成 `SourceRevision`/`Evidence`，违反"生成/依赖目录不得进入证据链"判据。根因是 `HARD_EXCLUDED_DIRECTORIES` 目录名精确匹配清单未覆盖应用打包产物这一类生成物格式，与 GJ-18/GJ-21 不同源但同属"排除机制是封闭枚举"这个更大模式。已按 L1 出卡 GJ-23，不合并进 GJ-16B（验收卡内不顺手修）。**GJ-23 已于 [#109](../collab/channel.md) 终审通过并合入**：新增路径段后缀匹配（`.app`/`.framework`/`.xcarchive`）与现有精确匹配并存，三个消费点（`_safe_history_path`/`_walk_directories`/`_iter_project_files`）同时接入，`.aab`/`.ipa` 顺带补入 `BINARY_ASSET_EXTENSIONS`。终审独立复现变异测试（15 红/7 绿，与报告一致）、额外做了 12 个报告未覆盖的边界探测与一次 5000 文件规模的剪枝性能验证（0.018 秒、零泄漏），零发现。**本卡首次由 Implementer（`glm-plus`）交付**，非 Sol-Impl；验收标准未因执行者不同而调整。GJ-16B 现从 CodeRoute 完整流程第三次重新开始（不复用此前已产生的 ScanRun/PreparationRun）；GJ-22（开源前脱敏审计）与本次停工无关，Implementer（Sol-Impl）全程未受影响。

**GJ-16B 第三次复工已于 [#112](../collab/channel.md) 终审通过并合入**，两个 Owner 指定真实工作区（CodeRoute、SliverShield）各自完整走完 Skill 只读流程，CodeRoute 三项、SliverShield 四项判据全部 pass，GJ-23 修复在真实工作区上验证为 0 `.app` 路径进入证据。终审独立复核不满足于交付报告数字：亲自重跑 gate-release（278 测试 + 152 浏览器核对 + mypy/ruff/doc-links/uv build 全绿，与报告逐字段一致）；直接读临时 data directory 的原始 JSON/txt（非报告转述）核对 claim/evidence/gap 计数、drift 行数、排除分类计数；自行重算全部 6 个产物文件的 SHA-256 与 manifest 比对一致；额外读 `git_metadata.py` 源码确认只读证明属"结构上不可能写"而非"这次没写"。**唯一未能坐实的一点**：runtime 的排除计数按类别汇总、不落具体路径，是否有真实 `.app` 内容被 GJ-23 机制命中无法在不违反"不碰真实工作区""不做前后基线对比"两条既有纪律的前提下证实到底，判定为当前隐私设计下的验证天花板，不计缺陷。交付人（`glm-plus`）与派卡对象（Sol-Impl）不一致且信道无交接说明，已在 #112 要求 Sol-Impl 补充；不影响本次验收结论。临时 data directory 保留至 `OWN-03` 完成本轮新产物的视觉核对。**GJ-16B 前后共停工三次**（GJ-18/GJ-19 缺陷组合、Sol 预审揪出的复工卡面设计缺陷、GJ-23 缺陷各一次），三次原因互不相同，均按「验收卡内不顺手修」原则另开卡处理，未合并掩盖。

### 批次 H · 证据完整性缺陷修复

由 GJ-16B 真实工作区验收撞出的产品缺陷，按收口原则「验收卡内不顺手修」另开。彼此**相互独立、互不兜底**，分别修复分别回归。

| 任务 | 卡面 | 状态 | 前置 | 验收强度 | 发布条件 |
| --- | --- | --- | --- | --- | --- |
| GJ-18 · IgnoreMatcher 多段路径模式失效 | [GJ-18](../collab/tasks/GJ-18.md) | ✅ 已验收合入（终审 [#89](../collab/channel.md)） | 无 | 对抗 | 条件 3 前置（GJ-16B 重跑） |
| GJ-19 · `*.env` 后缀漏判修复与判定基线锚定 | [GJ-19](../collab/tasks/GJ-19.md) | ✅ 已验收合入（终审 [#100](../collab/channel.md)） | 无 | 对抗 | 条件 3 前置（GJ-16B 重跑） |
| GJ-20 · 敏感文件排除的 `FR-15` 合规 | 待出卡（设计问题未定，见下） | 阻塞(设计问题未定,非任务依赖) | 无(GJ-19 已合入) | 对抗 | 条件 2（`FR-15` 证据） |
| GJ-21 · ignore 来源枚举不完整 | 待出卡 | 挂账 | GJ-18 | 对抗 | 条件 3 前置 |
| GJ-23 · 应用打包产物根排除 | [GJ-23](../collab/tasks/GJ-23.md) | ✅ 已验收合入（终审 [#109](../collab/channel.md)） | 无 | 对抗 | 条件 3 前置（GJ-16B 重跑） |

GJ-18 源于信道 [#69](../collab/channel.md) 与 [#73](../collab/channel.md) 裁决：`IgnoreMatcher.matches()` 缺少多段路径模式的前缀匹配规则，多段目录模式只匹配目录自身、不匹配后代；`_iter_project_files` 无目录剪枝故无兜底；该失效不触发任何 ScanIssue，违反 GJ-05 的「近似必须可见」。静默失效模式数经两轮核算：SliverShield **14 条**（复核确认无一被 `HARD_EXCLUDED_DIRECTORIES` 兜住）、CodeRoute 0 条、GoodJob 本仓 **1 条**（`prototypes/dashboard/out/`）。**出卡侧更正**：初次统计称本仓 2 条并引「`node_modules` 下 174 个文件会进证据」为例，该说法不成立——`node_modules` 在硬排除集合中，`_iter_project_files` 在目录层即不下钻，其文件永远到不了 `IgnoreMatcher`。Implementer 以信道 [#83](../collab/channel.md) L1 停工指出，并实测本仓真正泄入 `source_artifacts` 的为 **1 个文件**（`prototypes/dashboard/out/dashboard.html`）。根因是测出失效模式后未核上游是否已有兜底，把一个环节的观测当成全链路结论；GJ-18 的 D5 已按 [#85](../collab/channel.md) 裁决重写。本轮 SliverShield 后果为 5 个生成文件进入 `source_revisions`、产生 8 条 Evidence、**支撑 0 条 Claim**，故 Claim 层未污染、两份 ArtifactSnapshot 不作废。

GJ-19 源于同一轮调查的独立发现（[#73 七](../collab/channel.md)）：`_is_sensitive` 的 `.env` 未纳入既有后缀分支，`production.env` 等常见命名漏判。**卡面已更正 #71/#73 对该函数结构的描述**——四种判定形态（精确名/前缀/名字集合/后缀）本已齐备，缺口是成员覆盖而非机制缺失。该更正本身属「未验证断言当事实」，故本卡按 [protocol §2.1](../collab/protocol.md) 送 Reviewer 预审一轮后再派。

**GJ-18 已于 [#89](../collab/channel.md) 终审通过并合入**。终审在初审（[#88](../collab/channel.md)）之外另做三项独立验证：先经两次校准的 Git 原生差分探针（26 组规则 × 69 条路径）得 **0 条静默差异**，坐实契约 2；取 `bbec1b1` 旧实现同矩阵对拍得 **12 处行为变更全部向 Git 收敛、0 处偏离**，其中 4 处为收窄，实测本仓证据集**新增 0 条、剔除 1 条**；D5 以同 HEAD 仅变异代码重做，`out 1→0`、`node_modules 0→0`、`hard_excluded 14→14` 逐项复现。三条实施发现均定性为 L3：**①** 交付方的 D5 前后两次扫描 `head_commit` 分别为 `18edb17` 与 `bbec1b1`，语料未受控（Reviewer 读同两份现场故继承同一盲点），结论经终审同 HEAD 重做后确认成立——**属出卡侧缺陷**，已追加[出卡门禁](../collab/architect.md) §1.3 第 11 条与[反模式池](../collab/anti-patterns.md)条目；**②** 行为收窄面在交付报告中未穷举，记录不打回；**③** 见下 GJ-21。

**GJ-21（挂账，源于 GJ-18 终审）**：运行时只读取遍历中发现的 `.gitignore`，`.git/info/exclude`（`.git` 在硬排除内故永不可达）与 `core.excludesFile` 全局忽略**从不读取且无任何披露**——与 GJ-18 同为「既不生效也不披露」，但属**忽略来源**而非**模式语法**。GJ-18 契约 1 把枚举范围界定在 `gitignore(5)` PATTERN FORMAT，**该边界是出卡侧划的**，不计交付缺陷。出卡前须先裁定：读取全局/仓库级 exclude 是否越出既有文件系统边界（protocol §8），若不读则披露形态为何。

GJ-19 经 Reviewer 预审（信道 [#81](../collab/channel.md)）**四项发现全部成立，卡面已大幅收窄**，裁决见 [#84](../collab/channel.md)。删除的三条契约各自的病因：「成员来源」把"生态惯例"列为可接受来源，而它没有发布者、定位与版本，与同一契约禁止的"凭经验补几个"无法机械区分；「排除可见」与权威 `FR-15` 冲突，且按 `FR-15` 实现必然超出卡面允许范围，按计数实现则在生产代码零改动下即可通过——是**出卡侧造出的假绿出口**；「假阴性优先」对一个只接收 filename 的纯函数给了判定方向却没有判定域，实为把安全分类权下放给 Implementer。收窄后本卡只做已证实的 `.env` 后缀成员，另加独立 oracle 与四项变异门槛。

**GJ-19 已于 [#100](../collab/channel.md) 终审通过并合入**。初审（[#98](../collab/channel.md)）之外终审独立重做了全部五步，未采信任何一个报告数字：`_is_sensitive` 基线源码逐字重新转录核对 25 项 oracle（精确名 1、名字集合 20、后缀代表 4）零遗漏零多余；独立复现 D3.3（移除 `lower()`）与 D4（插入 `startswith("secret")`）两项变异，失败用例集合与交付报告逐字一致；独立构造含背景事故文件名 `flutter_native_integration.env` 在内的 12 个真实路径直接调用生产函数验证。三项契约（oracle 独立冻结、四类规则各自大小写变体、阴性样本覆盖）均实测成立，无实施发现。

**GJ-20 待出卡，三个设计问题未定，定完再写卡**（仓促出卡正是本轮 GJ-19 四项缺陷的共同来源）：

1. **`FR-15` 六类的实际合规面**——`FR-15` 列举权限不足、仓库损坏、无法识别项目、语言不支持、敏感文件排除、单个模块读取失败共六类，出卡前须**逐条核对现码是否已产生合规 ScanIssue**，不得只按本轮撞见的"敏感文件排除"一类出卡（门禁 9）；
2. **路径披露粒度**——敏感文件的路径本身可能即是敏感信息（如 `deploy/prod-aws-root.key`）。`FR-15` 要求"路径/范围"，须裁定何时给完整路径、何时只给范围，并与发布条件 5 对齐；
3. **消费点范围**——`_is_sensitive` 有两个消费方向（以 `grep -rn "_is_sensitive" runtime/src/goodjob/` 与 `grep -rn "_safe_history_path" runtime/src/goodjob/` 的实际输出为准，不写行号字面量，理由见 [architect.md 门禁 8](../collab/architect.md)）：工作树索引过滤与 `_safe_history_path` 注入的历史路径过滤。后者过滤 Git 历史路径，逐条产生 ScanIssue 可能产生数量级噪声；须裁定是否纳入、以及如何聚合。

**（历史记录，方案已废弃）** GJ-16B 复工筹备阶段最初计划把 2026-08-06 旧现场（`~/.codex/goodjob-career-review/acceptance/GJ-16B-2026-08-06/`，信道 #74）作为修复后前后对比基准。Sol 预审（[#103](../collab/channel.md)）指出该现场的 `manifest.json` 未记录 CodeRoute/SliverShield 自身的 canonical root/branch/HEAD，`git status --porcelain` 的 untracked 行也不含内容哈希，语料不可控——按 [architect.md 门禁 11](../collab/architect.md) 做不到语料受控就不能声称对比结论。裁决（[#104](../collab/channel.md)）采纳并整张放弃"前后对比"设计，复工卡面改为每次扫描独立判定 pass/fail、不新建也不另存任何供未来复跑使用的基准。**GJ-16B 最终于 [#112](../collab/channel.md) 按此设计终审通过**，详见上方批次 G 段落。

### Owner 决策与人工门

| 门 | 状态 | 说明 |
| --- | --- | --- |
| OWN-01 · CodeRoute worktree 验收场景 | ✅ Owner 已裁定（2026-08-05） | 已清理的临时工作树不再作为真实验收对象；相关能力由合成测试承担，缺口先由 GJ-16A 补齐；§4 只写 Owner 指定真实工作区，SliverShield 不变 |
| OWN-02 · DOC-01~07 权威文档核对 | 待 M2 行为缺口稳定后 | 只由 Owner 完成；若 GJ-14/GJ-15 揭示契约需改，先修契约再核对，避免重复验收 |
| OWN-03 · 离线 HTML 视觉验收 | 🟡 部分关闭（2026-08-06，信道 #71 六） | Owner 已打开 GJ-16B 两份产物目视复核并在断网状态下复开，`DASH-01` 实测通过，发布条件 4 的「视觉验收」分句关闭；`DASH-05`/`DASH-06` 由 Owner 决定本轮按通过计，**账本记 `owner_waived`，不得升为 `verified`**，两项自动化缺口维持挂账，条件 4 的账本分句仍未满足 |
| OWN-04 · 只读验收判据修订 | ✅ Owner 已裁定（2026-08-06） | 原判据「前后 branch/HEAD/status digest/计数逐项相同」所证为「无人写入」，强于所需证明的「GoodJob 未写入」，在 Owner 活跃使用的仓库上不可达——而活跃仓库正是本产品目标场景。Owner 裁定拆为三条并落入[验收基线 §4「只读证明」](acceptance-baseline.md)：(a) GoodJob 未写入（Git 写命令、写模式描述符、`.git` inode/mtime 三项**机器可验**，不接受声称）；(b) 分析基线自洽（HEAD 与深读文件哈希全程不变）；(c) 外部漂移按是否进入分析分类——影响分析基线的必须为空且点名路径，未进入分析的只记计数与状态类别，全量清单落仓库外。**出卡侧更正**：该判据原先只存在于 GJ-16B 卡面，`acceptance-baseline.md` 中并无，故本次不是修改既有权威条款而是首次补入；「Architect 不单方修改」的先前描述不准确。GJ-16B 卡面已改为指向 §4 不复述，`blocked-on-criterion` 状态解除 |

### 收口原则

- GJ-13 只盘点，不通过“补标签”制造假绿；完整场景没被证明就不能记 `verified`。
- GJ-14/GJ-15 的范围从证据账本生成；不得把所有缺口塞进一张万能卡。
- GJ-17 只在一次性临时 Codex home 验证安装与显式调用，不提前更新 Owner 当前用户级 Skill；最终验收通过后再由 Owner 决定是否安装候选版本。
- 条件 5（仓库无个人数据/密钥/真实源码副本）在每卡验收持续检查，并在最终候选 HEAD 由 Architect 独立复核。
- GJ-10 与 CI 接入不在发布关键路径，Owner 未裁定前继续留在机动池。

### 机动池（未出卡）

| 任务 | 说明 | 触发条件 |
| --- | --- | --- |
| GJ-10 · 剥离遍历与 ignore 簇 | GJ-06 的第二步。**GJ-06 合入后实测**：`WorkspaceScanner` 已降至 2509 行 / 52 方法，本簇为 `_discover`、`_walk_directories`、`_non_git_manifest`、`_iter_project_files`、`_is_sensitive`、`_classify` 共 6 方法 459 行，加 `IgnoreMatcher` 142 行，合计约 601 行。该簇管的是「扫描器能看到什么」——硬安全排除清单与敏感文件判定在其中。**GJ-06 已于 `ed53ff2` 合入，前置满足**。出卡时须在 GJ-06 的 DoD 基础上补一条「对外暴露名字的可写性对等」——见 GJ-06 验收的方法论盲区 | 待 Owner 决定 |
| CI 接入 | 仓库无任何 CI 配置，门禁全靠本地。GJ-07 明确把此项排除在外，是独立决策 | Owner 决定 |

## 开源准备（独立于发布条件 6 条，仓库可见性变更前置）

与[验收基线 §6 发布条件](../40-delivery/acceptance-baseline.md)是两件事：那 6 条门的是"能否创建/更新可安装私有版本"，这里问的是"仓库本身能否从 private 改为 public"。唯一交集是条件 5（仓库无个人数据/扫描缓存/密钥/真实项目源码副本）。

| 任务 | 卡面 | 状态 | 前置 | 验收强度 |
| --- | --- | --- | --- | --- |
| GJ-22 · 开源前脱敏审计 | [GJ-22](../collab/tasks/GJ-22.md) | ✅ 已验收合入（终审 [#123](../collab/channel.md)） | 无 | 对抗 |

Architect 已做一轮机械扫描（全历史 `git rev-list --all`，非仅当前 `main`）：私钥头/AWS 与 GitHub token 格式/通用 `secret=` 赋值模式均无命中；全历史 114 个曾出现过的文件名中无 `.env`/`credentials`/`id_rsa`/`*.pem`/`*.sqlite3` 等敏感文件；全部 8 条非 `main` 分支相对 `main` 的 diff 为空（早已合并，无孤立内容）；`prototypes/dashboard/fixture/report-bundle.json` 为合成测试数据；`.gitignore` 覆盖常规缓存/构建目录。**确认发现一项**：全部 120 次提交的 author/committer 均为 `lc.jin <lc.jin@invo.cn>`，公开后逐条提交、GitHub 贡献者信息均会暴露这一真实身份标识。**出卡侧疏漏**：披露这一发现时，Architect 把该身份原文写进了本节与 [#105](../collab/channel.md) 派卡消息正文，本身即违反 GJ-22 自己的"零原文"契约。

**GJ-22 于 [#114](../collab/channel.md) 触发 L1**：Sol-Impl 独立复核维度 2，142/142 commit author/committer 同一真实身份，且同一身份已被既有派卡消息原文写入仓库正文（`channel.md:3406`，即上一段落的出卡侧疏漏）——按契约 3 判红，立即停工上报，未自行处置。**Owner 已在对话中直接裁定：该身份同意作为本项目公开署名**（仓库当前为 private，最后一次 push 于 2026-08-02，此前未曾公开暴露）。裁决落入 [#115](../collab/channel.md)：GJ-22 契约 3 新增附则，该身份改判⚪已核查确认（例外仅适用于这一个身份，其余 PII 仍按原判据走 🔴），GJ-22 复工，从维度 2 之后继续。

**GJ-22 复工后于 [#116](../collab/channel.md) 触发第二次 L1**：Sol-Impl 通读维度 4 过程中发现仓库正文（README、`protocol.md`、`implementer-bootstrap.md`、任务卡、`channel.md`、`real-workspace-acceptance.md` 共 7 个文件、15 行、19 处实例）保存了真实 macOS 绝对路径，路径中的本机用户目录段是与 `lc.jin` 不同的另一条身份线索；全历史 8 个可达 commit，最早 2026-07-24。Architect 独立复核（重新扫描 + `git log --all -S`）与 Sol-Impl 数字完全一致。裁决前 Owner 先提出是否应把 `docs/collab/` 整体移出公开仓库历史，与 Architect 讨论确认 GitHub 可见性是仓库级、"同仓库建干净分支"无法真正隔离完整历史后，**放弃仓库结构重组**。落入 [#117](../collab/channel.md) 的最终裁决：`docs/collab/` 内的 8 处实例保留不处理（协作过程记录的一部分）；`README.md`/`real-workspace-acceptance.md` 的 7 处实例判定为面向公开访客的内容，单独替换为占位符 `/Users/<owner>/...`（commit `d72752f`）。GJ-22 契约 3 新增第二条附则固化这一裁定，GJ-22 复工，从维度 4 继续。

**GJ-22 复工后于 [#118](../collab/channel.md) 触发第三次 L1**：Sol-Impl 补查全部 ref 历史 blob，发现同一条本机路径线索还出现在 `docs/40-delivery/acceptance-baseline.md` 的历史版本（commit `25b7b93b` 引入、`4c61aebee` 移除，当前工作树零命中，148 个可达 commit 中 91 个历史快照包含）；该文件不在 [#117](../collab/channel.md) 精确列举的白名单内，Sol-Impl 未自行并入，按矩停工上报。Architect 独立复核（`-S` 定点搜索 + 148 个 commit 逐一 `git show | grep`）数字完全一致。因当前文件已干净，唯一能消除该历史实例的手段是历史改写——而 Owner 已在 #117 明确放弃任何形式的历史改写；[#119](../collab/channel.md) 裁决：该历史实例援引 #117 同一理由，保留不处理，纳入契约 3 附则二白名单。GJ-22 复工，从维度 4 继续（第三次，当前已知范围内最后一次因本机路径线索停工）。

**GJ-22 于 [#120](../collab/channel.md) 交付**：新增 [open-source-desensitization-audit.md](open-source-desensitization-audit.md)，固定 `audit_subject_head=5af1413`（149 个可达 commit、118 个历史路径、476 个唯一 blob），六维结论为密钥/凭据⚪、PII⚪（例外范围严格等于 #115/#117/#119 精确裁定）、真实扫描内容⚪、叙事文档⚪（7,153 行人工通读）、全部 branch/ref⚪、`.gitignore` 前瞻性防护🟡。总体建议**有条件建议开源**。Reviewer（本轮由 glm-plus 代行）于 [#122](../collab/channel.md) 完成对抗档初审，独立复核 D1-D6 与六维度数字全部通过，提出两项不阻塞的透明度/精度改进建议。Architect 于 [#123](../collab/channel.md) 终审：独立重验 commit 计数、报告零原文自证、`.gitignore` 判定与契约 4（本卡零 runtime/schema/前端改动）后 PASS，两项 Reviewer 建议留待报告下次因"最终候选 SHA 重跑"而重新生成时一并纳入，不为此单开修订轮。**GJ-22 到此验收合入**；是否切换仓库为 public、是否执行报告 §5 的三项前置条件（补窄 `.gitignore`、确认 data directory 在仓库外、以最终候选 SHA 重跑），留待 Owner 后续决定，需要时另立任务卡。

## 已完成

| 任务 | 结论 | 信道 |
| --- | --- | --- |
| 看板呈现契约与 ADR-0008 | [dashboard-design.md](../20-architecture/dashboard-design.md)、[ADR-0008](../30-decisions/adrs/ADR-0008-single-file-dashboard-and-structured-token-embedding.md) 已接受 | 部署前 |
| 首版实现评审与文档回写 | `D-043`/`D-044`/`EVID-INV-27`/`DASH-INV-11`/`F-009` 已落契约；`D-014`/`D-034`/`D-035` 已收窄 | 部署前 |
| 批次 A · GJ-01 + GJ-02 | 渲染门禁与归因校验改为按结构分层判定；merge `0e75a78` | #4 交付 / #5 验收 / #6 收口 |
| 批次 B · GJ-08 + GJ-04 + GJ-03 + GJ-05 | 数据区运行期守卫与归因双投影、看板跨引擎行为门禁、项目级排除与 `excluded` 生产者、ignore 子集显式化；merge `711cb33` | #7 派卡 / #9 L1 / #10 裁决 / #11 交付 / #12 验收收口 |
| 批次 C · GJ-09 | 转义集合内容获得独立于该集合的测试锚点，批次 B 验收发现的覆盖空洞闭合；merge `9f06b8f` | #13 交付 / #14 验收收口 |
