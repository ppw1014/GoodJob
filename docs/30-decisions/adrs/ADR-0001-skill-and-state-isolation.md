# ADR-0001：Skill 版本资产与个人状态分离

> 状态：已接受（平台限定部分由 [ADR-0009](ADR-0009-cross-platform-runtime-security.md) 替代；Codex-only 入口与数据目录绑定部分已由已接受的 [ADR-0010](ADR-0010-host-agent-neutral-session.md) 替代）
> 日期：2026-07-24  
> 权威范围：Skill 的产品形态、源码位置、安装位置与个人数据位置  
> 上游：[产品目标](../../00-product/vision-and-goals.md)  
> 下游：[系统设计](../../20-architecture/system-design.md)

## 背景

GoodJob 需要被 Codex 在不同工作区显式调用，同时持续积累扫描索引、访谈信息、简历草稿和复习记录。Skill 安装或升级会替换版本化文件，持续增长的个人状态不能和安装包同生命周期。

## 决策

1. GoodJob 采用一个跨工作区个人 Skill，而不是纯提示词或独立桌面程序。
2. 开发期源码位于 GoodJob 仓库；可发现入口位于仓库的 .agents/skills/goodjob-career-review。
3. GoodJob 的权威文档与后续源码保存在私有 GitHub；只有实现与发布验收通过后，才从可安装私有版本部署到用户级 Skill 目录。当前文档仓库不等于已发布 Skill。
4. Skill 包仅包含 SKILL.md、agents/openai.yaml、确定性脚本、参考框架与前端静态资源。
5. 工作区注册、SQLite、访谈回答、复习状态和输出快照统一放在 ~/.codex/goodjob-career-review；允许通过显式 data-dir 覆盖。
6. 个人数据目录永不进入 Skill 包或 GitHub；首版不自动删除其中的历史，只报告分类存储用量。

## 影响

- Skill 可安全升级、重装和回滚，不会覆盖个人知识库。
- 开发源码和运行状态可分别备份。
- 后续公开发布时不需要迁移个人数据，只需重新审查安装包。

## 否决方案

- 全部放进安装后的 Skill：升级和重装会覆盖状态，且增加误提交风险。
- 全部放进 GoodJob 仓库 data 目录：容易把个人或项目敏感信息带入 Git。
- 只保存 Markdown：无法可靠支持增量索引、跨项目查询和复习状态。

## 验证

- 删除并重新安装 Skill 后，外部数据目录仍完整可读。
- 仓库扫描不得发现 SQLite、访谈回答或生成简历。
- 运行时所有可变写入都落到数据目录，而不是 Skill 目录。
