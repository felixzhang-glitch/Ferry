# codeClaw

> 能力归 agent，编排归 harness。


## 设计

pi 负责思考。codeClaw 负责把消息送到 pi，再把回复送回来。

一条原则：pi 原生能做的事，codeClaw 不重复实现。所以没有内置 prompt 工程、RAG、workflow。这些 agent 自己会。

## 记忆

pi 原生支持 session 持久化。昨晚聊的内容，今天还在。codeClaw 只维护 `user_id:chat_id` → session_id 的映射，不碰上下文。

备选后端（opencode/codex/claude/qodercli）没有原生 session，codeClaw 替它们做 FIFO 历史拼接，默认保留 50 轮。

## 时间感知

`hooks/inject-time.js` 在每条消息末尾注入当前系统时间。agent 因此能理解"明天"、"刚才"这类相对时间。删掉文件即禁用，不影响正常对话。

## 规则与技能

`rules/AGENTS.md` 和 `rules/admin.md` 改完即生效，不需要重启。`skills/` 目录同理——放进去一个 SKILL.md，agent 自动识别。

## 多后端

默认 pi，运行时随时切换：

```
/pi   /codex   /claude   /qodercli
```

切换清空当前会话历史。pi 的 session 保留，切回来可以续上。

## 其他能力

- `/remind 10m 喝水` — 定时提醒
- `/daily 08:00 简报` — 每日定时任务
- `/stop` — 终止当前任务
- 收到的文件/图片自动归档，并在你下次开口时告知 agent 文件在哪（不会自动展开内容）
- 发文件给你：飞书/微信统一走 `POST /push/file`，详见 [渠道接入](docs/channels.md)

## 快速开始

```bash
./bin/start        # 首次启动引导配置飞书凭证
./bin/server status
./bin/server stop
./bin/server restart
```

微信（可选）：

```bash
./bin/server wx login
./bin/server wx start
```

## 项目结构

```
bin/          # 服务控制
conf/         # 配置
lib/python/   # Python 服务
lib/js/       # 微信 sidecar
rules/        # 提示词规则（热加载）
skills/       # 项目级技能
hooks/        # 时间注入插件
docs/         # 文档
tests/        # 测试
```

## 文档

- [核心信念](docs/core-beliefs.md)
- [架构](docs/architecture.md)
- [渠道接入](docs/channels.md)
- [后端路由](docs/routing.md)
- [会话管理](docs/sessions.md)

## 许可

MIT
