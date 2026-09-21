# 核心设计信念

## pi-native

Ferry 只接入 pi。判断标准：**pi 原生支持的能力，桥接层不重复实现**

- **会话记忆**：`PiCliClient` 维护会话 key → pi session ID 映射，pi 原生 `--session-id` 负责持久化；两渠道每轮只发当前 user 消息
- **上下文压缩**：pi 原生管理，不再维护 FIFO 历史或手工摘要；`/compact`、`/compress` 只给说明，不假装完成压缩
- **工具调用**：文件操作、代码执行等由 pi 承担，Ferry 不封装工具执行框架
- **规则、记忆与时间**：复用 `--append-system-prompt` 每轮注入，时间不拼进 user transcript；skills 发现与摘要归 `app.skills`

## 桥接不膨胀

Ferry 的职责：

- 消息收发与渠道适配：飞书 WS / Webhook、微信 Sidecar、格式化、分段、图片与文件
- 会话 key、附件通知、去重、消息队列与任务取消
- pi 进程生命周期及会话映射，不再提供后端路由或切换状态
- `/help`、`/new`、`/reset`、`/stop`、`/backend`、`/pi`、定时与技能查询等轻量命令
- 保留长期记忆、每日任务、定时提醒及渠道既有能力边界

不做的事：

- 不做 prompt engineering、RAG、workflow 引擎
- 不为预想中的后端扩展保留路由框架；轻量 `AgentClient` Protocol 只服务渠道契约与测试替身
- 不做多租户、外部存储或多实例编排
- 不维护其它 CLI 后端，旧命令只提示已移除

## 兼容优先，不搬运行数据

配置名称收敛不等于移动目录。`PI_WORK_DIR` 是最终 cwd，默认继续使用 `./runtime/codex-workdir/pi`，保留旧共享键迁移回退。现有 pi cwd、session store、agent dir 不自动迁移，旧状态文件、用户附件与记忆不删除

## 个人项目简洁优先

- 单实例部署，文件持久化，不引入 Redis/DB
- 配置通过 `.env` 管理，不上 config server
- 测试覆盖核心链路，不追求 100% coverage；未运行的测试不得标通过
- 文档描述当前契约，历史分析与历史验证明确标注；不把配置项存在当成可靠性保证
