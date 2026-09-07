# 知乎开放平台 HTTP API

核验时间：2026-07-16

本文件保留知乎开放平台原始 HTTP 协议、字段大小写和响应结构。日常内容任务优先使用 `zhihu-cli`；开发者直接集成 HTTP API 时读取本文件。

## 目录

- 鉴权
- 全网搜索 API
- 知乎搜索 API
- 知乎热榜 API
- 知乎直答 API
- 用户数据 API（独立文档）
- OAuth 应用集成（独立文档）

用户创作、关注、收藏夹与收藏接口见 [用户数据 API](user-api.md)。第三方应用授权登录与用户 token 获取见 [OAuth 应用集成](oauth.md)。这些用户接口在不传 `X-OAuth-Token` 时查询 Access Secret 所属账号本人；`zhihu-cli` 只使用这一模式。

---

# Bearer 鉴权说明

## 说明

知乎开放平台当前推荐通过 `Authorization: Bearer <your_access_secret>` 的方式调用数据接口。

对于 `zhihu_search`、`global_search`、`hot_list` 等接口，调用时统一使用 Bearer 鉴权即可。

## 获取 Access Secret

请在知乎开放平台[个人中心](https://developer.zhihu.com/profile)查看并获取 Access Secret

说明：

- 调用方需要将 Access Secret 作为 Bearer 凭证放入请求头。
- 服务端会校验 `Authorization` 与 `X-Request-Timestamp`。
- `X-Request-Timestamp` 需要传秒级 Unix 时间戳。

## 请求头示例

| 名称 | 示例值 | 说明 |
| - | - | - |
| Authorization | `Bearer <your_access_secret>` | Bearer 鉴权头 |
| X-Request-Timestamp | `1742822400` | 秒级 Unix 时间戳 |
| Content-Type | `application/json` | JSON 接口固定值 |

## Curl 示例

```shell
curl -G 'https://developer.zhihu.com/api/v1/content/zhihu_search' \
  --data-urlencode 'Query=怎么理解rave文化' \
  -H 'Authorization: Bearer <your_access_secret>' \
  -H "X-Request-Timestamp: $(date +%s)" \
  -H 'Content-Type: application/json'
```


---

# 全网搜索 API

## 接口说明
该接口用于全网内容搜索。

## 接口信息

| 说明 | 值                                                        |
| :- |:---------------------------------------------------------|
| HTTP URL | https://developer.zhihu.com/api/v1/content/global_search |
| HTTP Method | GET                                                      |

## 请求参数
### Header
- Authorization：`Bearer <your_access_secret>`
- X-Request-Timestamp：秒级 Unix 时间戳
- Content-Type：固定值 `application/json`
### Query

|名称|类型|必填|说明|
| :- | :- | :- | :- |
| Query | String | 是 | 查询关键词 |
| Count | Int32 | 否 | 请求数量，默认 10，最大 20 |
| Filter | String | 否 | 高级语法筛选表达式。作为 URL Query 参数传入时需进行 URL 编码，推荐使用 `--data-urlencode` 或 SDK 参数编码能力 |
| SearchDB | String | 否 | 索引库选择，默认 `all` |

### SearchDB 索引库选择

| 值 | 说明 |
| :- | :- |
| `all` | 全部索引库，默认值 |
| `realtime` | 仅搜索实时库 |
| `static` | 仅搜索静态库 |

### Filter 高级语法

`Filter` 用于按站点、发布时间等条件过滤搜索结果。

支持字段：

| 字段 | 含义 | 类型 | 示例 |
| :- | :- | :- | :- |
| host | 站点域名 | String | `host=="example.com"` |
| publish_time | 发布时间，秒级时间戳 | Int64 | `publish_time>=1778494631` |

支持操作符：

- `host` 支持 `==`、`!=`，字符串值必须使用双引号。`host=="zhihu.com"` 及其子域名不支持，如需搜索仅知乎站内内容，请直接使用 `zhihu_search` 接口。
- `publish_time` 支持 `==`、`!=`、`>`、`>=`、`<`、`<=`，数字值不使用引号。



支持逻辑符：

- `AND`、`OR` 必须大写。
- `AND` 优先级高于 `OR`。
- 可以使用括号 `()` 明确控制优先级。

示例：

```text
host=="example.com"
host=="example.com" AND publish_time>=1778494631
(host=="example.com" OR host=="news.example.com") AND publish_time>1778494631
```

## 响应参数

Data：

|参数名|类型|是否必返|描述|
| :- | :- | :- | :- |
| HasMore | Bool | 是 | 是否有下一页数据 |
| Items | Array[Item] | 是 | 内容数据列表 |

Item：

|参数名|类型|是否必返| 描述                             |
| :- | :- | :- |:-------------------------------|
| Title | String | 是 | 内容标题                           |
| ContentType | String | 是 | 内容类型，如回答、文章                    |
| ContentID | String | 是 | 内容 Token                       |
| ContentText | String | 是 | 内容摘要，高亮部分用 <em> 标签表示           |
| Url | String | 是 | 内容链接（带溯源 utm 参数）               |
| CommentCount | Int32 | 是 | 评论数                            |
| VoteUpCount | Int32 | 是 | 赞同数                            |
| AuthorName | String | 是 | 作者昵称，匿名时，展示为：知乎用户              |
| AuthorAvatar | String | 是 | 作者头像                           |
| AuthorBadge | String | 是 | 认证标图片 Url                      |
| AuthorBadgeText | String | 是 | 认证文案                           |
| EditTime | Int64 | 是 | 最后编辑时间戳，如 1745486539           |
| CommentInfoList | Array[CommentInfo] | 否 | 精选评论                           |
| AuthorityLevel  | String             | 是 | 权威等级（1 低权威，2 中权威，3 高权威，4 超高权威） |

CommentInfo:

|参数名|类型|是否必选|描述|
| :- | :- | :- | :- |
| Content | String | 是 | 评论内容 |

### 响应示例
``` json
{
    "Code": 0,
    "Message": "success",
    "Data": {
        "HasMore": false,
        "Items": [{
            "Title": "ChatGPT现在还值得开会员吗？",
            "ContentType": "Answer",
            "ContentID": "1903044959663284716",
            "ContentText": "首先要澄清一个常见误解：ChatGPT的免费版和付费版使用的是不同模型与功能配置，体验差距确实很大。很多人用了一下免费版就觉得"就这？"，其实是没体验过付费版完整的能力，比如文件上传、多模态理解等功能。\n虽然免费版目前也使用了GPT-4-turbo模型，但功能上仍有限，例如不能用代码解释器、不支持上传文件、无长期记忆能力等，而且还有使用频率限制。\n相比之下，花20美金开通的付费版支持更多高级功能，比如处理图片、文档、复杂代码分析、图表生成等，在实际使用中效率和精度明显提升。\n如果你每天只是问几句闲聊或搜索类问题，的确不必付费，国产的一些大模型（如DeepSeek、Kimi）也能胜任。但如果你依赖它来工作学习、频繁做复杂任务，这20美元绝对是值得投入的，光省下的时间就够本。\n最后不建议拼会员，多人共用一个账号容易导致模型输出错乱，影响效果；账号安全、IP污染等问题也无法忽视。一个账号专人使用，才是最稳定、最优的体验方式。",
            "Url": "https://www.zhihu.com/answer/1903044959663284716?utm_medium=openapi_platform&utm_source=6d23634e",
            "CommentCount": 22,
            "VoteUpCount": 18,
            "AuthorName": "时光纪",
            "AuthorAvatar": "https://picx.zhimg.com/50/v2-84ce3330420f9332a1d69d4cd1f10c2f_l.jpg?source=f1558865",
            "AuthorBadge": "",
            "AuthorBadgeText": "",
            "EditTime": 1748355858,
            "CommentInfoList": [{
                "Content": "没啥区别，免费也是4o 收费你也是用4o 那o1 o3都跟智障似的 4o也差不多，但是他比较快。 本月开始不续费了，换了gemini2.5 强太多了，除了think太啰嗦，翻译还是得用回不think的模型"
            }, {
                "Content": "免费版现在也可以用gpt4o啊，只不过有限制，用的不多也够用"
            }],
            "AuthorityLevel":"2",
        }, {
            "Title": "ChatGPT电脑桌面版安装指南+使用技巧（超详细）",
            "ContentType": "Article",
            "ContentID": "18698154193",
            "ContentText": " macOS 版本：14及以上\n 处理器： 建议使用M1芯片或更新的Mac电脑，以获得最佳性能（旧款设备可能出现卡顿）。\n 下载步骤：\n1.打开浏览器，打开 OpenAI 官方下载页面：https://openai.com/chatgpt/desktop/\n2.点击 "Download for macOS" 按钮，开始下载。\n安装步骤：\n1.下载完成后，双击 .dmg 文件，将 ChatGPT 应用拖动到 "应用程序" 文件夹。\n2.如果系统提示 "来自未知开发者"，请在 "系统偏好设置">"安全性与隐私" 中点击 "仍要打开"。\n安装完成： 完成以上步骤，macOS 用户即可正常使用桌面版 ChatGPT。\n 2. Windows 用户安装指南系统时区设置： 需将电脑系统地区和时区设置为阿美莉卡（或其他OpenAI支持服务的地区）。\n1.打开电脑的"设置">"时间和语言">"日期和时间"。\n2.在"自动设置时区"中，先关闭自动设置，然后在"时区"中选择阿美莉卡（或OpenAI支持的地区）的时区。\n下载步骤：\n1.设置好之后，打开OpenAI 官方下载页面： https://openai.com/chatgpt/desktop/\n2.点击 "Download for Windows" 按钮。\n安装步骤：\n1.浏览器会自动打开到微软应用商店页面。\n2.点击 "View in Store/在Microsoft Store中查看" 按钮，跳转到微软应用商店，按照提示完成安装。\n安装完成： 完成以上步骤，Windows 用户即可正常使用桌面版 ChatGPT。\n 三、ChatGPT桌面版使用技巧安装好 ChatGPT 桌面版之后，如何充分利用它的功能，提高效率呢？\n接下来，我分享一些实用的使用技巧：\n1. 快捷键：使用快捷键可以随时随地唤出 ChatGPT，无需切换窗口，非常便捷。\nmacOS： Option + 空格Windows： Alt + 空格 (可以自定义)2. 多模态输入：截图功能： 遇到问题，直接截图发给ChatGPT，它可以帮你分析解读，无论是编程题、Excel 表格，还是其他数据报表，通通不在话下。拍照功能： 拍照上传，可以让 ChatGPT 解答数学题、识别物体等。多文件上传： 可一次性上传多个文档，让 ChatGPT 帮你总结、归纳。3. 高级语音模式：点击输入框右侧的语音图标，即可开始与 ChatGPT 进行语音对话。免费用户也可以体验高级语音模式（有体验时长限制），ChatGPT Plus用户可以享受更长时间的语音对话。4. 多窗口支持：在桌面版中，你可以同时打开多个对话窗口，方便你同时进行多个任务。设置方式：鼠标放到左侧栏相应对话后的"···"，在选项弹窗中选择"在伴随浮窗中打开"。5. 自定义快捷键：如果你觉得默认的快捷键用着不习惯，可以在系统设置中自定义快捷键，让操作更加顺手。设置方式：点击左下角的账号头像>设置>应用，选择"伴随浮窗热键"进行更改。6. 直接启动第三方应用（macOS 独享）：macOS的ChatGPT Plus/Pro和Teams订阅用户，可以直接在ChatGPT中启动VS Code、Xcode、Terminal等第三方应用，进行跨应用协作。对于编辑器类应用，ChatGPT能够读取最前窗口的完整内容；对于终端类应用，可以读取最后200行内容。四、桌面版跟网页版有什么不一样？ChatGPT 桌面版和网页版虽然都使用相同的模型，但使用体验却大相径庭。\n来看一下两者之间的主要区别：\n如果你是一个经常要用的ChatGPT的用户，从效率和功能角度看，桌面版无疑是更好的选择。\n五、ChatGPT Plus或Pro方法不管是哪个端，如果你想解锁ChatGPT的全部功能，包括o1模型、sora、task、高级语音模式等，就需要订阅 ChatGPT Plus或者Pro。\n具体可以看⬇️：\nChatGPT Plus如何升级订阅最新方法全网汇总以上。\n如果有啥疑问也可以在留言告诉我。",
            "Url": "https://zhuanlan.zhihu.com/p/18698154193?utm_medium=openapi_platform&utm_source=6d23634e",
            "CommentCount": 15,
            "VoteUpCount": 27,
            "AuthorName": "文字机器凸哥",
            "AuthorAvatar": "https://picx.zhimg.com/50/v2-df39523084f28b407d21394b6210653c_l.jpg?source=f1558865",
            "AuthorBadge": "",
            "AuthorBadgeText": "",
            "EditTime": 1753954052,
            "CommentInfoList": [{
                "Content": "今天发现有桌面端 下下来后才发现似乎与网页端没什么区别 伴随浮窗无法使用 alt+space快捷键仅仅是呼出/隐藏桌面端主窗口 不知道为什么"
            }, {
                "Content": "显示网络设置有问题咋办[发呆]"
            }],
            "AuthorityLevel":"1",
        }]
    }
}
```


## 代码示例
Curl 请求示例:
``` shell
curl -G 'https://developer.zhihu.com/api/v1/content/global_search' \
  --data-urlencode 'Query=怎么理解rave文化' \
  --data-urlencode 'Filter=host=="example.com" AND publish_time>=1778494631' \
  --data-urlencode 'SearchDB=all' \
  -d 'Count=5' \
  -H 'Authorization: Bearer <your_access_secret>' \
  -H "X-Request-Timestamp: $(date +%s)"
```

Go 语言请求示例:
``` go
package main

import (
    "flag"
    "fmt"
    "io"
    "net/http"
    "net/url"
    "time"
)

const (
    RequestGlobalSearchURL = "https://developer.zhihu.com/api/v1/content/global_search"
)

func main() {
    accessSecret := flag.String("access-secret", "", "Access Secret for Bearer authentication")
    query := flag.String("query", "chatgpt", "Search query")
    count := flag.Int("count", 10, "Number of results to return")
    filter := flag.String("filter", "", "Advanced filter expression")
    searchDB := flag.String("search-db", "", "Search index: all, realtime, static")
    flag.Parse()

    response, err := RequestGlobalSearch(*accessSecret, *query, *count, *filter, *searchDB)
    if err != nil {
        fmt.Printf("Failed to request global search: %v\n", err)
        return
    }

    fmt.Printf("response: %+v\n", response)
}

func RequestGlobalSearch(accessSecret string, query string, count int, filter string, searchDB string) (string, error) {
    params := url.Values{}
    params.Set("Query", query)
    params.Set("Count", fmt.Sprintf("%d", count))
    if filter != "" {
        params.Set("Filter", filter)
    }
    if searchDB != "" {
        params.Set("SearchDB", searchDB)
    }

    req, err := http.NewRequest("GET", RequestGlobalSearchURL, nil)
    if err != nil {
        return "", fmt.Errorf("failed to create request: %w", err)
    }

    req.URL.RawQuery = params.Encode()
    req.Header.Set("Authorization", "Bearer "+accessSecret)
    req.Header.Set("X-Request-Timestamp", fmt.Sprintf("%d", time.Now().Unix()))

    client := &http.Client{}
    resp, err := client.Do(req)
    if err != nil {
        return "", fmt.Errorf("failed to send request: %w", err)
    }
    defer func() {
        if err := resp.Body.Close(); err != nil {
            fmt.Printf("Failed to close response body: %v\n", err)
        }
    }()

    body, err := io.ReadAll(resp.Body)
    if err != nil {
        return "", fmt.Errorf("failed to read response: %w", err)
    }

    return string(body), nil
}
```


---

# 知乎搜索 API

## 接口说明
该接口用于知乎站内内容搜索，返回与查询相关的问题、回答或文章结果。

## 接口信息

| 说明 | 值 |
| :- | :- |
| HTTP URL | https://developer.zhihu.com/api/v1/content/zhihu_search |
| HTTP Method | GET |

## 请求参数
### Header
- Authorization：`Bearer <your_access_secret>`
- X-Request-Timestamp：秒级 Unix 时间戳
- Content-Type：固定值 `application/json`

### Query

| 名称 | 类型 | 必填 | 说明 |
| :- | :- | :- | :- |
| Query | String | 是 | 查询关键词 |
| Count | Int32 | 否 | 请求数量，默认 10，最大 10 |

说明：

- `Query` 不能为空。
- 当 `Count <= 0` 时，服务端默认回退为 `10`。
- 当 `Count > 10` 时，服务端会自动截断为 `10`。

## 响应参数

Data：

| 参数名 | 类型 | 是否必返 | 描述 |
| :- | :- | :- | :- |
| HasMore | Bool | 是 | 当前实现固定返回 `false` |
| SearchHashId | String | 是 | 搜索请求标识 |
| Items | Array[Item] | 是 | 搜索结果列表 |
| EmptyReason | String | 否 | 无结果时的原因说明 |

Item：

| 参数名 | 类型 | 是否必返 | 描述 |
| :- | :- | :- | :- |
| Title | String | 是 | 内容标题 |
| ContentType | String | 是 | 内容类型 |
| ContentID | String | 是 | 内容标识 |
| ContentText | String | 是 | 内容摘要 |
| Url | String | 是 | 内容链接（带溯源 utm 参数） |
| CommentCount | Int32 | 是 | 评论数 |
| VoteUpCount | Int32 | 是 | 赞同数 |
| AuthorName | String | 是 | 作者昵称 |
| AuthorAvatar | String | 是 | 作者头像 |
| AuthorBadge | String | 是 | 作者认证图标 |
| AuthorBadgeText | String | 是 | 作者认证文案 |
| EditTime | Int32 | 是 | 发布时间或更新时间戳 |
| CommentInfoList | Array[CommentInfo] | 否 | 精选评论 |
| AuthorityLevel | String | 是 | 权威等级 |
| RankingScore | Float32 | 是 | 排序分数 |

CommentInfo：

| 参数名 | 类型 | 是否必返 | 描述 |
| :- | :- | :- | :- |
| Content | String | 是 | 评论内容 |

响应示例：
``` json
{
    "Code": 0,
    "Message": "success",
    "Data": {
        "HasMore": false,
        "SearchHashId": "1234567890",
        "Items": [
            {
                "Title": "RAG 评测方法综述",
                "ContentType": "Article",
                "ContentID": "123456789",
                "ContentText": "本文介绍了主流 RAG 评测框架，包括 RAGAS、TruLens ...",
                "Url": "https://zhuanlan.zhihu.com/p/123456789?utm_medium=openapi_platform&utm_source=6d23634e",
                "CommentCount": 15,
                "VoteUpCount": 128,
                "AuthorName": "张三",
                "AuthorAvatar": "https://picx.zhimg.com/example.jpg",
                "AuthorBadge": "",
                "AuthorBadgeText": "",
                "EditTime": 1710000000,
                "CommentInfoList": [],
                "AuthorityLevel": "2",
                "RankingScore": 0.98
            }
        ]
    }
}
```

## 错误码说明

| 错误码 | 说明 |
| - | - |
| 0 | 成功 |
| 10001 | 参数错误 |
| 20001 | 鉴权失败 |
| 30001 | 频率限制 |
| 90001 | 内部错误 |

## 代码示例
Curl 请求示例:
``` shell
curl -G 'https://developer.zhihu.com/api/v1/content/zhihu_search' \
  --data-urlencode 'Query=怎么理解rave文化' \
  -d 'Count=5' \
  -H 'Authorization: Bearer <your_access_secret>' \
  -H "X-Request-Timestamp: $(date +%s)"
```


---

# 知乎热榜 API

## 接口说明
获取当前知乎热榜内容，返回结构化的标题、链接、缩略图与摘要列表。

## 接口信息

| 说明 | 值 |
| - | - |
| HTTP URL | https://developer.zhihu.com/api/v1/content/hot_list |
| HTTP Method | GET |

## 请求参数
### Header
- Authorization：`Bearer <your_access_secret>`
- X-Request-Timestamp：秒级 Unix 时间戳
- Content-Type：固定值 `application/json`

### Query

| 名称 | 类型 | 必填 | 说明 |
| :- | :- | :- | :- |
| Limit | Int32 | 否 | 返回数量，默认 30，最大 30 |

说明：

- 当 `Limit <= 0` 或 `Limit > 30` 时，服务端会自动回退为 `30`。

## 响应参数

Data：

| 参数名 | 类型 | 是否必返 | 描述 |
| :- | :- | :- | :- |
| Total | Int64 | 是 | 实际返回的热榜条数 |
| Items | Array[Item] | 是 | 热榜内容列表 |

Item：

| 参数名 | 类型 | 是否必返 | 描述 |
| :- | :- | :- | :- |
| Title | String | 是 | 热榜标题 |
| Url | String | 是 | 热榜对应的知乎链接 |
| ThumbnailUrl | String | 是 | 缩略图 URL，无封面图时为空字符串 |
| Summary | String | 是 | 内容摘要，无摘要时为空字符串 |

说明：

- 当前仅返回问题和文章两类热榜内容。
- `ThumbnailUrl` 和 `Summary` 始终返回，无数据时值为 `""`。

响应示例：
``` json
{
    "Code": 0,
    "Message": "success",
    "Data": {
        "Total": 2,
        "Items": [
            {
                "Title": "如何评价某个热点问题？",
                "Url": "https://www.zhihu.com/question/123456789",
                "ThumbnailUrl": "https://pic1.zhimg.com/v2-d4b0f8158e064dbcc71eb6ce970230a9.jpg",
                "Summary": "这是该问题的内容摘要"
            },
            {
                "Title": "一篇正在热榜上的文章标题",
                "Url": "https://zhuanlan.zhihu.com/p/987654321",
                "ThumbnailUrl": "",
                "Summary": ""
            }
        ]
    }
}
```

## 错误码说明

| 错误码 | 说明 |
| - | - |
| 0 | 成功 |
| 20001 | 鉴权失败 |
| 30001 | 频率限制 |
| 90001 | 内部错误 |

## 代码示例
Curl 请求示例:
``` shell
curl 'https://developer.zhihu.com/api/v1/content/hot_list?Limit=10' \
  -H 'Authorization: Bearer <your_access_secret>' \
  -H "X-Request-Timestamp: $(date +%s)"
```


---

# 直答 API

## 接口说明

该接口提供知乎直答 3 个模型档位：快速回答、深度思考、智能思考。

当前支持 3 个请求字段：

- `model`
- `messages`
- `stream`

## 接口信息

| 说明 | 值 |
| :- | :- |
| HTTP URL | `https://developer.zhihu.com/v1/chat/completions` |
| HTTP Method | `POST` |
| 请求类型 | `application/json` |
| 响应类型 | `application/json`（`stream=false`） / `text/event-stream`（`stream=true`） |

## 鉴权

Header：

- `Authorization: Bearer <your_access_secret>`
- `X-Request-Timestamp: <unix_seconds>`

说明：

- 当前统一使用 Access Secret 的 Bearer 鉴权语义。
- `X-Request-Timestamp` 为秒级 Unix 时间戳。

## 请求参数

### Body

| 名称 | 类型 | 必填 | 说明 |
| :- | :- | :- | :- |
| `model` | String | 是 | 模型档位，支持 `zhida-fast-1p5`、`zhida-thinking-1p5`、`zhida-agent` |
| `messages` | Array[Message] | 是 | 对话消息列表 |
| `stream` | Bool | 否 | 是否流式返回，默认 `false` |

Message：

| 名称 | 类型 | 必填 | 说明   |
| :- | :- |:---|:-----|
| `role` | String | 是  | 消息角色 |
| `content` | String | 是  | 问题内容 |

## 响应说明

### 非流式（`stream=false`）

```json
{
  "id": "chatcmpl-xxxx",
  "object": "chat.completion",
  "created": 1740470400,
  "model": "zhida-thinking-1p5",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "reasoning_content": "先给出分析过程...",
        "content": "..."
      },
      "finish_reason": "stop"
    }
  ]
}
```

### 流式（`stream=true`）

```text
data: {"id":"chatcmpl-xxxx","object":"chat.completion.chunk","created":1740470400,"model":"zhida-thinking-1p5","choices":[{"index":0,"delta":{"role":"assistant","reasoning_content":"先分析背景"},"finish_reason":null}]}

data: {"id":"chatcmpl-xxxx","object":"chat.completion.chunk","created":1740470400,"model":"zhida-thinking-1p5","choices":[{"index":0,"delta":{"content":"最终回答片段"},"finish_reason":null}]}

data: {"id":"chatcmpl-xxxx","object":"chat.completion.chunk","created":1740470400,"model":"zhida-thinking-1p5","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: [DONE]
```

说明：

- 服务端会发送心跳注释：`: keep-alive`

## 错误响应

```json
{
  "error": {
    "message": "xxx",
    "type": "invalid_request_error",
    "param": "model",
    "code": "model_not_found"
  }
}
```

流式中途错误（HTTP 200 已发出）返回：

```text
data: {"id":"chatcmpl-xxxx","object":"chat.completion.chunk","created":1740470400,"model":"zhida-thinking-1p5","choices":[{"index":0,"delta":{},"finish_reason":"error"}],"error":{"message":"Internal server error","type":"server_error","code":"internal_error"}}

data: [DONE]
```

## 注意事项

1. 当前仅保证 `model/messages/stream` 三个字段的能力语义。
2. 其他请求字段当前不作为正式支持能力，不保证生效。
3. `id` 在同一次流式响应中保持一致。
4. `model` 为必填，缺失时返回 `missing_required_parameter`。
5. 支持 role、content 上下文传参的模型：`zhida-fast-1p5`、`zhida-thinking-1p5`。
6. 实际可用模型还会受租户授权配置影响。
