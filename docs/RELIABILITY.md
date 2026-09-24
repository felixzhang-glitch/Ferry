# RELIABILITY

## 可靠性目标

- 个人单实例服务，无 SLA；持久化的 pi 映射、长期记忆、提醒与每日任务应能恢复
- pi transcript 与映射共同构成会话恢复条件，不能只保留 `pi-sessions.json`
- `pending_files`、消息队列、运行中任务是内存态，不承诺重启恢复；归档文件本身保留

## 监控与告警

- 健康检查：`./bin/server status`；可观测独立进程 `./bin/server obs status`，Supervisor 名称 `ferry-observability`，主服务仍为 `ferry-stack:ferry`
- Web：对外部署地址示例为 `https://observability.example.com/observability`（替换为自己的域名，443），后端可监听 `0.0.0.0:38080`；访问控制由安全组和应用鉴权共同承担。HTTPS 部署应设 `OBSERVABILITY_COOKIE_SECURE=true`；不要用裸 HTTP 38080 登录
- Nginx：在目标站点配置（例如 `/etc/nginx/conf.d/observability.conf`）引入 `/etc/nginx/snippets/ferry-observability.conf`，模板为 `conf/nginx-observability.locations.conf`；仅代理观测页面、`/api/observability/v1/*`、两份静态资源及 `/metrics` 到 `127.0.0.1:38080`，`/internal/observability/` 对外返回 404。保留既有 HTTPS 入口及无关业务路由。修改后先 `nginx -t`，再 `nginx -s reload`
- 新部署默认值仍为回环监听；如果仅使用 SSH 隧道，可配置 `OBSERVABILITY_HOST=127.0.0.1`、`OBSERVABILITY_COOKIE_SECURE=false`，通过 `ssh -N -L 38080:127.0.0.1:38080 <服务器>` 在本机打开。已有凭证不会因监听切换被重新生成
- 凭证初始化：`./bin/server obs credentials`，仅首次显示随机密码与只读 Token，密码仅保存 scrypt 哈希；私有配置 `conf/.env.observability`（0600、gitignored），已有文件不覆盖。主服务和观测服务均读取该配置，配置变更后需重启对应进程
- 托管配置：`conf/supervisor-observability.conf` 为独立 program 模板，避免新增服务时更新既有组导致 IM 整组重启；安装到 Supervisor include 目录后 `supervisorctl reread && supervisorctl update ferry-observability`。日常统一使用 `./bin/server start|stop|restart|status` 或 `./bin/server obs start|stop|restart|status`
- 机器读取：`GET /api/observability/v1/summary?window=24h`、`timeseries`、`breakdown`、`runs`、`schema` 与 `GET /metrics`；请求头 `Authorization: Bearer <独立只读Token>`。Web 使用单用户 Session；上报回执使用独立写 Token，只允许实际回环来源；不复用文件推送凭证
- 采集：白名单事件单写入器、4096 条有界队列、本地 `runtime/observability/events-*.jsonl`；默认保留 30 天，读端增量缓存最多 200000 条，达到上限明确提示不完整。该限制仅针对运行事件；pi 历史用量走下述独立账本，不导入聊天正文
- 可靠性：写盘失败或缓冲溢出不阻断 IM；尚未落盘的事件在异常退出时可能丢失。未闭合轮次显示运行中或未知，不补成成功。心跳超过 15 秒显示过期；`/healthz` 只说明观测 HTTP 存活，不能代表模型或渠道健康
- 运行事件统计边界：业务轮次、pi 尝试、可见模型响应、工具和渠道操作分开；Token 只累计可见 `assistant message_end.usage`，不重复累计 `agent_end`，缓存计费、隐藏压缩/HTTP 重试用量及费用未验证。回发成功只表示平台接受；首段文本不是供应商首 Token，pipeline 不含队列等待。分位数对保留样本计算，不能平均多个 P95；Prometheus counter 按生产进程生命周期累计，重启归零，历史查询不回填 counter
- 隐私：不采集聊天、思考、工具参数/结果、文件名/路径、原始报错；会话仅 HMAC 去标识化且不作 Prometheus 标签。既有业务日志和 pi transcript 的保存行为不变，不承诺整个系统不存正文
- 日志：`logs/`（不入库）；观测进程 `logs/observability.log`，异常排查避免输出凭证和解密密钥
- 告警通知、价格表与自动备份：尚未提供；当前无服务级可用性保证

## pi 历史 Token 用量

- 范围：仅 pi 原生 JSONL；`OBSERVABILITY_PI_USAGE_ENABLED=true` 开启，`OBSERVABILITY_PI_SESSION_DIRS` 为可选 JSON 目录数组（最多 16 个，禁止空路径），默认 `PI_CODING_AGENT_DIR/sessions` 或 `~/.pi/agent/sessions`。不扫描其他 Agent 数据，不调用模型，也不改变原生会话
- 同步：首次全量，随后按 inode/size/mtime/ctime 检查变化文件，默认每 30 秒；`./bin/server obs sync` 手动同步。Web 和 CLI 通过文件锁串行、持锁后加载最新 checkpoint，查询使用最后完成快照，避免慢扫描阻塞页面
- 账本：`runtime/observability/pi-usage/index.json`（0600，目录0700），只保存数字、模型、日期和 HMAC 标识；v2 checkpoint 兼容迁移 v1，原子替换并 fsync。历史不受运行事件的 30 天/200000 条限制，源文件删除后仍保留已采集用量；缓存损坏且源也已删除时无法重建该部分历史，应备份账本
- 去重：优先原生记录 ID + 时间 + 类型，缺 ID 时使用响应元数据，再缺则源文件哈希与偏移并标弱身份。HMAC 身份别名关联不同时间字段或缺 envelope 的同一记录；首次观察来源负责已知字段修正，副本仅补缺，避免缺 usage 副本清零或旧副本回滚。fork 复制和重复同步不重复加账；用户回合沿 parentId 树关联，工具循环/模型多次响应不增加用户轮次，不按可能跨目录复用的 session ID 粗暴合并
- 口径：`in` 未缓存输入、`cr` 缓存读、`cw` 缓存写、`out` 输出；总量为四项之和。`reason` 已含在输出中，不再相加；上报 totalTokens 不一致时告警。缓存读取率为 `cr/(in+cr)`（与参考一致，无分母为 null）。零 usage 保留，缺字段不当完整零值，费用字段不用于估价
- 请求：`req` 是唯一有 usage 的记录数，不是供应商 HTTP 次数；含 `assistantReq` 与 `compactionReq`（上下文压缩和分支摘要）。摘要请求增加用量但不增加用户轮次；无法关联用户的用量保留并标明轮次未知。总轮次独立去重，不相加跨日或跨模型的局部轮次
- 页面：7/14/30/90天、自定义（最多十年）、全部历史；概览、180/365天热力图、模型堆叠日趋势和缓存率、Top5+其他环图/排名/分项表。日界统一 Asia/Shanghai。热力图独立于主日期筛选；采集质量针对全部扫描历史，尚未采集与失败不能伪装正常零值
- API：`GET /api/observability/v1/usage?range=all`；custom 带 `start=YYYY-MM-DD&end=YYYY-MM-DD`，两端包含。`refresh=1` 请求后台同步，单飞且最小间隔 5 秒；沿用现有密码/Cookie/只读 Token，不能修改会话。`generatedAt/lastScanAt` 使用 Unix 毫秒，与运行接口的秒区分
- Metrics：`ferry_pi_usage_history_tokens{type="in|cr|cw|out|reason|total"}`、history_requests、history_sessions 以及 scan/quality gauges；它们是历史快照，不与进程 `ferry_tokens_total` 相加，不假装累计 counter。导出器失败不影响运行指标导出
- 核对：显式运行 `PYTHONPATH=lib/python .venv/bin/python tests/audit_pi_usage.py`，独立读取源元数据并与在线 API 对账；结果默认 `runtime/pi-usage-reconciliation.json`。测试脚本不写原生历史、不会实发消息

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
