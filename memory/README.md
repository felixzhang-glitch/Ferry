# memory/ — 长期记忆目录

Ferry 的长期记忆存放处，每个类别一个 markdown 文件（basic / health / preference / work / finance / recent），
由 `MEMORY_CATEGORIES` 配置决定。

- **本目录只有此 README 入主仓**：类别文件包含隐私，被 `.gitignore` 排除，永不推送
- 类别文件无需手建：服务启动时 `ensure_workspace()`（`lib/python/app/memory.py`）自动生成骨架
- 本目录受**无 remote 的本地快照仓**保护（git dir 在 `runtime/memory-git`），每轮对话自动快照，
  可用 `git --git-dir=runtime/memory-git --work-tree=memory log / diff / revert` 审查回滚
- 写入仅在用户明确要求时由 agent 执行，规范见 `skills/memory/SKILL.md`

完整设计文档：[docs/memory.md](../docs/memory.md)
