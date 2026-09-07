# 知乎开放平台 MCP

核验时间：2026-07-16

本文件汇总知乎开放平台已经提供的 4 项 MCP 服务。它是配置和开发 reference，不要求另建或聚合 MCP Server。

## 目录

- 全网搜索 MCP
- 知乎热榜 MCP
- 知乎搜索 MCP
- 知乎直答 MCP

## 共同鉴权

所有 MCP 服务使用 `Authorization: Bearer <your_access_secret>`。Access Secret 在个人中心获取：<https://developer.zhihu.com/profile>。

---

# 全网搜索 MCP

## 接口说明

该服务通过 MCP over SSE 提供全网搜索能力，适合接入支持 MCP 的 Agent、助手或工作流系统。

当前服务仅提供工具能力，不提供资源（resources）与提示词（prompts）能力。

## 接口信息

| 说明 | 值 |
| :- | :- |
| SSE URL | `https://developer.zhihu.com/api/mcp/global_search/v1/sse` |
| Message URL | `https://developer.zhihu.com/api/mcp/global_search/v1/message` |
| 传输方式 | `MCP over SSE` |
| 工具名 | `global_search` |

说明：

- 客户端先连接 `sse` 端点。
- 服务端会通过 `endpoint` 事件返回实际可用的 `message` 地址，地址中会带 `sessionId`。
- 后续 `initialize`、`tools/list`、`tools/call` 请求均发送到该 `message` 地址。

## 鉴权

请求头：

- `Authorization: Bearer <your_access_secret>`

说明：

- 建议在 `sse` 连接和后续 `message` 请求中均携带同一份鉴权信息。

## 工具定义

### `global_search`

#### 入参

| 名称 | 类型 | 必填 | 说明 |
| :- | :- | :- | :- |
| `query` | String | 是 | 搜索关键词，长度 2-100 个字符 |
| `count` | Number | 否 | 返回条数，取值范围 1-20，默认 `10` |
| `filter` | String | 否 | 高级语法筛选表达式，例如 `host=="example.com" AND publish_time>=1778494631` |
| `search_db` | String | 否 | 索引库选择，可选值：全部 `all`、实时库 `realtime`、静态库 `static`，默认 `all` |

如需筛选知乎站内内容，请直接使用 `zhihu_search` MCP 工具。

#### 返回

工具调用结果为 `text` 类型内容，正文为面向大模型消费的结构化文本。

返回主体示例：

```text
<global_search query="人工智能" filter="host==&quot;example.com&quot;" search_db="all">
<search_item title="人工智能发展趋势与展望" content_type="Article" url="https://..." author_name="张三" author_avatar="https://..." author_badge_text="" edit_time="2025-03-01 10:00:00 +0000 UTC" authority_level="2" ranking_score="0.9800">
近年来，人工智能（AI）的发展速度令人瞩目 ...
</search_item>
</global_search>
```

说明：

- 返回值外层是 MCP `text` 类型，文本内容为 XML。
- `&quot;` 是 XML 属性中的双引号转义，示例里的实际 `filter` 值为 `host=="example.com"`。
- 建议将整段 XML 原样交给模型消费，不建议自行裁剪字段。

## 接入流程

### 1. 建立 SSE 连接

```bash
curl -N 'https://developer.zhihu.com/api/mcp/global_search/v1/sse' \
  -H 'Authorization: Bearer <your_access_secret>' \
  -H 'Accept: text/event-stream'
```

服务端会先返回一条 `endpoint` 事件，例如：

```text
event: endpoint
data: /api/mcp/global_search/v1/message?sessionId=xxx
```

### 2. 初始化 MCP 会话

将上一步拿到的 `message` 地址记为 `MESSAGE_URL`。

```bash
curl -X POST "$MESSAGE_URL" \
  -H 'Authorization: Bearer <your_access_secret>' \
  -H 'Content-Type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
      "protocolVersion": "2024-11-05",
      "clientInfo": {
        "name": "demo-client",
        "version": "1.0.0"
      },
      "capabilities": {}
    }
  }'
```

说明：

- `message` 端点通常会先返回 HTTP `202 Accepted`。
- 实际 JSON-RPC 响应会通过已建立的 SSE 通道异步返回。

### 3. 获取工具列表

```bash
curl -X POST "$MESSAGE_URL" \
  -H 'Authorization: Bearer <your_access_secret>' \
  -H 'Content-Type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/list"
  }'
```

### 4. 调用搜索工具

```bash
curl -X POST "$MESSAGE_URL" \
  -H 'Authorization: Bearer <your_access_secret>' \
  -H 'Content-Type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/call",
    "params": {
      "name": "global_search",
      "arguments": {
        "query": "人工智能",
        "count": 5,
        "filter": "host==\"example.com\" AND publish_time>=1778494631",
        "search_db": "all"
      }
    }
  }'
```

SSE 通道中会收到对应响应，例如：

```text
event: message
data: {"jsonrpc":"2.0","id":3,"result":{"content":[{"type":"text","text":"<global_search query=\"人工智能\">..."}]}}
```

## 注意事项

1. 该服务采用标准 MCP 工具调用模式，推荐直接使用现成 MCP Client 接入。
2. `tools/call` 的结果通过已建立的 SSE 通道返回，而不是直接同步返回在 POST 响应体中。
3. 全网搜索适合做信息补充与外部参考检索，若仅需知乎站内结果，建议优先使用知乎搜索 MCP。


---

# 热榜 MCP

## 接口说明

该服务通过 MCP over SSE 提供知乎热榜能力，适合接入支持 MCP 的 Agent、助手或工作流系统。

当前服务仅提供工具能力，不提供资源（resources）与提示词（prompts）能力。

## 接口信息

| 说明 | 值 |
| :- | :- |
| SSE URL | `https://developer.zhihu.com/api/mcp/hot_list/v1/sse` |
| Message URL | `https://developer.zhihu.com/api/mcp/hot_list/v1/message` |
| 传输方式 | `MCP over SSE` |
| 工具名 | `hot_list` |

说明：

- 客户端先连接 `sse` 端点。
- 服务端会通过 `endpoint` 事件返回实际可用的 `message` 地址，地址中会带 `sessionId`。
- 后续 `initialize`、`tools/list`、`tools/call` 请求均发送到该 `message` 地址。

## 鉴权

请求头：

- `Authorization: Bearer <your_access_secret>`

说明：

- 建议在 `sse` 连接和后续 `message` 请求中均携带同一份鉴权信息。

## 工具定义

### `hot_list`

#### 入参

| 名称 | 类型 | 必填 | 说明 |
| :- | :- | :- | :- |
| `limit` | Number | 否 | 返回条数，取值范围 1-30，默认 `30` |

#### 返回

工具调用结果为 `text` 类型内容，正文为面向大模型消费的结构化文本。

返回主体示例：

```text
<hot_list limit="30" total="3">
  <item rank="1">
    <title>如何看待当前 AI Agent 的发展趋势？</title>
    <url>https://www.zhihu.com/question/123456789</url>
    <thumbnail_url>https://pic1.zhimg.com/v2-d4b0f8158e064dbcc71eb6ce970230a9.jpg</thumbnail_url>
    <summary>这是该问题的内容摘要</summary>
  </item>
  <item rank="2">
    <title>有哪些值得关注的技术热点？</title>
    <url>https://zhuanlan.zhihu.com/p/987654321</url>
    <thumbnail_url>https://pic1.zhimg.com/v2-abcdef1234567890abcdef1234567890.jpg</thumbnail_url>
    <summary>这是该文章的内容摘要</summary>
  </item>
  <item rank="3">
    <title>为什么这条话题会进入热榜？</title>
    <url>https://www.zhihu.com/question/111111111</url>
    <thumbnail_url></thumbnail_url>
    <summary></summary>
  </item>
</hot_list>
```

说明：

- `thumbnail_url` 和 `summary` 始终返回，无数据时为空（如 rank="3" 示例）。
- 返回值外层是 MCP `text` 类型，文本内容为 XML。
- 建议将整段 XML 原样交给模型消费，不建议自行裁剪字段。

## 接入流程

### 1. 建立 SSE 连接

```bash
curl -N 'https://developer.zhihu.com/api/mcp/hot_list/v1/sse' \
  -H 'Authorization: Bearer <your_access_secret>' \
  -H 'Accept: text/event-stream'
```

服务端会先返回一条 `endpoint` 事件，例如：

```text
event: endpoint
data: /api/mcp/hot_list/v1/message?sessionId=xxx
```

### 2. 初始化 MCP 会话

将上一步拿到的 `message` 地址记为 `MESSAGE_URL`。

```bash
curl -X POST "$MESSAGE_URL" \
  -H 'Authorization: Bearer <your_access_secret>' \
  -H 'Content-Type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
      "protocolVersion": "2024-11-05",
      "clientInfo": {
        "name": "demo-client",
        "version": "1.0.0"
      },
      "capabilities": {}
    }
  }'
```

说明：

- `message` 端点通常会先返回 HTTP `202 Accepted`。
- 实际 JSON-RPC 响应会通过已建立的 SSE 通道异步返回。

### 3. 获取工具列表

```bash
curl -X POST "$MESSAGE_URL" \
  -H 'Authorization: Bearer <your_access_secret>' \
  -H 'Content-Type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/list"
  }'
```

### 4. 调用热榜工具

```bash
curl -X POST "$MESSAGE_URL" \
  -H 'Authorization: Bearer <your_access_secret>' \
  -H 'Content-Type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/call",
    "params": {
      "name": "hot_list",
      "arguments": {
        "limit": 10
      }
    }
  }'
```

SSE 通道中会收到对应响应，例如：

```text
event: message
data: {"jsonrpc":"2.0","id":3,"result":{"content":[{"type":"text","text":"<hot_list limit=\"10\" total=\"10\">..."}]}}
```

## 注意事项

1. 该服务采用标准 MCP 工具调用模式，推荐直接使用现成 MCP Client 接入。
2. `tools/call` 的结果通过已建立的 SSE 通道返回，而不是直接同步返回在 POST 响应体中。
3. 热榜结果偏实时性，列表顺序和内容会随时间变化。


---

# 知乎搜索 MCP

## 接口说明

该服务通过 MCP over SSE 提供知乎站内搜索能力，适合接入支持 MCP 的 Agent、助手或工作流系统。

当前服务仅提供工具能力，不提供资源（resources）与提示词（prompts）能力。

## 接口信息

| 说明 | 值 |
| :- | :- |
| SSE URL | `https://developer.zhihu.com/api/mcp/zhihu_search/v1/sse` |
| Message URL | `https://developer.zhihu.com/api/mcp/zhihu_search/v1/message` |
| 传输方式 | `MCP over SSE` |
| 工具名 | `zhihu_search` |

说明：

- 客户端先连接 `sse` 端点。
- 服务端会通过 `endpoint` 事件返回实际可用的 `message` 地址，地址中会带 `sessionId`。
- 后续 `initialize`、`tools/list`、`tools/call` 请求均发送到该 `message` 地址。

## 鉴权

请求头：

- `Authorization: Bearer <your_access_secret>`

说明：

- 建议在 `sse` 连接和后续 `message` 请求中均携带同一份鉴权信息。

## 工具定义

### `zhihu_search`

#### 入参

| 名称 | 类型 | 必填 | 说明 |
| :- | :- | :- | :- |
| `query` | String | 是 | 搜索关键词，长度 2-100 个字符 |
| `count` | Number | 否 | 返回条数，取值范围 1-10，默认 `10` |

#### 返回

工具调用结果为 `text` 类型内容，正文为面向大模型消费的结构化文本。

返回主体示例：

```text
<zhihu_search query="RAG">
<search_item title="RAG 评测方法综述" content_type="Article" url="https://..." author_name="张三" author_avatar="https://..." author_badge_text="" edit_time="2025-03-01 10:00:00 +0000 UTC" authority_level="2" ranking_score="0.9800">
本文介绍了主流 RAG 评测框架，包括 RAGAS、TruLens ...
</search_item>
</zhihu_search>
```

说明：

- 返回值外层是 MCP `text` 类型，文本内容为 XML。
- 建议将整段 XML 原样交给模型消费，不建议自行裁剪字段。

## 接入流程

### 1. 建立 SSE 连接

```bash
curl -N 'https://developer.zhihu.com/api/mcp/zhihu_search/v1/sse' \
  -H 'Authorization: Bearer <your_access_secret>' \
  -H 'Accept: text/event-stream'
```

服务端会先返回一条 `endpoint` 事件，例如：

```text
event: endpoint
data: /api/mcp/zhihu_search/v1/message?sessionId=xxx
```

### 2. 初始化 MCP 会话

将上一步拿到的 `message` 地址记为 `MESSAGE_URL`。

```bash
curl -X POST "$MESSAGE_URL" \
  -H 'Authorization: Bearer <your_access_secret>' \
  -H 'Content-Type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
      "protocolVersion": "2024-11-05",
      "clientInfo": {
        "name": "demo-client",
        "version": "1.0.0"
      },
      "capabilities": {}
    }
  }'
```

说明：

- `message` 端点通常会先返回 HTTP `202 Accepted`。
- 实际 JSON-RPC 响应会通过已建立的 SSE 通道异步返回。

### 3. 获取工具列表

```bash
curl -X POST "$MESSAGE_URL" \
  -H 'Authorization: Bearer <your_access_secret>' \
  -H 'Content-Type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/list"
  }'
```

### 4. 调用搜索工具

```bash
curl -X POST "$MESSAGE_URL" \
  -H 'Authorization: Bearer <your_access_secret>' \
  -H 'Content-Type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/call",
    "params": {
      "name": "zhihu_search",
      "arguments": {
        "query": "RAG",
        "count": 5
      }
    }
  }'
```

SSE 通道中会收到对应响应，例如：

```text
event: message
data: {"jsonrpc":"2.0","id":3,"result":{"content":[{"type":"text","text":"<zhihu_search query=\"RAG\">..."}]}}
```

## 注意事项

1. 该服务采用标准 MCP 工具调用模式，推荐直接使用现成 MCP Client 接入。
2. `tools/call` 的结果通过已建立的 SSE 通道返回，而不是直接同步返回在 POST 响应体中。
3. `query` 建议尽量具体，以获得更稳定的搜索结果。


---

# 直答 MCP

## 接口说明

该服务通过 MCP Streamable HTTP 提供直答能力，适合接入支持 MCP 的 Agent、助手或工作流系统。

当前服务仅提供工具能力，不提供资源（resources）与提示词（prompts）能力。

## 接口信息

| 说明 | 值 |
| :- | :- |
| HTTP URL | `https://developer.zhihu.com/api/mcp/zhida/v1/stream` |
| HTTP Method | `POST` |
| 传输方式 | `MCP Streamable HTTP` |
| 工具名 | `zhida` |

说明：

- 当前通过单一 `stream` 端点承载 `initialize`、`tools/list`、`tools/call` 请求。
- `initialize`、`tools/list` 返回 JSON-RPC 响应。
- `tools/call` 返回一次性 JSON-RPC 工具结果。

## 鉴权

请求头：

- `Authorization: Bearer <your_access_secret>`

说明：

- 建议每次请求均携带鉴权信息。

## 工具定义

### `zhida`

#### 入参

| 名称 | 类型 | 必填 | 说明 |
| :- | :- | :- | :- |
| `query` | String | 是 | 用户问题 |
| `member_id` | Number | 否 | 预留字段，可不传 |
| `model` | String | 是 | 直答模型，日常推荐使用 `zhida-fast-1p5` |

支持的 `model`：
| 模型 | 说明 |
| :- | :- |
| `zhida-fast-1p5` | 快速回答，日常推荐使用 |
| `zhida-thinking-1p5` | 深度思考模型 |
| `zhida-agent` | 智能检索与回答 |

#### 返回结果

`tools/call` 的结果会作为标准 MCP `CallToolResult` 返回，当前返回文本内容为直答最终答案。

说明：

- MCP 层默认等待下游直答完整输出后再返回工具结果。
- 如需消费增量事件或更丰富的思考过程，建议直接使用直答原生接口，而不是 MCP tool 调用。

## 接入流程

### 1. 初始化 MCP 会话

```bash
curl -X POST 'https://developer.zhihu.com/api/mcp/zhida/v1/stream' \
  -H 'Authorization: Bearer <your_access_secret>' \
  -H 'Content-Type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
      "protocolVersion": "2025-10-28",
      "clientInfo": {
        "name": "demo-client",
        "version": "1.0.0"
      },
      "capabilities": {}
    }
  }'
```

### 2. 获取工具列表

```bash
curl -X POST 'https://developer.zhihu.com/api/mcp/zhida/v1/stream' \
  -H 'Authorization: Bearer <your_access_secret>' \
  -H 'Content-Type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/list"
  }'
```

### 3. 调用直答工具

```bash
curl -X POST 'https://developer.zhihu.com/api/mcp/zhida/v1/stream' \
  -H 'Authorization: Bearer <your_access_secret>' \
  -H 'Content-Type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/call",
    "params": {
      "name": "zhida",
      "arguments": {
        "query": "怎么理解rave文化",
        "model": "zhida-fast-1p5"
      }
    }
  }'
```

指定模型：

```bash
curl -X POST 'https://developer.zhihu.com/api/mcp/zhida/v1/stream' \
  -H 'Authorization: Bearer <your_access_secret>' \
  -H 'Content-Type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/call",
    "params": {
      "name": "zhida",
      "arguments": {
        "query": "怎么理解rave文化",
        "model": "zhida-thinking-1p5"
      }
    }
  }'
```

响应示例：

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "Rave 文化最早兴起于 20 世纪 80 年代末到 90 年代初的英国和欧洲电子音乐场景，核心不只是“蹦迪”，而是一种围绕电子音乐、现场氛围、群体连接和短暂逃离日常秩序的青年亚文化。很多人会用 PLUR 来概括它的精神，即 Peace、Love、Unity、Respect。"
      }
    ],
    "isError": false
  }
}
```

## 注意事项

1. 当前推荐按“工具调用”方式接入，即使用 `initialize`、`tools/list`、`tools/call` 三个方法完成对接。
2. `tools/call` 默认等待完整回答后返回结果。
