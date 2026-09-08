# PRODUCT

## 产品定位

个人使用的 IM → 本机 CLI agent 桥：通过飞书 / 微信与本机运行的 CLI agent（默认 pi）对话，agent 能力全由 CLI 原生承载，codeClaw 只做编排。

## 功能项清单

> 新增需求先更新本表再开发，保证多轮开发不偏离。状态取值：规划中 / 开发中 / 已完成 / 已废弃。

| 功能 | 状态 | 描述 |
|---|---|---|
| 飞书渠道 | 已完成 | WS 长连接 + webhook 双入口，签名校验、解密、格式化回发、图片/文件处理 |
| 微信渠道 | 已完成 | iLink Bot 长轮询 sidecar（Node.js）转发，webhook token 校验 |
| 多后端路由 | 已完成 | `/pi` `/opencode` `/codex` `/claude` `/qodercli` 运行时切换，状态持久化；pi 为默认，opencode 为主要备选，其余只维护不投入新特性 |
| 会话映射 | 已完成 | `user_id:chat_id` → session_id 映射；pi 原生 session 自管，备选后端 FIFO 历史拼接（默认 50 轮） |
| 会话命令 | 已完成 | `/new` `/reset` `/stop`、消息去重、同会话连发 FIFO 排队 |
| 定时提醒 | 已完成 | `/remind 10m 喝水`，时间解析 + 持久化，重启恢复 |
| 每日任务 | 已完成 | `/daily 08:00 简报`，创建 / list / cancel，重启恢复，飞书 + 微信双推送 |
| 时间感知 | 已完成 | 每轮注入带时段的系统时间，文本由 `app/clock.py` 统一生成：pi 走 `--append-system-prompt`，claude/qodercli/codex 拼 prompt 首行，opencode 走 `hooks/inject-time.js` |
| 规则与技能热加载 | 已完成 | `rules/` 与 `skills/` 改完即生效，无需重启 |
| 长期记忆 | 已完成 | `memory/` 目录，明确要求时写入并回执，常驻注入，快照仓本地无 remote |
| 文件消息处理 | 已完成 | 文件自动归档，图片自动下载交给 agent |
| 推送密钥扫描 | 已完成 | pre-push 钩子扫描待推送新增行，fail-closed 硬阻断 |

## 非目标

> 明确不做什么，防止范围蔓延。

- 不造智能：不做内置 prompt 工程、RAG、workflow，agent 原生能做的桥接层一律不重复实现
- 不引入外部存储：纯文件持久化，不接数据库 / Redis
- 不做多实例：单实例个人部署
- 备选后端（codex / claude / qodercli）不投入新特性，只维护并关注 CLI 升级后的行为变化

## 用户反馈与待验证问题

- `TODO: 待补充`
