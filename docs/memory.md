# 长期记忆

## 三层记忆载体

codeClaw 的"记忆"分三层，职责与可写方严格分离：

| 载体 | 内容 | 谁可写 | 注入方式 |
|------|------|--------|----------|
| `rules/AGENTS.md` | 人格、风格、时间感知、工具偏好 | 仅人工 | pi `--append-system-prompt` |
| `rules/admin.md` | 权威静态事实（身份、权限、邮箱、微信 user_id、运行环境） | 仅人工 | pi `--append-system-prompt` |
| `memory/*.md` | 动态事实（基础档案、健康、偏好、工作、投资、近况） | agent（用户明确要求时）+ 人工 | 生成的记忆块，常驻注入 |

分层的意义：agent 只能写 `memory/`，改不到人格与权威设定；会变的事实只存在 `memory/` 一处，
不会出现 `admin.md` 与 `memory/health.md` 各存一份体重、数值互相矛盾的情况。

## 写入触发

只有用户明确表达记忆意图时才写入，例如"记住/记一下/存下来/以后都/别忘了"。
日常对话中顺带提到的事实不会被记录。

写入由 agent 自己执行（读文件、判断新增还是更新、写回），完整规范见 `skills/memory/SKILL.md`。
Python 侧不参与写入，只负责渲染注入块和维护 git 快照。

写入后 agent 必须回执 `已记入 memory/<类别>.md：<条目原文>`，这是纯自然语言触发的必要补偿——
让你当场就能发现记错或记漏。

## 注入机制

`app/memory.py` 把「写入协议 + 常驻类别全文 + 非常驻类别索引」渲染到
`runtime/server/memory-context.md`，`PiCliClient` 每轮通过 `--append-system-prompt <path>` 注入

写入协议必须每轮在场，不能依赖仅首轮发送的 skills 摘要。每轮都会启动新的 pi 进程并读取规则与记忆文件，修改下一轮即生效；不把这些内容拼进 user 历史，也不由 `SessionManager` 保存副本

定时任务 `daily:<id>:<日期>` 每天使用新 session，但常驻记忆仍经同一 system 通道注入，保持跨天的事实基础

## 配置

`conf/.env`：

| 配置项 | 作用 |
|--------|------|
| `MEMORY_ENABLED` | 总开关，`false` 时完全不注入 |
| `MEMORY_DIR` | 记忆目录，相对路径按项目根解析（不受 CWD 影响） |
| `MEMORY_CATEGORIES` | 分类 schema，agent 只允许写这些类别，禁止新建文件 |
| `MEMORY_ALWAYS_INJECT` | 常驻注入白名单，须为 `MEMORY_CATEGORIES` 子集（非子集时取交集并告警） |
| `MEMORY_MAX_INJECT_CHARS` | 记忆内容的体积上限，超出按类别边界截断（不包含固定的写入协议） |
| `MEMORY_GIT_AUTO_COMMIT` | 是否自动快照 |
| `MEMORY_CONTEXT_PATH` | 渲染产物路径（在 `runtime/` 下，天然不入库） |

常驻类别每轮都消耗上下文预算，所以只把真正需要"始终在场"的类别放进 `MEMORY_ALWAYS_INJECT`，
其余类别只在注入块里列出路径，由 agent 按需读取。

`MEMORY_MAX_INJECT_CHARS` 只约束记忆内容，不约束写入协议。协议是固定开销（约 700 字）且始终完整注入：
被截半的规则集比没有规则更危险（可能正好切掉"禁止修改 admin.md"这类约束）。

新增类别：在 `MEMORY_CATEGORIES` 加名字（限 `[a-z0-9_-]`），重启后自动创建骨架文件。

## 人工审查与增删改查

记忆是纯 markdown，直接编辑就是最权威的手段。快照仓的 git dir 在 `runtime/memory-git`（工作区内不能有
`.git`，否则主仓会把 `memory/` 当嵌套仓边界，拒绝跟踪 `memory/README.md`），建议先定义别名：

```bash
alias mgit='git --git-dir=runtime/memory-git --work-tree=memory'

vim memory/health.md    # 改
mgit log --oneline      # 看变更历史
mgit diff HEAD~1        # 看上一次改了什么
mgit checkout -- .      # 回滚未提交的误写
mgit revert <commit>    # 回滚某次快照
```

也可以用自然语言让 codeClaw 代劳（"看下你记了我什么"、"把体重那条删了"）。
删除走软删除：条目移入文件末尾的 `## 已归档` 小节，不物理删除。

## 保密与不可推送

- `.gitignore` 以 `/memory/*` + `!/memory/README.md` 排除记忆内容，仅目录占位 README 入库
- 快照仓 git dir 在 `runtime/memory-git` 且**不配置 remote**，`git push` 没有目标，物理上无法推送 github
- `runtime/` 整体 gitignore，快照仓与渲染产物 `memory-context.md` 都不入库

## 快照时机

`auto_commit()` 在每轮渲染注入块前调用，捕获上一轮 agent 的写入；服务 shutdown 时再快照一次。
git 不可用、身份未配置等任何失败都只记 warning，绝不影响对话。

代价是快照会滞后一轮（本轮写入在下一轮才提交），作为防误写的兜底手段可以接受。

## 文件

```
memory/                        → 记忆内容（仅 README 入库，其余 gitignore）
runtime/memory-git/            → 快照仓 git dir（无 remote，不入库）
lib/python/app/memory.py       → 渲染注入块 + git 快照维护
skills/memory/SKILL.md         → agent 的完整操作规范
runtime/server/memory-context.md → 渲染产物，供 pi --append-system-prompt 读取
```
