# 知乎开放平台 CLI 使用文档

`zhihu-cli` 是知乎 Skill 的标准执行入口，统一封装公共内容、本人知乎 Context 和 Access Secret 管理。Agent 日常解决用户问题时优先使用 CLI；只有开发者需要自己集成服务时，才直接读取 HTTP API、OAuth 或 MCP 文档。

## 能力与命令

| 能力 | 命令 | 适用场景 |
|---|---|---|
| 知乎搜索 | `zhihu-cli search zhihu` | 查找知乎回答、文章、经验、观点和原文链接 |
| 全网搜索 | `zhihu-cli search global` | 查找新闻、官网和外部权威来源 |
| 知乎热榜 | `zhihu-cli hot` | 了解当前知乎热点议题 |
| 知乎直答 | `zhihu-cli answer` | 快速获得检索增强的 AI 答案 |
| 我的创作 | `zhihu-cli me contents` | 查看当前 Access Secret 所属账号的回答、文章、视频、想法和问题 |
| 我的关注 | `zhihu-cli me followees` | 查看当前账号关注的用户 |
| 我的收藏夹 | `zhihu-cli me favorites lists/items` | 浏览收藏夹及其中公开内容 |
| 我的近期收藏 | `zhihu-cli me favorites recent` | 查看最近一批收藏，不代表完整历史 |

搜索用于取得原始资料，直答用于快速获得总结。深度研究、事实核查和观点比较不要用直答替代搜索。

## 安装与检查

每个 Session 第一次激活 Skill 时，先定位本 Skill 根目录，并做一次无副作用检查：

```bash
# macOS
bash <skill-dir>/scripts/run.sh status

# Windows PowerShell
powershell -ExecutionPolicy Bypass -File <skill-dir>/scripts/run.ps1 status
```

如果 `status` 成功返回合法 JSON 且 `installed=false`，说明当前没有可用且满足最低版本要求的 CLI。Agent 请求用户授权安装或修复；得到明确同意后执行 setup：

```bash
# macOS
bash <skill-dir>/scripts/setup.sh

# Windows PowerShell
powershell -ExecutionPolicy Bypass -File <skill-dir>/scripts/setup.ps1
```

此时还会返回 `next_action=request_install_consent` 和 `update_check.status=not_applicable`。Agent 不需要等待 `verified`；setup 完成后再次运行 `status`。如果命令报错或没有返回合法 JSON，先排查脚本故障。

如果 `status` 返回 `installed=true`，远端更新检查成功时 `update_check.status=verified`，失败时为 `unavailable`。`unavailable` 不等于“已是最新版”，但兼容的本地 CLI 仍可继续使用。后续操作以 `next_action` 为准。

CLI 支持 macOS Apple Silicon、macOS Intel、Windows x64、Linux amd64 和 Linux arm64。Skill ZIP 不包含 CLI 二进制；setup 查询官方 manifest，选择当前平台最新版，下载后校验官方域名与跳转、大小、SHA-256、归档结构和二进制版本。CLI 安装到用户数据目录，不使用管理员权限、不修改 PATH。Linux 默认目录是 `${XDG_DATA_HOME:-$HOME/.local/share}/zhihu-cli`，可用绝对路径 `ZHIHU_CLI_HOME` 覆盖。CLI、Skill 和凭证分别存放，覆盖升级 Skill 不会删除已安装 CLI 或 Access Secret。

setup 成功时 stdout 最后一行返回：

```json
{"ok":true,"installed":true,"downloaded_cli_version":"<cli-version>","binary_path":"/absolute/path/to/zhihu-cli","auth_configured":false,"next_action":"request_access_secret"}
```

Agent 必须读取绝对 `binary_path`，本次任务后续全部通过该路径调用，不依赖 PATH。setup 可重复执行；本地 CLI 等于或高于 Skill 声明的最低版本时直接复用，日常更新使用 `upgrade`，setup 不会主动降级已安装的兼容版本。

更新命令：

```bash
zhihu-cli upgrade --check
zhihu-cli upgrade
zhihu-cli upgrade --rollback
```

`upgrade` 只更新 CLI；Skill 更新由 status 返回 ZIP、SHA-256 和大小，Agent 获得用户同意后交给宿主 Skill 安装机制处理。业务命令不附带更新提醒。

查看当前 CLI 支持的能力，不需要 Access Secret：

```bash
zhihu-cli capabilities
```

CLI 的每一级 `--help` 都包含用途、适用场景、数据边界、参数约束和可复制示例：

```bash
zhihu-cli --help
zhihu-cli search --help
zhihu-cli search zhihu --help
zhihu-cli me favorites items --help
```

Agent 用 `capabilities` 判断当前版本有哪些结构化能力，用具体命令的 `--help` 决定如何调用和管理预期。本文档保留安装、鉴权、错误与完整参数资料，不要求 Agent 在每次调用前重新读取全文。

## 初始化与 Access Secret

### 用户已经提供 Access Secret

```bash
zhihu-cli auth set --secret-stdin
```

Agent 启动命令后通过进程标准输入发送用户在对话中提供的 Access Secret。CLI 仅在设置时调用一次本人内容接口 `ContentType=all&Limit=1&Offset=0` 验证，成功后写入操作系统密钥链。日常业务命令不额外预检。不得在结果、日志或后续回复中重复完整 Access Secret。

CLI 兼容直接参数与 `-`，但桌面 Agent 初始化默认使用 `--secret-stdin`，避免终端交互以及凭证进入进程参数：

```bash
printf '%s' "$ZHIHU_ACCESS_SECRET" | zhihu-cli auth set --secret-stdin
```

### 用户尚未提供 Access Secret

```bash
zhihu-cli init
```

没有可用凭证时，CLI 返回 `AUTH_REQUIRED` 和：

```text
https://developer.zhihu.com/profile
```

引导用户用知乎账号登录，点击「申请新 Access Secret」，再把获得的 Access Secret 交给 Agent，随后通过 `auth set --secret-stdin` 配置。

### 查看和清除凭证

```bash
zhihu-cli auth status
zhihu-cli auth status --verify
zhihu-cli auth logout
```

- `auth status` 查看凭证来源、密钥链状态、脱敏值和本地最后验证时间，不联网验证。
- `auth status --verify` 显式调用一次本人内容接口在线验证当前 Access Secret；普通 `auth status` 不消耗额度。
- `auth logout` 只删除本机密钥链中的 Access Secret，不撤销开放平台上的 Access Secret，也不修改环境变量。

### 凭证读取顺序

业务命令按以下顺序读取：

1. `ZHIHU_ACCESS_SECRET` 环境变量。
2. 操作系统密钥链。
3. 无凭证时返回 `AUTH_REQUIRED`。

环境变量适用于 CI、容器、Agent 宿主 Secret Store 和无法访问系统密钥链的沙箱：

```bash
export ZHIHU_ACCESS_SECRET='<access-secret>'
zhihu-cli auth status
```

CLI 不自动读取项目 `.env`，也不把 Access Secret 写入 Skill 或项目目录。环境变量存在但无效时不得静默回退密钥链。

Linux 桌面的“操作系统密钥链”具体指 Secret Service/D-Bus（例如 GNOME Keyring）。纯 SSH、CI 或容器通常没有可用的会话 D-Bus，应由宿主 Secret Store 为单个进程注入 `ZHIHU_ACCESS_SECRET`。`auth set` 在 Secret Service 不可用时返回 `KEYCHAIN_UNAVAILABLE`，不会把凭据降级保存到普通文件或本地加密文件。

Access Secret 是不透明字符串。CLI 不假设固定长度、前缀或字符集，只做非空检查和在线验证。

v0.1 没有剩余额度查询 API，也不从网页抓取额度。需要查看余额时，打开 <https://developer.zhihu.com/profile> 的用量统计页面。

## 搜索知乎

```bash
zhihu-cli search zhihu --query 'Agent Memory' --count 10
```

参数：

| 参数 | 必填 | 默认值 | 范围 | 说明 |
|---|---:|---:|---:|---|
| `--query` | 是 | - | 非空字符串 | 搜索问题或关键词 |
| `--count` | 否 | 10 | 1-10 | 返回结果数量 |

返回知乎社区原始内容及链接。回答用户时优先呈现真正有阅读价值的标题、作者、摘要和 `Url`，不要只输出脱离来源的二次总结。

## 搜索全网

```bash
zhihu-cli search global \
  --query 'Agent Memory' \
  --count 10 \
  --search-db all
```

参数：

| 参数 | 必填 | 默认值 | 范围 | 说明 |
|---|---:|---:|---:|---|
| `--query` | 是 | - | 非空字符串 | 搜索问题或关键词 |
| `--count` | 否 | 10 | 1-20 | 返回结果数量 |
| `--filter` | 否 | - | API filter 表达式 | 筛选站点、时间等条件 |
| `--search-db` | 否 | `all` | `all`、`realtime`、`static` | 选择索引库 |

`--filter` 的完整语法以 [HTTP API 文档](http-api.md) 为准。CLI 负责 URL 编码，不改写表达式。

## 获取知乎热榜

```bash
zhihu-cli hot --limit 20
```

| 参数 | 必填 | 默认值 | 范围 | 说明 |
|---|---:|---:|---:|---|
| `--limit` | 否 | 30 | 1-30 | 返回热榜条目数 |

热榜适合发现议题，不等于完整事实。用户要理解背景、核实信息或阅读讨论时，再调用知乎搜索、全网搜索或直答。

## 调用知乎直答

```bash
zhihu-cli answer \
  --query '如何理解 Agent Memory' \
  --model zhida-fast-1p5
```

参数：

| 参数 | 必填 | 默认值 | 说明 |
|---|---:|---|---|
| `--query` | 是 | - | 单轮用户问题 |
| `--model` | 否 | `zhida-fast-1p5` | `zhida-fast-1p5`、`zhida-thinking-1p5` 或 `zhida-agent` |
| `--stream` | 否 | false | 启用流式输出，默认原样透传 SSE |
| `--output` | 否 | `json` / 流式时 `sse` | `json`、`sse`、`text`；`text` 只用于显式的终端打字机输出 |

CLI 的 `answer` 聚焦单轮 Agent 调用。Agent 使用非流式 JSON 或流式 SSE；人类需要打字机效果时使用 `--stream --output text`。CLI 不根据 TTY 自动切换协议。开发者需要多轮 `messages` 或完整原始参数时，直接使用 [HTTP API 文档](http-api.md)。

## 查看我的知乎创作

```bash
zhihu-cli me contents --type all --sort ts --order desc --offset 0 --limit 20
```

| 参数 | 默认值 | 范围 |
|---|---|---|
| `--type` | `all` | `all/answer/article/zvideo/pin/question` |
| `--sort` | `ts` | `like_count/ts` |
| `--order` | `desc` | `asc/desc` |
| `--offset` | `0` | 非负 Int64 |
| `--limit` | `20` | `1-50` |

## 查看我的关注

```bash
zhihu-cli me followees --offset 0 --limit 20
```

`--offset` 默认为 0，`--limit` 默认为 20、范围 1-50。

## 查看我的收藏

```bash
zhihu-cli me favorites lists --limit 20
zhihu-cli me favorites items --url-token 123456789 --offset 0 --limit 20
zhihu-cli me favorites items --id 123456789 --offset 0 --limit 20
zhihu-cli me favorites recent --limit 20
```

- `items` 的 `--url-token` 与 `--id` 必须且只能提供一个，均为正 Int64。
- `offset` 是非负 Int64，`limit` 默认 20、范围 1-50。
- 分页响应使用 `Paging.IsEnd` 和 `Paging.NextOffset`；CLI 不自动拉取全部分页。
- `recent` 没有 Offset，只返回近期收藏。
- 收藏夹列表线上响应不返回 Paging，且 2026-07-23 实测服务端忽略 Offset。CLI 只提供 `--limit`，不能承诺遍历全部收藏夹。
- 所有 `me` 命令只查询当前 Access Secret 所属账号，不接受 OAuth Token。

## 全局参数

```text
zhihu-cli <command> [subcommand] [flags]
```

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--timeout <duration>` | 按能力设置 | 请求超时，例如 `10s`、`120s` |
| `--pretty` | false | 美化 JSON 输出，不改变字段和值 |
| `--verbose` | false | 向 stderr 输出不含凭证的诊断信息 |
| `--help` | - | 查看帮助 |

stdout 只输出结果，stderr 只输出诊断。Agent 同时使用退出码和 stdout 判断结果。

## 返回数据

成功时，CLI 将服务端原始响应写入 stdout：

- 不裁剪字段。
- 不修改字段名、大小写或嵌套结构。
- 不丢弃 API 后续新增的未知字段。
- `--pretty` 只改变 JSON 空白。
- 直答流式模式保持服务端事件顺序，不拼装成另一套结构。
- v0.1 服务域名固定为 `https://developer.zhihu.com`，Access Secret 不会发送到其他主机。

各字段的含义以 [HTTP API 文档](http-api.md) 为准。

## 错误与退出码

CLI 自身错误使用稳定 JSON：

```json
{
  "ok": false,
  "error": {
    "source": "cli",
    "code": "AUTH_REQUIRED",
    "message": "请登录知乎数据开放平台并申请新 Access Secret",
    "action_url": "https://developer.zhihu.com/profile"
  }
}
```

常见错误：

| 错误码 | 处理方式 |
|---|---|
| `AUTH_REQUIRED` | 打开 `action_url`，申请 Access Secret 后执行 `auth set --secret-stdin` |
| `AUTH_INVALID` | 重新检查或申请 Access Secret；不要回显旧 Access Secret |
| `KEYCHAIN_UNAVAILABLE` | 通过宿主 Secret Store 注入 `ZHIHU_ACCESS_SECRET` |
| `ENV_SHADOWS_KEYCHAIN` | 当前环境变量覆盖了刚保存的密钥链 Access Secret |
| 服务端 `Code: 30001` | 频率限制；停止主动重试 |
| 服务端 `Code: 30002` | 配额耗尽；告知受影响能力和恢复条件 |
| `NETWORK_ERROR` / `TIMEOUT` | 检查网络；仅对幂等搜索和热榜做有限重试 |
| `UPSTREAM_ERROR` | 保留 request ID，稍后重试或联系开放平台 |

退出码：

| 退出码 | 含义 |
|---:|---|
| 0 | 成功 |
| 2 | 参数错误 |
| 3 | 鉴权缺失、无效或来源冲突 |
| 4 | 配额耗尽或频率限制 |
| 5 | 网络错误或超时 |
| 6 | 服务端或未知协议错误 |
| 7 | 系统密钥链不可用 |
| 8 | 安装、升级或完整性校验失败 |

内容 GET API 可能在 HTTP 200 中返回业务错误。Agent 必须以 CLI 的非零退出码判断失败，不能把 `Code: 20001` 当成空结果。

## 安全要求

- 不在回复、stdout、stderr、日志、Shell 历史摘要或项目文件中重复完整 Access Secret。
- 不把 Access Secret 写入 Skill 内部；Skill 更新不得覆盖凭证。
- 不因 `--verbose` 输出 Authorization 请求头。
- 不记录用户 query、直答 messages、创作、关注、收藏或完整响应内容。
- Access Secret 泄露后，引导用户在开放平台个人中心删除并重新申请；删除后的 Access Secret 无法恢复。

## 相关资料

- 注册、额度与客服：[开放平台指南](open-platform.md)
- 原始请求、响应与字段：[HTTP API 文档](http-api.md)
- MCP 客户端接入：[MCP 接入文档](mcp.md)
