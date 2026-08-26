# ADR-0013：平台无关的 launcher 预检报告
> 状态：已接受（2026-08-18）
> 权威范围：broker 启动前的平台无关诊断协议、输出通道、退出码、Windows 兼容边界与预检副作用
> 上游：[产品需求](../../10-product/product-requirements.md)（FR-17、NFR-10）、[ADR-0010](ADR-0010-host-agent-neutral-session.md)、[ADR-0011](ADR-0011-native-windows-security-contract.md)
> 下游：[系统设计](../../20-architecture/system-design.md)、[验收基线](../../40-delivery/acceptance-baseline.md)（IMP-30、IMP-31）
`launch_broker.py` 原先只有 Windows 私有的结构化 prerequisite 报告；其他平台的缺依赖与启动失败是自由文本。Skill 因而必须猜测操作系统和输出形态，无法在读取工作区源码、创建个人状态或启动 broker 之前，使用一条稳定路径解释依赖、权限与能力缺口。

## 决策

1. `runtime/scripts/launch_broker.py --preflight-only` 是所有平台唯一的新公开预检入口。它与 `--windows-preflight-only` 互斥；后者只作为兼容入口保留，不写入 Skill 流程。
2. 新入口只输出封闭的 `launcher-preflight-v1` JSON：顶层字段固定为 `contract_version/status/can_start_broker/platform/launcher_kind/checks/notices`。平台、launcher、check、失败码、remediation 及 notice 的合法组合与顺序由运行时注册表定义；producer 与严格 parser 读取同一注册表。
3. `passed` check 只能有 `id/status/message`；`failed` check 还必须有 `code/remediation`。Remediation 固定包含 `action/purpose/requires_explicit_consent`，仅注册表声明时允许 `source_url`。未知字段、未知组合、重复 check/notice、缺 check、顺序变化及跨字段矛盾全部拒绝。
4. 预检 ready 时 stdout 恰有一份 v1、stderr 为空、退出 `0`；not-ready 或可结构化的协议失败时仍由 stdout 输出 accepted v1、stderr 为空、退出 `2`。参数语法错误仍由 argparse 返回 `2`，不伪装成 v1。
5. 普通启动成功不输出 launcher 报告并透传 broker 业务退出码。broker 建立前的合法失败只在 stderr 输出一份 accepted v1 并返回 `2`；stdout 保持为空。
6. 预检不得启动 broker、读取工作区源码、创建数据目录或执行业务写入，不得联网、安装、提权或修改系统策略。被中断或超时的 Windows prerequisite 子进程必须先终止并 wait，不能遗留常驻进程。
7. Windows 新报告必须包装同一次旧 prerequisite 结果，逐项保留 checks、notices、message 与 remediation，不另做第二次事实判断。普通 Windows 启动仍在 launcher 运行旧 prerequisite，并由 session 做第二次安全检查。
8. `windows-bootstrap-report-v1` 与 `windows-prerequisite-preflight-v1` 的版本、内容、顺序、输出通道和 `0/2` 语义保持不变。旧 parser 只新增封闭字段校验；历史上接受未知字段不属于兼容承诺。
9. Windows 普通启动自本 ADR 接受起立即改用 `launcher-preflight-v1`；`--windows-preflight-only` 的兼容窗口覆盖 `launcher-preflight-v1` 的完整生命周期。旧入口最早只能随后继协议版本退出，且退出前必须由独立 accepted ADR 记录迁移方案、证明仓库与受支持 host 已无旧入口消费者，并移除对应兼容回归门禁；任一条件不满足时不得删除旧入口。

## 后果与验证

Skill 只理解一份跨平台协议；修复动作在 Owner 同意安装或提权前接受机器校验。协议演进必须更新注册表、producer/parser 对抗测试与后继决策。Windows 保留两个入口，但事实只生产一次。mutation 与进程测试覆盖封闭 schema、跨字段等价式、输出/退出码及零副作用；Windows 验证新包装无损等值与旧 flag 字节输出。同一冻结 commit 先通过 macOS 门禁，再由独立环境复核 Ubuntu 与原生 Windows。
