# codeClaw

## 项目核心描述

个人 IM → CLI 桥接服务：飞书 + 微信双通道接入，把消息送到唯一后端 pi，再把回复送回来。能力归 pi，编排归 harness——不造智能，只做消息收发、渠道适配与 pi 进程管理。技术栈：Python 3.13+ / FastAPI / httpx / pydantic-settings / cryptography，微信侧为 Node.js sidecar。

## 一级指令

> 本节是 Agent 必须遵守的最高优先级指令，任何开发动作前先核对。

### 常用命令

- 运行：`./bin/server start`（首次启动引导配置飞书凭证）、`./bin/server stop|restart|status`
- 测试：`cd conf && pytest -q`（配置在 `conf/pytest.ini`，pythonpath 指向 `lib/python`）
- 依赖安装：`pip install -r conf/requirements.txt`

### 硬性约束

- 个人项目简洁优先：单实例部署，文件持久化，不引入外部存储；代码改动优先验证现有测试通过
- 迭代必须回归核心功能：每次迭代完成后对照 `docs/functional-tests.md` 验证，避免破坏已有功能
- 每次需求变化（新功能 / 行为调整 / 缺陷修复）完成后，在 `docs/requirement-changes.md` 顶部追加一条记录（日期 + 需求内容 + 影响范围）
- 不提交敏感信息：配置走 `conf/.env`（不入库，仅 `.env.example`），`rules/admin.md` 保持 gitignored；推送 GitHub 前走 pre-push 密钥扫描钩子（`.qoder/hooks/`），注意脱敏

## 代码地图

> 粒度到一级模块，不下探到函数级。

```
lib/python/
  app/            → FastAPI 入口(main)、配置(config)、命令分发(commands)、skills 发现(skills)、记忆(memory)、日志
  channel/feishu/ → 飞书渠道全链路：webhook 解析(handler)、WS 长连接(ws_client)、
                    安全校验(security)、消息回发(client)、格式化(formatting)、图片(media)
  channel/wechat/ → 微信渠道处理（接收 sidecar 转发的消息）
  core/agent/     → PiCliClient（原生会话/流式/取消/重试/熔断）与轻量接口和异常(types)
  core/session/   → 会话隔离与附件通知(manager)、去重(deduplicator)、任务注册(task_registry)、
                    定时提醒(reminder_scheduler)、每日任务(daily_scheduler)、消息队列(message_queue)

lib/js/wechat-sidecar.mjs → 微信 iLink Bot 长轮询 sidecar（Node.js）
bin/server        → 服务控制（start/stop/restart/status/wx login|start|stop）
conf/.env.example → 全部配置项及默认值（配置绑定在 lib/python/app/config.py）
rules/            → 注入 pi 的规则：system.md 公共 / admin.md 私有（gitignored），走 `--append-system-prompt`
skills/           → 项目级 skills；每轮时间通过 pi system prompt 注入，不落入会话历史
docs/index.md     → 项目文档索引 **重点，不了解项目的话优先看这里**
```

## 文档索引

| 文档 | 用途 |
|---|---|
| `docs/index.md` | 文档总索引 |
| `docs/PRODUCT.md` | 产品功能项清单，开发方向锚点 |
| `docs/architecture.md` | 系统架构与技术栈（对应设计文档） |
| `docs/requirement-changes.md` | 需求变更记录（带时间戳，倒序） |
| `docs/TEST.md` | 测试要点与用例映射 |
| `docs/functional-tests.md` | 核心功能回归测试清单（迭代验收必过） |
| `docs/SECURITY.md` | 安全要求 |
| `docs/RELIABILITY.md` | 可靠性与运维 |
| `docs/QUALITY_SCORE.md` | 质量评分 |
| `docs/references/` | 第三方依赖与外部系统参考（pi / 飞书 / 微信） |

## 开发文档

- 微信接入参考：https://docs.openclaw.ai/zh-CN/channels/wechat
- 飞书应用开发：https://open.feishu.cn/document/client-docs/bot-v3/bot-overview
