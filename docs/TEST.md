# TEST

## 测试策略

- pytest 单元 / 链路测试为主，pi JSONL 与飞书 / 微信 API 使用 mock；进程取消另用本地 Python 子进程验证；真实链路单独手动冒烟
- 运行：项目根执行 `pytest -c conf/pytest.ini -q`，或 `cd conf && pytest -q`
- Node sidecar：项目根执行 `node --test tests/wechat-sidecar.test.mjs`
- 以下是回归目标和文件映射；本次执行记录见 [需求变更记录](requirement-changes.md)。真实模型和渠道实发需单独冒烟，不能由 mock 测试替代

## 测试要点

1. 两渠道拒绝伪造签名或错误 token，保留消息解析、格式化、文件与图片能力
2. 会话隔离、message_id 去重、同会话 FIFO 排队；两渠道每轮只传当前 user 消息，不存 assistant 历史
3. pi JSONL 契约：只取正文 delta，过滤 user / thinking / toolcall，依据最后一条 assistant `message_end` 判断成败
4. `/new`、`/reset` 清附件与 pi 映射；`/backend`、`/pi` 和旧命令、压缩说明命令不改变会话状态
5. 新配置优先于旧回退；`PI_WORK_DIR` 是最终 cwd，默认与旧配置解析结果保持历史路径，不能多一层或少一层 `/pi`
6. 定时、记忆、队列、图片发现与推送功能不因后端模块删除而丢失
7. 取消、重试、熔断与超时分别验证；持续输出时的总超时不能以 idle 超时用例代替
8. 入站图片经 pi `@file` 多模态传递：`image_paths` 去重/转绝对/过滤不存在，插入 prompt 前；大图 base64 回显不撑爆 readline limit；微信图片纯 hex AES 密钥可解密

## 要点与用例映射

| 测试要点 | 用例路径 | 类型 |
|---|---|---|
| 飞书 WS 与单条回复 | `tests/test_feishu_ws.py`、`tests/test_handler_single_reply.py` | 链路 |
| 飞书格式化与分段 | `tests/test_feishu_formatting.py` | 单元 |
| 飞书图片与文件归档 | `tests/test_feishu_media.py`、`tests/test_file_archive.py` | 单元 |
| pi 多模态图片输入 | `tests/test_pi_multimodal.py`、`tests/test_handler_single_reply.py`、`tests/test_wechat_handler.py`、`tests/wechat-sidecar.test.mjs` | 单元 |
| 飞书 reaction / 回执 | `tests/test_feishu_reaction.py` | 单元 |
| 微信消息与鉴权 | `tests/test_wechat_handler.py`、`tests/test_signature_validation.py` | 单元 |
| 消息解析 | `tests/test_message_parsing.py` | 单元 |
| key、附件与去重 | `tests/test_session_manager.py`、`tests/test_inbound_file_session.py` | 单元 |
| FIFO 与取消 | `tests/test_message_queue.py`、`tests/test_new_command.py` | 单元 |
| 会话 / 状态 / 旧命令兼容 | `tests/test_new_command.py`、`tests/test_wechat_handler.py` | 单元 |
| 每日任务与提醒恢复 | `tests/test_daily_scheduler.py`、`tests/test_reminder_scheduler.py` | 单元 |
| 长期记忆注入 | `tests/test_memory.py` | 单元 |
| pi 调用、原生会话与错误语义 | `tests/test_pi_session.py`、`tests/test_pi_chain.py` | 单元 |
| 时间注入与相对日期 | `tests/test_clock.py`、`tests/test_pi_session.py` | 单元 |
| skills 发现与摘要 | `tests/test_skills.py` | 单元 |
| 配置迁移与最终 cwd | `tests/test_config.py` | 单元 |
| pi 启动检查与模型注册同步 | `tests/test_server_startup.py` | 脚本 |
| 出站文件与鉴权 | `tests/test_feishu_file_send.py`、`tests/test_push_file_route.py`、`tests/wechat-sidecar.test.mjs` | 单元 / 路由 |
| pre-push 密钥扫描 | `tests/test_secret_scan.py` | 单元 |

其它 CLI 的专属测试已移除，共用行为迁移到 pi / `AgentClient` 测试替身。技能发现覆盖 `app.skills`，配置迁移覆盖新键、旧键、跨输入源冲突优先级与默认值；项目不再导入旧客户端模块

## 手工验证与安全边界

- 完整清单见 [functional-tests.md](functional-tests.md)
- 需要重启、真实模型调用、改规则或发送渠道消息的场景，在隔离环境执行，不对用户现有服务自动操作
- 使用临时 cwd、session store、agent dir 和调度数据；验证生产路径兼容时只检查解析结果，不移动或删除生产数据
- 未完成项保留待验状态，不把历史测试数量或历史分析报告当成本次证据
