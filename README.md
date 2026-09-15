# codeClaw

飞书 / 微信 → pi 的个人助手桥接服务

pi 是唯一后端，负责推理、工具调用、原生会话与上下文压缩。codeClaw 负责消息收发、渠道适配、任务队列和进程管理，后续围绕 pi 持续迭代，不再维护多后端路由

## 架构

```text
飞书 WebSocket / Webhook ─┐
                         ├→ 命令分发 / 去重 / 会话队列 → PiCliClient → pi CLI
微信 iLink sidecar ───────┘                                  ↓
                         ← 渠道格式化 / 文件与图片回传 ← JSONL 回复
```

Python + FastAPI 提供服务，Node.js sidecar 接入微信。单实例、文件持久化，不依赖数据库或 Redis

## 能力

- 双渠道对话，支持会话隔离、消息去重、FIFO 排队与任务取消
- pi 原生会话续接与自动压缩，不在桥接层重复保存对话历史
- 文件归档与附件通知、飞书图片处理、双渠道文件推送
- 长期记忆、规则热加载、技能查询、定时提醒与每日任务
- pi 进程超时、重试、熔断与取消回收；部分流式输出后不重试

## 快速开始

准备 Python 环境和可用的 pi CLI，先完成 pi 的模型配置；微信接入另外需要 Node.js。pi 参数和模型模板见 [pi CLI 参考](docs/references/pi-cli.txt)

```bash
# 仅首次创建，保留已有配置
[ -f conf/.env ] || cp conf/.env.example conf/.env

python3 -m venv .venv
.venv/bin/python -m pip install -r conf/requirements.txt
```

编辑 `conf/.env`，填写飞书凭证 `FEISHU_APP_ID`、`FEISHU_APP_SECRET`，并确认 `PI_CLI_BIN`、`PI_MODEL` 及模型服务凭证。模型注册模板在 `conf/pi/`，真实密钥不要提交到仓库

未使用进程管理器时：

```bash
./bin/start
./bin/server status
./bin/server restart
./bin/server stop
```

微信首次登录与独立启动：

```bash
./bin/server wx login
./bin/server wx start
```

### Supervisor 托管

当前部署由 Supervisor 托管，使用已有服务组管理，不要混用 `./bin/server start|restart`，避免重复启动。`./bin/server status` 只检查脚本 PID 文件，不能代表 Supervisor 托管状态

```bash
supervisorctl status
supervisorctl restart 'codeclaw-stack:*'
curl --fail http://127.0.0.1:8080/healthz
```

健康接口返回 `{"status":"ok"}` 表示 HTTP 服务可用，不代表模型调用或渠道收发已验证

## 对话命令

| 命令 | 行为 |
|---|---|
| `/help` | 查看当前渠道帮助 |
| `/new`、`/reset` | 清空待处理附件与 pi 会话映射，下轮开启新上下文 |
| `/stop` | 取消当前任务并清空排队消息 |
| `/backend`、`/pi` | 显示唯一后端 pi，不切换或重置会话 |
| `/skills` | 查询本机可用技能 |
| `/remind 10m 内容` | 飞书定时提醒，支持 `s/m/h/d`；微信暂不支持 |
| `/daily 08:00 提示词` | 创建每日任务，支持 `/daily list`、`/daily cancel <id>` |

`/compact`、`/compress` 仅说明上下文由 pi 原生管理，不执行手工压缩。旧后端切换命令仅提示已移除

## 会话、记忆与技能

`PiCliClient` 维护渠道会话 key → pi session ID 映射。两个渠道每轮只发送当前 user 消息，历史留在 pi；`SessionManager` 只管理附件通知。`/new`、`/reset` 不删除旧 transcript 或归档文件

文件归档后不主动唤醒 pi，路径通知搭载下一轮文本；飞书图片会下载并交给 pi 处理。出站文件统一走 `POST /push/file`，须配置 `PUSH_API_TOKEN`，详见 [渠道接入](docs/channels.md)

`rules/system.md`、私有 `rules/admin.md` 与长期记忆通过 `--append-system-prompt` 每轮重新读取。时间与时段也走 system prompt，不写入 user transcript。长期记忆仅在用户明确要求时由 agent 写入，详见 [记忆设计](docs/memory.md)

`app.skills` 扫描项目及本机技能目录，`/skills` 可实时查询；技能摘要只在新会话首轮注入，新增技能后可用 `/new` 刷新摘要

## 配置与迁移

全部示例见 [conf/.env.example](conf/.env.example)

| 配置 | 说明 |
|---|---|
| `PI_CLI_BIN` | pi 命令名或绝对路径，默认 `pi` |
| `PI_MODEL` | `provider/model-id`，留空使用 pi 配置 |
| `PI_WORK_DIR` | pi 最终工作目录，默认 `./runtime/codex-workdir/pi` |
| `PI_SESSION_STORE_PATH` | 会话映射文件，默认 `./runtime/server/pi-sessions.json` |
| `PI_TIMEOUT_SECONDS` | 每次 CLI 尝试总时限，默认 300 秒，不包含排队与重试退避 |
| `PI_IDLE_TIMEOUT_SECONDS` | 等待下一行 stdout 的空闲时限，默认 120 秒 |
| `GENERATED_IMAGES_DIR` | 飞书生成图片发现目录 |

新配置键优先，旧 `CODEX_*` 共享参数仅作为迁移回退；旧 `CODEX_WORK_DIR` 需要追加 `/pi` 才对应新 `PI_WORK_DIR`。保留历史目录名称是为兼容已有 pi 会话，不代表仍支持旧后端

不要直接移动现有 cwd、session store 或 `PI_CODING_AGENT_DIR`：恢复会话需要映射与 pi transcript 同时可用。详细规则见 [单 pi 接入与迁移](docs/routing.md)

## 测试

```bash
.venv/bin/python -m pytest -c conf/pytest.ini -q
node --test tests/wechat-sidecar.test.mjs
```

2026-09-11 回归：463 项 Python 测试、16 项 Node 测试通过。随后已通过 Supervisor 重启主服务与微信 sidecar，健康检查正常；真实模型与渠道实发仍需单独冒烟。验收清单见 [核心功能测试](docs/functional-tests.md)

## 项目结构

```text
bin/          服务控制与托管启动入口
conf/         环境配置与 pi 模型模板
lib/python/   应用、渠道、pi 客户端与会话队列
lib/js/       微信 sidecar
rules/        公共规则与私有管理员设定
skills/       项目级技能
memory/       长期记忆
runtime/      会话映射与运行状态
tests/        Python 与 Node 测试
docs/         架构、运维与变更记录
```

[文档索引](docs/index.md) · [架构](docs/architecture.md) · [会话管理](docs/sessions.md) · [变更记录](docs/requirement-changes.md)

## 许可

MIT
