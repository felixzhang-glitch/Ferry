# 核心功能测试清单

> 每次迭代（新功能 / 行为调整 / 缺陷修复）合入前必须对照本清单验证，避免开发改动破坏核心功能。
> 验证方式分为自动化（pytest 与 Node 测试分别执行）和手动冒烟（真实链路）。下表是验收目标；自动化执行结果见需求变更记录，真实模型及渠道冒烟尚未执行
> 涉及重启、改规则、模型或渠道实发的项目在隔离环境执行，不操作用户现有服务、真实配置或运行数据
> `tests/` 为本地目录，不随仓库分发：下表的自动化用例只对本地工作副本有效，clone 仓库后只能手动冒烟

## 核心功能项

| # | 功能 | 验证要点 | 验证方式 |
|---|------|---------|---------|
| 1 | 飞书发送文件 | 文件下载成功并归档存储到指定目录 | `tests/test_file_archive.py` |
| 2 | 入站图片多模态 | 飞书图文混排/微信图片下载归档后经 pi `@file` 传给模型（非路径文本）；图片-only 消息能唤醒模型；大图 base64 回显不撑爆 readline limit（32MB）；微信图片 AES 纯 hex 密钥可解密 | `tests/test_pi_multimodal.py`、`tests/test_feishu_media.py`、`tests/test_handler_single_reply.py`、`tests/test_wechat_handler.py`、`tests/wechat-sidecar.test.mjs` + 手动冒烟：两渠道各发一张大图确认模型能描述图片内容 |
| 3 | 定时任务 `/daily` | 创建 / list / cancel 解析正确；到点执行并推送（飞书 + 微信）；重启后任务恢复 | `tests/test_daily_scheduler.py` |
| 4 | 定时提醒 `/remind` | 时间解析（s/m/h/d）、到点提醒、持久化恢复 | `tests/test_reminder_scheduler.py` |
| 5 | 基础对话链路 | 飞书 WS 收文本 → Typing 回执 → 最终答案单条稳定回复 | `tests/test_feishu_ws.py`、`tests/test_handler_single_reply.py` |
| 6 | 微信文本对话 | sidecar 转发消息处理正常；webhook token 校验拒绝非法请求 | `tests/test_wechat_handler.py`、`tests/test_signature_validation.py` |
| 7 | 唯一 pi 状态与旧命令 | `/backend`、`/pi` 只读状态；旧 `/opencode` `/codex` `/claude` `/qodercli` 只提示移除；均不修改 pi 映射、附件通知或写后端状态 | `tests/test_new_command.py`、`tests/test_wechat_handler.py`+ 隔离环境手动冒烟 |
| 8 | 会话命令与去重 | `/new` `/reset` `/stop` 行为正确；同 `message_id` 消息不重复处理；同会话连发消息按 FIFO 排队 | `tests/test_new_command.py`、`tests/test_session_manager.py`、`tests/test_message_queue.py` |
| 9 | 回复格式化 | 超长文本智能分段（保留段落/代码块边界）；Markdown 卡片渲染失败自动降级纯文本 | `tests/test_feishu_formatting.py` |
| 10 | pi 规则与技能 | 规则改后下一轮 `--append-system-prompt` 生效；skills 由 `app.skills` 发现，**只扫项目 `skills/`**，不读 `~/.pi`、`~/.agents`、`~/.claude`、`~/.codex` 等任何全局 CLI 目录；摘要仅首轮注入，`/skills` 可查询 | `tests/test_skills.py`、`tests/test_pi_session.py`、`tests/test_pi_chain.py` + 隔离环境手动冒烟 |
| 11 | 长期记忆 memory/ | 明确要求时写入并回执（"记住…" → `已记入 memory/…`），条目为 `- [key] @日期 内容 (source: user)`；日常提及不在对话中写入（交给 Dream）；查看/修改/软删除（移入 `## 已归档` 并记 `archived:`）可用；重启后记忆仍注入；记忆内容不被主仓跟踪且快照仓无 remote | `tests/test_memory.py` + 手动冒烟：对话中"记住 X"验回执与文件格式，`git --git-dir=runtime/memory-git log` 验快照 |
| 12 | pi 原生会话与重置 | 两渠道每轮只传当前 user，不写桥接历史；`--session-id` 续接；`/new`、`/reset` 清附件和映射但保留旧 transcript；新轮次不受旧任务回写污染；`/stop` 按 trace_id 回收对应进程组 | `tests/test_pi_chain.py`、`tests/test_pi_session.py` + 隔离环境手动冒烟：追问上一轮，重置后确认隔离 |
| 13 | pi 退出码陷阱与成败判定 | `pi --mode json` 失败时退出码仍为 0，必须靠**最后一条** assistant `message_end.stopReason` 判定；部分输出后报错不得当成功，auto-retry 中间失败后重试成功不得误判为失败；任何情况下不得静默回空 | `tests/test_pi_session.py::test_provider_error_raises_even_though_the_process_exits_zero`、`::test_error_after_partial_text_still_raises`、`::test_intermediate_failure_followed_by_a_retry_success_is_not_an_error` |
| 14 | 出站文件推送 `/push/file` | 飞书 `im/v1/files` 上传 + `msg_type:file` 发送；微信转发 sidecar `/send_file`（getuploadurl → AES-128-ECB → CDN → `type:4` file_item）；`receive_id_type` 按 `ou_`/`on_`/`oc_` 自动识别；无 `PUSH_API_TOKEN` 时 503 关闭；401/400/404/413/502 错误分档；caption 先于文件发出 | `tests/test_feishu_file_send.py`、`tests/test_push_file_route.py`、`tests/wechat-sidecar.test.mjs` + 手动冒烟：小文件实发两个渠道确认收到 |
| 15 | 入站文件进会话 | 归档后排入 `pending_files` 且**不唤醒 agent**；通知搭载下一轮 user 文本（两渠道每轮只传当前 user，桥接层不保存历史）；文件+文字同发合并为一轮；命令轮不消费通知；任务被拒不 drain；归档失败不排通知；`/new` `/reset` 清空；队列上限 20；微信 `type:2` 图片不再被静默丢弃；`describeFileItem` 保留字段名但掩掉 `encrypt_query_param`/`aes_key` | `tests/test_inbound_file_session.py`、`tests/wechat-sidecar.test.mjs` + 手动冒烟：发个文件→再发一句话→确认 agent 知道文件路径且未主动展开 |
| 16 | pi 时间感知与相对日期 | 每轮 `--append-system-prompt` 注入四行块（当前时间 / 相对日期 / 本周·下周 / 硬约束），user prompt 不带时钟；时段与"昨天/明天/周末/下周X"均由 `app.clock` 预计算，禁止心算；历史时间戳与相对说法不得复读；不在 transcript 中新增时钟副本 | `tests/test_clock.py`（含跨周/跨月用例）、`tests/test_pi_session.py::test_clock_rides_the_system_prompt_after_rules_and_memory` + 隔离新会话冒烟，检查新增 transcript |
| 17 | 压缩命令说明 | `/compact`、`/compress` 仅提示 pi 原生管理，不调用摘要模型、不改变 pi 映射或 pending_files、不伪报成功 | `tests/test_new_command.py`、`tests/test_wechat_handler.py` |
| 18 | 配置与启动兼容 | 仅需 pi CLI；新键优先、旧共享键回退；`PI_WORK_DIR` 为最终 cwd，默认路径不变；`GENERATED_IMAGES_DIR` 兼容旧键；忽略旧后端状态且不删除用户文件 | `tests/test_config.py`、`tests/test_server_startup.py` + 隔离环境检查解析目录，不移动生产数据 |
| 19 | pi 故障与进程回收 | 取消、空闲超时、持续输出时总超时、stdout EOF 后进程未退、部分输出后失败与重试分别验证；超时按每次 CLI 尝试计，不覆盖排队和退避 | `tests/test_pi_session.py`、`tests/test_pi_chain.py` + 隔离子进程测试（包括真实本地子进程取消） |
| 20 | pi 模型注册同步与规则文件防护 | 启动时 `conf/pi/models.json` 同步到 `~/.pi/agent/models.json`（字节对比、差异才写、写前备份、失败不阻断）；`PI_MODEL` 未注册启动告警；`rules/system.md` 缺失告警不静默 | `tests/test_server_startup.py::test_sync_pi_models_copies_registry_and_warns_unregistered`、`tests/test_pi_session.py::test_system_prompt_files_carry_rules_and_memory`、`::test_missing_local_rules_warns_but_does_not_raise` |
| 21 | pi 常驻运行时 | 连续请求同 PID、两个 worker 有界并发、同 session 串行、等 `ferry_done` 才复用；取消/超时/崩溃后补建，Node 卡死仍回收独立工具组且不误杀复用 PID；每轮重读 AGENTS/规则/记忆/时间且不污染 transcript；图片实际输入；CLI 回退 | `tests/test_pi_worker.py`、`tests/pi-worker.test.mjs`、`tests/test_config.py` + 隔离真实 SDK 冒烟与冷暖测速 |
| 22 | 飞书渐进式卡片与工具进度 | 进入流式即发占位卡（不等首字）；pi 执行工具期间卡片显示 `🔧 <工具> · <参数摘要>`，无正文时显示占位、定稿清空 footer；更新必须 PATCH 同一张卡而非新建消息；卡片创建或 PATCH 失败降级为整段单条回复并覆盖在途卡片；`on_progress` 回调抛错不影响本轮；微信通道不受影响 | `tests/test_feishu_streaming.py`、`tests/test_feishu_reaction.py::test_update_markdown_card_patches_the_same_card`、`tests/test_pi_session.py::test_tool_execution_events_decode_into_progress_and_timing` + 真实 pi 冒烟核对卡片写入序列 |
| 23 | 延迟观测字段 | 日志 formatter 不再丢 extra（`backend`、`feishu_stream_updates`、`feishu_streamed` 可见），非 JSON 值不炸日志；飞书四个传输助手成功路径统一带 `duration_ms`/`attempt`；`pi.tool_call`/`pi.tool_result` 给出工具级耗时 | `tests/test_logging.py`、`tests/test_feishu_reaction.py`（MockTransport 覆盖传输层）+ 生产日志抽查 |
| 24 | 记忆 NOTE-FORMAT 解析与投影 | frontmatter / `[key] @date` / 行尾 context 元数据 / relation / `## 已归档` 正确解析，勾选框、链接、时间码不算 observation；旧格式与无 key 列表项以 `fact` 兜底不丢；注入块不含 frontmatter、context、归档；`memory-index.json` 每轮刷新；行级补丁保留手写内容；迁移工具 dry-run 可审、`--apply` 内容守恒、可重入 | `tests/test_memory_format.py`、`tests/test_memory.py`、`tests/test_memory_migrate.py` + `python -m app.memory_migrate` dry-run 审 diff |
| 25 | Dream 离线整理 | 只读聊天会话（wechat / 飞书 `oc_`，排除 daily 等）的 watermark 增量，跳过工具/思考/系统快照/skills 前言；pi 一次性调用无工具无会话、不占 worker 不计熔断；evidence 不在用户原话中、敏感信息、未知类别/id、重复处理同一条目的 op 被拒；recent 超 90 天自动归档；整理期间被对话改过的类别跳过且 watermark 不推进；模型失败不改文件；有变更单独 `dream:` 提交并推送摘要，无变更定时运行静默 | `tests/test_dream.py` + 临时副本真实冒烟：复制 memory 到临时目录跑一次 `DreamRunner.run`，核对运行记录 evidence 与 diff |
| 26 | `/memory` `/memory undo` `/dream` `/dream all` | 命令不进模型；`/dream` 只整理发起命令的对话（同 session_key 含 `/new` 前旧会话）、只推进该对话 watermark，不影响全局 watermark，重复发送不重复喂同一批消息，全量运行跳过已整理过的部分；`/dream all` 整理全部对话；后台运行、运行中再发提示进行中、结果推给发起会话；无新消息时回复"没有新的对话"；`/new` / `/reset` 删除映射前把旧 session_id 记入台账；`/memory undo` 回滚最近一次有提交的 Dream（之后的空运行不遮挡），冲突自动 abort，不能重复撤销；`/memory` 显示条目数与最近一次结果 | `tests/test_dream.py`、`tests/test_wechat_handler.py::test_wechat_memory_commands_route_to_dream_runner_not_agent` + 微信/飞书实发冒烟 |
| 27 | 控制台登录与会话 | 用户名 + 密码登录（默认 `admin`），用户名或密码错误统一提示且都跑 scrypt；会话 30 天、只存哈希并跨观测进程重启保留，退出即失效；只读 Token 不能调用控制台 | `tests/test_observability_web.py`（username / persist / console 用例）+ iOS Safari 实机登录 |
| 28 | 控制台系统配置（只选不写） | 设置项只能从主服务下发的选项中选择，菜单外的值 422；改动即时生效（Dream 重新排期、模型/思考强度下一轮生效、日志级别）并持久化，「恢复默认」回到 `.env`；定时任务可启停 / 改时间 / 改推送目标 / 删除但不能改提示词；提醒可取消；`/internal/console` 仅回环 + 令牌 | `tests/test_console.py`、`tests/test_daily_scheduler.py`、`tests/test_observability_web.py` + 控制台实机切换一次报告时间并在 `/memory` 核对 |
| 29 | Dream 报告定时推送与邮件 | 02:00 整理、08:00 推送（或整理完立即）；待发报告重启不丢、6 小时内补发；多目标逐个推送互不影响；邮件授权码发送时从文件读取、SMTP 报错不回显 | `tests/test_dream.py`、`tests/test_mailer.py` + 部署后发一封测试邮件 |
| 30 | PWA 与界面 | manifest / sw / 图标 200 且 CSP 允许；移动回车换行/按钮发送/组合输入；visualViewport 缩小与 offsetTop 偏移后消息仍可滚、发送按钮可见、旧消息锚点与下一条草稿保留；默认聊天入口；SW 不缓存 `/api/` 或附件；移动端底部标签栏与安全区、桌面侧边栏；明暗主题；320px 无横向滚动；配置模块仍只选不写，聊天模块允许文本/附件/重命名 | `tests/test_observability_web.py::test_pwa_assets_are_served_with_csp_allowances`、`tests/test_pi_usage_ui.py`（隔离浏览器环境）+ iOS「添加到主屏幕」实机 |

iPhone PWA 修复验收（运行时，不重跑 CI）：实际独立主服务/网关 + 触摸 Chromium，缺失/opaque 来源头先复现 403，增加会话 CSRF 后为 200；错误/缺失/跨登录 CSRF、重复头、跨站来源、read Bearer 仍为 401/403。模拟键盘单独/同时缩小视口、offsetTop、中文组合输入、多行、触摸、重复 submit、流式新草稿、旧消息锚点、320px 和横屏通过，截图已查看、浏览器无错误。只有 Chromium 模拟，实体 iOS 键盘仍需用户真机确认；不能等同于 iPhone 已完整验收。

PWA 首版验收：Python 1116 passed / 18 skipped；独立 Chromium 聊天 14/14、监控 6/6；Node 34/34。真实主服务与网关在本机隔离端口联调通过（生成器/IM 连接为合成后端，未外发私人数据）；桌面/390px 深色/320px/抽屉截图已查看。上线后已通过 HTTPS 真 Pi 回复、附件上传/删除、会话恢复与 Chromium 桌面/移动截图验收；实体 iOS 安装与软键盘仍待实机验收。

工具活动本轮运行时验收通过：实际 Pi 解析链 + 本机事件进程 + HTTP/Chromium 验证并行、失败、中断/重试隔离、截断、展开状态/焦点、历史恢复、旧消息锚点、+ 上传下载与移动键盘布局；截图已查看，浏览器无错误。上线后真实模型 printf 工具进度及持久化已通过；本轮未重跑 CI。

新对话/具体工具摘要本轮运行时验收：实际 app/HTTP/Chromium 的桌面与手机创建、慢请求/失败/连点/初始化中创建/流切换草稿、具体摘要/敏感参数隐藏、390/320px 键盘与监控回切均通过，截图已查看、浏览器无错误；用户授权上线后真实模型 printf 摘要及历史保存通过。未跑 CI，实体 iPhone 仍需用户确认。

新对话/聊天头验收：聊天页无重复大标题和右侧改名/重置；桌面、手机顶部和抽屉创建按钮均真正请求并切新 id，创建中有反馈、失败有提示、连点不重复；流中创建/返回保留旧草稿，监控标题/刷新不受影响。

工具活动验收：首字前出现默认折叠工具活动，折叠头能看到实际脚本/文件/命令子操作/搜索词摘要而非仅 bash，展开后显示脱敏名称/状态/耗时/摘要；流式与完成时展开状态不丢、并行结束不误清其他任务，失败/停止/中断状态真实，最多 30 条并明确截断；刷新恢复记录并默认折叠，旧记录无字段可读；命令/路径/结果不进入新增展示数据，`+` 可选附件且有 44px 触摸区域；移动键盘与历史滚动不回归。

PWA 聊天回归：`tests/test_web_chat_store.py`、`tests/test_web_chat_service.py`、`tests/test_web_chat_foundations.py`、`tests/test_observability_chat.py`、`tests/test_web_chat_ui.py`。覆盖多会话 CRUD/历史恢复、线程 key 隔离、reset/delete/retirement、附件大小与 MIME/签名/路径拒绝、幂等/重试、NDJSON/取消/输出上限、Cookie/Origin/Fetch Metadata/Token 分离、核心命令补全与 inline Dream。真实浏览器验收需单独在隔离 Playwright 环境执行，不能用单元测试代替。

可观测回归：`tests/test_observability_store.py`、`tests/test_observability_web.py`、`tests/test_observability_server.py`、`tests/test_observability_instrumentation.py`、`tests/test_observability_nginx.py`。覆盖白名单不泄露正文、轮次/尝试/模型响应去重、失败与取消用量、文件恢复/保留/缓冲溢出、密码/Session/Token/Origin/上报隔离、私有38080入口、server代理Supervisor。真实模型和IM实发不由这些隔离测试替代；Web无内容不等于既有业务日志不保存内容

pi 历史 Token 回归：`tests/test_pi_usage.py`（原生结构、缓存四项、压缩/分支摘要、跨日回合、fork/别名去重、增量/重写/删除归档、缓存恢复/并发锁、隐私）、`tests/test_pi_usage_routes.py`（鉴权、日历日期、全部历史、异步刷新、独立metrics）、`tests/test_pi_usage_integration.py`（临时原生日志→真实账本→API→追加同步→重启）。界面测试需单独运行 `runtime/browser-check-env/bin/python tests/test_pi_usage_ui.py`，普通pytest环境无Playwright时浏览器用例会跳过，不能当作已验证；覆盖6项含日期、热力图、模型图、竞态、CSP和320px。实际历史独立核对脚本为 `tests/audit_pi_usage.py`，只读取原生元数据，不发送模型或IM请求

常驻模式下，上表 `--session-id` / `--append-system-prompt` / `@file` 由 worker 适配到对应 pi SDK API，语义保持不变。涉及主服务重启的上线验收仅在用户授权后执行；渠道实发与本地 mock/webhook 冒烟分别记录，不互相替代

## PWA 移动优化与通知验收

2026-10-22 本地结果：1404 pytest 用例及 3 subtests 通过（包含 Chromium），Node 34 通过；真实隔离 app/HTTP/移动浏览器交互与断流后完成通知捕获通过，截图已查看。随后经用户授权上线，HTTPS 真 Pi/Qwen 回复、刷新恢复、触摸命令/右侧抽屉、头像上传/保存/重载与恢复、鉴权错误路径均通过；主服务和控制台已更新，微信 sidecar 未重启。Web Push 服务已启用，用户设备仍需授权，未验证真实系统投送；当前 Debian 11 无法安装 Playwright WebKit，实体 iPhone 锁屏/触感/缩放尚待确认。上线验收只观察实际运行，未重跑测试。

- 手机头部仅左侧列表图标、右侧新建加号，长标题不裁字、不挤压按钮；右滑打开左侧抽屉、左滑关闭，纵滚/代码横滚/文本选择不误触；列表不显示“我的对话”，改名/复制是至少 44px 热区的小图标；命令菜单能触摸滚动再轻点，中文组合输入与键盘操作保留。
- 自动标题由首条有效输入产生，手改后不覆盖；旧占位标题回填，历史裁剪不改名；用户署名与助手名称独立。
- 刷新/离开/断网超过 32 个增量仍在服务端完成，恢复同一轮次无重复生成或重复正文；慢订阅快照重放、取消与注销仍有效；最终答案不用跨 assistant 拼接。
- 外观名称、头像上传、错误草稿、重载后状态与版本图标须经过真实 HTTP 验证；iPhone 硬件触感、聚焦/双击/双指缩放、软键盘与主屏幕图标更新须真机确认。
- 通知覆盖后台回复、提醒与简报：首次安装/拒绝权限/授权/关闭/测试、锁屏/点击重开、注销/到期不推送、前台抑制；订阅 keys 更新不被旧 410 响应删掉。模拟 sender 通过不等于真实 Apple 推送通过。
- `/remind`、`/daily` 结果落会话但不进 Pi 上下文；通知未授权仍可读，投送失败不丢已生成答案，删除会话停止关联任务；补充 `tests/test_web_push.py`。

## PWA 聊天微调与统一模型运行验收

2026-10-22 本轮仅进行运行时观察，未运行测试套件或类型检查。隔离真实主服务／控制台与合成 Pi 子进程，经浏览器切换模型、刷新和恢复默认，隔离服务重启后仍显示并调用已保存模型；自动重试保留当前轮旧参数，下一轮 PWA／微信／飞书均使用新参数，IM 回发仅到本地替代接口。七个控制台写路由正确会话 CSRF 下兼容缺失／null Origin，跨站、重复头、跨会话／跨用途、Bearer 和菜单外模型分别被拒绝。

移动／桌面浏览器已观察左抽屉、图标复制与改名、删除确认、长标题、320px／横屏／明暗主题、原生换行／Chromium IME、纯文本粘贴／撤销、长度超限及改短后清提示、命令触摸菜单、旧消息键盘锚点、附件上传发送、停止、下一条草稿及刷新恢复。v8 SW 已激活且缓存不含 API／附件，截图已查看、浏览器无错误。目标 iPhone 原生辅助栏是否消失、真实输入法与安全区仍待设备验收；不代表生产已更新或真实供应商／IM 实发通过。

2026-10-22 v8 生产验收：用户授权后备份并重启主服务／控制台，微信 sidecar 和私有配置／Nginx 不变。真实 HTTPS 浏览器创建合成会话，DeepSeek 回复、刷新恢复、切换 Qwen 后下一轮回复均成功，原生元数据确认实际模型与正常结束；随后恢复原共享模型及覆盖状态。左抽屉／图标／320px／明暗主题／命令触摸／多行粘贴撤销／模拟键盘均已观察、截图已查看；非法来源／CSRF／模型选项正确拒绝，v8 SW 生效。临时线程与登录已清理，原登录保留，最终健康 200、无 Web 在途任务。未跑测试／类型检查、未实发 IM；iPhone 原生辅助栏与实际输入法仍待设备验收。

v0.13.0 发布隐私验收：隔离 FastAPI／Chromium 使用合成署名完成登录、发送、刷新与退出，验证署名只来自认证会话、HTML 字符不解释为标记、公开资源和只读 Token 不返回署名、发送请求不携带署名；应用接口报告 0.13.0。邮件 CLI 仅 dry-run，显式账号可解析，缺账号在网络请求前报错。私有规则取消跟踪后内容哈希不变；控制台重启后旧账号、署名与有效会话保留。本轮不运行测试套件或类型检查。

## 迭代验收规则

1. 任何功能改动合入前必须全量测试通过：

```bash
source .venv/bin/activate
pytest -c conf/pytest.ini -q
```

2. 改动涉及 `lib/js/` 时，额外跑 JS 侧测试（pytest 不收集 `.mjs`）。**必须用 `PI_NODE_BIN` 指向的 Node（当前 22.23.2）**：默认 PATH 上的 20.19.4 会因 undici 缺 `markAsUncloneable` 让 `pi-worker.test.mjs` 失败 10/14，那不是代码问题

```bash
"$(grep -m1 '^PI_NODE_BIN=' conf/.env | cut -d= -f2)" --test tests/pi-worker.test.mjs tests/wechat-sidecar.test.mjs
```

3. 改动涉及上表功能项时，除自动化测试外，额外执行该项对应的手动冒烟
4. 新增核心功能时，同步在本清单追加一行（功能 + 验证要点 + 验证方式）
