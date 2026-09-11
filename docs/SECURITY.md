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

## 数据安全

- 敏感数据清单：`conf/.env`（飞书 / 微信凭证、模型 API key）、`rules/admin.md`（管理员 user_id 等）、`secret-allowlist.local.txt`（扫描白名单，gitignored）、`conf/wechat/account.json`
- 脱敏要求：推送 GitHub 前检查 `rules/`、`conf/`、`.qoder/` 下是否有用户敏感信息
- 备份策略：`TODO: 待补充`（当前无自动备份）；现有 pi cwd、session 映射与 `PI_CODING_AGENT_DIR` 中的会话数据不自动迁移或删除，后续人工变更前应同时备份这些恢复条件

## 已知风险与例外

| 风险 | 等级 | 处理状态 |
|---|---|---|
| pi CLI 失败时仍 `EXIT=0`，需解析 `stopReason=error` 判断（0.84.2 依旧） | 中 | 已在解析层处理，见 `docs/references/pi-cli.txt` |
| 模型生成内容误提交密钥 | 中 | pre-push 扫描兜底 |
| 密钥扫描白名单误放行 | 低 | 白名单本地文件不入库，逐条维护 |
