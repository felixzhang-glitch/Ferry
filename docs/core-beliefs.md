# 核心设计信念

## CLI-native（原 opencode-first）

codeClaw 的核心后端是一个具备原生会话管理的 CLI agent。设计判断标准不变：**如果一个能力后端 CLI 原生支持，codeClaw 不重复实现。**

默认后端自 2026-08-04 起为 **pi**（Pi Coding Agent），opencode 降为可切换备选。信念本身没变，变的只是承载它的 CLI —— 所以这一节按“默认后端”而不是按 opencode 来读。

具体表现：

- **会话记忆**：pi 后端走原生 `--session-id` 持久化（opencode 走 `--session`），上下文管理、超限压缩全部交给后端自行处理。codeClaw 只负责维护 session ID 的映射关系（`user_id:chat_id` -> 后端 session id）。区别只在于 ID 归谁生成：pi 的 `--session-id` 接受任意 ID 并按需创建，所以由 codeClaw 生成；opencode 只能从事件流里反解。
- **工具调用**：后端支持的文件操作、代码执行、搜索等能力，codeClaw 不封装也不代理。
- **上下文压缩**：后端自管上下文窗口，codeClaw 不介入。`/compact` 在这两个后端下都不需要 codeClaw 出手。

备选后端（codex/claude/qodercli）因为不具备原生会话管理能力，codeClaw 才为它们维护 FIFO 历史拼接。

### 这条信念的边界：后端不支持的，codeClaw 才补

pi 明确不做 MCP、子 agent、内建权限系统、webfetch/websearch，也不加载 opencode 的 `plugin`。落到 codeClaw 上只补了一处：时间感知从 `hooks/inject-time.js`（opencode 插件）换成 pi 既有的 `--append-system-prompt`——和规则、记忆同一条通道，没有为时间单开机制。skills 摘要与规则、记忆注入同样是所有后端共用的既有注入点。

## 桥接不膨胀

codeClaw 的职责边界：

- 消息收发（飞书 Webhook / 微信 Sidecar）
- 后端路由切换（运行时命令切换 + 状态持久化）
- 渠道适配（Markdown 卡片渲染、超长文本分段、图片上传）
- 命令系统（/help /new /stop /backend /remind 等轻量命令）

不做的事：

- 不做 prompt engineering（原文透传给后端）
- 不做 RAG / 知识库（后端 CLI 有自己的上下文管理）
- 不做多租户 / 权限管理（个人项目，单实例）
- 不做 agent 编排 / workflow 引擎

## 个人项目简洁优先

- 单实例部署，无需容器编排
- 文件持久化（JSON），不引入 Redis/DB
- 配置通过 .env 管理，不上 config server
- 测试覆盖核心链路，不追求 100% coverage
- 文档服务于自己和 AI agent 理解项目，不做外部用户文档
