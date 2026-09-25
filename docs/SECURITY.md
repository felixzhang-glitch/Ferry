# SECURITY

## 安全基线

- 密钥、密码、token 一律不进代码库：配置放 `conf/.env`（不入库，模板为 `conf/.env.example`），通过环境变量注入
- 本仓库为 public repo：推送前强制走 pre-push 密钥扫描（`.qoder/hooks/secret_scan.py`，随仓库分发，克隆后执行 `bash .qoder/hooks/install.sh` 挂载）
- 扫描策略：只扫待推送 commit 的新增行，命中即 exit 1 硬阻断；扫描器异常也阻断（fail-closed）；紧急逆转 `SKIP_SECRET_SCAN=1 git push`
- 所有外部输入（飞书回调、微信 webhook、文件上传）先校验再使用

## 认证与授权

- 飞书：签名校验 + AES-CBC 回调解密（`lib/python/channel/feishu/security.py`）
- 微信：sidecar webhook token 校验，非法请求直接拒绝（`tests/test_signature_validation.py` 兜底）
- 权限模型：单用户个人使用，管理员私有规则走 `rules/admin.md`（gitignored），不入库
- 观测看板：单用户 scrypt 密码哈希 + 服务端 Session（默认 24h）、登录限流与 Origin 校验（`lib/python/observability/security.py`）；明文密码不落盘、不入日志，凭证只存 `conf/.env.observability`，不提供默认密码
- 观测机器读取：`/api/observability/v1/*` 与 `/metrics` 用独立只读 Bearer Token；只读 Token 与上报写 Token 不允许同值（配置校验拒绝），只读 Token 不能改会话也不能上报
- 观测上报：`POST /internal/observability/events` 只接受回环来源 + 独立写 Token，不复用文件推送凭证；Nginx 模板对 `/internal/observability/` 对外返回 404

## 数据安全

- 敏感数据清单：`conf/.env`（飞书 / 微信凭证、模型 API key）、`conf/.env.observability`（观测密码哈希、只读 / 上报 Token、HMAC key，0600）、`rules/admin.md`（管理员 user_id 等）、`secret-allowlist.local.txt`（扫描白名单，gitignored）、`conf/wechat/account.json`
- 观测隐私：只采集白名单运行元数据，不落聊天正文、思考、工具参数 / 结果、文件名与路径、原始报错；会话仅 HMAC 去标识化，且不作为 Prometheus 标签。pi 历史用量只读原生 JSONL 的 usage 元数据，账本仅存数字、模型、日期与 HMAC 标识，源文件删除后保留已采集用量
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
