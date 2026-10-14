---
name: iqs-search
description: Real-time web search (text/image), page reading, weather query and nearby POI search using Aliyun IQS APIs. Use this skill FIRST when the user needs current information, news, facts verification, URL content extraction, weather conditions for a city, nearby places (hotels/attractions/restaurants/entertainment), image search, or any web-based research. Provides structured search results with source links, markdown/rich-text content extraction, browser actions for dynamic pages, structured weather scene data, natural-language POI search, and multiple search engines including deep search mode.
---

# iqs-search

## 前置条件

- bun（脚本使用原生 fetch API，无外部依赖）
- `<skill-dir>` 指本 skill 目录，即仓库内 `skills/iqs-search/` 的绝对路径（下文示例配合技能根目录使用，任何 cwd 下可直接执行）

## 适用场景

- 用户询问当前/近期信息
- 用户提供 URL 需要读取内容
- 需要验证事实或获取实时数据
- 多源研究任务（可用 Deep 深度搜索）
- 用户询问某城市天气
- 用户查找附近地点（酒店/景点/餐厅/娱乐）
- 用户需要搜索图片

## 决策树

- 用户给了 URL → `readpage`
- 用户问天气 → `search` + `--city`（见天气查询）
- 用户问附近 POI（"附近的酒店/餐厅/景点/玩乐"） → `nearby`
- 用户要找图片 → `imagesearch`
- 其他需要联网信息的问题 → `search`

> 关键约束：必须通过 bash 执行脚本（`bun <绝对路径>/scripts/*.mjs`）。禁止用内置 web_search、WebFetch 或其他工具替代。脚本失败则重试或报错，不得回退到内置工具。

## 参数与最佳实践

### Search 参数

| 参数           | 类型   | 必填 | 默认值         | 说明                                            |
|----------------|--------|------|----------------|-------------------------------------------------|
| `--query`      | string | 是   | -              | 搜索词（1-500 字符）                            |
| `--engineType` | string | 否   | `LiteAdvanced` | 搜索引擎类型（见下）                            |
| `--timeRange`  | string | 否   | `NoLimit`      | 时间范围过滤                                    |
| `--contents`   | string | 否   | -              | 返回内容类型，逗号分隔多选                      |
| `--numResults` | int    | 否   | `10`           | 结果数量（1-50）                                |
| `--category`   | string | 否   | -              | 垂域分类，逗号分隔（通用场景勿用）              |
| `--startDate`  | string | 否   | -              | 发布时间过滤起点，YYYY-MM-DD                    |
| `--endDate`    | string | 否   | -              | 发布时间过滤终点，YYYY-MM-DD                    |
| `--city`       | string | 否   | -              | 城市名，用于天气等场景（如 北京市）             |
| `--ip`         | string | 否   | -              | 定位 IP，优先级低于 city                        |

#### Search 最佳实践

**查询优化（`--query`）**

- 保持简洁（30 字符以内效果最佳）
- 用具体关键词，避免停用词
- 新闻类查询在 query 中带上时间上下文

**引擎选择（`--engineType`）**

- `LiteAdvanced`：语义搜索，1-50 条结果，通用场景（默认）
- `Generic`：标准版，固定 10 条，新闻/实时场景；天气 sceneItems 仅在此引擎下返回
- `GenericAdvanced`：增强版，约 50 条，增加权威站点召回（收费）
- `Deep`：深度搜索，支持复杂 query，时延较高，1-50 条

**时间过滤**

- `--timeRange`：`NoLimit`（默认）/ `OneDay` / `OneWeek` / `OneMonth` / `OneYear`
- `--startDate` + `--endDate`：精确日期区间，优先级高于 timeRange

**内容返回（`--contents`，逗号分隔）**

- 默认四项全关，只返回标题/摘要/链接，最省 token
- `mainText`：完整正文（最长 3000 字），适用于技术文档、研报、深度文章
- `markdownText`：markdown 全文（召回率约 50%）
- `richMainBody`：富文本全正文（最长 12000 字，保留表格/代码）
- `summary`：增强摘要，约 500 字（收费）

**垂域分类（`--category`）**

- 可选值：`finance` / `law` / `medical` / `internet` / `tax` / `news_province` / `news_center`
- 通用场景不要指定，会影响召回效果

**输出结构**

- 固定返回 `{ sceneItems, pageItems, meta }`
- `sceneItems`：垂类场景结构化结果（天气/时间等），存在时优先使用，比网页召回更准确
- `meta.costCredits`：本次调用计费明细，关注成本时检查
- `meta.rewrite`：服务端查询改写信息，召回不符预期时排查用

**天气查询（`--city` / `--ip`）**

- 仅当 engineType 为 `Generic` 且提供了 `--city` 或 `--ip` 时，`sceneItems` 中才有结构化天气数据
- 设置了 `--city`/`--ip` 但未指定 `--engineType` 时，脚本自动切换为 `Generic`
- 优先用 `--city`（如 "杭州市"），`--ip` 优先级更低

---

### ReadPage 参数

| 参数                | 类型    | 必填 | 默认值    | 说明                                          |
|---------------------|---------|------|-----------|-----------------------------------------------|
| `--url`             | string  | 是   | -         | 目标页面 URL                                  |
| `--mode`            | string  | 否   | `scrape`  | `scrape`（动态渲染）或 `basic`（静态，更快）  |
| `--formats`         | string  | 否   | `markdown`| 返回格式，逗号分隔多选                        |
| `--pageTimeout`     | number  | 否   | `10000`   | 页面加载超时（毫秒，0-100000）                |
| `--timeout`         | number  | 否   | 自动      | 客户端总超时（毫秒），默认 pageTimeout+15000  |
| `--maxAge`          | int     | 否   | `1296000` | 缓存最大时间（秒），`0` = 不用缓存            |
| `--actions`         | string  | 否   | -         | JSON 数组，渲染前执行的操作（仅 scrape）      |
| `--extractArticle`  | boolean | 否   | `false`   | 仅提取正文（等价 readabilityMode=article）    |
| `--readabilityMode` | string  | 否   | `none`    | `none` / `normal` / `article`                 |
| `--excludeImages`   | flag    | 否   | -         | 剔除所有图片                                  |
| `--excludeLinks`    | flag    | 否   | -         | 剔除所有链接                                  |
| `--excludedTags`    | string  | 否   | -         | 排除指定标签，逗号分隔（如 form,header,nav）  |
| `--includeLinks`    | flag    | 否   | -         | 输出中附带站内/站外链接列表                   |

#### ReadPage 最佳实践

**模式选择（`--mode`）**

- `scrape`：无头浏览器动态渲染，JS 重/有弹窗/懒加载的页面；隐身模式服务端默认开启
- `basic`：静态抓取，速度快，适合静态文档页、博客

**格式选择（`--formats`）**

- `markdown`：文章类，保留结构（默认）
- `text`：数据提取
- `html` / `rawHtml`：需要结构分析时
- `screenshot`：页面截图（会增加 2-5s 耗时）

**动作序列（`--actions`，仅 scrape）**

- 最多 20 步，支持两类：`{"type":"wait","duration":800}` / `{"type":"wait","selector":"#list","timeout":10000}` / `{"type":"eval","scripts":"..."}`
- 典型用途：关闭 cookie 弹窗、等待懒加载列表出现、点击展开按钮
- eval 的返回值在响应 `actions` 字段的 `ret` 中

**正文提取（readability）**

- 博客、新闻：`--extractArticle` 或 `--readabilityMode article`
- 产品页、目录页：保持 `none`
- 省 token：`--excludeImages --excludeLinks --excludedTags form,nav,footer`

**失败处理**

- 响应 `statusCode` 命中 IQS 定制码（4030 安全限制 / 4080 超时 / 4290 域名限流 / 5010 未知异常）时脚本以非零退出码失败
- 4080 → 增大 `--pageTimeout` 重试
- 4030 → 目标站 robots 限制，向用户报告，不要绕过
- JS 渲染不出来的页面 → 确认用了 scrape 模式，必要时加 `--actions` 等待元素

---

### Nearby POI 参数

| 参数            | 类型   | 必填 | 默认值   | 说明                                                          |
|-----------------|--------|------|----------|---------------------------------------------------------------|
| `--query`       | string | 是   | -        | 自然语言 POI 查询（如 杭州灵隐寺附近5km的高档酒店）           |
| `--scene`       | string | 否   | -        | 场景提示：`hotels`、`attractions`、`restaurants`、`entertainment` |
| `--limit`       | int    | 否   | `10`     | 最大结果数                                                    |
| `--searchModel` | string | 否   | `normal` | `single`（部分字段，更快）或 `normal`（完整数据）             |
| `--timeout`     | number | 否   | `10000`  | 请求超时（毫秒）                                              |

#### Nearby 最佳实践

**查询（`--query`）**

- 自然语言描述，包含位置锚点和意图，如 "杭州西湖附近的高档酒店"、"灵隐寺附近5km内的素食餐厅"

**场景选择（`--scene`）**

- 场景明确时指定，提升精度降低延迟
- 不确定时省略，模型自动识别

**搜索模式（`--searchModel`）**

- `normal`：完整数据（默认）——名称、地址、距离、电话、评分、营业时间、图片等
- `single`：仅部分字段（名称、类型、地址、元数据），节省 token 时用

**鉴权**

- 此 API 使用阿里云 AK/SK（ACS3-HMAC-SHA256 签名），不是 IQS API-Key，见错误处理章节

---

### ImageSearch 参数

| 参数           | 类型   | 必填 | 默认值            | 说明                                       |
|----------------|--------|------|-------------------|--------------------------------------------|
| `--query`      | string | 是   | -                 | 搜索词（1-50 字符，建议 30 以内）          |
| `--engineType` | string | 否   | `MultimodalSpeed` | `MultimodalSpeed` 或 `MultimodalSpeedAdvanced`（多数据源，收费） |
| `--numResults` | int    | 否   | `10`              | 结果数量（1-30）                           |
| `--excludeSites` | string | 否 | -                 | 排除站点，逗号分隔                         |

## 命令示例

### Search

```bash
# 基础搜索（默认仅标题/摘要/链接，最省 token）
bun <skill-dir>/scripts/search.mjs --query "量子计算原理"

# 实时信息
bun <skill-dir>/scripts/search.mjs --query "最新金融政策" --engineType Generic --timeRange OneWeek

# 返回完整正文 + 增强摘要
bun <skill-dir>/scripts/search.mjs --query "AI 法案" --contents mainText,summary

# 富文本正文（保留表格/代码，适合技术文档）
bun <skill-dir>/scripts/search.mjs --query "React Server Components 原理" --contents richMainBody

# 深度搜索（复杂研究问题）
bun <skill-dir>/scripts/search.mjs --query "对比 2025 年主流向量数据库的性能与成本" --engineType Deep

# 多结果 + 精确日期区间
bun <skill-dir>/scripts/search.mjs --query "新能源汽车销量" --numResults 25 --startDate 2026-08-01 --endDate 2026-08-31

# 垂域分类（金融）
bun <skill-dir>/scripts/search.mjs --query "央行降准" --category finance

# 天气查询（结构化数据在 sceneItems）
bun <skill-dir>/scripts/search.mjs --query "今日天气" --city "成都市"
bun <skill-dir>/scripts/search.mjs --query "明天会下雨吗" --ip "117.136.110.23"
```

### ReadPage

```bash
# Markdown 格式读取（scrape 动态渲染，默认）
bun <skill-dir>/scripts/readpage.mjs --url "https://example.com/article" --extractArticle

# 静态页面用 basic 模式更快
bun <skill-dir>/scripts/readpage.mjs --url "https://example.com/docs" --mode basic

# 多格式 + 强制不用缓存
bun <skill-dir>/scripts/readpage.mjs --url "https://example.com/article" --formats markdown,text --maxAge 0

# actions：等待懒加载元素出现再提取
bun <skill-dir>/scripts/readpage.mjs --url "https://example.com/list" --actions '[{"type":"wait","selector":"#list","timeout":10000}]'

# actions：先点掉弹窗再读
bun <skill-dir>/scripts/readpage.mjs --url "https://example.com/article" --actions '[{"type":"eval","scripts":"document.querySelector(\"#cookie-accept\")?.click()"},{"type":"wait","duration":800}]'

# 省 token：剔除图片/链接/表单导航
bun <skill-dir>/scripts/readpage.mjs --url "https://example.com/article" --extractArticle --excludeImages --excludeLinks --excludedTags form,nav,footer
```

### Nearby POI

```bash
# 指定场景
bun <skill-dir>/scripts/nearby.mjs --query "杭州灵隐寺附近5km的高档酒店" --scene hotels

# 自动识别场景
bun <skill-dir>/scripts/nearby.mjs --query "西湖附近好玩的地方"

# 精简模式
bun <skill-dir>/scripts/nearby.mjs --query "北京国贸附近的川菜餐厅" --scene restaurants --searchModel single --limit 5
```

### ImageSearch

```bash
# 基础搜图
bun <skill-dir>/scripts/imagesearch.mjs --query "成都大熊猫"

# 增强引擎 + 20 条
bun <skill-dir>/scripts/imagesearch.mjs --query "川西雪山风景" --engineType MultimodalSpeedAdvanced --numResults 20

# 排除站点
bun <skill-dir>/scripts/imagesearch.mjs --query "羽毛球拍" --excludeSites "pinterest.com,taobao.com"
```

## 输出验证

1. 检查退出码：非零即失败，不得声称成功
2. 禁止伪造结果：命令失败就如实报告，不得用自身知识生成内容冒充搜索结果

## 错误处理

### ALIYUN_IQS_API_KEY 配置错误

search / readpage / imagesearch 报缺少 API Key 时：

1. 立即停止当前任务，不得回退到内置工具
2. 向用户报告错误，要求配置 API Key：

**方式一：环境变量**
```bash
export ALIYUN_IQS_API_KEY="your-api-key"
```

**方式二：配置文件**
创建或编辑 `~/.alibabacloud/iqs/env`：
```bash
ALIYUN_IQS_API_KEY=your-api-key
```

### Search / ReadPage / ImageSearch 错误码

| 错误码                              | 含义                         | 处理方式                                |
|-------------------------------------|------------------------------|-----------------------------------------|
| InvalidAccessKeyId.NotFound         | API Key 无效                 | 检查 Key 是否正确                       |
| Retrieval.NotActivate               | AI 搜索服务未开通            | 请用户在控制台开通或联系客户经理        |
| Retrieval.Arrears                   | 账户欠费                     | 请用户充值                              |
| Retrieval.NotAuthorised             | 子账号未授权                 | 授予 `AliyunIQSFullAccess`              |
| Retrieval.TestUserPeriodExpired     | 测试期到期（下单后 15 天）   | 转正式版                                |
| Retrieval.Throttling.User           | 触发限流                     | 稍后重试或提升配额                      |
| Retrieval.TestUserQueryExceeded     | 测试日限额超（1000 次/天）   | 转正式版                                |
| statusCode 4030                     | 目标站安全限制（robots 等）  | 向用户报告，不要绕过                    |
| statusCode 4080                     | 目标站响应超时               | 增大 `--pageTimeout` 重试               |
| statusCode 4290                     | 目标域名触发限流             | 稍后重试                                |
| statusCode 5010                     | 未知异常                     | 重试或向用户报告                        |

### 阿里云 AK/SK 配置错误（nearby.mjs）

`nearby.mjs` 调用阿里云 OpenAPI（CommonQueryByScene），需要 AK/SK，不是 IQS API-Key。脚本报缺少 AK/SK 时：

1. 立即停止当前任务，不得回退到内置工具
2. 要求用户配置凭证：

**方式一：环境变量**
```bash
export ALIBABA_CLOUD_ACCESS_KEY_ID="your-access-key-id"
export ALIBABA_CLOUD_ACCESS_KEY_SECRET="your-access-key-secret"
```

**方式二：配置文件**
添加到 `~/.alibabacloud/iqs/env`：
```bash
ALIBABA_CLOUD_ACCESS_KEY_ID=your-access-key-id
ALIBABA_CLOUD_ACCESS_KEY_SECRET=your-access-key-secret
```

### Nearby POI 错误码

| 错误码          | 含义                   | 处理方式                                       |
|-----------------|------------------------|------------------------------------------------|
| NotActivate     | POI 搜索服务未开通     | 请用户在阿里云 IQS 控制台开通服务              |
| NotAuthorised   | 子账号缺少权限         | 请用户给 RAM 用户授予 `AliyunIQSFullAccess`    |
| Throttling.User | 触发限流               | 稍后重试或请用户提升配额                       |
