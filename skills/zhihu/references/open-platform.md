# 知乎数据开放平台

核验时间：2026-07-16

## 平台是什么

知乎数据开放平台向开发者和 Agent 提供知乎搜索、全网搜索、知乎热榜和知乎直答能力。使用者可以通过统一 CLI、原始 HTTP API 或平台已经提供的 MCP 服务调用。

## 注册和领取 Access Secret

1. 访问知乎开放平台个人中心：<https://developer.zhihu.com/profile>
2. 使用知乎账号登录。
3. 点击「申请新 Access Secret」。
4. 获取 Access Secret 后，通过 Agent 执行：

```bash
zhihu-cli auth set --secret-stdin
```

Agent 应通过进程标准输入传入用户在对话中提供的 Access Secret，不要求用户介入终端交互。

开放平台后台的鉴权凭证统一称为 **Access Secret**。HTTP 请求使用：

```http
Authorization: Bearer <your_access_secret>
X-Request-Timestamp: <Unix 秒级时间戳>
```

不要在回答、日志或文件中重复完整 Access Secret。CLI 配置完成后只显示脱敏信息和验证状态。

## 当前邀测免费额度

| 能力 | 每日额度 |
|---|---:|
| 全网搜索 | 5,000 次 |
| 知乎搜索 | 5,000 次 |
| 知乎热榜 | 100 次 |
| 知乎直答 | 100 次 |

额度规则：

- 单个账号最多可申请 20 个 Access Secret。
- 同一账号下所有 Access Secret 共享同一个试用调用额度池。
- 页面效果测试与 API 直接调用共享同一额度池。
- 对应能力额度耗尽后，该账号下所有 Access Secret 均无法继续调用该能力。
- Access Secret 具有完整 API 访问权限，不要泄露给无关人员。
- 删除 Access Secret 后无法恢复；相关调用记录仍保留用于额度计算。

额度和邀测规则可能调整。对额度敏感的任务应以个人中心「用量统计」中的实时数据为准。

## 常见问题

### 如何申请 API 访问权限？

通过邮件联系开放平台，提供使用场景和预估调用量。平台将在 1 个工作日内处理申请。

### API 如何计费？

当前处于邀测阶段。具体计费方案由商务团队根据使用需求提供定制方案。

### 是否支持流式输出？

知乎直答 API 支持流式输出，在请求参数中设置 `stream: true`。知乎搜索、全网搜索和热榜暂不支持流式输出。

### Access Secret 无效或需要更换怎么办？

在个人中心申请或管理 Access Secret，然后重新执行：

```bash
zhihu-cli auth set --secret-stdin
```

CLI 应先在线验证新 Access Secret，再替换系统密钥链中的旧 Access Secret。

## 联系方式

- 邮箱：<openplatform@zhihu.com>
- 工作时间：周一至周五 09:00-18:00（北京时间）
- 回复时效：1 个工作日内

适用场景：API 接入咨询、访问权限申请、商务合作和技术问题。

## 相关入口

- 开放平台：<https://developer.zhihu.com/>
- 文档中心：<https://developer.zhihu.com/docs>
- 个人中心与 Access Secret 申请：<https://developer.zhihu.com/profile>
