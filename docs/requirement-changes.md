# 需求变更记录

> 本文件稳定维护：每次需求变化（新功能、行为调整、架构决策变更）在此追加一条记录。
> 格式：日期 + 版本/提交 + 需求内容 + 影响范围。新记录添加在最上方。

## 2026-09-25 · 可观测模块文档补齐

- 需求：核查最新私有可观测看板与 pi 历史用量是否已同步文档并推送 GitHub。代码与运维说明已随 `89e22e6` 推送，但索引类文档（AGENTS 代码地图、架构、产品清单、测试映射、安全）仍停留在该模块之前
- 改动：`AGENTS.md` 新增 `observability/` 一级模块与 `./bin/server obs ...` 命令；`docs/architecture.md` 补技术栈行、埋点与账本分层图、目录结构、独立进程部署及「可观测与用量账本」小节；`docs/PRODUCT.md` 新增看板与 pi 历史用量两项功能及观测非目标；`docs/TEST.md` 补两条测试要点与四行用例映射；`docs/SECURITY.md` 补看板 / 只读 / 上报三类鉴权、观测隐私与三条已知风险；`docs/index.md`、`README.md` 项目结构、`docs/QUALITY_SCORE.md` 复评记录同步
- 边界：纯文档改动，未触碰运行代码、配置与线上进程；不写真实域名、凭证、会话数量与 Token 分项；`conf/.env.observability`、`runtime/observability/`、`logs/`、`tests/` 保持 gitignored

## 2026-09-24 · GitHub 提交前隐私脱敏

- 需求：检查数据并脱敏后推送 GitHub，仅发布代码、无凭证配置模板和可公开的功能/测试记录
- 改动：公开 README 与运维文档改用保留示例域名，删除变更记录中的真实会话数量、Token 分项、活跃日期、缓存率和部分历史运行 PID；保留实现口径与核验结论。Supervisor 配置明确标注为需替换安装路径/账号的模板
- 边界：真实凭证、会话及用量账本、截图、核验结果、日志和本地测试继续留在 gitignored 目录；不修改线上密码、用量记录、代理和监听，不重写已发布 Git 历史
- 闸门：提交前核查暂存区隐私路径、当前凭证匹配及仓库扫描器；推送前逐提交预检并保留 pre-push 钩子，不使用强推或跳过检查

## 2026-09-24 · pi 全历史 Token 看板（对齐 dsh-panel）

- 需求：参考 `dsh-panel` 的 Token 消耗页，只收集 pi，并加入历史。已先核对参考源码并输出方案，再改造；参考仓库未改动
- 页面：将现有「用量」升级为独立 Token 页，含 7/14/30/90 天、自定义及全部历史、6 项概览、输入/缓存读/缓存写/输出、独立 180/365 天热力图、Top5+其他模型堆叠日趋势与缓存率、环图、排名及模型明细。UTC+8 日界，保留缺失与零差异，日期/刷新竞态隔离，不受 Ferry 运行筛选误导，不与运行事件 Token 相加
- 数据：新增 `observability/pi_usage.py` 与 `usage_cli.py`，只读 pi 原生 assistant usage、compaction usage、branch_summary usage；全量初导后按 stat/ctime 检查变化文件。总量按 input+cacheRead+cacheWrite+output，reasoning 已含 output；缓存读取率 cacheRead/(input+cacheRead)，费用不估算。原生 user 回合关联，工具循环不重复计轮次；上下文维护计消耗但不增加用户轮次
- 持久化：独立 `runtime/observability/pi-usage/index.json`（目录0700/文件0600，gitignored），不受运行日志30天或200000条上限影响。跨进程 flock、原子 fsync、v1→v2 保留归档迁移；HMAC 原来源与多身份别名去重，缺usage副本不清零、旧fork不回滚原来源修正，源删后保留历史；缓存损坏且源也已删除仍有不可恢复边界
- 接口与运维：`GET /api/observability/v1/usage?range=all`（custom 带 start/end，refresh=1 后台单飞），`schema` 补口径，`/metrics` 增独立历史 gauges。`server obs sync` 手动同步，Web进程后台每30秒检查；沿用38080/Nginx/密码及只读Token，无需变更模型主链路。仅重启观测进程；主服务和微信 PID 未变
- 历史核对：已使用独立原生元数据脚本与 HTTPS API 逐项核对会话、请求、轮次、活跃日、缓存率和 Token 分项，结果一致；升级账本后复核一致，重复同步不增加总量。真实数量、日期范围与用量仅保留在本机核验记录，不随公开仓库分发
- 验证：最终Python **877 passed / 5 skipped**（跳过的是主Python环境无Playwright的5项浏览器用例，另在隔离浏览器环境运行全部**6项通过**）；Node22 **33 passed**。包括追加后台同步、手动刷新、重启保持、跨日/跨模型轮次、fork复制、等长改写、丢字段副本、缓存恢复、旧版本迁移及无正文白名单。真实HTTPS页面验收历史总量、日期、自定义、180/365热图、趋势/环图/排名、320px、同步与退出；JSON与Prometheus历史用量一致
- 证据：`tests/audit_pi_usage.py` 为独立元数据核对；结果 `runtime/pi-usage-reconciliation.json`，截图 `runtime/pi-usage-desktop.png` / `pi-usage-mobile.png`，测试日志 `runtime/pi-usage-final-*-tests.log`。没有为了测试调用真实模型或向IM实发消息，不导入非pi历史，不保存prompt/content/思考/工具参数/路径

## 2026-09-24 · 可观测开放监听与 Nginx HTTPS 代理

- 需求：按用户要求改为监听 `0.0.0.0:38080`，检查并补齐 Nginx 对外代理；安全组由用户自行限制，不改安全组或防火墙
- 配置：`conf/.env.observability` 的 HOST 改为 `0.0.0.0`、COOKIE_SECURE 改为 `true`；密码哈希、只读 Token、上报 Token、HMAC key 均保持不变。通过 `server obs restart` 生效，未重启主服务和微信
- Nginx：新增 `conf/nginx-observability.locations.conf` 模板，由目标站点引入。复用既有 HTTPS 入口，代理页面、API、两份静态资源和 metrics 到 38080；内部上报路径对外 404，无关业务代理保持不变。配置和私有环境的回滚副本仅保留本地
- 验证：`nginx -t` 通过后 reload；监听与代理链路检查通过，HTTPS 页面/静态资源 200，未认证 API/metrics 401，Secure/HttpOnly/SameSite Cookie、退出失效和只读 Token 验证通过。桌面浏览器已验证目标 HTTPS 页面；全量 Python **801 passed**（4 项既有弃用警告）
- 访问示例：`https://observability.example.com/observability`（替换为自己的域名），入口端口 443；38080 是后端 HTTP 端口，不用于发送明文网页登录密码。新增子路径主页链接和代理路由回归，原有业务路由未替换

## 2026-09-24 · 私有可观测模块（38080 / 密码登录 / LLM 指标 / Supervisor）

- 需求：统计会话轮次、响应、尝试、Token、工具、队列与渠道结果，不展示聊天内容；独立 Web 38080，统一 server 脚本和 Supervisor 管理，随机生成私人登录凭证
- 实现：新增 `lib/python/observability/`，白名单事件经 4096 条有界后台队列写本地 JSONL，默认保留 30 天；增量读端提供总览、趋势、分组、最近轮次、schema 和 Prometheus。主服务采样资源、worker、任务与队列；两渠道、pi 统一解析器、定时任务接入埋点；微信 sidecar 回传无正文发送结果
- 口径：内部随机轮次 ID 与微信 transport 一次性关联；重试不重复计轮次，失败用量保留，无终态 usage 的尝试计缺口。模型筛选关联轮次阶段、Token 仅计目标模型。辅助传输与回发、定时生成与单次 agent 耗时分开；跨 Task 流式消费不跨上下文复用 ContextVar token
- 安全：默认 `127.0.0.1:38080`，未开放公网；scrypt 密码哈希、24h 服务端 Session、限流与 Origin 校验；只读 Token 与本机上报写 Token 分离。私有配置 `conf/.env.observability` 权限 0600、gitignored；明文密码不落盘，不传观测认证变量到 pi worker
- 管理：新增独立 Supervisor program `ferry-observability`、`bin/run-observability` 及配置模板；`bin/server` 真正代理全栈与 obs/wx 的管理命令，支持一次性凭证初始化。已通过统一 `server restart` 加载变更，三个程序 RUNNING，8080 与 38080 健康检查通过；原主服务和微信端口不变
- 验证：最终全量 Python **785 passed**（4 项既有 FastAPI on_event 弃用警告），Node 22 **33 passed**。实际 API 验证未认证 401、密码/Cookie/只读 Token、退出失效、读 Token 无法上报；系统 Chromium 验证登录退出、五页签、320px 无横向溢出、无 JS 错误和浏览器持久化凭证。截图在 gitignored runtime；未向真实 IM 实发测试消息或发起真实模型测试调用
- 边界：仅采集启用后的数据，不导入聊天历史；无 180 天聚合、费用估算、缓存计费、已读、渠道连接心跳或首段正文回发指标。读端最多 200000 条缓存，上限/丢弃/落盘异常提示不完整；不承诺崩溃零丢失，Prometheus counter 随生产进程重启归零。既有业务日志/pi transcript 不在本次正文裁剪范围

## 2026-09-23 · 推送前隐私闸门收口（.env 变体不入库 / tests 停止分发 / 误报白名单）

- **需求**：接入通用 `git-push` skill（真身 `~/.codex/skills/git-push/`，经 `~/.agent/skills/git-push` 符号链接进 omp 的唯一 skills 根；闸门脚本 `scripts/preflight.py`）后，处理它在本仓查出的三项：① `.env` 时间戳备份未被忽略，一次 `git add -A` 就会把真密钥推上公开仓；② `memory/README.md` 只是目录说明、无隐私数据，应正常分发且不再告警；③ 测试文件后续不再推送（用户确认范围为整个 `tests/`）
- **改动**：
  - `.gitignore`：`/.env` + `/conf/.env` 两条窄规则换成 `.env` / `.env.*` / `!.env.example` —— 任意层级的 `.env` 本体与一切变体（`.env.local`、`conf/.env.bak.20260921-172446` 这类时间戳备份）全部不入库，只放行 `*.example` 模板
  - `git rm -r --cached tests/` + `.gitignore` 追加 `tests/`：28 个已跟踪文件（27 个 `.py` + `wechat-sidecar.test.mjs`）取消跟踪，文件保留在本地磁盘，后续不再随仓库分发。`conf/pytest.ini` 的 `testpaths = ../tests` 不变，本地开发流程零影响
  - 新增 `.git-push-allowlist.txt`（入库共享）：`path:memory/README.md`；`fingerprint:061bec1…` 放行 `skills/zhihu/references/http-api.md` 里被误判成手机号的知乎 ContentID（11 位文章 ID，同值也出现在 `zhuanlan.zhihu.com/p/<ContentID>` 链接中；该目录第三方托管、升级会覆盖，故不改文件而是走白名单）
  - 文档同步标注 `tests/` 为本地目录：`README.md`（测试小节 + 目录树）、`AGENTS.md`（常用命令 + 不提交敏感信息，并写入 preflight 闸门与 allowlist 约定）、`docs/TEST.md`、`docs/architecture.md`、`docs/functional-tests.md`
- **影响范围**：`.gitignore`、新增 `.git-push-allowlist.txt`、`tests/` 的跟踪状态、5 份文档 + 本文件；业务代码与配置零改动。**已推送的历史里 `tests/` 与旧 `.env` 规则下的内容仍然存在**——取消跟踪不等于清历史，如需彻底移除要另走 `git filter-repo` + 强推
- **验证**：`git check-ignore -v` → `conf/.env` 命中 `.gitignore:6:.env`、`conf/.env.bak.20260921-172446` 命中 `:7:.env.*`、`runtime/.env.bak-2026*` 命中 `runtime/`、`tests/conftest.py` 命中 `:25:tests/`，而 `conf/.env.example` 未被忽略且 `git ls-files --error-unmatch` 确认仍被跟踪；`cd conf && pytest --collect-only -q` → **582 tests collected**（取消跟踪后本地用例收集不受影响）；`preflight.py --scope worktree` → A/C/D 三段全通过、B 段 0 ERROR，allowlist 生效后 0 ERROR / 0 WARN，D 段自动调用 `.qoder/hooks/secret_scan.py` 通过

## 2026-09-23 · skills 发现范围收敛（Ferry 只扫项目 `skills/`；omp 只留 `~/.agent/skills`）

- **需求**：① omp 的 skills 加载路径只保留 `~/.agent/skills`；② Ferry 注入 pi 的 skills 只允许读 `/data/app/Ferry/skills`，其它全局目录一律不读
- **Ferry 侧**：`app/skills.py` 的 `SKILL_ROOTS` 从 5 个根（项目 `skills/` + `~/.pi/agent/skills` + `~/.agents/skills` + `~/.claude/skills` + `~/.codex/skills`）收敛为**只剩项目 `skills/`**。顺带消掉一个真实隐患：原先 `os.walk` 会下探 `~/.codex/skills/.system/`，把 Codex 内置的 `skill-creator` / `skill-installer` / `openai-docs` / `imagegen` / `plugin-creator` 当成可用技能写进 pi 的 system prompt；现在这些根不再被读
- **omp 侧**：`~/.omp/agent/config.yml` 显式写入 `skills.enableAgentsUser: true`，其余来源全关（`enableAgentsProject` / `enableClaudeUser` / `enableClaudeProject` / `enableCodexUser` / `enablePiUser` / `enablePiProject` 均为 `false`）；`enabledProviders` 保持空数组，外部 CLI 的用户级根不入发现。实测 `omp read skill://notion-cli` 返回的来源头就是 `.agent/skills/notion-cli/SKILL.md`（该目录下 8 个符号链接即当前全部可用技能）
- **影响范围**：`lib/python/app/skills.py`、`tests/test_skills.py`、`docs/architecture.md`、`docs/functional-tests.md`、本文件；`/skills` 命令、首轮摘要注入与 `build_skill_summary` 的三个调用方（`channel/feishu/handler.py`、`channel/wechat/handler.py`、`core/agent/pi_cli.py`）签名与行为不变
- **验证**：`cd conf && pytest -q` → **582 passed**（`test_summary_supports_multiline_description_and_project_precedence` 拆成 `test_summary_folds_multiline_description`，多根优先级断言随需求删除；`test_project_root_points_to_actual_skills` 升级为 `test_only_project_skills_dir_is_discovered`，断言 `SKILL_ROOTS` 恰为项目 `skills/` 单根——改动前 5 根必失败）。真机冒烟：`build_skill_summary()` 只出 13 个项目技能，`lark-cli` / `notion-cli` / `pdf` / `daily-diet-log` 等仅存在于全局目录的技能全部消失。omp 侧用 14 个探针目录（用户级 `~/.agent|.agents|.claude|.codex|.pi|.omp/agent/skills|managed-skills` + 项目级 `.agents|.agent|.claude|.codex|.omp|.github|skills`）逐个 `omp read skill://<probe>` 跑出加载矩阵，探针已全部清除、目录状态已复原
- **边界**：omp 仍有两处不受 `skills.*` 开关管辖的残留来源——`~/.omp/agent/managed-skills`（auto-learn，实测会被发现，当前为空）与外部 CLI 的**项目级**根 `<repo>/.codex/skills`、`<repo>/.github/skills`（实测会被发现，Ferry 仓内均不存在；`<repo>/skills` 从来不是 omp 的根）。要彻底封死只能上 `disabledProviders: [codex, github]`，但那会连带停掉这两个来源的 context files / MCP / commands / hooks，爆炸半径超出本次需求，故未动

## 2026-09-22 · 延迟四项优化 + 观测缺口修复（首字前进度 / 限流不 sleep / 往返合并 / 超时收敛）

- **需求**：先分析近期会话定位耗时，再对 ①首字前静默 ②限流 sleep 退避 ③模型往返次数 ④超时上限 逐项优化，并补齐正在阻碍优化的观测缺口
- **定位**：依据本地运行日志和原生事件发现首字前等待、工具退避、模型往返和超时预算是主要延迟来源。真实样本量、时段分布和用量统计不在公开文档保存；下面保留改进方案与验证方法
- **观测缺口修复**：
  - `app/logging.py`：删掉硬编码 extra 白名单，改收 record 上所有非 `LogRecord` 内置字段（`json.dumps(default=str)` 兜底）。此前 `backend`、`feishu_stream_updates`、`feishu_streamed` 一直被静默丢弃——37 条 `pipeline.streaming` 无一能看出流式是否生效
  - `channel/feishu/client.py`：四个传输助手（`_post_authenticated_json` / `_post_authenticated_multipart` / `_get_authenticated_bytes` / `_post_json_with_retries`）在成功返回前统一打点 `duration_ms` + `attempt`，删掉 10 处各方法重复的成功日志；`update_markdown_card` 此前成功路径零日志（全日志 `feishu.update_card` 出现 0 次），现在自动获得打点。`upload_file` 的 `file_type`/`size` 经新增 `log_extra` 保留
  - `core/agent/pi_cli.py`：解析 pi 的 `tool_execution_start` / `tool_execution_end`（schema 实测于 0.84.2 + deepseek），新增 `pi.tool_call`（`tool`/`tool_detail`/`elapsed_ms`）与 `pi.tool_result`（`tool`/`duration_ms`/`is_error`），Ferry 侧首次有工具级耗时数据
- **① 首字前静默**：`AgentClient.chat_stream` 增 `on_progress` 回调（`core/agent/types.py` 定义 `ProgressCallback`），pi_cli 在工具开始/结束时推送或清除标签；飞书 `_stream_to_feishu` 改为 body+footer 渲染——**进入流式立刻建占位卡（`> 🌿 正在处理…`）不等首字**，工具运行时显示 `> 🔧 bash · <命令>`，正文开始后自动撤掉占位、定稿清空 footer。节流放宽为「间隔到 且（正文增长 ≥ `FEISHU_STREAM_MIN_CHARS` 或 footer 变化）」，否则纯 footer 更新会被字数门卡死。回调异常由 `_emit_progress` 吞掉并告警，进度提示不会掀掉整轮
- **② 限流 sleep 退避**：新增 `bin/zhihu-hot`（TTL 300s 缓存；命中直接返回并标注抓取时间；限流/失败回退上次缓存并标注原因；**任何情况都不 sleep、不重试**）。`zhihu-cli` 限流时 body 是 `{"Code":30001,"Message":"rate limit exceeded"}`，故按 body 的 `Code` 判定失败而非退出码。`rules/system.md` 热榜改走该包装，并新增「效率」段硬禁在 bash 里 `sleep` 等限流。未改 `skills/zhihu/`（第三方托管，升级会覆盖）
- **③ 往返次数**：`rules/system.md`「效率」段要求探查类操作合并成一条 bash、多文件一次读全，并写明每次往返约 1.2s 固定开销
- **④ 超时**：`PI_TIMEOUT_SECONDS` 默认 300 → 180（`config.py` + `conf/.env` + `.env.example`），deepseek-flash p90 25s 留 7 倍余量。`PI_IDLE_TIMEOUT_SECONDS` **保持 120 不动**——它是卡死检测器，实测最长合法静默工具 90.3s（且那次正是 sleep），压到 60s 会误杀正常的慢 curl / find
- **影响范围**：`app/logging.py`、`channel/feishu/{client.py,handler.py}`、`core/agent/{pi_cli.py,types.py}`、`app/config.py`、`conf/.env`、`conf/.env.example`、`rules/system.md`、新增 `bin/zhihu-hot`、Python 测试与 6 份文档。`channel/wechat/`、`lib/js/`、会话映射、记忆、队列、定时任务一行未改
- **边界**：微信通道无卡片可编辑，不接进度（否则会变成刷屏）；`FEISHU_STREAMING_EDIT=false` 仍可一键回退整段发送；`PI_PERSISTENT_ENABLED=false` 仍可回退逐轮 CLI；模型、thinking、cwd、transcript 均未动
- **验证**：全量 `cd conf && pytest -q` → **582 passed**（基线 577，新增 5 条：`_tool_execution` 用真实 pi 载荷解码、占位卡先于首字并渲染工具进度、`update_markdown_card` 必须走 PATCH 而非新建消息、formatter 保留任意 extra、formatter 序列化非 JSON 值）；JS 侧用 `PI_NODE_BIN` 的 22.23.2 跑 `pi-worker.test.mjs` + `wechat-sidecar.test.mjs` → **33 passed**（默认 PATH 的 20.19.4 会因 undici 缺 `markAsUncloneable` 失败 10/14，已在 `docs/functional-tests.md` 写明）。真实 pi + 真实 `PiCliClient`/worker 池 + 真实 handler + 记录型 feishu client 冒烟两次：第一次抓到 `pi.tool_call`(elapsed_ms 1662)、`pi.tool_result`(duration_ms 4039, is_error false)、`pi.stream` 带 `backend`、`pipeline.streaming` 带 `feishu_stream_updates:5 / feishu_streamed:true`，同时暴露占位卡带前导 `\n\n` 的缺陷；修复后第二次卡片序列为 `1ms CREATE "> 🌿 正在处理…"` → `1685ms UPDATE "> 🔧 bash · echo smoke-start; sleep 4; echo smoke-end"` → `5809ms UPDATE` 工具结束回占位 → `6621/6660ms UPDATE` 正文与定稿，footer 已清空。`bin/zhihu-hot` 三条路径实测：无缓存+限流 exit 1 并明示原因、10 分钟前缓存+限流回退 exit 0 标注 600s 前、TTL 内命中 0.336s 完全不打接口
- **生效**：已重启主服务并验证 worker 预热、飞书 WS 重连、健康检查及超时配置；微信 sidecar 未重启。真实 PID、启动计时和进程配置快照仅保留本地。未向真实 IM 会话发消息，飞书卡片进度的实际视觉效果需用户端确认

## 2026-09-22 · 切换到 DeepSeek 官方 deepseek-flash（新增 deepseek provider）

- **需求**：切换模型，新增 DeepSeek 官方端点（OpenAI 格式 `https://api.deepseek.com`）的 `deepseek-flash`（实际版本 DeepSeek-V4.1-Flash，1M 上下文、支持图文），`reasoning_effort` 配 `low`
- **改动**：
  - `conf/pi/models.json`：新增 `deepseek` provider（`baseUrl`/`api: openai-completions`/`apiKey: $DEEPSEEK_API_KEY`）与模型 `deepseek-flash`（`reasoning`、`input: ["text","image"]`、`contextWindow: 1000000`、`maxTokens: 16384`）。`deepseek` 是 pi 内置 provider，自定义条目按 id upsert，内置 `deepseek-v4-flash`/`deepseek-v4-pro` 保留；不写 `compat`，pi 按 provider id 自动判定 deepseek `thinkingFormat`、`max_tokens` 字段、不发 developer role、`supportsReasoningEffort: true`
  - `conf/.env`：`PI_MODEL=deepseek/deepseek-flash`、`PI_THINKING=low`、新增 `DEEPSEEK_API_KEY`；改前备份 `runtime/service-backups/env-before-deepseek-switch-20260922-202209`
  - `app/config.py` + `core/agent/pi_cli.py`：新增 `pi_deepseek_api_key`（`DEEPSEEK_API_KEY`），`_process_env` 按 DASHSCOPE 同样方式注入 pi 子进程与常驻 worker，密钥不进命令行、不进 `ps`；`tests/test_pi_session.py`、`tests/test_pi_multimodal.py` 的 settings 桩补同名字段
  - `conf/.env.example`、`docs/references/pi-cli.txt`（配置表 + DeepSeek 注册小节 + 推理强度差异说明）、`docs/routing.md` 凭证说明
- **边界**：百炼 provider 与 `bailian/*` 三个模型条目原样保留；回退只需 `PI_MODEL=bailian/qwen3.8-flash`（`PI_THINKING` 按需回 medium）+ 重启。渠道、会话、记忆、队列与业务规则代码未动
- **验证**：全量 `cd conf && pytest -q` → 577 passed；`sync_pi_models` 实跑同步成功且无未注册告警，`PI_OFFLINE=1 pi --list-models deepseek` 显示 `deepseek-flash` 1M/16.4K/thinking yes/images yes（内置两条并列）；隔离 agent dir + 本地 HTTP mock 抓到 pi 真实请求体：`model=deepseek-flash`、`reasoning_effort=low`、`thinking={"type":"enabled"}`、`max_tokens=16384`（无 `max_completion_tokens`）、角色仅 system/user、路径 `<baseUrl>/chat/completions`、图片以 `image_url` data URL 传入；真实 API 经生产 `PiCliClient`（常驻 worker）三轮：文本答「在线」、蓝色图答「蓝色」、同 session_key 续接答「在线」。证据 `runtime/deepseek-switch-verification.json`
- **生效**：已重启主服务并确认模型配置加载、双 worker 预热、飞书 WS 重连和健康检查；真实 PID 与进程配置快照仅保留本地，微信 sidecar 未重启。未做真实 IM 客户端实发

## 2026-09-22 · 飞书回复改为渐进式流式（微信不动）

- **需求**：飞书改成流式输出，随 pi 生成边出边显示，缩短"整段等待"的体感；微信通道保持不变
- **根因/现状**：`_stream_to_feishu` 虽消费 pi 流但攒完整段才一次性发卡片，用户在生成完成前无任何可见文本
- **改动**：
  - `app/config.py`：新增 `FEISHU_STREAMING_EDIT`(默认 true)、`FEISHU_STREAM_MIN_INTERVAL_SECONDS`(0.7)、`FEISHU_STREAM_MIN_CHARS`(40) 与 `feishu_message_url_template`
  - `channel/feishu/client.py`：`_post_authenticated_json` 支持 `method`；`reply_markdown` 返回卡片 message_id；新增 `update_markdown_card`(PATCH 覆盖卡片内容)
  - `channel/feishu/handler.py`：`_stream_to_feishu` 先发交互卡片占位、按"间隔+字数"节流 PATCH 更新、收尾定稿并单独补发图片；新增 `_active_stream_cards` 跟踪在途卡片，失败/取消时经 `_finalize_or_reply` 覆盖占位卡而非新发消息，避免残留半成品；`_safe_reply_with_images` 增加 `send_text` 开关
  - `conf/.env.example`：补充上述三项配置说明
- **边界**：`channel/wechat/handler.py` 一行未改；`FEISHU_STREAMING_EDIT=false` 可一键回退到整段发送
- **验证**：新增 `tests/test_feishu_streaming.py`（流式一卡多次更新、关开关回退单发）；升级 `tests/test_pi_chain.py` 的 FakeFeishuClient 支持卡片更新语义；全量 `cd conf && pytest -q` → 577 passed。真实飞书首字可见需线上发一条消息肉眼确认

## 2026-09-22 · 恢复 Qwen3.8 Flash 思考模式为 medium（修复思考外泄到正文）

- **需求**：用户反馈微信回复里仍出现"思考"（如"让我核实""设鸡x只…"的推理旁白），要求开启思考且思考不外发
- **根因**：`PI_THINKING=off` 时模型无私有思考通道，遇到需要推理的任务会把推理当正文（`text_delta`）输出，Ferry 过滤器只丢 thinking 事件、无法拦截合法正文，于是"思考"漏进回复；同时拉长输出显得更慢
- **改动**：仅将 `conf/.env` 的 `PI_THINKING=off` 改回 `medium`，其余不变
- **验证**：实测 pi `--thinking medium` 时推理走独立 `thinking_start/delta/end`（contentIndex 0），正文走 `text_delta`（contentIndex 1）；用 Ferry 真实 `PiCliClient.chat_stream` 路径跑"鸡兔同笼、只要答案不要过程"，组装正文仅 `鸡 23 只，兔 12 只。`，无任何元推理旁白
- **生效**：Ferry 外部执行 `supervisorctl restart ferry-stack:ferry`（新 pid 2291208，RUNNING）；微信 sidecar 未重启。未改业务代码，未做真实 IM 客户端实发

## 2026-09-22 · 关闭当前 Qwen3.8 Flash 思考模式

- **需求**：用户反馈响应仍慢，要求关闭 `qwen3.8-flash` 的思考模式
- **改动**：仅将 `conf/.env` 的 `PI_THINKING=medium` 改为 `off`，保留 `PI_MODEL=bailian/qwen3.8-flash`、常驻 worker 与原生会话；备份 `runtime/service-backups/env-before-qwen-thinking-off-20260922-095748`
- **验证**：隔离的真实 SDK + 本地 HTTP mock 确认 medium 会话续接后 off 覆盖生效，请求包含 `enable_thinking=false` 且无 `reasoning_effort`；真实 Qwen 调用成功，thinking 事件数 0、reasoning tokens 0。全量 Python 575 passed；证据 `runtime/qwen-thinking-off-verification.json`、`runtime/qwen-thinking-off-tests.log`
- **生效**：从 Ferry 外部执行 `supervisorctl restart ferry-stack:ferry`，进程 1615898 环境确认 `PI_THINKING=off`；两个 worker 预热正常，健康检查通过；微信 sidecar 2987879 未重启。未修改自重启机制或业务代码，未进行真实 IM 客户端实发

## 2026-09-21 · 常驻 pi SDK worker：消除每条消息的 Node 冷启动

- **需求**：先读取 AGENTS.md、给出完整方案后实施；复用 Node/pi 运行时降低响应延迟，允许完成后重启主服务
- **实现**：新增 `lib/js/pi-worker.mjs`，复用 Node 模块但每轮创建/释放原生 AgentSession；重新加载 AGENTS/规则/skills/记忆/时间，不把 system 内容写进 transcript。使用 pi 0.84.2 SDK 与原生图片处理，保留会话 ID/文件、模型/工具、重试和压缩；不用缺少动态 system 更新与精确 ID 创建能力的原生 RPC
- **编排**：新增 `core/agent/pi_worker.py` 有界池，默认 2 个 worker、启动预热、同 session 串行；关联 ID 校验、等待 `ferry_done` 才复用。取消/超时 SIGTERM 清理独立 bash 工具进程组后强杀兜底；崩溃/协议错误回收，后续补建；服务停止回收空闲与活动 worker
- **兼容**：`PI_PERSISTENT_ENABLED=false` 可回退 CLI；新增 Node/SDK 路径与池大小配置，不改变模型、thinking、cwd、映射或现有 transcript。修复外层流提前关闭未等待内部清理，以及 Python 3.10 `wait_for` 同 tick 读完时吞取消的竞态
- **观测**：增加 worker 启动、逐轮准备、首字耗时与 PID/复用标记；API Key 只走环境变量，不写入新增代码或命令参数
- **影响范围**：`core/agent/{pi_cli.py,pi_worker.py}`、`lib/js/pi-worker.mjs`、`app/{config,main,logging}.py`、`conf/.env.example`、Python/Node 测试与架构/会话/验收文档。渠道 handler、微信 sidecar 和业务规则不改
- **审查修复**：Linux 持续记录后代 PID/启动时间/PGID，强杀也清理独立工具组，防止 Node 卡死/崩溃时遗漏及 PID 复用误杀；补齐 pi 原生 HTTP 代理 bootstrap，复用 dispatcher、配置变更时有界释放旧连接；拿到 worker 后重建 system 输入，排队期间规则/时间变化下一轮立即生效
- **验证**：全量 Python 575 passed；Node 33 passed（SDK/代理 14 + sidecar 19），未跳过。真实本地 SDK/CLI 互续同一 transcript、跨轮同 PID、规则刷新、重置隔离、无 session_key 的临时调用、真实 detached bash 取消与关闭回收均通过；另已复现 Node 事件循环阻塞，SIGKILL 兜底仍能清理原生独立 bash 进程组。代理测试覆盖环境/全局配置代理、大小写优先级、NO_PROXY、配置更新/移除、连接复用与释放
- **测速**：最终版本、当前 `bailian/deepseek-v4-flash-0731` / `medium`，同短提示 CLI/worker 交替各 3 次：完整耗时中位数 4.289s → 1.295s，减少 2.994s（约 70%）；同 PID。隔离 localhost mock 去除模型网络后：CLI 中位数 2.768s，暖 worker 0.065s。仅是小样本短回复，不代表长任务总耗时同比降低；证据 `runtime/pi-worker-{real-verification,verification}.json`
- **上线**：2026-09-21 19:14（北京时间）`supervisorctl restart ferry-stack:ferry` 完成，主进程 3642650，worker 3643101/3643186；健康接口正常、飞书 WS 重连、无新增 ERROR，微信 sidecar 2987879 未重启。生产 localhost webhook 两轮均回复 OK，同 worker PID，第二轮准备 35ms、完整响应 1.444s；证据 `runtime/pi-worker-production-{verification,state}.json`。这是本地 webhook + 真实模型验收，不是两端 IM 客户端实发；现有用户会话未迁移/清空，`.env` 备份在 `runtime/service-backups/`

## 2026-09-17 · 入站图片多模态修复：走 pi 原生 `@file` 语法

- **需求**：当前模型（qwen3.8-flash）支持图片输入，但微信、飞书发图给机器人时模型「看不到」图片。微信只能单独发文字或图片、飞书支持图文混排，两种入站形式都要能正确把图片喂给模型
- **根因**：pi CLI 官方语法 `pi [options] [@files...] [messages...]` 中的 `@file` 才是多模态输入入口。此前两个渠道都只把图片**路径以文本形式**拼进 prompt（飞书 `[Feishu images saved locally]\n- /path`；微信 sidecar 把图片当 file 归档、只回「已收藏」且 handler 因 text 为空直接 `return []` 不唤醒模型），pi 需主动调 `read` 工具才可能读到，链路不可靠
- **改动**：
  - `PiCliClient.chat/chat_stream` 新增 `image_paths` 参数，`_build_command` 在 prompt 前插入 `@{abs_path}` 位置参数；新增 `_resolve_image_paths`（去重、展开 `~`、转绝对路径、过滤不存在文件）；`AgentClient` 协议同步
  - 飞书 `_build_user_text` 改为返回 `(user_text, image_paths)`，移除路径文本拼接，单张下载失败降级为 `[图片下载失败: key]` 注释不阻断；`_run_llm_job`/`_stream_to_feishu` 透传 image_paths
  - 微信 sidecar 拆分 `collectImageItems`(type=2) 与 `collectFileItems`(type=4/5)；新增 `handleImageItems` 静默归档（不发「已收藏」）；`postToCodexClaw`/`handleInbound` 新增 `images` 字段透传
  - 微信 handler `WeChatTextMessageEvent` 新增 `images` 字段；`_parse_event` 放宽校验（text/files/images 三者有其一即可）；图片-only 消息用兜底文案「用户发送了一张图片。」唤醒模型并传 image_paths
  - 【二次修复】`pi_stream_read_limit_bytes` 256KB→32MB：pi `--mode json` 会在 `message_start/update/end` 多行回显图片 base64（原图×1.33），大图单行远超 256KB，`asyncio readline` 招 `LimitOverrunError` 被 handler 兜底为「服务繁忙」。同步改 `config.py` 默认值、`conf/.env(.example)`、`test_config.py` 断言
  - 【三次修复・仅微信】sidecar `decryptMediaBuffer` 支持纯 32 字符 hex 密钥：微信图片 `image_item.aeskey`（顶层）是 16 字节密钥的**纯 hex 字符串**，而旧逻辑只处理文件用的 `base64(hex)`（44 字符）；图片密钥走 base64 解码得 24 字节、再 utf8 回退成 32 字节，触发 `unexpected aes key length: 32`，归档失败→images 为空→webhook 400。新增分支：入参匹配 `/^[0-9a-fA-F]{32}$/` 时直接 hex 解码为 16 字节（纯增量，不影响文件路径）
- **影响范围**：`core/agent/{pi_cli.py,types.py}`、`channel/feishu/handler.py`、`channel/wechat/handler.py`、`lib/js/wechat-sidecar.mjs`、`app/config.py`、`conf/.env(.example)`、`tests/{test_pi_multimodal.py(新增9),test_handler_single_reply.py,test_wechat_handler.py(+3),test_inbound_file_session.py,test_config.py,wechat-sidecar.test.mjs(+2)}`
- **验证**：全量 483 passed + Node 19 passed；`pi @img` 冒烟正确识别；重启后经微信 webhook 发 **201KB 大图**（base64≈275KB，超旧 256KB limit、修复前必失败），模型正确识别图片内容、status_code=0、无 LimitOverrunError（飞书侧共用同一 readline 逻辑，limit 提高后同样生效）；微信 hex 密钥修复经新增 Node 单测验证（旧逻辑复现 `key.length=32` 报错、新逻辑得 16 字节），已经真实微信发图端到端确认成功
- **不改动**：出站路径（微信发图/飞书 post 富文本回复）、文件归档链路（type=4/5）、`push_file` API
- **教训**：首次验证只用了 128KB 小图（base64 172KB < 256KB 侥幸未超限），未覆盖大图；且微信验证走了直连 webhook（带 images 字段）、**绕过了 sidecar 归档解密段**，漏掉了 hex 密钥 bug。多模态修复必须：①用足够大的真实图片；②走完整真实链路（微信要经 sidecar，不能直连 webhook）

## 2026-09-15 · rules/AGENTS.md 更名 rules/system.md + 规则缺失告警

- **需求**：消除与根 `AGENTS.md`（开发文档）的撞名。`AGENTS.md` 之名源于 opencode 时代 `{work_dir}/AGENTS.md` 原生加载，opencode 移除后已无用途
- **改动**：文件更名（内容零变化）；`pi_cli._system_prompt_files` 切换路径并新增缺失告警（此前文件缺失会静默跳过，规则无声失效）；`app/memory.py` 记忆协议、`skills/memory/SKILL.md` 与文档同步
- **影响范围**：`rules/system.md`、`core/agent/pi_cli.py`、`app/memory.py`、`tests/test_pi_session.py`（+2 防护测试）、`README.md`、`AGENTS.md`、`docs/{memory.md,references/pi-cli.txt}`
- **验证**：全量 471 passed；重启后系统提示四件套（system.md / admin.md / memory-context.md / 时间块）齐全

## 2026-09-15 · 时间感知修复：相对日期预计算 + 禁止复读历史表述

- **需求**：用户报告「时间又混乱了」——微信会话中模型把"明天生日"复读成 4 天前（9/11）当时正确的"下周三"
- **排查**：注入机制无误（本轮注入的时间完全正确，模型亦承认"是换算错不是注入错"）。三处结构缺口：①历史里的相对时间表述不会自动过期；②旧 hook 时代（opencode 插件 `inject-time.js`）的 `<system-context>` 前缀持久化在会话历史（飞书 25 处 / 微信 2 处），持续提供旧锚点；③注入只给"现在"，"明天/周几/下周X"全靠心算
- **改动**：`app/clock.py` 注入块升级为四行（当前时间 / 相对日期 / 本周·下周范围 / 强化 directive：禁止心算、历史时间戳与相对说法一律过期不得复读）；`rules/system.md` 时间感知段同步（含 `date -d` 兜底）
- **影响范围**：`lib/python/app/clock.py`、`rules/system.md`、`tests/test_clock.py`（+2 用例：9/15 场景、跨月跨周边界）
- **验证**：全量 469 passed；生产 settings 构建的命令确认四行块进入 `--append-system-prompt`
- **遗留**：两个活跃会话里 27 处旧 `<system-context>` 时间块未清洗（用户选择保留），由新 directive 压制

## 2026-09-15 · 模型切换 deepseek-v4.1-flash + 注册表入仓自动同步

- **需求**：用户要求「conf/.env 为唯一配置源」，改 `PI_MODEL` 即切模型。排查发现两处独立配置各自为政：`.env` 只决定 `--model`；`~/.pi/agent/models.json` 决定元数据与可用性且靠手工维护——只改 .env 不生效，未注册模型会被 pi 以 "custom model id" 警告后透传（元数据退化为默认 128K）
- **注册表入仓**：`conf/pi/models.json.example` 升级为正式版本化 `conf/pi/models.json`，收录新上线的 `deepseek-v4.1-flash`（1M / 16.4K / reasoning / 多模态）与 `deepseek-v4-flash-0731`（保底回退）
- **启动自动同步**：`bin/server` 新增 `sync_pi_models`，挂入 `bin/run-app` 与 `start_app` 双路径；字节对比后有差异才写 `~/.pi/agent/models.json`，写前备份 `models.json.bak.时间戳`；`PI_MODEL` 未注册启动告警；同步失败只告警不阻断
- **切换流程**：此后切模型只改 `conf/.env` `PI_MODEL` + `supervisorctl restart ferry-stack:ferry`；加新模型只改 `conf/pi/models.json`
- **影响范围**：`conf/pi/models.json`（新增）、`bin/{server,run-app}`、`conf/.env(.example)` 注释、`tests/test_server_startup.py`（+4）、`README.md`、`docs/{routing,functional-tests,PRODUCT,TEST}.md`
- **验证**：全量 467 passed；重启日志出现同步记录、家目录文件与仓内一致（旧文件已备份）；`--list-models` 双条目（v4.1-flash：1M/16.4K/thinking yes/images yes）；实跑无 custom model 警告、thinking 事件正常

## 2026-09-15 · 配置与仓库清理：.env 收敛 + .qoder 忽略 + 死文件清理

- **需求**：v0.8.0 收敛唯一后端后，清理四后端时代残留，消除误导
- **conf/.env**：删除 21 个零引用死键（`CODEX_*`/`CLAUDE_*`/`QODERCLI_*`/`OPENCODE_*` 后端项与 `ACTIVE_BACKEND`、`BACKEND_STATE_PATH`、`MAX_HISTORY_ROUNDS` 等）；7 个仍生效的旧别名改新名（`CODEX_WORK_DIR` → `PI_WORK_DIR=./runtime/codex-workdir/pi`）。注意：`CODEX_TIMEOUT_SECONDS=180` 早已失效，pi 实际超时为默认 300s
- **文件清理**：`runtime/server/{backend.json,opencode-sessions.json}`、`runtime/codex-workdir/{claude,opencode,qodercli}`、`conf/runtime`、`conf/.pytest_cache`、`conf/opencode` 空壳、旧「每日股市摘要」cron 定义（claude 后端已无执行通道）删除；`runtime/.env.bak-20260915` 备份保留；`conf/pytest.ini`、`conf/requirements.txt` 经引用检查为在用，保留
- **仓库**：`.qoder/` 整目录加入 .gitignore 并取消跟踪（不再随 GitHub 分发）；`docs/ferry-analysis.html` 旧分析快照删除
- **影响范围**：`conf/.env`（gitignored）、`.gitignore`、`docs/index.md`；无代码改动
- **验证**：清理前后 .env 解析的生效配置程序化对比零差异；全量 463 passed；重启后服务健康

## 2026-09-11 · README 更新与重启记录

- **需求**：按当前单 pi 架构更新 README，补充部署方式、命令、配置迁移、测试与运维说明
- **运行记录**：用户授权后使用 `supervisorctl restart 'ferry-stack:*'` 重启主服务和微信 sidecar，两者均为 RUNNING，`/healthz` 返回正常；未执行真实模型或渠道实发冒烟
- **影响范围**：`README.md` 与本记录；说明 Supervisor 和脚本 PID 管理不可混用，移除旧的未重启描述。本次仅更新文档，不修改代码或真实配置

## 2026-09-11 · v0.8.0 · 单 pi 收敛

- **需求**：Ferry 仅保留 pi 后端，删除其它 CLI 实现与多后端路由；保留飞书、微信既有文件 / 图片 / 定时 / 记忆 / 队列功能，不清用户运行数据、不修改真实配置、不重启上线
- **接口与职责**：`app.main` 直接实例化 `PiCliClient`，无 router / `active_backend`；异常与轻量 `AgentClient` Protocol 归 `core.agent.types`，skills 发现归 `app.skills`。`SessionManager` 只管理 key 与 `pending_files`，不保存历史、独立桥接 UUID 或手工压缩；两个渠道每轮仅传当前 user，pi 自管历史与压缩
- **命令行为**：`/backend`、`/pi` 只读显示唯一 pi 状态；旧后端命令提示已移除；`/compact`、`/compress` 仅提示由 pi 原生管理，不能伪报压缩成功；`/new`、`/reset` 清附件及当前 pi session 映射，旧 pi transcript 保留
- **迁移兼容**：后端参数统一 `PI_*`，旧 `CODEX_*` 共享键仅作迁移回退，新键优先。`PI_WORK_DIR` 是最终 cwd，未设置时取旧 `<CODEX_WORK_DIR>/pi`，默认仍为 `./runtime/codex-workdir/pi`；不能照抄旧父目录或重复追加 `/pi`。`GENERATED_IMAGES_DIR` 替代 `CODEX_GENERATED_IMAGES_DIR`，保留旧键回退；渠道、记忆与 provider 凭证等共享配置继续保留
- **数据边界**：现有 pi cwd、`PI_SESSION_STORE_PATH`、`PI_CODING_AGENT_DIR` 及其会话文件都不能自动迁移。后端选择状态不再读写，但旧状态、其它后端目录、用户附件、记忆不自动删除；恢复 pi 会话必须同时保留映射与对应 cwd 下的 transcript
- **影响范围**：入口 / 配置 / 命令、`core/agent`、删除 `core/codex` 与其它 CLI 实现、`app/skills`、会话管理、双渠道 handler、定时任务装配、启动检查、配置模板及相关测试；文档 `README.md`、`docs/` 当前架构与回归说明同步。`routing.md` 精简但保留链接，references 只留 pi / 渠道参考，旧 HTML 分析仅标记历史快照，以下历史记录保留原文
- **pi 可靠性**：会话重置后旧请求不能回写覆盖新映射；每次 CLI 尝试的总时限覆盖持续输出、进程退出及 stderr 等待，保留独立空闲超时；超时、任务取消、关闭流时回收进程，服务关闭时终止活动进程；即使出现 `agent_settled`，无有效输出仍判失败
- **验证**：`.venv/bin/python -m pytest -c conf/pytest.ini -q`：463 passed；Node sidecar：16 passed。覆盖双渠道到 pi 的 mock 链路、原生会话重置/隔离/重启续接、附件与配置兼容、持续输出总超时、EOF 后挂起及真实本地子进程取消；脚本语法、旧模块引用与当前文档本地链接检查通过。服务固定 Python 解释器亦完成全量回归：463 passed；独立代码复核未发现阻断或高影响回归
- **交付边界**：本机 pi 0.84.2 的版本与 CLI 参数已只读核对；未调用真实模型、发送渠道消息或重启服务，线上冒烟仍待执行。保留既有 FastAPI `on_event` 弃用警告，不在本次扩大改造范围

## 2026-09-08 · v0.7.1 · 时间注入改走 system 通道 + 四后端全覆盖

- **需求**：用户报告「时间注入偶发失效」。排查 pi 两个主会话共 348 轮，**注入覆盖率 100%、一条不缺**，所以失效不在投递而在消费：微信主会话 2026-09-06 11:45（周日中午）注入正确，模型却说"今晚陪你到这儿""今天早点休息"，被用户质问后才回查纠正
- **四个成因**：①时钟拼在 user 消息首行，不是 system 通道，长对话里被话题带走；②pi 把 user 文本写进自己的 transcript，时间戳逐轮累积——微信主会话 1118 条事件里攒了 **327 个** `当前系统时间`，模型找"现在"时有几百个同格式候选；③两次 compaction（8/7 `tokensBefore=112641`、8/14 `111738`）的摘要里留着 `2026-08-04`、`2026年7月` 这类旧绝对日期却没有"今天几号"，形成竞争锚点；④只给 `11:45:24` 不给时段，"这是中午不是晚上"要模型自己换算，9/6 那次栽的正是这一步
- **时钟搬到 system 通道**：`pi_cli._build_command` 追加一个 `--append-system-prompt <时间文本>`（该参数值可传文本），`_build_native_prompt` / `_build_prompt` 不再拼首行。依据是本文档 `references/pi-cli.txt` 已验证的两条性质：注入的 system prompt **不参与压缩**、pi 每轮一个新进程 ⇒ 每轮全新、不落 transcript、不被折进摘要，②③两个成因直接消失
- **时段免推导**：新增 `lib/python/app/clock.py`，输出 `当前系统时间: 2026-09-06 11:45 周日（中午）` + 一句时段词硬约束。时段边界 0-4 凌晨 / 5-8 早上 / 9-10 上午 / 11-13 中午 / 14-17 下午 / 18-22 晚上 / 23 深夜。顺带去掉秒，模型用不上
- **补齐后端缺口**：`claude_cli._build_prompt`（claude + qodercli）与 `core/codex/client.py::_build_prompt` 此前**完全没有时间注入**，而 `rules/AGENTS.md` 却写着"每轮会自动注入时间、优先使用注入时间"——模型被告知自己有时钟，于是自信地编日期。两者改为在 prompt 首行内联同一个 `time_context()`；这两家每轮无状态、历史由 Ferry 用原始消息 FIFO 拼接，内联不会像 pi 那样累积
- **opencode 同步格式**：`hooks/inject-time.js` 的注入文本对齐新格式（含时段与硬约束），通道不变——它在 `chat.message` 里推 synthetic part，实测有效（opencode.db 里 754 条注入痕迹）
- **规则纠偏**：`rules/AGENTS.md` 时间感知段原来只描述了 hook 一条路径（对 pi 是错的），改为如实列出各后端通道，并加两条：时段直接用不要自己换算、只有本轮注入的才是"现在"（历史 transcript 里仍残留改动前累积的旧时间戳，这条要顶一段时间）
- **影响范围**：`lib/python/app/clock.py`（新增）、`core/agent/pi_cli.py`、`core/agent/claude_cli.py`、`core/codex/client.py`、`hooks/inject-time.js`、`rules/AGENTS.md`；文档 `README.md`、`docs/PRODUCT.md`、`docs/core-beliefs.md`、`docs/references/pi-cli.txt`。`PiCliClient._time_context()` 删除，无外部调用方
- **验证**：`tests/test_clock.py`（时段边界 + 文本格式）、`tests/test_pi_session.py`（时钟在 `--append-system-prompt` 里、user prompt 不再含时钟）、`tests/test_claude_cli.py`（prompt 首行带时钟）；`cd conf && pytest -q` 全绿

## 2026-09-07 · v0.7.0 · 双向文件通道打通 + 入站文件进会话

- **需求**：用户要求「从微信/飞书让 Ferry 把一个文件发出来」。实测飞书成功、微信失败，排查后确认飞书那次是 agent 绕过 Ferry 自己 curl OpenAPI 成的（Ferry 自身两个渠道都发不了文件），微信则是 sidecar 出站只有文本
- **微信出站文件**：`wechat-sidecar.mjs` 补齐 iLink 官方四步链路 —— `getuploadurl`(`media_type=3`) → AES-128-ECB 加密 → **POST** 密文到 `novac2c.cdn.weixin.qq.com/c2c/upload`（下载凭证在响应头 `x-encrypted-param`，不在 body）→ `sendmessage` 发 `type:4` file_item。新增 `POST /send_file` 路由
  - 协议依据：腾讯官方 npm 包 `@tencent-weixin/openclaw-weixin` v2.4.8 的 `messaging/send.js`、`cdn/upload.js`、`api/api.js`，逐字段对齐
  - 三个易错点已写进代码注释与 `docs/channels.md`：`sendmessage` 的 `type` 是 **4**（MessageItemType.FILE）而 `getuploadurl` 的 `media_type` 是 **3**（UploadMediaType.FILE），两个枚举数值不同；`aes_key` 是 base64 包裹的 **hex 字符串**（与入站解密侧对称）；`len` 是明文字节数的字符串
  - AES-ECB 是 iLink CDN 线格式强制的，不是本地选型，代码里注明禁止"升级"成 GCM
- **飞书原生发文件**：`client.py` 新增 `upload_file`（`im/v1/files`，`file_type` 按扩展名映射 pdf/doc/xls/ppt，其余 `stream`；mp4/opus 刻意不映射，因为那两个还要求 `duration` 字段）+ `send_file`（`msg_type:file`）。此前 agent 只能自己读 `conf/.env` 里的凭据手搓 multipart
- **统一入口 `POST /push/file`**：`{channel, to, path, caption?}` 一个接口管两个渠道，agent 只需学一次。飞书 `receive_id_type` 按 `ou_`/`on_`/`oc_` 前缀自动识别。`caption` 先于文件发出
  - **安全**：服务绑 `0.0.0.0` 且机器在公网，一个能把任意本地文件发到指定会话的接口不能裸奔，故强制 `PUSH_API_TOKEN`（`hmac.compare_digest` 比对，编码成 bytes 以免非 ASCII 头退化成 500）；未配置直接 503 关闭，不降级为免鉴权
  - 错误分档：400 参数/空文件、401 令牌、404 文件不存在、413 超限、502 上游失败、503 未配置
- **入站文件进会话**（本次核心缺陷）：两个渠道此前都是「归档 → 回执 → 提前 return」，而 `append_round` 全仓 3 个调用点都在 `_run_llm_job` 内，文件消息永远到不了，agent 完全不知道用户发过文件，用户只能自己把归档路径粘回对话
  - **为什么不能直接写 `append_round`**：pi 用原生 session，`_build_native_prompt` 只取最后一条 user 消息，其余历史在 pi 自己的 session 文件里，写进 Ferry 的 `rounds` 对 pi 后端完全无效
  - **改为通知队列**：`SessionManager.pending_files` + `note_incoming_file`/`take_pending_files`，归档时排队、用户下次开口时由 `apply_pending_notices` 注入该轮 user 文本，对所有后端一致生效
  - **到达时刻意不唤醒 agent**：唤醒即意味着它会去读文件；用户发个文件存档不该被自动展开分析（白烧一次配额）。通知头部写明「仅当本轮确实需要时才读取」
  - 边界：文件+文字同发合并为一轮（微信此前会直接丢掉文字）；纯文件消息不跑 LLM、webhook 返回空 `replies`；任务被拒时不 drain，留给下一轮；命令轮不消费通知；归档失败不排通知；队列上限 20；`/new` `/reset` 清空；内存态，重启丢失
- **缺陷修复：微信入站图片被静默丢弃** —— `firstText` 只认 type 1/3，`collectFileItems` 只认 type 4/5，**type 2 两边都不管**，用户发图片给 bot 得到彻底沉默。现已纳入归档（`image_item` 解析链 + `.jpg` 兜底名）
- **安全修复：日志明文泄露文件解密密钥** —— `archiveFileItem` 曾把完整 raw item 打进日志，`encrypt_query_param` + `aes_key` 齐备即可从微信 CDN 下载并解密原文件；日志还是 644 全局可读。改为 `describeFileItem`：**保留字段名**（当初打日志就是为了校准 iLink schema，这个用途不丢）、掩掉凭证值，实测 2000 字符降到 371
  - **umask 绕了两道弯**：先在 `[supervisord]` 加 `umask=077` 无效 —— 查源码发现 `os.umask()` 只在 `_daemonize()` 里调用，而 supervisord 以 `-n` 运行，那段代码永不执行，属死配置，已回滚。正确层级是 systemd drop-in `/etc/systemd/system/supervisor.service.d/umask.conf` 的 `UMask=0077`，日志与其轮转产物均为 600
- **验证**：微信实发 txt + 中文文件名 txt 均 `code:0`；飞书实发拿到 `file_key`/`message_id`；404/401/400/413 实机分档正确；入站真机跑通（发文件 → `chunks=0` 不唤醒 → 问「我上一句是啥」时 agent prompt 里含通知且答对）；脱敏后日志正则扫不到任何凭证值。`pytest` 276 passed（新增 75）、`node --test` 16 passed（新增 16，仓库首次引入 JS 测试，用 Node 内置 runner 不加依赖）
- 影响：`lib/js/wechat-sidecar.mjs`、`lib/python/channel/feishu/{client,handler}.py`、`lib/python/channel/wechat/handler.py`、`lib/python/core/session/manager.py`、`lib/python/app/{main,config}.py`、`conf/.env`（新增 `PUSH_API_TOKEN`/`PUSH_FILE_MAX_MB`，权限收 600）、`conf/.env.example`、`rules/AGENTS.md`、`docs/{channels,functional-tests}.md`、`README.md`、4 个新测试文件；仓外 `/etc/systemd/system/supervisor.service.d/umask.conf`（新增）。上线需 `systemctl restart supervisor`
- **遗留**：出站图片/视频/语音（`type` 2/5/3）未做，CDN 链路可复用但图片还需缩略图字段；`pending_files` 未持久化；百炼 429 配额耗尽时回复被截断成「服务繁忙」是独立问题，未动

## 2026-08-19 · pi 升级 0.84.2 + 1M 上下文与 80% 压缩阈值

- **需求**：把 pi 升到最新稳定版，并把上下文窗口拉到 1M、自动压缩阈值改为窗口的 80%
- **pi 升级**：`npm install -g @earendil-works/pi-coding-agent@0.84.2`（0.83.0 → 0.84.2）。`engines: node >=22.19.0`，`/root/.local/bin/pi` wrapper 钉的 v22.23.2 满足，wrapper 未动
- **0.84.0 breaking change 已排查并实测**：`message_update` 改为只发 `assistantMessageEvent` delta、移除累积 `message` 与 `partial`。Ferry 不受影响，因为 `pi_cli.py` 本来只读 `assistantMessageEvent.text_delta.delta` 与权威的 `message_end.message`。实测 0.84.2 输出：`message_update` 中带累积 `message` 的为 0 个，`text_delta` 在场，用 Ferry 的解析器直接回放能拼出正确回复
- **1M 上下文**：`~/.pi/agent/models.json` 与 `conf/pi/models.json.example` 的模型条目补 `contextWindow: 1000000` / `maxTokens: 16384`。依据：百炼公告 DeepSeek-V4-Flash-0731「原生 1M 超长上下文，最大输出 384K」；官方 API 文档说 `max_tokens` 与 `thinking_budget` 合计上限 393,216。`maxTokens` 取 16384 = pi 旧隐式默认值，行为零变化，只为防止未来 pi 改默认值时静默漂移。`--list-models` 已从 128K 变为 1M
- **80% 压缩**：`~/.pi/agent/settings.json` 新增 `compaction: {enabled: true, reserveTokens: 200000, keepRecentTokens: 150000}`，仓内新模板 `conf/pi/settings.json.example`。pi 的公式是 `contextTokens > contextWindow - reserveTokens` → `1000000-200000 = 800000`（窗口 80%）。`keepRecentTokens` 从默认 20000 抬到 150000，因为那套默认是按 128K 窗口调的，搭 1M 会变成“压一次只剩 20k”。摘要输出预算 `min(0.8×reserveTokens, maxTokens) = 16384`，与现状一致
- **压缩机制已摸清**（写进 `docs/references/pi-cli.txt` 新增的“上下文自动压缩”一节）：两个触发点在 `AgentSession` 核心（`agent_end` 之后 + 发新 prompt 之前的 pre-prompt check），与运行模式无关，`--mode json` 同样生效；另有 overflow 分支会先压缩再重试。历史实测：微信主会话在 128K 窗口下以 111,616 为阈值触发过 2 次（2026-08-07 tokensBefore=112641、2026-08-14 111738），阈值公式已被真实数据验证
- **验证**：`pi --version` = 0.84.2；`--list-models` 显示 1M / 16.4K；新会话 json 冲烟（delta + message_end 形状与 0.83.0 一致）；0.83.0 写入的存量会话能被 0.84.2 恢复（用 timecheck:1 测试会话，stderr 无 creating a new session，cacheRead=1024）；无效 API key 仍 `EXIT=0` + `stopReason=error` + 401 errorMessage（退出码陷阱在 0.84.2 依旧）；`pytest -q --ignore=tests/test_feishu_ws.py` 188 passed
- 影响：`~/.pi/agent/models.json`、`~/.pi/agent/settings.json`（均已备份 `.bak.20260819`）、`conf/pi/models.json.example`、`conf/pi/settings.json.example`（新增）、`docs/references/pi-cli.txt`、`tests/test_pi_session.py` 与 `tests/test_pi_chain.py` 的版本标注。**无 Ferry 代码改动，不改 `conf/.env`，不需 supervisorctl 重启**（models.json / settings.json 由每轮新起的 pi 进程读取）
- **成本注意**：阈值从 111,616 提到 800,000 后，微信主会话（当前约 100k tokens/轮）会继续长到约 8 倍才压缩；按百炼 ¥1/百万 input tokens、缓存未命中估算，单轮 input 成本上限从约 ¥0.1 升到约 ¥0.8。需要限制开销时调高 `reserveTokens` 即可
- **遗留**：`/compact` 对 pi 后端仍是空操作（它压的是 Ferry 自己的 rounds，而 pi 路径只发最后一条用户消息），真要清上下文用 `/new` 或 `/reset`；本次未修，已记入 pi-cli.txt

## 2026-08-07 · 新增 pre-push 密钥扫描钩子

- **需求**：仓库是 public repo，需要一道 `git push` 前的自动闸门，防止 AK/SK、API Key、Token、私钥等敏感信息随代码推上 GitHub（尤其是模型生成内容的误提交）
- **实现**：`.qoder/hooks/secret_scan.py`（Python 3 标准库，零依赖）+ `.qoder/hooks/pre-push`（解析 git 传入的 ref，算出待推送区间），`install.sh` 把它挂到 `.git/hooks/pre-push`
- **扫描策略**：只扫本次待推送 commit 的**新增行**（`git diff --unified=0`），命中即 exit 1 硬阻断；fail-closed（扫描器异常退出、python3 缺失也阻断）；逆转开关 `SKIP_SECRET_SCAN=1 git push`
- **挂载方式**：刻意**不改 `git config core.hooksPath`**，而是在 `.git/hooks/pre-push` 写一个转发脚本。原因：`.git/hooks/post-commit`（Qoder AI tracker）已在使用，改 hooksPath 会让它静默失效
- **规则覆盖**：AWS/Azure/阿里云/腾讯云/火山、OpenAI/Anthropic/DashScope/智谱/Google/HF、GitHub/GitLab/npm/PyPI、飞书（app id / tenant token / 机器人 webhook）/微信/钉钉/Slack、Notion/Stripe/SendGrid/Twilio/Telegram、PRIVATE KEY 块 / JWT / 带口令连接串 / `Authorization: Bearer`、项目专属环境变量（`FEISHU_APP_SECRET` 等六个）、危险文件名（`.env`、`*.pem`、`id_rsa*`、`conf/wechat/account.json`、`rules/admin.md` 等）
- **降噪关键决策**：香农熵**不做独立规则**，仅用于关键字类弱规则命中后的二次确认（否则 lockfile 哈希/UUID 大面积误报）；含非 ASCII 字符的值一律当占位符（实测修正了 `README.md` 里 `WECHAT_WEBHOOK_TOKEN=请换成一段随机字符串` 的误报）；标识符/路径/版本号形态放行；三种白名单条目（`path:` / `regex:` / `fingerprint:`）+ 行尾 `secret-scan: ignore`
- **入库**：`.gitignore` 新增 `!.qoder/hooks/` 白名单（否则 `.qoder/*` 会让钩子无法随仓库分发）；个人白名单 `secret-allowlist.local.txt` 保持 gitignored
- 影响：`.qoder/hooks/`（新增）、`.gitignore`、`docs/index.md`、`tests/test_secret_scan.py`（新增 51 个用例）、`.git/hooks/pre-push`（本机，不入库，克隆后需自行跑 `bash .qoder/hooks/install.sh`）。无服务端改动，不需重启

## 2026-08-04 · v0.6.0 后续补丁 · pi 推理强度 reasoning_effort 可调

- **问题**：pi 的 models.json 里 `compat.supportsReasoningEffort: false`（初次接 pi 时的保守设置）会把 `reasoning_effort` 从每次请求剥掉，叠加 `PI_THINKING=` 空，pi 一个 effort 值都不下发。注：deepseek-v4-flash-0731 服务端默认思考模式即 `high`，所以此前实际仍在 high 档跑，本次是把档位变显式且可调，并解锁 `xhigh`/`max`
- **pi 改动**：`~/.pi/agent/models.json` 与 `conf/pi/models.json.example` 的 `supportsReasoningEffort` 翻为 `true`；`PI_THINKING` 默认值 `"" → "high"`（config.py + conf/.env + .env.example）。取值 `off/minimal/low/medium/high/xhigh/max`，百炼实际两档（low/medium/high→high，xhigh/max→max）
- **opencode**：线上 `~/.config/opencode/opencode.jsonc` 已含 `reasoningEffort: high`（v0.6.0 前的 8e73344 已做），本次仅补仓内模板 `conf/opencode/opencode.jsonc.example` 防重建丢失，无行为变化
- 影响：`lib/python/app/config.py`、`conf/.env*`、`conf/pi/models.json.example`、`conf/opencode/opencode.jsonc.example`（新增）、`docs/references/{pi-cli,opencode-cli}.txt`、`~/.pi/agent/models.json`；上线靠 `supervisorctl restart ferry-stack:ferry`

## 2026-08-04 · v0.6.0 · 新增 pi 后端并设为默认

- **需求**：新增 pi（Pi Coding Agent 0.83.0）作为第 5 个可切换后端并设为默认，模型走阿里云百炼；skills / 记忆 / 规则等现有能力不受影响
- **集成方式**：`pi --mode json`（每轮一个短进程，与 `opencode_cli.py` 同构，复用重试/熔断/idle 超时/进程组 kill）。选它而不选 `--mode rpc` 是因为 rpc 需要进程池 + 请求响应关联且一进程只能一个 active session；也没用 pi 的 TypeScript SDK（需新增 Node sidecar，而 SDK 的两项独家能力——进程内定义 tool、虚拟内存态 AGENTS.md——对本项目价值为 0）
- **会话管理**：pi 的 `--session-id` 接受任意 ID 并按需创建，所以 session ID **由 Ferry 生成**（uuid4 hex）并持久化到 `runtime/server/pi-sessions.json`，不需要像 opencode 那样从事件流反解；首轮（skills 摘要 preamble）改用“映射是否新建”判定，而不是 `session_id is None`
- **规则与记忆注入**：复用现有链路，`app/memory.py` 零改动。pi 侧走 `--append-system-prompt <path>`（实测会读文件内容），指向 `rules/AGENTS.md` / `rules/admin.md` / `runtime/server/memory-context.md`，等价于 opencode 的 `instructions[]`；写入协议仍每轮在场
- **事件解析**：pi 是原生 delta，删掉了 opencode 那套按 `part.id` 算增量的逻辑；三个已实测验证的陷阱：① `--mode json` **退出码恒为 0**（认证失败也是 0），成败只能看 `message_end.message.stopReason` 与 `errorMessage`；② `message_end` 对 user 轮也会发，必须判 `role == "assistant"`，否则会把用户提问当回复回显；③ `thinking_delta` / `toolcall_delta` 一律丢弃，思考不进回复
- **时间注入**：pi 不加载 opencode 插件，`hooks/inject-time.js` 的等价物改为 `PiCliClient._time_context()` 在 prompt 首行拼 `<system-context>`（fail-open）
- **百炼 provider**：`~/.pi/agent/models.json` 新增 `bailian`（`https://dashscope.aliyuncs.com/compatible-mode/v1` + `openai-completions` + `compat.supportsDeveloperRole/supportsReasoningEffort: false`），模型 `deepseek-v4-flash-0731`；apiKey 用 `"$DASHSCOPE_API_KEY"` 环境插值，密钥只落 gitignored 的 `conf/.env`，不用 `--api-key`（避免进 `ps`）；仓内模板 `conf/pi/models.json.example`
- **supervisor 相关**：`PI_CLI_BIN` 必须给绝对路径（`bin/run-app` 的 PATH 不含 `/root/.local/bin`，且里面 node 是 v20.19.4 低于 pi 要求的 22.19，靠 `/root/.local/bin/pi` wrapper 钉住 v22.23.2）；spawn 沿用 `stdin=DEVNULL`（supervisor 的 stdin 是永不关闭的 pipe，而 pi 会把管道 stdin 并入首条 prompt）+ `start_new_session=True` + `os.killpg`
- **默认切换**：`ACTIVE_BACKEND=pi` 与 `runtime/server/backend.json` 两处都要改（后者优先级更高）；上线靠 `supervisorctl restart ferry-stack:ferry`；回退一句 `/opencode`
- **成败判定（last-wins）**：因为退出码不可信，一轮的成败以**最后一条 assistant `message_end` 的 `stopReason`** 为准：它是 `error` / `aborted` 就抛 `CodexClientError`，即使已经流出了部分文本（否则被截断的回答会当完整回答发给用户）。反之，pi 在单个进程内会 auto-retry，**中间尝试失败后重试成功不算失败**，所以不能用“出现任何错误事件就抛”这种写法
- **config 新增**：`PI_CLI_BIN/PI_MODEL/DASHSCOPE_API_KEY/PI_THINKING/PI_TOOLS/PI_CODING_AGENT_DIR/PI_OFFLINE/PI_APPROVE_PROJECT/PI_TIMEOUT_SECONDS/PI_IDLE_TIMEOUT_SECONDS/PI_SESSION_STORE_PATH`
- 新增 28 个测试（`tests/test_pi_chain.py` 5 + `tests/test_pi_session.py` 23），全量 149 passed
- 影响：`core/agent/pi_cli.py`（新增）、`core/agent/router.py`、`app/config.py`、`app/commands.py`、`conf/.env*`、`conf/pi/models.json.example`（新增）、`docs/references/pi-cli.txt`（新增）、`docs/{index,routing,architecture,sessions,functional-tests,core-beliefs}.md`、`README.md`、`.qoder/AGENTS.md`、`~/.pi/agent/models.json`

## 2026-07-31 · v0.5.0 · 长期记忆（memory/）

- **需求**：仅在用户明确要求时记录的长期记忆；类别由 conf 配置；markdown 存放可人工审查增删改查；禁止推送 github
- **三层记忆分工**：`rules/AGENTS.md`（人格，仅人工）/ `rules/admin.md`（权威静态事实，仅人工）/ `memory/*.md`（动态事实，agent 可写）；admin.md 中体重/偏好/投资等动态事实迁入 memory/，消除双源矛盾
- **写入机制**：纯自然语言触发（"记住/记一下"），由 agent 按 `skills/memory/SKILL.md` 规范自行读写；时序事实追加保留趋势、状态事实覆盖、软删除归档、写后必回执；Python 侧不参与写入
- **注入机制**：`app/memory.py` 渲染「写入协议 + 常驻类别全文 + 非常驻索引」到 `runtime/server/memory-context.md`；opencode 走 `instructions` 追加（协议须每轮在场，preamble 仅首轮不可用），claude/qodercli 走 `load_system_rules()` 追加；`MEMORY_MAX_INJECT_CHARS` 只约束记忆内容，协议始终完整
- **防误写与保密**：记忆内容受本地快照仓保护（git dir 在 `runtime/memory-git`，无 remote，物理上不可推送），每轮自动快照；主仓 `.gitignore` 排除记忆内容，仅 `memory/README.md` 占位入库
- **config 新增**：`MEMORY_ENABLED/DIR/CATEGORIES/ALWAYS_INJECT/MAX_INJECT_CHARS/GIT_AUTO_COMMIT/GIT_DIR/CONTEXT_PATH`
- 新增 16 个测试，全量 111 passed
- 影响：`app/memory.py`（新增）、`app/config.py`、`app/rules.py`、`app/main.py`、`core/agent/opencode_cli.py`、`skills/memory/`、`rules/admin.md`、`docs/memory.md`（新增）、`.gitignore`、`conf/.env*`

## 2026-07-27 · 项目定位更新：GitHub About + README 重写

- GitHub About 更新为：「Harness 范式的工程落地：核心能力交给 opencode，Ferry 收敛为接入层 + 后端路由」
- README 重写：新增设计哲学章节（能力归 agent，编排归 harness）；补全遗漏功能（`/daily`、文件归档 `FILE_ARCHIVE_DIR`、消息队列）；配置表精简为常改项；项目结构与 docs 单层目录同步；312 行 → 230 行
- 影响：README.md、GitHub 仓库描述，无代码变更

## 2026-07-27 · 文档整理：docs 拍平 + AGENTS.md 精简

- docs 拍平为单层目录，统一小写连字符命名：`ARCHITECTURE/CHANNELS/ROUTING/SESSIONS/FUNCTIONAL-TESTS` → 小写；`design-docs/` 下文件全部迁出后移除该目录
- `routing.md` 合并原 `design-docs/backend-routing.md` 的设计决策章节，机制与决策一处看全
- 新增 `docs/index.md` 总索引（取代原 design-docs/index.md）
- 删除：`architecture.drawio`、`architecture.svg`（零引用孤儿文件）、`tech-debt-tracker.md`（技术债追踪机制废弃）
- `.qoder/AGENTS.md` 精简为纯地图：删除后端策略表，文档引用统一指向 `docs/index.md`，约定路径同步新位置
- 影响：`docs/`、`.qoder/AGENTS.md`，无代码变更

## 2026-07-27 · 文档调整：移除 exec-plans + 新增核心功能测试清单

- 删除 `docs/exec-plans/` 目录（active/completed 均为空），`tech-debt-tracker.md` 迁移至 `docs/design-docs/`
- 新增 `docs/FUNCTIONAL-TESTS.md`：10 项核心功能回归清单（文件归档、图片、/daily、/remind、对话链路、微信、后端切换、会话命令/去重、格式化、opencode 会话/规则），每项映射自动化测试或手动冒烟步骤
- `.qoder/AGENTS.md` 新增核心规则：每次迭代完成必须对照清单回归验证；同步更新技术债路径引用
- 影响：`docs/`、`.qoder/AGENTS.md`，无代码变更

## 2026-07-26 · 缺陷修复：opencode 规则（含 admin.md）不加载

- **根因**：`asyncio.create_subprocess_exec(cwd=...)` 只改子进程 cwd，不更新继承的 `$PWD`；opencode 依据 `$PWD` 绑定会话项目目录，导致所有会话绑到项目根（无 AGENTS.md），整份规则丢失（不只 admin.md）
- **修复**：
  - spawn 时显式 `env["PWD"] = work_dir`，会话正确绑定 `runtime/codex-workdir/opencode`
  - 规则加载改用 opencode 原生 `instructions` 配置（`OPENCODE_CONFIG_CONTENT`），直指 `rules/AGENTS.md` + `rules/admin.md` 源文件：admin.md 不落盘拷贝、改完即生效、对已绑错目录的旧会话同样生效
  - 移除 `_sync_agents_md` 工作目录同步及 `_build_prompt` 中的规则重复注入
- 影响：`core/agent/opencode_cli.py`、`tests/test_opencode_session.py`、README

## 2026-07-26 · v0.4.0 · 每日定时简报 + 消息顺序队列

- **每日定时简报（飞书+微信）**：`/daily HH:MM 提示词` 创建、`/daily list` 查看、`/daily cancel <id>` 取消
  - 新增 `DailyTaskScheduler`（`core/session/daily_scheduler.py`）：每任务一个 asyncio 循环，JSON 原子写持久化（`runtime/server/daily-tasks.json`），重启后 6 小时窗口内补偿执行，失败重试 1 次后推送失败通知
  - 执行走 `chat(prompt, session_key=daily:{task_id}:{日期})`，每天新会话；飞书推送 send_markdown 降级 send_text，微信推送 POST sidecar `/send`
  - config 新增 `WECHAT_SIDECAR_BASE_URL`、`DAILY_TASK_STORE_PATH`
- **消息顺序队列（per-session FIFO）**：新增 `SessionMessageQueue`（`core/session/message_queue.py`），修复同会话连发多条只处理第 1 条的问题
  - 飞书队列上限 10，排队回复「已排队，前面还有 N 条」；微信上限 3，hold webhook 至任务完成
  - `/stop` 升级：终止当前任务 + 清空队列（回复附清空条数，被清消息回「已被 /stop 清出队列」）；命令不入队直接响应
- **微信 sidecar token 持久化**：contextTokens 落盘 `runtime/wechat/context_tokens.json`，重启后主动推送仍可用（已知限制：token 可能有时效）
- 新增 17 个测试，全量 97 passed
- 影响：`core/session/`、`app/commands.py`、`app/config.py`、`app/main.py`、两渠道 handler、`wechat-sidecar.mjs`

## 2026-07-26 · v0.4.0 · supervisor 接管 + 稳定性修复

- **运维需求：接入 supervisor 进程管理**
  - 新增 `bin/run-app`、`bin/run-wechat` 前台启动脚本，`/etc/supervisor/conf.d/ferry.conf` 管理两个服务（autorestart + 进程组级停止）
  - 原 `bin/server` 脚本保留，日常运维改用 `supervisorctl restart ferry-stack:*`
  - 影响：`bin/`、系统 supervisor 配置
- **缺陷修复：CLI 子进程泄漏**
  - 三个后端客户端（opencode/claude/codex）超时或取消时只杀 CLI 主进程，孙进程（如 `codex app-server`）泄漏成孤儿
  - 修复：`start_new_session=True` 独立进程组 + `os.killpg` 整组清理
  - 影响：`core/agent/opencode_cli.py`、`core/agent/claude_cli.py`、`core/codex/client.py`
- **缺陷修复：supervisor 下后端 CLI 挂死**
  - supervisor 给主进程的 stdin 是永不关闭的 pipe，CLI 子进程继承后等待输入 EOF 导致零输出超时
  - 修复：三个后端 spawn 时显式 `stdin=DEVNULL`
- **缺陷修复：微信文件收取失败**
  - iLink 实际下发结构与预期不符：下载地址在 `file_item.media.full_url`、AES 密钥为 base64 包裹的 hex、大小字段是 `len`
  - 影响：`lib/js/wechat-sidecar.mjs`

## 2026-07-26 · v0.3.0 (d760960)

- **文件收藏功能**：飞书 file/audio/media 消息归档到 `FILE_ARCHIVE_DIR`（默认 `/data/file`），回复"已收藏+绝对路径"；微信 sidecar 支持文件/视频下载 AES-128-ECB 解密落盘
- **rules 原生加载**：`rules/system.md` → `rules/AGENTS.md`，opencode 通过 `{work_dir}/AGENTS.md` 原生加载规则，移除 prompt preamble 注入；根 `AGENTS.md`（开发文档）移至 `.qoder/`

## 2026-07-25 (ca22922, 38632bc, 3423e1b, 97a4b28)

- **规则调整**：角色改为 Agent 助手，回复规则精简为"优先中文 + 禁止反问"
- **项目级定制体系**：rules 脱敏拆分（公开 `AGENTS.md` + gitignore 的 `admin.md`）、时间感知 hook（每轮注入当前时间）、提示词隔离
- **飞书长连接模式**：Webhook 之外新增 WebSocket 长连接接入；新增 yfinance skill
- **文档体系重构**：docs/ 分层（design-docs / exec-plans / references）、rules/skills 项目级加载、项目重命名为 Ferry

## 2026-07-20 (9886812)

- **opencode 原生会话续接**：`user_id:chat_id` → opencode session_id 映射持久化，`/new` 生成新 session；移除飞书"处理中"提示

## 2026-07-07 · opencode 默认后端 (ded3f9b)

- **新增 OpenCode CLI 后端并设为默认**，确立 opencode-first 设计信念（见 core-beliefs.md）
- 各 CLI 超时从 wall-clock 改为 idle-timeout（流式输出期间不计时）

## 2026-07-05 (c488305)

- **代码审查修复**：全量修复 25 项审查问题；生成项目 Wiki 文档

## 2026-06 (c4787c1 ~ 639c1ef)

- **多后端路由**：`/codex` `/claude` `/qodercli` 运行时切换命令，后端选择状态原子写持久化；切换后清空会话隔离上下文
- **稳定性**：Claude 系 CLI wall-clock 超时与请求超时、流式 markdown 空白保留、本地 skills 确定性返回
- **飞书体验**：回复改用 Markdown 卡片渲染

## 2026-05 (3068ab0 ~ 8bce872)

- **微信渠道接入**：新增 WeChat sidecar（Node.js，iLink 协议），项目布局重组
- **飞书图片能力**：图片投递与处理改进

## 2026-03 (96766ee)

- **长任务体验**：长任务进行中提示 + `/stop` 停止支持

## 2026-02-22 · 项目启动 (7f87781)

- **初始需求**：飞书渠道 + codex-cli 后端的对话机器人（CodexClaw）
