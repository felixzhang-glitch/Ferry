# 会话与任务管理

## 会话管理

### pi 原生会话

pi 是唯一后端。飞书与微信每轮只向 `PiCliClient` 传当前 `user` 消息，不拼接历史、不缓存 assistant 回复。pi 的 `--session-id` 接受调用方生成的 ID，不存在时按该 ID 新建；映射由 `PiCliClient` 管理，历史持久化与上下文压缩由 pi 管理

- 会话 ID 映射：飞书 `user_id:chat_id`、微信 `wechat:account_id:user_id` -> Ferry 生成的 uuid4 hex
- 持久化：`PI_SESSION_STORE_PATH`，默认 `runtime/server/pi-sessions.json`
- pi 侧会话文件：默认 `~/.pi/agent/sessions/` 下按 cwd 分目录的 JSONL 树；设置 `PI_CODING_AGENT_DIR` 时使用对应 agent 目录
- `/new`、`/reset`：清附件通知并删除映射，下轮自然生成新 uuid（旧会话在 pi 侧保留）
- 首轮判定：靠“映射是否新建”而不是“session_id 是否为空”，skills 摘要只在首轮注入

### 桥接层状态与命令

`SessionManager` 仅管理渠道会话 key 和 `pending_files`，不保存历史、独立桥接 UUID，也不手工压缩。pi session ID 不等于被删除的桥接层 UUID

- 文件归档后排入附件通知，不主动唤醒 agent；下一轮普通 user 文本搭载通知，文件与文字同发合为一轮
- 命令轮和被拒绝的任务不消费通知，归档失败不排通知；上限 20 条，超出丢最旧
- 附件通知为内存态，重启丢失，已归档文件保留
- `/new`、`/reset`：清附件通知和当前 pi 映射，下轮生成新 ID；不删除旧 pi transcript 或归档文件
- `/compact`、`/compress`：仅提示由 pi 原生管理，不调用摘要模型、不修改状态、不声称已压缩
- `/backend`、`/pi`：只读唯一 pi 状态；旧后端命令仅提示移除

### 迁移与恢复

现有 pi cwd、`PI_SESSION_STORE_PATH`、`PI_CODING_AGENT_DIR` 及其 transcript 位置均不能自动迁移。`PI_WORK_DIR` 是最终 cwd，默认仍为 `./runtime/codex-workdir/pi`；旧 `CODEX_WORK_DIR` 回退规则见 [单 pi 接入](routing.md)。恢复会话需要映射与 pi transcript 同时可用，不能只备份其中一份

## 去重

- 基于 `message_id`，TTL 1 小时（`DEDUPLICATE_TTL_SECONDS`）
- 防止飞书/微信重试导致重复处理

## 任务注册

- 会话维度：同一会话同一时刻只允许一个运行中任务
- 注册信息：trace_id、message_id、启动时间、通知状态
- 同会话连发消息按 FIFO 排队；这是消息队列，不是历史窗口
- `/stop`：通过 trace_id 取消 pi 进程组并清空待执行消息队列
- 飞书 Quick Ack：收到消息立即发 Typing reaction；微信保留现有渠道限制

## 定时提醒

- `/remind <time> <content>`：时间单位 s/m/h/d
- 持久化：`runtime/server/reminders.json`
- 到期后通过飞书 API 主动发送，微信保留现有提醒限制
- 重启时从文件恢复未触发的提醒

## 每日任务

- `/daily HH:MM <prompt>`：创建 / list / cancel，每日执行并推送飞书或微信
- 持久化：`runtime/server/daily-tasks.json`，重启恢复
- 每日 session key：`daily:<id>:<日期>`，每天独立上下文，长期记忆仍按轮注入
- 单 pi 收敛不改变既有定时、队列与渠道能力边界

## 文件

```
lib/python/core/session/
  manager.py            → 会话 key 与 pending_files
  deduplicator.py       → message_id 去重
  task_registry.py      → 运行中任务注册与取消
  message_queue.py      → 同会话 FIFO 消息队列
  reminder_scheduler.py → 定时提醒与持久化
  daily_scheduler.py    → 每日任务与恢复
```
