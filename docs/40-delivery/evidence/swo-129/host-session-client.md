## SWO-129 官方 Host 会话客户端取证记录

在 `goodjob.session_client.SessionClient` 中提供了官方、轻量、任务生命周期绑定的 host session client，并提供参考集成脚本 `scripts/host_session_example.py`。

### 设计与安全保障

- **单任务生命周期**：封装 `launch_broker.py` 子进程 stdin/stdout 协议交互，严格遵守 task-scoped 契约。支持上下文管理器（`with SessionClient(...): `），退出或发生异常时自动完整回收子进程，严禁 daemon、FIFO、磁盘邮箱与 PTY。
- **预检封装**：提供 `run_preflight()` 接口，在启动 broker 之前即可获得结构化跨平台预检结论（`LauncherPreflightReport`）。
- **显式授权与回执流转**：通过标准接口注入 Owner 确认、回执提取与 JobInput 校验参数，不绕过任何 Capability / Receipt 校验。
- **异常分级处理**：明确区分 `SessionPreflightError`、`BrokerProcessError` 与 `BrokerProtocolError`。

### 可重复验证

在 runtime 目录执行：

```text
uv run pytest -q tests/test_session_client.py
uv run python scripts/host_session_example.py --workspace ../../../
```
