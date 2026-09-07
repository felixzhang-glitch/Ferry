---
name: zhihu
description: >-
  使用知乎开放平台搜索知乎和全网内容、获取热榜、调用知乎直答，或读取当前用户自己的知乎创作、关注与收藏。用户提到知乎搜索、社区观点、真实经验、热点、热榜、知乎直答、我的知乎内容、我的关注、我的收藏、开放平台、API、MCP、Access Secret，或要求查看、安装和配置知乎 Skill 时使用。深度研究优先返回搜索原始来源；本人数据只读取完成任务所需的最小范围。
---

# 知乎开放平台

当前 Skill 版本：0.3.0

通过知乎官方 CLI 使用公共知识与当前用户自己的知乎 Context。日常任务优先调用 CLI；只有开发接入场景才读取原始 HTTP API、OAuth 或 MCP 文档。

## 首次检查与初始化

每个 Session 第一次激活这个 Skill 时，先定位本文件所在的 Skill 根目录，再运行一次无副作用的状态检查。同一 Session 后续调用不要重复检查，也不要先调用 PATH 中来源不明的 `zhihu-cli`。

Skill 的安装、升级、备份与回滚由宿主管理。若宿主创建备份，应备份完整的 Skill 目录，并存放在非自动发现区域；不得在任何 Skill 自动发现目录中创建同名、带后缀或其他仍可被识别为 Skill 的备份目录，避免宿主同时发现多个 `zhihu` Skill。

```bash
# macOS / Linux
bash <skill-dir>/scripts/run.sh status

# Windows PowerShell
powershell -ExecutionPolicy Bypass -File <skill-dir>/scripts/run.ps1 status
```

根据返回 JSON 处理：

1. `installed=false`：说明 CLI 将安装到用户目录，不需要管理员权限，也不修改 PATH；询问用户是否现在安装。未得到明确同意时停止。
2. `update_check.status=unavailable`：只表示本次无法确认远端版本，不得声称已是最新版；本地 CLI 可用时继续用户任务。
3. `compatible=false`：当前 CLI 低于 Skill 最低要求，先请求用户授权升级，升级完成前不调用业务命令。
4. CLI 或 Skill 存在可选更新：先完成当前任务，再简短提醒用户；只有用户同意后才更新。
5. 用户已经明确要求“安装并初始化”，或提供了明确要求完成初始化的开放平台安装 prompt：视为同时授权安装 CLI、在线验证 Access Secret，并最小读取一条本人内容作为验收；后两项会调用开放平台接口，可能消耗接口额度，无需逐项重复询问。若用户只授权安装，不执行第 9 步。
6. 获得授权后，运行对应平台的 `scripts/setup.sh` 或 `scripts/setup.ps1`，保存 stdout JSON 中的绝对 `binary_path`，再运行一次 status。
7. `auth.configured=false`：引导用户打开 <https://developer.zhihu.com/profile>，登录并手动生成 Access Secret，然后等待用户在对话中发送。
8. 收到 Access Secret 后，不在回复中复述完整内容。启动以下命令，并通过进程标准输入传入 Secret：

```text
<binary_path> auth set --secret-stdin
```

9. 配置成功且已有第 5 条初始化授权，或另行得到用户明确同意后，执行：

```text
<binary_path> auth status --verify
<binary_path> me contents --type all --limit 1
```

`auth status --verify` 会发起一次本人内容相关请求验证凭证，`me contents --type all --limit 1` 会再发起一次最小业务请求验收实际命令；两次调用都可能消耗接口额度。两条命令都成功后才报告初始化完成；内容列表为空也算成功。已经安装并完成授权时，不重复初始化，直接处理当前任务。

本次任务的所有调用都使用状态检查或 setup 返回的 `binary_path`。下文 `<CLI>` 均代表这个绝对路径，不是要求 PATH 中存在裸命令。

Skill 包不携带 CLI 二进制。setup 获得用户授权后，从发布时注入的官方 HTTPS manifest 只下载当前平台版本，校验 host、文件大小、SHA-256、归档结构和二进制自报版本后安装到用户目录；不使用 sudo，也不修改 PATH。Linux 默认遵循 XDG 用户数据目录；桌面凭据使用 Secret Service，headless 使用进程级 `ZHIHU_ACCESS_SECRET`。安装协议和故障处理见 [CLI 使用文档](references/cli.md)。

## 选择能力

| 用户目标 | 命令 | 边界 |
|---|---|---|
| 找知乎回答、文章、经验或观点 | `search zhihu` | 返回知乎社区原始内容和链接，适合阅读、研究和保留证据 |
| 找新闻、官网或外部权威来源 | `search global` | 返回知乎之外的全网来源 |
| 同时需要社区观点和外部证据 | 两种搜索分别调用 | 分开检索后综合，不把两类来源混成一个黑盒 |
| 了解当前关注热点 | `hot` | 只代表当前热度；需要解释或核实时继续搜索 |
| 快速获得综合答案 | `answer` | 先检索再生成答案，不替代原始资料研究 |
| 查看我的创作、关注和收藏 | `me ...` | 只查询当前 Access Secret 所属账号的公开范围数据 |

只调用完成用户目标所需的最小组合。深度研究、事实核查、观点比较和原文阅读使用搜索，不用直答替代原始资料。

## 调用

不确定参数、输出或边界时，先运行 `<CLI> <command> --help`。CLI help 是当前版本的运行时事实源；`<CLI> capabilities` 提供机器可解析的能力清单。

### 搜索知乎

```text
<CLI> search zhihu --query "用户问题" --count 10
```

优先使用返回的 `Title`、`AuthorName`、`ContentText` 和 `Url`。搜索摘要不是完整原文。

### 搜索全网

```text
<CLI> search global --query "用户问题" --count 10
```

需要站点、时间、索引库等高级筛选时，先运行 `<CLI> search global --help`。

### 获取知乎热榜

```text
<CLI> hot --limit 20
```

热榜适合发现议题，不等于事实核查或完整事件解释。

### 调用知乎直答

```text
<CLI> answer --query "用户问题"
```

需要切换快速、深度思考或智能检索模型，以及使用流式输出时，先运行 `<CLI> answer --help`。

### 查看我的创作和关注

```text
<CLI> me contents --type all --sort ts --order desc --offset 0 --limit 20
<CLI> me followees --offset 0 --limit 20
```

创作接口只返回标题与摘要，不把 `Summary` 当作完整正文。分页响应的 `Paging.IsEnd=false` 时，只有用户需要更多结果才使用 `NextOffset` 请求下一页。

### 查看我的收藏

```text
<CLI> me favorites recent --limit 20
<CLI> me favorites lists --limit 20
<CLI> me favorites items --url-token 123456789 --offset 0 --limit 20
```

- `recent` 只表示近期收藏，没有分页，不等于完整历史。
- 读取指定收藏夹时，先从 `favorites lists` 获取 URL Token，再调用 `favorites items`。
- 收藏夹列表当前没有 Paging，服务端忽略 Offset；CLI 因此只提供 `--limit`，不承诺遍历全部收藏夹。

本人命令不得添加 OAuth Token、用户 ID 或其他代查参数。未经用户明确要求，不把完整关注或收藏写入文件或长期记忆。

## 呈现搜索结果

根据用户问题组织结论，并把支撑判断的来源放在附近：

```text
结论或资料说明

- 标题 — 作者
  最相关的原始摘要
  原文链接
```

优先保留真正支撑回答的结果，不机械罗列全部返回项。来源冲突时直接呈现差异，不强行合并。

## 按需读取参考资料

- 安装、认证、完整命令、输出和错误：读取 [CLI 使用文档](references/cli.md)。
- Access Secret 申请、额度、术语和联系方式：读取 [开放平台指南](references/open-platform.md)。
- 在代码或服务中直接接入公共内容 API：读取 [HTTP API 文档](references/http-api.md)。
- 开发本人或 OAuth 授权用户的创作、关注和收藏能力：读取 [用户数据 API](references/user-api.md)。
- 开发“知乎登录”或代表其他已授权用户访问数据：同时读取 [OAuth 应用集成](references/oauth.md) 和 [用户数据 API](references/user-api.md)。CLI 日常调用不使用 OAuth。
- 在 MCP 客户端中配置知乎现有服务：读取 [MCP 接入文档](references/mcp.md)。本 Skill 不建设新的 MCP Server。

日常调用不要自行重写 CLI 已封装的 HTTP 鉴权、时间戳、重试和错误处理。根据对应命令返回的 `Code`、`Message`、`Data` 或 Chat Completions 字段处理结果。

## 错误处理

- `AUTH_REQUIRED`：展示 `action_url`，引导用户申请 Access Secret。
- Access Secret 无效：引导用户重新生成或配置，不回显原值。
- `ENV_SHADOWS_KEYCHAIN`：说明环境变量正在覆盖系统凭证库配置。
- 配额或频率限制：停止重复调用，说明受影响能力和服务端错误。
- 搜索无结果：缩短或改写查询；不要把鉴权失败误报为无结果。
- 服务端错误或超时：遵循 CLI 返回，不额外重试直答 POST。
