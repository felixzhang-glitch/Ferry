# RELIABILITY

## 可靠性目标

- 个人单实例服务，无 SLA；持久化的 pi 映射、长期记忆、提醒与每日任务应能恢复
- pi transcript 与映射共同构成会话恢复条件，不能只保留 `pi-sessions.json`
- `pending_files`、消息队列、运行中任务是内存态，不承诺重启恢复；归档文件本身保留

## PWA 聊天运维

- 本地聊天署名用私有配置 `OBSERVABILITY_USER_DISPLAY_NAME`，默认“我”；用户名默认 `admin`，已有 `OBSERVABILITY_USERNAME` 不变。改署名后只需重启独立控制台，不重启主服务或微信；配置不入 Git，保留已有会话
- v0.13.0 起 `rules/system.md` 不再随源码发布；升级时先保留本机规则副本，Git 更新可能移除旧跟踪路径，需恢复到被忽略的同名位置。新克隆按需创建本地 system/admin 规则，缺失时提示并跳过，不自动生成个人规则

- App 外观单独持久化在 `OBSERVABILITY_STORE_DIR/appearance/`，包括 0600 状态与归一化图标版本；变更原子提交，不存原始照片。与观测数据一起备份；坏状态不自动覆盖，读页面可回默认外壳，修改接口明确报错。新增 Pillow 用于安全解码图像，不依赖外部图片服务
- 自定义名称/图标不改变 manifest 的 id/scope/start_url。既有 iPhone 桌面安装可能不自动更新图标/名称，需要用户重新添加；主要操作的触感在 iOS 上依赖按钮内的透明 label 覆盖隐藏原生开关，并受系统触感设置影响，属于平台行为，将来的系统更新可能使其失效
- PWA 外壳升级不缓存 API／附件；更新后重新打开页面取得当前会话的控制台 CSRF。旧页面缺令牌时提示刷新，不自动重放写操作或强刷丢草稿。纯文本聊天输入旨在减少 iOS 表单辅助栏，是否隐藏及键盘间距必须使用目标 iPhone 和输入法验收，不能由 Chromium 模拟结果代替
- `./bin/server obs credentials ensure` 补齐独立 `OBSERVABILITY_CHAT_TOKEN`，不改登录密码；主服务与观测进程重启后生效。聊天数据默认 `runtime/server/web-chat/`，改路径用 `WEB_CHAT_STORE_DIR`；与 Pi 原生 transcript/映射一起备份，聊天 UI 历史不是 Pi 上下文恢复的替代品
- iOS 设备副本默认 `runtime/server/device-data/`（改路径用 `DEVICE_DATA_STORE_DIR`），0700/0600，与观测事件、聊天、memory/Dream 分离，不自动注入 pi。按分项保留旧记录，达到预算时提交被拒；需备份时单独打包该目录，恢复后仍按原设备标识查询。代理须按 `conf/nginx-observability.locations.conf` 放行 `/api/observability/v1/device-data/` 的 256KiB 并屏蔽 `/internal/device-data/`，旧 snippet 不更新时大批次会 413
- JSON 控制配置维持 4KB；聊天 turn 为 64KiB，附件每个 10MiB，Web turn 最长 600 秒；NDJSON 每 15 秒 heartbeat，代理 read timeout 700 秒。部署时需同步新版 nginx snippet，启用 chat 专用流式 location，并显式屏蔽 `/internal/chat/`
- 刷新/切换线程/断网只解除订阅，原 turn 在服务端继续；重新打开通过 active + events 取完整快照/序号恢复，不自动重发提示词。Stop、注销、删除、超时和主服务退出仍取消；重启后的在途轮次标 interrupted，不承诺续算。没有离线发送队列
- PWA、飞书、微信及 Web 每日任务共享 `PI_MODEL/PI_THINKING` 与控制台持久化覆盖；`WEB_PI_MODEL/WEB_PI_THINKING` 已移除，私有配置中残留值不再生效。设置修改与恢复默认从下一轮生效，当前轮及其自动重试保持进入时的参数；Dream 独立模型不变。最终显示与持久化使用成功轮次末条 assistant text，不拼接工具前正文；旧污染历史不自动删除
- 通知需显式设 `WEB_PUSH_ENABLED=true`、`WEB_PUSH_CONTACT` 为有效 mailto 或 HTTPS 联系地址，重启主服务；观测进程须允许回环校验登录有效性。`WEB_PUSH_STORE_DIR` 默认 `runtime/server/web-push/`，VAPID 私钥与订阅/outbox 都须备份，不能只恢复订阅而换密钥。iPhone 需 HTTPS、主屏幕安装及用户主动授权
- 通知是尽力投送：32 个订阅、512 个待发、24 小时有效期与最多 8 次尝试，429/5xx 退避、404/410 清理失效订阅；每线程待转交元数据最多 256 条，超量只保留最近通知，聊天正文不受影响。前台已读回复不提醒，短可见租约只延后投送，避免后台切换丢失时误吞通知；提醒/简报按订阅开关投送。系统专注模式可能阻止显示。通知点击回会话，锁屏不发正文
- `/remind` 与 `/daily` 支持 PWA；结果独立保存在目标会话，不进入 Pi 原生历史，无推送授权也可查看。Web 每日任务落库失败保留已生成答案再投送，不重复生成；删除会话会停止关联任务，启动时清理已删除会话的残留调度
- 存储损坏不静默覆盖，启动不可用时聊天 API 返回未启用；其他 IM 不受影响。异常日志只写 error 类型，不打印上传文件/聊天内容

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

### Dream 记忆整理

- 定时 `DREAM_TIME`（默认 02:00，`FERRY_TIMEZONE`）整理，`DREAM_REPORT_TIME`（默认 08:00）推送报告；两者都在到点后 6 小时内重启时补跑 / 补发，同日不重复。待发报告存在 `state.json` 的 `pending_report`，重启不丢
- 控制台改时间、开关或推送目标即时生效（唤醒调度循环重新计算），无需重启；改动持久化在 `runtime/server/console-settings.json`，启动时在 `conf/.env` 之后加载并优先；删除该文件即回到 `.env`
- 失败（pi 报错 / 超时 / 输出不是 JSON）不改记忆、不推进 watermark，失败通知随报告推送；修复后发 `/dream` 或在控制台点「立即整理」即可从同一 watermark 继续
- 邮件发送失败（授权码缺失、SMTP 拒绝）只记日志，不影响其他推送目标；授权码文件为 `MAIL_ENV_FILE`
- 整理期间某类别被对话修改：跳过该类别，watermark 不推进，下次运行重抽；已生效的其他类别仍单独提交
- 误记：发 `/memory undo` 回滚最近一次 Dream 提交；之后记忆又被改过导致冲突时自动 `revert --abort`，按 `docs/memory.md` 的 `mgit` 别名手工 `mgit revert <dream 提交>` 或直接编辑文件
- 审计：`runtime/server/dream/run-*.json` 保存每次的范围（`scope: all|chat`）、变更、evidence、被拒 op 与模型原始输出；`state.json` 保存全局 `watermark`、各对话 `chat_watermarks` 与最近一次结果。删除 `state.json` 等于从"过去 7 天"重新开始，靠去重收敛不会重复写入
- 漏读排查：Dream 只认 `pi-sessions.json` 与 `pi-session-history.jsonl` 里的聊天会话；若有会话 transcript 不在两者中（台账上线前被 `/new` 掉的），先停主进程（`supervisorctl stop ferry-stack:ferry`，`DreamRunner` 状态在内存里），备份后往台账补 `{at, session_key, session_id}`、把 `state.json` 的 `watermark` 回退到需要重读的时间，再启动并「立即整理」
- 隔离：Dream 走独立 CLI 子进程，不占 worker 池、不计入熔断，不会因为整理失败影响白天对话

### 控制台

- 控制台是观测进程（38080）的一部分；主服务不可达时监控页照常可用，控制页显示「主服务不可达」并在恢复后自动刷新
- 登录会话持久化在 `runtime/observability/console-sessions.json`（只存哈希），观测进程重启不掉线；需要强制全部下线时删除该文件后 `./bin/server obs restart`
- 控制台令牌缺失（老的 `conf/.env.observability`）时控制页提示未配置：执行 `./bin/server obs credentials ensure` 补齐 `OBSERVABILITY_USERNAME` 与 `OBSERVABILITY_CONSOLE_TOKEN`，再 `./bin/server restart`（主服务与观测进程都要读到同一个令牌）
- PWA 外壳由 Service Worker 网络优先缓存，发布新静态资源后前台打开即更新；如遇旧版本，iOS 上删除桌面图标重新添加即可

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
