# 核心功能测试清单

> 每次迭代（新功能 / 行为调整 / 缺陷修复）合入前必须对照本清单验证，避免开发改动破坏核心功能。
> 验证方式分为自动化（pytest 与 Node 测试分别执行）和手动冒烟（真实链路）。下表是验收目标；自动化执行结果见需求变更记录，真实模型及渠道冒烟尚未执行
> 涉及重启、改规则、模型或渠道实发的项目在隔离环境执行，不操作用户现有服务、真实配置或运行数据

## 核心功能项

| # | 功能 | 验证要点 | 验证方式 |
|---|------|---------|---------|
| 1 | 飞书发送文件 | 文件下载成功并归档存储到指定目录 | `tests/test_file_archive.py` |
| 2 | 飞书发送图片 | 图片下载成功并交给 CLI 处理 | `tests/test_feishu_media.py` |
| 3 | 定时任务 `/daily` | 创建 / list / cancel 解析正确；到点执行并推送（飞书 + 微信）；重启后任务恢复 | `tests/test_daily_scheduler.py` |
| 4 | 定时提醒 `/remind` | 时间解析（s/m/h/d）、到点提醒、持久化恢复 | `tests/test_reminder_scheduler.py` |
| 5 | 基础对话链路 | 飞书 WS 收文本 → Typing 回执 → 最终答案单条稳定回复 | `tests/test_feishu_ws.py`、`tests/test_handler_single_reply.py` |
| 6 | 微信文本对话 | sidecar 转发消息处理正常；webhook token 校验拒绝非法请求 | `tests/test_wechat_handler.py`、`tests/test_signature_validation.py` |
| 7 | 唯一 pi 状态与旧命令 | `/backend`、`/pi` 只读状态；旧 `/opencode` `/codex` `/claude` `/qodercli` 只提示移除；均不修改 pi 映射、附件通知或写后端状态 | `tests/test_new_command.py`、`tests/test_wechat_handler.py`+ 隔离环境手动冒烟 |
| 8 | 会话命令与去重 | `/new` `/reset` `/stop` 行为正确；同 `message_id` 消息不重复处理；同会话连发消息按 FIFO 排队 | `tests/test_new_command.py`、`tests/test_session_manager.py`、`tests/test_message_queue.py` |
| 9 | 回复格式化 | 超长文本智能分段（保留段落/代码块边界）；Markdown 卡片渲染失败自动降级纯文本 | `tests/test_feishu_formatting.py` |
| 10 | pi 规则与技能 | 规则改后下一轮 `--append-system-prompt` 生效；skills 由 `app.skills` 发现，摘要仅首轮注入，`/skills` 可查询；不导入其它 CLI | `tests/test_skills.py`、`tests/test_pi_session.py`、`tests/test_pi_chain.py` + 隔离环境手动冒烟 |
| 11 | 长期记忆 memory/ | 明确要求时写入并回执（"记住…" → `已记入 memory/…`）；日常提及不写入；查看/修改/软删除可用；重启后记忆仍注入；记忆内容不被主仓跟踪且快照仓无 remote | `tests/test_memory.py` + 手动冒烟：对话中"记住 X"验回执与文件，`git --git-dir=runtime/memory-git log` 验快照 |
| 12 | pi 原生会话与重置 | 两渠道每轮只传当前 user，不写桥接历史；`--session-id` 续接；`/new`、`/reset` 清附件和映射但保留旧 transcript；新轮次不受旧任务回写污染；`/stop` 按 trace_id 回收对应进程组 | `tests/test_pi_chain.py`、`tests/test_pi_session.py` + 隔离环境手动冒烟：追问上一轮，重置后确认隔离 |
| 13 | pi 退出码陷阱与成败判定 | `pi --mode json` 失败时退出码仍为 0，必须靠**最后一条** assistant `message_end.stopReason` 判定；部分输出后报错不得当成功，auto-retry 中间失败后重试成功不得误判为失败；任何情况下不得静默回空 | `tests/test_pi_session.py::test_provider_error_raises_even_though_the_process_exits_zero`、`::test_error_after_partial_text_still_raises`、`::test_intermediate_failure_followed_by_a_retry_success_is_not_an_error` |
| 14 | 出站文件推送 `/push/file` | 飞书 `im/v1/files` 上传 + `msg_type:file` 发送；微信转发 sidecar `/send_file`（getuploadurl → AES-128-ECB → CDN → `type:4` file_item）；`receive_id_type` 按 `ou_`/`on_`/`oc_` 自动识别；无 `PUSH_API_TOKEN` 时 503 关闭；401/400/404/413/502 错误分档；caption 先于文件发出 | `tests/test_feishu_file_send.py`、`tests/test_push_file_route.py`、`tests/wechat-sidecar.test.mjs` + 手动冒烟：小文件实发两个渠道确认收到 |
| 15 | 入站文件进会话 | 归档后排入 `pending_files` 且**不唤醒 agent**；通知搭载下一轮 user 文本（两渠道每轮只传当前 user，桥接层不保存历史）；文件+文字同发合并为一轮；命令轮不消费通知；任务被拒不 drain；归档失败不排通知；`/new` `/reset` 清空；队列上限 20；微信 `type:2` 图片不再被静默丢弃；`describeFileItem` 保留字段名但掩掉 `encrypt_query_param`/`aes_key` | `tests/test_inbound_file_session.py`、`tests/wechat-sidecar.test.mjs` + 手动冒烟：发个文件→再发一句话→确认 agent 知道文件路径且未主动展开 |
| 16 | pi 时间感知 | 每轮 `--append-system-prompt` 注入 `当前系统时间: YYYY-MM-DD HH:MM 周X（时段）`，user prompt 不带时钟；时段由 `app.clock` 计算，不在 transcript 中新增时钟副本 | `tests/test_clock.py`、`tests/test_pi_session.py::test_clock_rides_the_system_prompt_after_rules_and_memory` + 隔离新会话冒烟，检查新增 transcript；不要求清除历史遗留时间戳 |
| 17 | 压缩命令说明 | `/compact`、`/compress` 仅提示 pi 原生管理，不调用摘要模型、不改变 pi 映射或 pending_files、不伪报成功 | `tests/test_new_command.py`、`tests/test_wechat_handler.py` |
| 18 | 配置与启动兼容 | 仅需 pi CLI；新键优先、旧共享键回退；`PI_WORK_DIR` 为最终 cwd，默认路径不变；`GENERATED_IMAGES_DIR` 兼容旧键；忽略旧后端状态且不删除用户文件 | `tests/test_config.py`、`tests/test_server_startup.py` + 隔离环境检查解析目录，不移动生产数据 |
| 19 | pi 故障与进程回收 | 取消、空闲超时、持续输出时总超时、stdout EOF 后进程未退、部分输出后失败与重试分别验证；超时按每次 CLI 尝试计，不覆盖排队和退避 | `tests/test_pi_session.py`、`tests/test_pi_chain.py` + 隔离子进程测试（包括真实本地子进程取消） |

## 迭代验收规则

1. 任何功能改动合入前必须全量测试通过：

```bash
source .venv/bin/activate
pytest -c conf/pytest.ini -q
```

2. 改动涉及 `lib/js/wechat-sidecar.mjs` 时，额外跑 JS 侧测试（pytest 不收集 `.mjs`）：

```bash
node --test tests/wechat-sidecar.test.mjs
```

3. 改动涉及上表功能项时，除自动化测试外，额外执行该项对应的手动冒烟
4. 新增核心功能时，同步在本清单追加一行（功能 + 验证要点 + 验证方式）
