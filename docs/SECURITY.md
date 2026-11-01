# SECURITY

## 安全基线

- 密钥、密码、token 一律不进代码库：配置放 `conf/.env`（不入库，模板为 `conf/.env.example`），通过环境变量注入
- 本仓库为 public repo：推送前强制走 pre-push 密钥扫描（`.qoder/hooks/secret_scan.py`，随仓库分发，克隆后执行 `bash .qoder/hooks/install.sh` 挂载）
- 扫描策略：只扫待推送 commit 的新增行，命中即 exit 1 硬阻断；扫描器异常也阻断（fail-closed）；紧急逆转 `SKIP_SECRET_SCAN=1 git push`
- 所有外部输入（飞书回调、微信 webhook、文件上传）先校验再使用

## 认证与授权

- 飞书：签名校验 + AES-CBC 回调解密（`lib/python/channel/feishu/security.py`）
- 微信：sidecar webhook token 校验，非法请求直接拒绝（`tests/test_signature_validation.py` 兜底）
- 权限模型：单用户个人使用，`rules/system.md` 与 `rules/admin.md` 均为本地私有规则（gitignored），不入库；历史提交中的内容不会因停止跟踪自动删除
- 聊天署名：`OBSERVABILITY_USER_DISPLAY_NAME` 仅在私有配置中设置，公开默认“我”；只向已登录 Cookie 聊天客户端下发，不进入公开品牌、manifest 或静态脚本。已有私有登录用户名优先于公开默认值
- 控制台登录：单用户「用户名（`OBSERVABILITY_USERNAME`，默认 `admin`）+ scrypt 密码哈希」；用户名常量时间比较，用户名错误时也照常跑一次 scrypt，统一返回「用户名或密码无效」；登录限流与 Origin 校验（`lib/python/observability/security.py`）。明文密码不落盘、不入日志，凭证只存 `conf/.env.observability`，不提供默认密码
- 控制台会话：服务端 Session，最长 30 天（`OBSERVABILITY_SESSION_TTL_SECONDS` ≤ 2592000）；为支持 iOS 桌面 PWA 重启不掉线，持久化到 `runtime/observability/console-sessions.json`（0600），**只存 Cookie 值的 SHA-256 与到期时间**，文件泄露拿不到可用 Cookie；TTL 调短时旧会话随之缩短；退出登录立即删除对应记录
- 观测机器读取：`/api/observability/v1/*` 与 `/metrics` 用独立只读 Bearer Token；只读 Token 与上报写 Token 不允许同值（配置校验拒绝），只读 Token 不能改会话也不能上报
- 观测上报：`POST /internal/observability/events` 只接受回环来源 + 独立写 Token，不复用文件推送凭证；Nginx 模板对 `/internal/observability/` 对外返回 404
- 控制台写操作（`/api/observability/v1/console/*`）：只认有效登录 Cookie，拒绝任何 Authorization；七个写接口要求单个会话绑定 `X-Ferry-CSRF`，以 console Token 和独立用途派生，与聊天 CSRF 不通用，不依赖聊天开启。`/session` 仅向 Cookie 控制台客户端下发、前端仅存内存。正确 CSRF 下兼容缺失／null Origin；显式来源必须同源，Fetch Metadata 如存在只能 same-origin，重复安全头一律拒绝；登录／登出仍严格 Origin 校验。请求体严格 schema（键名白名单格式、值长度与数量上限）后，由观测进程携带独立的 `OBSERVABILITY_CONSOLE_TOKEN` 转发到主服务 `/internal/console/*`
- 主服务 `/internal/console/*`：Bearer 令牌常量时间比较 + 仅接受回环来源 + JSON 且 ≤ 4KB；令牌未配置返回 503。**只能在白名单菜单里选择**：每个配置项的可选值由主服务生成（模型注册表、记忆类别、`MAIL_RECIPIENTS`、已知聊天会话），菜单外的值一律 422；定时任务提示词不可改、不可新建；状态接口不返回密钥、路径或记忆正文。`OBSERVABILITY_*` 变量不会传给 pi 子进程，模型拿不到控制台令牌
- PWA 聊天 `/api/observability/v1/chat/*`：仅登录 Cookie + 绑定该 Cookie 的 `X-Ferry-CSRF`（既有 chat Token 用途隔离 HMAC，`/session` 只向 Cookie 聊天客户端下发，前端仅存内存）。所有请求包括附件读取均要求单个正确 CSRF；显式 Fetch Metadata 只能 same-origin，显式非 null Origin 仍严格同源。兼容 iPhone PWA 缺失 Metadata/Origin 或 Origin=null，但不能绕过 CSRF；重复头、foreign Origin、cross-site/same-site、错误或跨登录 CSRF 拒绝。只读 Bearer 一律拒绝，不能取得 CSRF。独立 `OBSERVABILITY_CHAT_TOKEN` 代理到主服务 `/internal/chat/v1/*`（真实回环来源 + 常量时间令牌校验），不开放浏览器指定模型、工具、会话 key、路径。聊天是有工具的 Agent 执行入口，公网必须 HTTPS + Secure Cookie；正文限额、附件流量、频率、并发和输出上限独立于控制台的 4KB 限制
- PWA 取消：Stop、超时、登出、删除会话与关闭服务回收任务；普通断流只解绑订阅。恢复运行轮次仍校验 owner，client_turn_id + 请求摘要幂等，不因重新连接重复执行。聊天 Token 同样由 `PiCliClient._process_env()` 剔除
- PWA 通知：订阅/撤销/测试/前台状态沿用 Cookie + CSRF；owner 与到期由可信网关下发，浏览器不能自报。发送前经 `/internal/observability/push-owner` 回环 + chat Token 校验会话，失败不发送；注销、过期、续登清理旧订阅与待发。订阅实例 generation 防止旧投送结果删除新订阅
- Web Push endpoint 只允许支持的推送服务 HTTPS 域名/443，检查公网 DNS、拒绝凭证/重定向，不使用环境代理；验证 P-256 公钥与 auth 长度。VAPID、订阅和 outbox 存运行目录 0600，不进日志或仓库。通知只含固定简短文案及 opaque 会话 ID，SW 必须显示可见通知，点击目标仅同源会话；不缓存聊天 API、附件或用静默推送后台取正文
- App 外观设置：只认有效登录 Cookie + 会话 CSRF；名称/触感/头像使用单独严格接口，不能写任意模型配置。名称与图标刻意公开给登录页和 PWA 安装资源，不宜填写敏感资料；头像只收 PNG/JPEG/WebP bytes（≤2MiB），实际解码限制尺寸/像素、拒绝动画/SVG、重采样为无元数据 PNG，用固定路由与有界版本提供；不接受路径或外链，不进入 Pi 环境或聊天历史
- 邮件推送：163 SMTP SSL；授权码在发送时才从 `MAIL_ENV_FILE`（默认 `~/.env/smtp-mail-assistant.env`）读取，不进主服务进程环境（pi 子进程继承的是主服务环境）；SMTP 报错不回显服务器原文；收件人只能从 `MAIL_RECIPIENTS` 中选

## iOS 设备数据边界

- 健康、Apple 日历/提醒数据是用户主动上传的独立业务数据，不属于观测白名单；只允许登录 Cookie 查询，写操作要求绑定会话 console CSRF，观测只读 Bearer 不可读取。内部设备接口仅回环 + 控制台 Token；批次专用限额不扩大普通控制台 4KB 写面。
- 手机仅 HTTPS、验证证书与固定来源，不提供任意 JavaScript 原生桥，不保存密码/内部 Token/健康持久缓存。登出、登录失效、切站及进入后台取消同步；Apple 授权接口是只读调用，不修改设备上的日历、提醒或健康数据。
- 服务端 `runtime/server/device-data/` 使用 0700/0600、严格有界结构和原子存储，不进入 telemetry、Prometheus、聊天、memory/Dream 或默认 pi 提示词。文件权限不等于加密；同一 OS 用户运行的工具仍可能读取，需信任部署主机及执行身份，备份另行保护。
- 空结果/权限拒绝/部分结果不隐式抹除旧数据；缺失条目可能是旧副本，不保证反映当前设备。用户显式清除副本时废弃旧事务，迟到提交不可恢复数据；该动作不删除手机原件。Apple 提醒不导入 Ferry 自动提醒调度。
- 真机签名与授权、SDK 构建仍须用户在 macOS 验证，详见 [ios.md](ios.md)；不能通过移除 HealthKit entitlement、放宽证书或鉴权来规避配置问题。

## 数据安全

- 敏感数据清单：`conf/.env`（飞书 / 微信凭证、模型 API key）、`conf/.env.observability`（观测密码哈希、只读 / 上报 / 控制台 Token、HMAC key，0600）、`~/.env/smtp-mail-assistant.env`（邮件授权码，0600）、`rules/admin.md`（管理员 user_id 等）、`secret-allowlist.local.txt`（扫描白名单，gitignored）、`conf/wechat/account.json`
- 观测隐私：只采集白名单运行元数据，不落聊天正文、思考、工具参数 / 结果、文件名与路径、原始报错；会话仅 HMAC 去标识化，且不作为 Prometheus 标签。pi 历史用量只读原生 JSONL 的 usage 元数据，账本仅存数字、模型、日期与 HMAC 标识，源文件删除后保留已采集用量
- 控制台例外（用户确认）：定时任务与提醒显示前 20 字摘要以便辨认；整理面板不展示 Dream 正文；推送目标显示时脱敏（`z***@foxmail.com`、`微信 · o9cq…Ta`）
- PWA 聊天正文是独立业务数据，不属于观测事件：服务端 `runtime/server/web-chat/` 以 0700/0600 保存有界可见历史和附件；展示 user/assistant/system 文本及至多 30 条脱敏工具活动（名称、简短参数摘要、状态、耗时）；摘要可含实际脚本名、文件名、命令子操作或搜索词，但先隐藏认证头/密钥/JWT/环境赋值/敏感文件，再截断，不展示绝对路径、任意内联代码或工具结果。不读取 Pi JSONL 的思考/原始工具内容，不另存完整原始参数；工具元数据纳入历史字节预算。Web `/dream` 结果直接返回当前聊天，不外发；有效 Web 会话参与离线 Dream，删除线程后退休归属、后续不再收集，但不删除 Pi 全局 transcript 或已提炼的长期记忆
- 附件上传只接受 bytes 与 opaque ID；扩展名由验证后的 MIME 决定，禁止路径/符号链接和不支持格式；非图片强制下载。聊天输入是纯文本编辑区，粘贴／文本拖入不接纳 HTML；前端字符上限与服务端请求体限制同时保留。聊天文本仅经安全 DOM 节点渲染，禁止 HTML 注入；API/附件不进入 SW 缓存，正文不写浏览器持久存储；退出清空 DOM 并释放 blob 预览
- 脱敏要求：推送 GitHub 前检查 `rules/`、`conf/`、`.qoder/` 下是否有用户敏感信息；公开文档与变更记录不写真实域名、会话数量、Token 分项与活跃日期
- 备份策略：`TODO: 待补充`（当前无自动备份）；现有 pi cwd、session 映射与 `PI_CODING_AGENT_DIR` 中的会话数据不自动迁移或删除，后续人工变更前应同时备份这些恢复条件

## 已知风险与例外

| 风险 | 等级 | 处理状态 |
|---|---|---|
| pi CLI 失败时仍 `EXIT=0`，需解析 `stopReason=error` 判断（0.84.2 依旧） | 中 | 已在解析层处理，见 `docs/references/pi-cli.txt` |
| 模型生成内容误提交密钥 | 中 | pre-push 扫描兜底 |
| 密钥扫描白名单误放行 | 低 | 白名单本地文件不入库，逐条维护 |
| 看板监听改 `0.0.0.0` 后只靠安全组 + 应用鉴权 | 中 | HTTPS 入口必须 `OBSERVABILITY_COOKIE_SECURE=true`，禁止用裸 HTTP 38080 登录；默认仍为回环 + SSH 隧道 |
| 观测写盘失败 / 队列溢出时事件丢失，指标可能不完整 | 低 | 不阻断 IM；读端明确提示不完整，未闭合轮次显示运行中或未知，不补成成功 |
| 用量账本损坏且 pi 原生历史已删除时该段用量不可重建 | 低 | 人工备份 `runtime/observability/pi-usage/index.json`（gitignored） |
| 控制台由只读变为可写，Cookie 被盗可改配置 | 中 | 写面只有白名单选项与固定动作（立即整理 / 撤销 / 启停任务）；Cookie HttpOnly + SameSite=strict + Secure（HTTPS），写请求会话 CSRF 与来源校验；所有改动可在控制台「恢复默认」，记忆改动有 git 快照 |
| 30 天持久会话扩大被盗窗口 | 低 | 只存哈希；退出即失效；需要时调小 `OBSERVABILITY_SESSION_TTL_SECONDS` 或删除 `console-sessions.json` 让全部会话失效 |
