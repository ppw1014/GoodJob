## SWO-130 C/C++ 源码适配器取证记录

`goodjob.adapters._cpp_facts` 为 C/C++ 仓库提供有界、无代码执行的 v1 源码适配器。

### 覆盖与能力

- **扩展名与构建配置**：`.c`、`.cc`、`.cpp`、`.cxx`、`.h`、`.hh`、`.hpp`、`.hxx` 与 `CMakeLists.txt`。
- **事实提取**：
  - `#include <...>` 产生 `technology_usage`
  - `#include "..."` 产生 `module_dependency`
  - 类、结构体、枚举、函数定义产生 `symbol_definition`
  - `main()` 入口点产生 `entry_point`
  - 线程、网络、进程等系统调用产生 `capability_boundary`
  - `CMakeLists.txt` 声明产生 `dependency_declaration`、`entry_configuration`、`module_boundary`
- **安全与边界防护**：
  - 字符串字面量、行/块注释和完整预处理器指令（包括反斜杠续行）预先清洗，避免宏、字符串伪造符号
  - CMake 的行注释和 bracket 注释会在匹配 `find_package`、target 声明前清洗，注释文本不产生构建事实
  - 括号平衡校验；畸形源码产生 `source_parse_failed` 诊断，不抛异常
  - 单文件事实截断保护（`MAX_FACTS_PER_FILE`）
  - 不运行编译器、预处理器、构建脚本或仓库二进制

### 可重复验证

在 runtime 目录执行：

```text
uv run pytest -q tests/test_adapters.py -k 'cpp'
uv run pytest -q tests/test_scanner.py -k 'test_scan_indexes_cpp_sources_and_cmake_manifest_with_cpp_v1'
```
