# RELIABILITY

## 可靠性目标

- 个人单实例服务，无 SLA；持久化的 pi 映射、长期记忆、提醒与每日任务应能恢复
- pi transcript 与映射共同构成会话恢复条件，不能只保留 `pi-sessions.json`
- `pending_files`、消息队列、运行中任务是内存态，不承诺重启恢复；归档文件本身保留

## 监控与告警

- 健康检查：`./bin/server status`
- 日志：`logs/`（不入库）；异常可通过渠道回复和 pi 事件日志排查，避免输出凭证与文件解密密钥
- 告警阈值与自动备份：待补充，当前无服务级可用性保证

## 故障处理

- pi 报错或卡死：使用 `/stop` 取消当前任务及待执行队列；必要时经人工确认再重启，不作为本次变更动作
- pi JSON 模式不能只凭进程退出码判成功；必须检查最后一条 assistant `message_end.message.stopReason`，部分文本后失败不能当完整成功回复
- 需要新上下文时用 `/new` 或 `/reset`，清附件与 pi session 映射，不删除旧 transcript
- `/compact`、`/compress` 仅说明由 pi 原生管理，不执行桥接层压缩；旧后端命令不能用于故障切换
- 重试、退避、熔断与流读取保护归 `PiCliClient`，配置统一 `PI_*`，不再依赖已删除的其它后端实现

### 超时验证边界

`PI_IDLE_TIMEOUT_SECONDS` 限制等待下一行 stdout 的空闲时间；`PI_TIMEOUT_SECONDS` 限制每次 CLI 尝试的总耗时（默认 180s ≈ deepseek-flash 实测 p90 25s 的 7 倍；再放宽只是让失败轮次白等更久，历史上出现过跑满 300s 才报超时的轮次），持续输出不会刷新该预算，stdout EOF 后的进程退出与 stderr 等待也计入。空闲时限不要压到最长合法静默工具之下（实测 90.3s），否则会误杀慢 curl / find。超时、取消及关闭流会回收进程；服务关闭会终止仍注册的进程。

预算按每次尝试计算，不包含此前尝试、退避或渠道队列等待；客户端最多执行 `1 + PI_MAX_RETRIES` 次，pi 内部重试仍受单次进程预算约束。部分流式输出后不会重试，避免重复回复。空闲、持续输出、EOF 后挂起与真实子进程取消已由隔离测试覆盖，真实模型长任务仍需上线前冒烟

## 迁移与运维清单

- 启动仅检查 pi CLI，保留飞书、微信 sidecar、文件 / 图片、记忆、定时和队列功能
- `PI_WORK_DIR` 是最终 cwd；不配置时兼容旧 `CODEX_WORK_DIR/pi`，默认仍 `./runtime/codex-workdir/pi`
- 保持现有 cwd、`PI_SESSION_STORE_PATH`、`PI_CODING_AGENT_DIR` 及其文件原位；pi 按 cwd 组织会话，改 cwd 可能使旧 ID 无法续接
- `GENERATED_IMAGES_DIR` 接替旧 `CODEX_GENERATED_IMAGES_DIR`，需核对实际解析目录，不能因改名丢失图片发现
- 不再使用 `backend.json` 或旧后端选择配置；旧运行数据只是不再读取，不自动清理
- 本次不改真实配置、不迁移数据、不重启上线；后续变更需先备份并在隔离环境验证，再由人工决定发布或回滚
- 服务管理入口：`./bin/server start|stop|restart|status`；微信：`./bin/server wx login|start|stop`
- 调度仍由应用内 `/daily`、`/remind` 承担，无新增系统 cron 依赖

配置兼容细节见 [routing.md](routing.md)，回归要求见 [functional-tests.md](functional-tests.md)
