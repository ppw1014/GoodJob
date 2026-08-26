## WFP 真机复核报告

### 环境

- Windows 10 Pro 25H2, build 26200.8875, NTFS
- Python 3.11.9 x64
- BFE、RpcSs、mpssvc：Running / Automatic
- IPv4 外网：TCP 和 UDP/DNS 均可用
- IPv6 外网：没有默认路由；改用 IPv6 回环的独立阳性对照

### 旧 spike 判定无效的原因

原 `spike1_wfp.py` 没有遵循 `FwpmEngineOpen0` 的 ABI：该 API 要求五个参数
`serverName, authnService, authIdentity, session, engineHandle`，原脚本仅传四个，且把
`None` 传给 `authnService`。原 `FWPM_SESSION0` 结构也缺少 `sid`、`username`、
`kernelMode` 字段；多处 ALE layer GUID 以及 `FWP_ACTION_BLOCK` 的 terminating flag
也不正确。因此 `ERROR_NOT_SUPPORTED (50)` 不能归因于 WFP/RPC/Insider build。

### 正确复测结果

1. `FwpmEngineOpen0`
   - 使用完整 `FWPM_SESSION0`（72 bytes）、正确五参数和 `RPC_C_AUTHN_WINNT`。
   - 非管理员 token 也可成功打开并关闭动态会话，证实 BFE/RPC 基础链路正常。
2. 非管理员 fail-closed
   - `FwpmSubLayerAdd0` 返回 `ERROR_ACCESS_DENIED (5)`，不会静默退化。
3. 管理员的应用范围 IPv4 阻断
   - 取得 `python.exe` application ID，成功安装且逐条读回 4 条动态 ALE filter：
     `AUTH_CONNECT_V4/V6`、`AUTH_RECV_ACCEPT_V4/V6`。
   - 激活时：IPv4 TCP 返回 `WinError 10013`；UDP/DNS 超时。
   - 关闭动态会话后：4 个 filter id 全部返回 `FWP_E_FILTER_NOT_FOUND`；IPv4 TCP 和 UDP/DNS 恢复。
4. 管理员的 IPv6 断言
   - 未过滤时 IPv6 回环 TCP、UDP 均成功。
   - 安装应用范围的 `AUTH_CONNECT_V6` 和 `AUTH_RECV_ACCEPT_V6` 后，TCP 返回
     `WinError 10013`、UDP 超时。
   - 关闭动态会话后两条 filter id 均为 `FWP_E_FILTER_NOT_FOUND`，IPv6 回环 TCP、UDP 恢复。

### 结论与限制

- WFP 动态会话、应用范围 ALE 阻断及 BFE 自动清理在本机通过，旧的“WFP 不可用”结论应撤回。
- 未覆盖真实 IPv6 外网 egress：该测试机无 IPv6 路由，不可将环境自身失败记为 WFP 负测。应在具备 IPv6 连通的正式发行版 Windows 11 24H2 或 Windows 10 22H2 上执行同一脚本回归。
- 当前验证的 application ID 是 `python.exe`，用来证明 WFP scope；实现阶段必须对规范化后的 `git.exe` 调用 `FwpmGetAppIdFromFileName0`，并复用相同动态会话生命周期。

### 附件

- `wfp_open_retest.py`: 只验证动态会话的 ABI 正确性。
- `wfp_filter_spike.py` 和 `wfp_network_target.py`: 应用范围 IPv4/IPv6 外网验证。
- `wfp_v6_loopback_spike.py` 和 `wfp_loopback_v6_target.py`: IPv6 无外网时的本地阳性/负向对照。
