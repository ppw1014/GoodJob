## SWO-131 Git 失败诊断矩阵

本夹具记录的是诊断边界，不改变 macOS Git 候选顺序。生产 resolver 仍按平台信任路径选择 Xcode Git、Command Line Tools Git、`/usr/bin/git` 和 `/bin/git`；只有在可重复证明“Xcode 失败而 CLT 成功”后，才允许调整顺序。

### 可重复命令

在 runtime 目录执行下面的命令即可重跑稳定分类夹具。测试不会执行工作区脚本、读取 hooks 或访问工作区外路径：

```text
uv run --isolated --no-project --no-config --offline --no-python-downloads --python 3.12 \
  pytest -q tests/test_platform.py -k git_failure_diagnosis
```

夹具把每个命令的退出码和 stderr 作为不可信输入，验证分类器只保留稳定的诊断 kind、message 和 remediation，不把原始 stderr 写入扫描结果。

### 诊断矩阵

| 样本 | 代表性信号 | 稳定 kind | 结论 |
| --- | --- | --- | --- |
| Git/Xcode 工具链不可用 | `xcrun: error:`、无法执行 Git，或不含权限拒绝的 `gitconfig` 读取失败 | `git_executable_unusable` | 先修复/选择可信 Git 工具链，不扩大工作区授权 |
| `.git/config`、`gitconfig` 或对象关系越界 | `Operation not permitted` / `Permission denied`（优先于路径名和退出码） | `git_repository_boundary_violation` | 只修复仓库元数据边界，不能由错误自动扩权 |
| 普通仓库损坏或不是仓库 | 其他非零退出 | `broken_repository` | 修复仓库元数据后 refresh |
| `/usr/bin/git` shim | `xcrun: error:` 或退出码 126/127 | `git_executable_unusable` | 记录 shim/toolchain 故障，不误报为仓库边界 |

Git 子进程统一传入 `LC_ALL=C`、`LANG=C` 和 `LANGUAGE=C`，避免宿主 locale 改变上述可识别信号；未识别的输出保守归为 `broken_repository`，且不会保存原始 stderr。

### 当前裁决

已证实的回归只覆盖稳定分类和现有 resolver 的不变性。父卡面记录的正式 Seatbelt 矩阵显示：普通仓库下 Xcode Git 与 CLT Git 均可完成 `rev-parse`；显式禁止各自前缀的系统配置文件也未形成“Xcode 失败、CLT 成功”的证据；授权根外的 `.git/config` 外链则由两者共同触发边界失败。因此本卡不提交“CLT 优先”的生产改动。

真实 macOS 主机仍需由评审者按同一命令、同一 Seatbelt profile 补录 Xcode/CLT/`/usr/bin/git` 的实际路径、`xcode-select` 状态、退出码和 stderr。该环境差异不能由本地 Linux 测试推断。
