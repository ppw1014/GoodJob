## SWO-128 文件系统能力探测证据

`goodjob.platform.filesystem_probe` 只读取一个工作区路径对应的系统文件系统能力信息，不枚举目录、不打开源码、不执行工作区内容。macOS 使用 `statfs` 的 filesystem type 与 `MNT_LOCAL`；Linux 保留 bwrap 作为实际访问隔离边界，并用本地路径探测维持既有行为。

### 注入矩阵

| 注入结果 | launcher check | scanner 入口 | 处理 |
| --- | --- | --- | --- |
| `apfs`, local | `workspace_filesystem: passed` | 继续 discovery | 支持本地扫描 |
| `sshfs`/macFUSE/FUSE/NFS/SMB，或 `is_local=false` | `workspace_filesystem: failed`、`unsupported_capability` | `workspace_filesystem_unsupported` error，终态 `failed` | 提示选择可验证的本地工作区 |
| 未知 filesystem type | failed、`unsupported_capability` | 同上 | 保守拒绝，保留 type/flags 诊断 |
| statfs 探测异常 | failed、`unsupported_capability` | 同上 | 不暴露底层异常文本，不继续访问 Git |

### 可重复验证

在 runtime 目录执行：

```text
uv run pytest -q tests/test_launcher_preflight.py -k 'filesystem_probe or preflight_consumes_workspace'
uv run pytest -q tests/test_scanner.py -k 'direct_scanner_fails_before_discovery'
```

这些用例通过注入结果覆盖本地、网络/FUSE、未知和失败分支，并验证 unsupported 发生在 discovery/Git 之前。测试不需要真实网络挂载，也不会尝试 SSH、远程 Git 或自动复制。
