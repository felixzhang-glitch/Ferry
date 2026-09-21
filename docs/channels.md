# 渠道接入

## 飞书

### 架构

```
lark-oapi SDK (WebSocket 长连接) → FeishuWsClient → FeishuWebhookHandler.handle_event
                                                          ↓
                                               事件解析 → 命令/消息分发
                                                          ↓
                                               PiCliClient → 回复（Markdown 卡片 / 图片）
```

### 接入方式：长连接模式

使用飞书官方 Python SDK (`lark-oapi`) 建立 WebSocket 全双工通道，无需公网域名或加密策略配置。

**配置步骤**：
1. 确保 `FEISHU_APP_ID` 和 `FEISHU_APP_SECRET` 已配置
2. 启动 Ferry 服务，SDK 自动建立长连接
3. 登录[开发者后台](https://open.feishu.cn/app) → 事件与回调 → 事件配置
4. 编辑订阅方式，选择「使用长连接接收事件」并保存
5. 添加事件 `im.message.receive_v1`（接收消息 v2.0）

**与 Webhook 模式对比**：

| | 长连接模式 | Webhook 模式（已废弃） |
|---|---|---|
| 公网域名 | 不需要 | 需要 |
| 加密/验签 | SDK 内置 | 需手动实现 |
| 防火墙/白名单 | 不需要 | 需配置 |
| 部署 | 服务器能访问公网即可 | 需暴露 8080 端口 |

**约束与限制**：
- 仅支持企业自建应用
- 事件处理需在 3 秒内完成（当前用 `asyncio.create_task` 后台处理，满足要求）
- 每个应用最多 50 个长连接
- 集群模式：多 client 部署时随机一个收到消息（不支持广播）
- 失败重推间隔：15s → 5min → 1h → 6h，最多重试 4 次

**SDK 依赖**：`lark-oapi>=1.3.0`

**参考文档**：[使用长连接接收事件](https://open.feishu.cn/document/ukTMukTMukTM/uYDNxYjL2QTM24iN0EjN/event-subscription-configure-/request-url-configuration-case)

### 关键实现

- **消息类型**：私聊文本 + 图片；群聊 @ 触发（`FEISHU_GROUP_REQUIRE_MENTION`）
- **回复格式**：Markdown 卡片渲染，失败自动降级纯文本；超长文本智能分段（保留代码块/段落边界）
- **图片处理**：接收图片（下载到本地交给 pi）+ 发送图片（识别 CLI 输出中的本地路径自动上传）；生成图片发现使用 `GENERATED_IMAGES_DIR`，兼容旧 `CODEX_GENERATED_IMAGES_DIR`，不依赖 Codex CLI
- **文件处理**：接收文件归档到 `FILE_ARCHIVE_DIR` 并回执「已收藏」，同时排入会话通知（见下文「入站文件与会话」）；发送文件走 `im/v1/files` 上传 + `msg_type:file`，由统一入口 `/push/file` 触发
- **Quick Ack**：收到消息立即发 Typing reaction，最终答案汇总后单条回复

### 文件

```
lib/python/channel/feishu/
  ws_client.py   → SDK 长连接封装（主入口）
  handler.py     → 消息处理流程
  client.py      → OpenAPI 调用（token/reply/send/image/file/reaction）
  security.py    → 签名校验与解密（legacy webhook 模式）
  formatting.py  → Markdown 格式化与分段
  media.py       → 图片下载/上传/路径识别
  models.py      → 事件解析模型
```

---

## 微信

### 架构

```
iLink Bot API ← 长轮询 ← wechat-sidecar.mjs (Node.js)
                                    ↓
                        POST /webhook/wechat (本地 HTTP)
                                    ↓
                        WechatWebhookHandler → PiCliClient → 文本回复
                                    ↓
                        POST iLink Bot 发送接口 ← sidecar 代发
```

### 关键实现

- **Sidecar 模式**：wechat-sidecar.mjs 独立进程，负责扫码登录、长轮询、发送消息
- **共享 Token**：sidecar 与 Ferry 间通过 `WECHAT_WEBHOOK_TOKEN` 做简单鉴权
- **消息类型**：入站 私聊文本 + 语音转文字 + 图片/文件/视频归档（`item_list[].type` 2/4/5）；出站 文本 + 文件
- **限制**：出站暂不支持图片/视频/语音、typing 回执、长任务通知、定时提醒通知

### 出站文件

sidecar 暴露 `POST /send_file`（`{to, path, context_token?, file_name?}`），走 iLink 官方四步：

1. `POST ilink/bot/getuploadurl` — `media_type=3`（UploadMediaType.FILE），带 `rawsize`/`rawfilemd5`/`filesize`/`aeskey`(hex)/`no_need_thumb`
2. AES-128-ECB 加密明文（协议强制，入站解密同一套）
3. **POST** 密文到 `https://novac2c.cdn.weixin.qq.com/c2c/upload`，下载凭证从响应头 `x-encrypted-param` 取
4. `POST ilink/bot/sendmessage` — `item_list[{type:4, file_item}]`

三个易错点：`sendmessage` 的 `type` 是 **4**（MessageItemType.FILE），而 `getuploadurl` 的 `media_type` 是 **3**，两个枚举数值不同；`aes_key` 是 base64 包裹的 **hex 字符串**；`len` 是明文字节数的字符串。

预签名 URL 绑定收件人，`getuploadurl` 的 `to_user_id` 必须与 `sendmessage` 收件人一致。上限 `WECHAT_MAX_FILE_MB`（默认 50）。

### 文件

```
lib/js/wechat-sidecar.mjs          → sidecar 主程序
lib/python/channel/wechat/handler.py → webhook 处理
```

---

## 出站文件统一入口

两个渠道都由 Ferry 主服务的 `POST /push/file` 统一收口，agent 只需学一个接口：

```bash
curl -X POST http://127.0.0.1:8080/push/file \
  -H "Authorization: Bearer $PUSH_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"channel":"wechat","to":"<user_id>@im.wechat","path":"/data/file/x.pdf","caption":"可选"}'
```

- `channel`：`feishu` | `wechat`
- `to`：飞书填 chat_id(`oc_`) 或 open_id(`ou_`)，自动识别 `receive_id_type`；微信填 `<user_id>@im.wechat`
- 飞书走 `im/v1/files` 上传（`file_type` 按扩展名映射，默认 `stream`）+ `msg_type:file` 发送，上限 30MB
- 微信转发到 sidecar `/send_file`
- 服务绑 `0.0.0.0`，该接口**必须**带 `PUSH_API_TOKEN`；未配置直接 503 关闭，不降级为免鉴权
- 错误码：400 参数/空文件、401 令牌、404 文件不存在、413 超限、502 上游失败、503 未配置

---

## 入站文件与会话

两个渠道每轮只向 pi 传当前 user 消息，历史由 pi 管理；`SessionManager` 不再保存历史。入站附件通过待处理通知搭载到 user 文本：

```
文件到达 → 归档到 FILE_ARCHIVE_DIR → 回执「已收藏」
        → 排入 SessionManager.pending_files（不唤醒 agent）
        → 用户下一次开口时，通知注入该轮 user 文本
```

**为什么注入下一轮**：pi 只接收当前 user 消息，历史保存在 pi 原生 session 中。Ferry 只维护 key 与附件通知，不另存对话副本；搭载下一轮 user 文本才能把附件路径送到 pi

**为什么到达时不唤醒 agent**：唤醒就意味着 agent 会去读文件。用户发个文件存档，不该被自动展开分析一遍（4MB PDF 白烧一次配额）。通知头部已写明「仅当本轮确实需要时才读取」

**边界**：
- 文件 + 文字同发 → 合并成一轮，通知搭载该轮文字（微信此前会直接丢掉文字）
- 纯文件消息 → 不跑 LLM，webhook 返回空 `replies`，回执由 sidecar/飞书侧自己发
- 任务被拒（已有任务在跑）→ 不 drain 通知，留给下一轮，避免静默丢失
- 命令轮（`/help` `/stop` 等）→ 提前返回，不消费通知
- 归档失败 → 不排通知，不能把不存在的文件广告给 agent
- 队列上限 20 条，超出丢最旧；`/new` `/reset` 清空
- 通知是内存态，重启丢失；文件本身仍在 `FILE_ARCHIVE_DIR`

**日志红线**：`describeFileItem` 只输出字段名与非敏感元数据。`encrypt_query_param` 与 `aes_key` 绝不能进日志 —— 两者齐备即可从微信 CDN 下载并解密原文件。历史上这里打过完整 raw item，凭证随日志落盘，而日志曾是 644 全局可读；现已改为脱敏输出，并把 supervisord 的 umask 收到 0077（systemd drop-in，因为 `-n` 模式下 `[supervisord] umask` 不会生效）
