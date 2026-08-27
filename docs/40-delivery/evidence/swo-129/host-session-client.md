## SWO-129 官方 Host 会话客户端取证记录

在 `goodjob.session_client.SessionClient` 中提供了官方、轻量、任务生命周期绑定的 host session client，并提供参考集成脚本 `scripts/host_session_example.py`。

### 设计与安全保障

- **单任务生命周期**：封装 `launch_broker.py` 子进程 stdin/stdout 协议交互，严格遵守 task-scoped 契约。启动后先执行版本化 handshake；正常路径发送 `session_complete`，取消发送 `session_cancel`，退出、异常、协议损坏或超时都会回收 broker 进程树，严禁 daemon、FIFO、磁盘邮箱与 PTY。
- **严格预检封装**：`run_preflight()` 以严格 UTF-8 解码 stdout/stderr，再把 stdout、stderr 和退出码交给 `classify_launcher_preflight` / `apply_launcher_preflight_decision`；不合规 envelope、额外 stderr、错误退出码或非法字节均不能启动 broker。预检超时会终止并回收其进程树。
- **显式授权与回执流转**：`authorize_source_analysis` 的 `confirmed` 为必填参数；参考 host 在启动 broker 前展示处理类别、本地持久化边界和模型处理边界，再接收真实 Owner 输入，不绕过任何 Capability / Receipt 校验。
- **有界 I/O 与异常分级**：所有 broker 往返受 `timeout_seconds` 约束；stdout JSONL 单行和待消费响应队列均有上限，stderr 只保留有界尾部并持续排水，避免 PIPE 互锁或内存无界增长。POSIX 以独立会话、Windows 以独立进程组启动，在超时、协议错误和异常关闭时回收完整进程树。明确区分 `SessionPreflightError`、`BrokerProcessError`、`BrokerTimeoutError` 与 `BrokerProtocolError`。

### 可重复验证

在 runtime 目录执行：

```text
uv run pytest -q tests/test_session_client.py
uv run python scripts/host_session_example.py --workspace ../../../
```
