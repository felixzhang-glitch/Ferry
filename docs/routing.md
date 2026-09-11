# pi 单后端接入

> 保留 `routing.md` 文件名以兼容既有链接；多后端路由与运行时切换已移除

## 装配与接口

`app.main` 直接实例化 `PiCliClient`，注入飞书、微信 handler 和每日任务调度器，不再经过 router，也没有 `active_backend` 状态

- `core.agent.pi_cli`：pi JSONL 调用、原生会话 ID 映射与进程生命周期
- `core.agent.types`：轻量 `AgentClient` Protocol，以及 `AgentClientError` / `AgentClientCancelled`；渠道与测试替身只依赖该接口
- 接口：`chat` / `chat_stream` / `cancel` / `reset_session` / `close`
- `app.skills`：skills 发现与摘要；不再借用其它 CLI 客户端

两个渠道每轮只传当前 `user` 消息（含需要搭载的附件通知），不拼接历史。pi 使用 `--mode json --session-id <id>`，负责历史持久化与原生压缩，详见 [pi CLI 参考](references/pi-cli.txt) 与 [会话管理](sessions.md)

## 命令兼容

| 命令 | 行为 |
|------|------|
| `/backend`、`/pi` | 只读显示唯一 pi 状态，不切换、不清会话、不写后端状态文件 |
| `/opencode`、`/codex`、`/claude`、`/qodercli` | 提示该后端已移除，不调用 CLI、不改变会话 |
| `/compact`、`/compress` | 提示上下文由 pi 原生管理，不执行桥接层压缩、不宣称压缩成功 |
| `/new`、`/reset` | 清待处理附件与当前 pi session 映射，下轮创建新会话；旧 pi transcript 保留 |

## 配置与迁移边界

后端配置统一使用 `PI_*`，渠道、文件、记忆、队列等共享配置继续保留，模型服务凭证（如 `DASHSCOPE_API_KEY`）仍按 pi provider 配置使用

- `PI_WORK_DIR` 是 pi 子进程的**最终 cwd**，不会再追加 `/pi`
- 未设置新键时，兼容旧 `CODEX_WORK_DIR`，按旧语义取 `<CODEX_WORK_DIR>/pi`；两者均未设置时仍为 `./runtime/codex-workdir/pi`
- 例如原 `CODEX_WORK_DIR=/data/work` 对应新 `PI_WORK_DIR=/data/work/pi`，不能直接照抄 `/data/work`
- 重试、退避、熔断、流读取上限等旧 `CODEX_*` 共享键只作对应 `PI_*` 新键的迁移回退，新键优先；不恢复 Codex 后端
- `GENERATED_IMAGES_DIR` 替代 `CODEX_GENERATED_IMAGES_DIR`，旧键仅作回退；目录仍供既有图片发现链路使用，不依赖 Codex CLI
- `ACTIVE_BACKEND`、`BACKEND_STATE_PATH` 与 `runtime/server/backend.json` 不再参与启动或后端选择；其它后端专属配置不再生效

**不能自动迁移或删除运行数据**：保持 pi 现有 cwd、`PI_SESSION_STORE_PATH`（默认 `./runtime/server/pi-sessions.json`）、`PI_CODING_AGENT_DIR`（未设置时 `~/.pi/agent`）及其 session 文件位置不变。pi 会话按 cwd 组织，改 cwd 可能使旧 ID 无法续接；只保留映射文件还不够。旧后端目录、状态文件、用户文件与记忆均不在代码清理范围

启动仅需 pi CLI，不检查其它 agent CLI。迁移验证使用隔离配置；本次不修改真实配置、不移动数据、不重启上线。超时与取消的语义见 [可靠性说明](RELIABILITY.md)
