---
name: memory
description: Long-term memory operations for Ferry. Use this skill when the user explicitly asks to remember, update, review or forget a personal fact (记住 / 记一下 / 存下来 / 以后都 / 别忘了 / 看下我的记忆 / 把某条删了). Memory lives as NOTE-FORMAT markdown files under the project's memory/ directory, one file per configured category, and is injected into every turn's context. Never record facts the user merely mentioned in passing — offline Dream consolidates those.
---

# memory

Ferry 的长期记忆。记忆内容保存在项目根 `memory/` 目录，每个类别一个 markdown 文件，
由 `MEMORY_CATEGORIES` 配置决定，常驻类别每轮自动注入上下文。文件遵循 basic-memory 的
NOTE-FORMAT：frontmatter 说明这个类别是什么，observation 记录事实。

## 何时使用

**只在用户明确表达记忆意图时使用。** 触发词示例：

- 写入：「记住我体重 70 了」「记一下我不喝冰的」「以后都用 rg 搜索」「别忘了我住在城南」
- 查看：「看下你记了我什么」「我的健康记录」
- 修改：「把体重改成 71」
- 删除：「把股票那条删了」「忘掉我说的地址」

**不要使用的情况：** 用户在正常对话中顺带提到某个事实（例如聊天时说「我今天打了羽毛球」），
不要主动写入。离线的 Dream 每晚会从对话里提炼长期事实、合并重复、归档过期条目，
对话中你只处理用户明确授权的记忆动作。

## 文件结构

```markdown
---
title: 健康与身体数据
type: memory
permalink: memory/health
category: health
nature: timeseries
description: 身体与健康数据：体重、运动、作息、身体状况。
---

# 健康与身体数据

## Observations
- [weight] @2026-07-30 体重 70kg (source: user)
- [exercise] @2026-10-19 每周打羽毛球 2 次 (source: dream 2026-10-20)

## 已归档
- [weight] @2026-05-02 体重 72kg (source: user; archived: 2026-07-30)
```

- frontmatter（两行 `---` 之间）由系统维护，**不要改动**。
- `nature` 提示这个类别默认是状态事实（`state`）还是时序事实（`timeseries`）。
- `## 已归档` 之后的条目视为已删除，不再注入上下文。

## 条目格式

```markdown
- [key] @YYYY-MM-DD 内容 (source: user)
```

- `key`：小写英文短词，`[a-z][a-z0-9_]*`，描述这条事实是什么，如 `weight`、`city`、`coffee`、
  `position`。**同一主题沿用文件里已有的 key**，不要同义换词（体重一律 `weight`）。
- `@YYYY-MM-DD`：事实成立的日期，用本轮注入的当前时间，不要凭记忆猜日期。
- 内容：一条一个事实，单行；**不要使用半角括号**（行尾的半角括号组会被解析成元数据），需要时用全角（）。
- 行尾 `(source: user)`：你在对话中按用户要求写入的条目一律标 `source: user`。
  `source: dream …` 是 Dream 推断写入的，`source: migrated` 是旧格式迁移来的。

## 类别归属

| 类别 | 存什么 | 事实性质 |
|---|---|---|
| `basic` | 生日、身高、常驻城市、住址、办公地点 | 状态 |
| `health` | 体重、运动、作息、身体状况 | 时序 |
| `preference` | 口味、审美、工具习惯、生活方式 | 状态 |
| `work` | 岗位、职责、在做的项目、技术栈 | 状态 |
| `finance` | 仓位、风险偏好、关注标的 | 时序 |
| `recent` | 最近在做什么、在关注什么 | 时序 |

判断不了归属时，选语义最贴近的一个，不要同时写多个文件造成重复。
类别清单以每轮注入的常驻块为准；**禁止新建清单外的文件**。

## 时序事实 vs 状态事实

判断标准是「旧值是否还有价值」：

- **时序事实**：旧值构成趋势。→ 用同一个 key **追加**新条目，保留旧条目。
  例：`[weight]` 70kg → 68kg，两条都留着才能看出在减；仓位变化同理。
- **状态事实**：旧值一旦过期就是错误信息。→ **原地覆盖**旧条目（内容和日期都更新）。
  例：`[address]` 从 A 搬到 B，留着 A 只会让你答错。

拿不准时按状态事实处理（覆盖），避免记忆里堆积矛盾信息。

## 操作步骤

### 新增 / 更新

1. 判断类别，读取 `memory/<类别>.md` 当前内容。
2. 检查是否已有同 key 或同主题条目：
   - 无 → 在 `## Observations` 下最后一条之后追加新条目（在 `## 已归档` 之前）。
   - 有且为时序事实 → 追加新条目，旧条目保持原样。
   - 有且为状态事实 → 原地替换该行的内容与日期，`source` 改为 `user`。
3. 写回文件（只改目标行，不动 frontmatter 和其他行）。
4. 回执：`已记入 memory/<类别>.md：<条目原文>`

### 查看

读取相关类别文件后按原文回答。常驻类别（`MEMORY_ALWAYS_INJECT`）的内容已在你的上下文里，
不必重复读文件；非常驻类别的路径见常驻块的「按需读取的记忆类别」索引。

### 修改

定位到具体条目，改内容并把日期更新为今天。回执：`已更新 memory/<类别>.md：<新条目>`

### 删除（软删除）

不要物理删除。把整行移到该文件末尾的 `## 已归档` 小节，并在行尾括号里追加归档日期：

```markdown
## 已归档

- [address] @2026-05-02 旧的住址信息 (source: user; archived: 2026-07-30)
```

小节不存在时创建。回执：`已归档 memory/<类别>.md：<条目原文>`

## 冲突处理

用户给的新值与已有条目矛盾时：

- 状态事实 → 直接覆盖，回执里说明「原值 X 已更新为 Y」，让用户有机会纠正。
- 时序事实 → 追加新条目，不动旧值。
- 无法判断用户是要纠错还是记录变化时，按纠错处理（覆盖）并在回执里说明，
  不要为此反问用户。

## 整理与 Dream

合并重复、归档过期（`recent` 超过 90 天）等整理工作由 Dream 每晚自动完成，变更会推送摘要，
用户可以发 `/memory undo` 撤销最近一次整理，`/memory` 查看状态，`/dream` 立即整理一次。
对话中除非用户明确要求，不要自己做批量整理。

## 硬约束

- 禁止修改 `rules/system.md` 和 `rules/admin.md`。那是人工维护的人格与权威设定，
  记忆只能写 `memory/` 下的类别文件。
- 禁止新建 `MEMORY_CATEGORIES` 之外的记忆文件。
- 每次写入后必须回执，让用户能当场发现记错。
- `memory/` 受本地快照仓保护（git dir 在 `runtime/memory-git`，无 remote），每轮对话会自动快照，
  所以不需要你手动备份；但也因此不要往里放临时文件。
