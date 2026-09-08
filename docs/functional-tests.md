# 核心功能测试清单

> 每次迭代（新功能 / 行为调整 / 缺陷修复）合入前必须对照本清单验证，避免开发改动破坏核心功能。
> 验证方式分两类：自动化（对应 `tests/` 下测试文件，跑 pytest 即覆盖）、手动冒烟（无法自动化的真实链路）。

## 核心功能项

| # | 功能 | 验证要点 | 验证方式 |
|---|------|---------|---------|
| 1 | 飞书发送文件 | 文件下载成功并归档存储到指定目录 | `tests/test_file_archive.py` |
| 2 | 飞书发送图片 | 图片下载成功并交给 CLI 处理 | `tests/test_feishu_media.py` |
| 3 | 定时任务 `/daily` | 创建 / list / cancel 解析正确；到点执行并推送（飞书 + 微信）；重启后任务恢复 | `tests/test_daily_scheduler.py` |
| 4 | 定时提醒 `/remind` | 时间解析（s/m/h/d）、到点提醒、持久化恢复 | `tests/test_reminder_scheduler.py` |
| 5 | 基础对话链路 | 飞书 WS 收文本 → Typing 回执 → 最终答案单条稳定回复 | `tests/test_feishu_ws.py`、`tests/test_handler_single_reply.py` |
| 6 | 微信文本对话 | sidecar 转发消息处理正常；webhook token 校验拒绝非法请求 | `tests/test_wechat_handler.py`、`tests/test_signature_validation.py` |
| 7 | 后端切换 | `/pi` `/opencode` `/codex` `/claude` `/qodercli` 切换生效；状态持久化重启保留；切换后清空会话上下文 | 手动冒烟：切换后端 → `/backend` 确认 → 重启服务再确认 |
| 8 | 会话命令与去重 | `/new` `/reset` `/stop` 行为正确；同 `message_id` 消息不重复处理；同会话连发消息按 FIFO 排队 | `tests/test_new_command.py`、`tests/test_session_manager.py`、`tests/test_message_queue.py` |
| 9 | 回复格式化 | 超长文本智能分段（保留段落/代码块边界）；Markdown 卡片渲染失败自动降级纯文本 | `tests/test_feishu_formatting.py` |
| 10 | opencode 会话与规则 | 原生 `--session` 续接（上下文保留）；修改 `rules/AGENTS.md` 后无需重启即生效 | `tests/test_opencode_session.py` + 手动冒烟：改 rules 后发消息验证 |
| 11 | 长期记忆 memory/ | 明确要求时写入并回执（"记住…" → `已记入 memory/…`）；日常提及不写入；查看/修改/软删除可用；重启后记忆仍注入；记忆内容不被主仓跟踪且快照仓无 remote | `tests/test_memory.py` + 手动冒烟：对话中"记住 X"验回执与文件，`git --git-dir=runtime/memory-git log` 验快照 |
| 12 | pi 会话与规则（默认后端） | `--session-id` 续接（追问上一轮内容命中）；`/new` 后上下文已断；规则与记忆经 `--append-system-prompt` 生效；流式逐字输出；`/stop` 后 `ps aux \| grep pi` 无残留 | `tests/test_pi_chain.py`、`tests/test_pi_session.py` + 手动冒烟：问一只有 `rules/admin.md` / `memory/` 才知道的信息 |
| 13 | pi 退出码陷阱与成败判定 | `pi --mode json` 失败时退出码仍为 0，必须靠**最后一条** assistant `message_end.stopReason` 判定；部分输出后报错不得当成功，auto-retry 中间失败后重试成功不得误判为失败；任何情况下不得静默回空 | `tests/test_pi_session.py::test_provider_error_raises_even_though_the_process_exits_zero`、`::test_error_after_partial_text_still_raises`、`::test_intermediate_failure_followed_by_a_retry_success_is_not_an_error` |
| 14 | 出站文件推送 `/push/file` | 飞书 `im/v1/files` 上传 + `msg_type:file` 发送；微信转发 sidecar `/send_file`（getuploadurl → AES-128-ECB → CDN → `type:4` file_item）；`receive_id_type` 按 `ou_`/`on_`/`oc_` 自动识别；无 `PUSH_API_TOKEN` 时 503 关闭；401/400/404/413/502 错误分档；caption 先于文件发出 | `tests/test_feishu_file_send.py`、`tests/test_push_file_route.py`、`tests/wechat-sidecar.test.mjs` + 手动冒烟：小文件实发两个渠道确认收到 |
| 15 | 入站文件进会话 | 归档后排入 `pending_files` 且**不唤醒 agent**；通知搭载下一轮 user 文本（pi 原生 session 只传最后一条 user 消息，写 `rounds` 无效）；文件+文字同发合并为一轮；命令轮不消费通知；任务被拒不 drain；归档失败不排通知；`/new` `/reset` 清空；队列上限 20；微信 `type:2` 图片不再被静默丢弃；`describeFileItem` 保留字段名但掩掉 `encrypt_query_param`/`aes_key` | `tests/test_inbound_file_session.py`、`tests/wechat-sidecar.test.mjs` + 手动冒烟：发个文件→再发一句话→确认 agent 知道文件路径且未主动展开 |

| 16 | 时间感知（四后端） | 每轮注入 `当前系统时间: YYYY-MM-DD HH:MM 周X（时段）`；pi 走 `--append-system-prompt` 且时间**不落 pi transcript**（落进去会逐轮累积旧时间戳，微信主会话曾攒到 327 个）；claude/qodercli/codex 拼 prompt 首行；opencode 走 `hooks/inject-time.js`；时段由 `app/clock.py` 算好，模型不自行换算 | `tests/test_clock.py`、`tests/test_pi_session.py::test_clock_rides_the_system_prompt_after_rules_and_memory`、`tests/test_claude_cli.py::test_claude_prompt_leads_with_the_clock`、`tests/test_codex_streaming_mock.py::test_codex_prompt_leads_with_the_clock` + 手动冒烟：问「现在几点、什么时段」核对答案，再 `grep -c 当前系统时间 <pi session jsonl>` 应为 0 |

## 迭代验收规则

1. 任何功能改动合入前必须全量测试通过：

```bash
source .venv/bin/activate
pytest -c conf/pytest.ini -q
```

2. 改动涉及 `lib/js/wechat-sidecar.mjs` 时，额外跑 JS 侧测试（pytest 不收集 `.mjs`）：

```bash
node --test tests/
```

3. 改动涉及上表功能项时，除自动化测试外，额外执行该项对应的手动冒烟
4. 新增核心功能时，同步在本清单追加一行（功能 + 验证要点 + 验证方式）
