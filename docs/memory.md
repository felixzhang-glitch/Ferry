# 长期记忆

## 三层记忆载体

Ferry 的"记忆"分三层，职责与可写方严格分离：

| 载体 | 内容 | 谁可写 | 注入方式 |
|------|------|--------|----------|
| `rules/system.md` | 人格、风格、时间感知、工具偏好 | 仅人工 | pi `--append-system-prompt` |
| `rules/admin.md` | 权威静态事实（身份、权限、邮箱、微信 user_id、运行环境） | 仅人工 | pi `--append-system-prompt` |
| `memory/*.md` | 动态事实（基础档案、健康、偏好、工作、投资、近况） | agent（用户明确要求时）+ Dream（离线整理）+ 人工 | 生成的记忆块，常驻注入 |

分层的意义：agent 与 Dream 只能写 `memory/`，改不到人格与权威设定；会变的事实只存在 `memory/` 一处，
不会出现 `admin.md` 与 `memory/health.md` 各存一份体重、数值互相矛盾的情况。

## 文件格式：NOTE-FORMAT

每个类别一个文件，遵循 basic-memory 的 NOTE-FORMAT（不扩展 Markdown，只约定列表项写法）。
文件是真源，`memory-index.json` 是它的结构化投影。

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
- works_at [[阿里云]]

## 已归档
- [weight] @2026-05-02 体重 72kg (source: user; archived: 2026-07-30)
```

| 部分 | 约定 |
|------|------|
| frontmatter | `title` / `type: memory` / `permalink` / `category` / `nature`（`state` 状态事实覆盖、`timeseries` 时序事实追加）/ `description`；由系统维护，agent 不改 |
| observation | `- [key] @YYYY-MM-DD 内容 #tag (k: v; k: v)`：key 为 `[a-z][a-z0-9_]*`，同 key 重复即时序数组；`@date` 是 basic-memory 时态限定符（也解析 `@effective:` 等带 kind 的点形式）；行尾唯一括号组是 context，内部按 `k: v` 拆成元数据 |
| source | `user` 对话中按用户明确要求写入；`dream YYYY-MM-DD` Dream 推断写入；`migrated` 旧格式迁移 |
| relation | `- rel [[Target]]`，解析并原样注入；Dream 不产出 relation |
| 归档 | `## 已归档` 之后的条目视为已删除，不注入；context 里 `archived:` 记日期，合并产生的归档另带 `reason: merged` |

解析器 `app/memory_format.py` 的边界与 NOTE-FORMAT 一致：`- [ ]` 等勾选框、`- [text](url)`、`- [[X]]`、
`[00:00:11]` 时间码不算 observation。为了不丢记忆，旧格式 `- [YYYY-MM-DD] 内容` 与没有 `[key]` 的普通列表项
仍会以 key=`fact` 的 legacy 条目建索引并注入，同时在投影的 `warnings` 里提示。

写入走 `patch_note` 行级补丁：只改目标行，手写的注释、子标题与正文逐字保留。

## 写入触发

**对话中**：只有用户明确表达记忆意图时才写入，例如"记住/记一下/存下来/以后都/别忘了"，
由 agent 按 `skills/memory/SKILL.md` 读写文件，条目标 `(source: user)`，写入后回执
`已记入 memory/<类别>.md：<条目原文>`。日常对话中顺带提到的事实不在对话中记录。

**离线 Dream**：每天定时（以及 `/dream` 手动整理当前对话、`/dream all` 整理全部）从聊天记录中提炼长期事实、整理旧记忆，见下节。

## Dream：离线主动记忆

参照 Agent Memory Repo 的 Dreaming：一个独立的、周期运行的整理过程，负责"新增记忆"和"清理记忆"。

```
DREAM_TIME（默认 02:00）、/dream all、控制台「立即整理」→ 全部对话；/dream → 只整理发起命令的这个对话
 1 auto_commit 快照对话中的写入，记 pre_commit；解析全部类别，记每个文件的 sha256
 2 确定性维护：recent 中 @date 超过 90 天的条目直接归档
 3 收集聊天会话增量：wechat:* 与飞书 <user>:oc_* 的会话（排除 daily:* 等），按 session_key 归组；
   每个对话取 max(全局 watermark, 该对话 watermark) 之后的 user / assistant 文本，
   跳过工具调用、思考、系统快照与首轮 skills 前言
 4 pi 一次性调用（--no-tools --no-session --no-mcp，不走 worker 池与熔断）
   system = rules/dream.md + rules/admin.md，记忆投影与对话增量作为 @附件 → 只返回 JSON ops
 5 Python 校验 ops 后按行补丁写回；若文件在整理期间被对话改过（sha 变了）则跳过该类别
 6 单独提交 "dream: <日期> +新增 ~更新 merge 合并 -归档"
 7 写运行记录、推进 watermark（全量推进全局 watermark 并清掉已被追平的对话 watermark；
   /dream 只推进该对话的 watermark）；定时运行的摘要排队到 DREAM_REPORT_TIME（默认 08:00）推送，
   也可选「整理完立即」；手动 /dream 立即回复发起会话，控制台触发立即推送到报告目标；无变更静默
```

核心原则：**模型只提议，Python 落盘**。Dream 的 pi 进程没有任何工具，碰不到文件。

ops 协议（`rules/dream.md`）：`add` / `update`（原位替换状态事实）/ `merge`（同类别重复条目合并，
原条目移入已归档）/ `archive`。Python 侧校验：

- `add` / `update` 的 `evidence` 必须逐字（忽略空白与标点）出现在本次输入的某条**用户**消息里——
  防幻觉，也防网页、转发内容、助手复述把"指令"写进记忆；日期默认取该消息日期
- 改动或归档 `source: user` 的条目必须带可校验的 evidence
- 类别白名单、key 格式、内容单行且 ≤200 字、不含 wikilink、命中密码/token/密钥/卡号/证件号则丢弃
- 同一条目一次只能被一个 op 处理，单次最多 30 个 op；不合格的 op 记入运行记录，不影响其他 op

失败处理：

| 情况 | 行为 |
|------|------|
| pi 失败 / 超时 / 输出不是 JSON | 不改文件、不推进 watermark，推送失败通知，下次定时或 `/dream` 重试 |
| 某类别在整理期间被对话修改 | 跳过该类别，watermark 不推进，下次重抽靠去重自然收敛 |
| 没有新的用户消息且无到期归档 | 不调用模型，定时运行静默结束；手动 `/dream` 回复"自上次整理以来没有新的对话"（与"模型认为没有可记的内容"区分） |

控制台（`/observability/` →「记忆整理」与「系统设置」）可以：开关 Dream、选择整理时间与报告时间（5 分钟步长）、
勾选推送目标、选择整理模型、立即整理、撤销最近一次；以上都即时生效，不需要重启。

命令（单用户，不做鉴权）：

| 命令 | 作用 |
|------|------|
| `/memory` | 各类别条目数、最近一次 Dream 的时间与结果、定时设置 |
| `/memory undo` | 撤销最近一次有变更的 Dream：`git revert` 那次提交；之后记忆又被改过导致冲突时自动 abort 并提示手工处理；同一次只能撤销一次 |
| `/dream` | 立即后台整理**当前对话**（同一 session_key 下含 `/new` 之前的旧会话），结果推送给发起命令的会话 |
| `/dream all` | 立即后台整理全部对话（与定时、控制台「立即整理」相同），结果推送给发起命令的会话 |

PWA 的 `/dream` 使用当前 Web thread 的 `web:<id>` key；结果 inline 返回聊天流，不走邮件或 IM 推送。活跃 Web 线程也参与夜间全量整理；删除线程会写入 retirement 台账，不再进入后续 Dream，但已提炼的长期记忆仍需显式修改或撤销。

会话归属：`/new` 会把旧 session_id 从 `pi-sessions.json` 删掉，所以 `PiCliClient` 在新建 session_id 时、
以及 `/new` / `/reset` 删除映射之前，都追加一行到 `runtime/server/pi-session-history.jsonl`，
Dream 合并两者找全聊天记录（台账上线前就存在的会话也能在下次 `/new` 时被记下）。

## 注入机制

`app/memory.py` 解析全部类别文件，把「写入协议 + 常驻类别的有效条目 + 非常驻类别索引」渲染到
`runtime/server/memory-context.md`，`PiCliClient` 每轮通过 `--append-system-prompt <path>` 注入；
同时把全部元数据投影写到同目录的 `memory-index.json`。

常驻类别只注入 `- [key] @date 内容` 与 relation：frontmatter、来源 context、已归档条目都不进上下文。

写入协议必须每轮在场，不能依赖仅首轮发送的 skills 摘要。每轮都会启动新的 pi 进程并读取规则与记忆文件，修改下一轮即生效；不把这些内容拼进 user 历史，也不由 `SessionManager` 保存副本

定时任务 `daily:<id>:<日期>` 每天使用新 session，但常驻记忆仍经同一 system 通道注入，保持跨天的事实基础

## 配置

`conf/.env`：

| 配置项 | 作用 |
|--------|------|
| `MEMORY_ENABLED` | 总开关，`false` 时完全不注入，Dream 也不运行 |
| `MEMORY_DIR` | 记忆目录，相对路径按项目根解析（不受 CWD 影响） |
| `MEMORY_CATEGORIES` | 分类 schema，agent 与 Dream 只允许写这些类别，禁止新建文件 |
| `MEMORY_ALWAYS_INJECT` | 常驻注入白名单，须为 `MEMORY_CATEGORIES` 子集（非子集时取交集并告警） |
| `MEMORY_MAX_INJECT_CHARS` | 记忆内容的体积上限，超出按类别边界截断（不包含固定的写入协议） |
| `MEMORY_GIT_AUTO_COMMIT` | 是否自动快照 |
| `MEMORY_CONTEXT_PATH` | 渲染产物路径（在 `runtime/` 下，天然不入库）；`memory-index.json` 与 `dream/` 写在同目录 |
| `DREAM_ENABLED` | Dream 开关，默认 `true` |
| `DREAM_TIME` | 每日整理时间 `HH:MM`（`FERRY_TIMEZONE`），默认 `02:00`；重启后 6 小时内补跑 |
| `DREAM_REPORT_TIME` | 报告推送时间 `HH:MM`，默认 `08:00`；`immediate` 为整理完立即推送；重启后 6 小时内补发 |
| `DREAM_NOTIFY` | 报告推送目标，逗号分隔多个：`email:<地址>` / `wechat:<user_id>` / `feishu:<chat_id>`；留空不推送，用 `/memory` 查看 |
| `MAIL_SMTP_HOST` / `MAIL_SMTP_PORT` | 邮件渠道 SMTP SSL，默认 `smtp.163.com:465` |
| `MAIL_SENDER` | 发件地址；留空取 `MAIL_ENV_FILE` 的 `MAIL_ASSISTANT_EMAIL` |
| `MAIL_RECIPIENTS` | 控制台可选的收件人清单（逗号分隔），推送目标与每日任务只能选其中的地址 |
| `MAIL_ENV_FILE` | 授权码文件（`MAIL_ASSISTANT_AUTH_CODE`），默认 `~/.env/smtp-mail-assistant.env`，发送时读取 |
| `DREAM_MODEL` | Dream 使用的模型，留空沿用 `PI_MODEL` |
| `DREAM_TIMEOUT_SECONDS` | 单次整理的模型调用超时，默认 600 |
| `DREAM_MAX_TRANSCRIPT_CHARS` | 喂给模型的对话增量上限，超出保留最新部分，默认 40000 |

常驻类别每轮都消耗上下文预算，所以只把真正需要"始终在场"的类别放进 `MEMORY_ALWAYS_INJECT`，
其余类别只在注入块里列出路径，由 agent 按需读取。

`MEMORY_MAX_INJECT_CHARS` 只约束记忆内容，不约束写入协议。协议是固定开销（约 900 字）且始终完整注入：
被截半的规则集比没有规则更危险（可能正好切掉"禁止修改 admin.md"这类约束）。

新增类别：在 `MEMORY_CATEGORIES` 加名字（限 `[a-z0-9_-]`），重启后自动创建 NOTE-FORMAT 骨架文件。

## 旧格式迁移

一次性工具，把 `- [YYYY-MM-DD] 内容` 改写为 NOTE-FORMAT：

```bash
PYTHONPATH=lib/python python3 -m app.memory_migrate           # dry run，只打印 diff
PYTHONPATH=lib/python python3 -m app.memory_migrate --apply   # 写入，前后各做一次快照提交
```

- 模型只负责给每条旧条目分配 key（失败回退 `fact`，`--no-llm` 直接用 `fact`），日期与内容原样保留，
  迁移前后条目集合不一致即中止
- 原地改写：标题、子标题、注释与正文位置不变；`## [YYYY-MM-DD] …` 小节下没有日期的条目继承小节日期；
  hint 注释转为 frontmatter 的 `description`
- dry run 分配的 key 缓存在 `runtime/server/memory-migrate-keys.json`，`--apply` 写入的就是审过的那份 diff
- 已是 `type: memory` 的文件跳过，可重复执行

## 人工审查与增删改查

记忆是纯 markdown，直接编辑就是最权威的手段。快照仓的 git dir 在 `runtime/memory-git`（工作区内不能有
`.git`，否则主仓会把 `memory/` 当嵌套仓边界，拒绝跟踪 `memory/README.md`），建议先定义别名：

```bash
alias mgit='git --git-dir=runtime/memory-git --work-tree=memory'

vim memory/health.md    # 改
mgit log --oneline      # 看变更历史（dream: 开头的是 Dream 整理）
mgit diff HEAD~1        # 看上一次改了什么
mgit checkout -- .      # 回滚未提交的误写
mgit revert <commit>    # 回滚某次快照
```

也可以用自然语言让 Ferry 代劳（"看下你记了我什么"、"把体重那条删了"）。
删除走软删除：条目移入文件末尾的 `## 已归档` 小节，不物理删除。

Dream 的每次运行都在 `runtime/server/dream/run-<时间>.json` 留有审计记录：生效的变更（含 evidence 原话）、
被拒绝的 op 与原因、冲突类别、模型原始输出。

## 保密与不可推送

- `.gitignore` 以 `/memory/*` + `!/memory/README.md` 排除记忆内容，仅目录占位 README 入库
- 快照仓 git dir 在 `runtime/memory-git` 且**不配置 remote**，`git push` 没有目标，物理上无法推送 github
- `runtime/` 整体 gitignore，快照仓、渲染产物、投影、Dream 状态与运行记录都不入库
- Dream 发给模型的对话内容本来就经同一 provider 处理过，不新增外发对象

## 快照时机

`auto_commit()` 在每轮渲染注入块前调用，捕获上一轮 agent 的写入；服务 shutdown 时再快照一次。
Dream 开始前先 auto_commit，结束后单独提交，保证 `/memory undo` 只回滚 Dream 自己的改动。
git 不可用、身份未配置等任何失败都只记 warning，绝不影响对话（此时 Dream 照常生效但无法 undo）。

代价是对话写入的快照会滞后一轮（本轮写入在下一轮才提交），作为防误写的兜底手段可以接受。

## 文件

```
memory/                               → 记忆内容（仅 README 入库，其余 gitignore）
runtime/memory-git/                   → 快照仓 git dir（无 remote，不入库）
lib/python/app/memory_format.py       → NOTE-FORMAT 解析、行级补丁、投影
lib/python/app/memory.py              → 渲染注入块 + 投影 + git 快照/提交/回滚
lib/python/app/dream.py               → Dream：会话收集、ops 校验与落盘、调度、undo
lib/python/app/memory_migrate.py      → 旧格式一次性迁移
rules/dream.md                        → Dream 的系统提示词（ops 协议与取舍规则）
skills/memory/SKILL.md                → agent 的完整操作规范
runtime/server/memory-context.md      → 渲染产物，供 pi --append-system-prompt 读取
runtime/server/memory-index.json      → 全部元数据的结构化投影
runtime/server/dream/                 → Dream 状态（watermark、最近一次结果）、输入快照与运行记录
runtime/server/pi-session-history.jsonl → session_key 与 session_id 的追加台账
```
