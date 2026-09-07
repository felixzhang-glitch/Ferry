# 知乎用户数据 API

资料整理时间：2026-07-22
资料来源：知乎开放平台用户内容、关注与收藏接口文档

本文件记录用户数据接口的原始 HTTP 契约。日常 Agent 场景通过 `zhihu-cli` 使用当前 Access Secret 所属账号的数据；开发者代表其他用户访问时，需另外完成知乎 OAuth 授权并传入 `X-OAuth-Token`。

## 目录

- [身份模型](#身份模型)
- [公共请求约定](#公共请求约定)
- [用户内容](#用户内容)
- [用户关注](#用户关注)
- [用户收藏夹列表](#用户收藏夹列表)
- [收藏夹内容](#收藏夹内容)
- [近期收藏](#近期收藏)
- [公共响应与错误码](#公共响应与错误码)
- [协议待确认项](#协议待确认项)

## 身份模型

同一组接口支持两种身份来源：

| 场景 | `Authorization` | `X-OAuth-Token` | 返回的数据 |
|---|---|---|---|
| 当前调用方本人 | 开放平台 Access Secret | 不传 | Access Secret 所属知乎账号的公开范围数据 |
| 第三方应用中的授权用户 | 开放平台 Access Secret | 传入用户 OAuth access token | 该 OAuth 用户授权范围内的公开数据 |

两种凭证职责不同：

- Access Secret 识别并鉴权开放平台调用方，每次请求都必须提供。
- OAuth access token 识别第三方应用当前代表的知乎用户，仅访问其他授权用户时提供。
- OAuth 流程、`app_id`、`app_key` 和用户 token 的后端安全要求见 [OAuth 应用集成](oauth.md)。
- `zhihu-cli` 的普通用户场景只支持第一行：它不发起 OAuth，也不接受、保存或转发 `X-OAuth-Token`。

## 公共请求约定

基础域名：`https://developer.zhihu.com`

所有接口均为 `GET`，并使用以下 Header：

| Header | 必填 | 说明 |
|---|---:|---|
| `Authorization: Bearer <access_secret>` | 是 | 知乎开放平台 Access Secret |
| `X-Request-Timestamp` | 是 | 秒级 Unix 时间戳 |
| `X-OAuth-Token` | 否 | 第三方应用代表已授权用户访问时传入 |
| `Content-Type: application/json` | 见接口原文 | 用户内容、用户关注、近期收藏明确要求；其余接口原文未声明 |

分页接口使用 `Offset` 和 `Limit`。当响应包含 `Paging` 且 `Paging.IsEnd=false` 时，下一次请求应将 `Paging.NextOffset` 原样作为 `Offset`。

## 用户内容

获取用户公开范围内的创作内容，包括回答、文章、视频、想法和问题。

```text
GET /api/v1/user/contents
```

### Query

| 参数 | 类型 | 必填 | 默认值 | 约束与说明 |
|---|---|---:|---|---|
| `Offset` | Int64 | 否 | `0` | 分页偏移量 |
| `Limit` | Int64 | 否 | `20` | 最大 `50` |
| `ContentType` | String | 是 | - | `all`、`answer`、`article`、`zvideo`、`pin`、`question` |
| `SortField` | String | 否 | `ts` | `like_count` 或 `ts` |
| `SortOrder` | String | 否 | `desc` | `asc` 或 `desc` |

### `Data`

| 字段 | 类型 | 必返 | 说明 |
|---|---|---:|---|
| `Items` | Array[`ContentItem`] | 是 | 内容列表 |
| `Paging` | `Paging` | 是 | 分页信息 |

### `ContentItem`

| 字段 | 类型 | 必返 | 说明 |
|---|---|---:|---|
| `ContentType` | String | 是 | 内容类型，固定为小写：`answer`、`article`、`zvideo`、`pin`、`question` |
| `Url` | String | 是 | 内容链接 |
| `CreatedAt` | Int64 | 是 | 创建时间，秒级时间戳 |
| `LikeCount` | Int64 | 是 | 点赞数 |
| `CommentCount` | Int64 | 是 | 评论数 |
| `FavoriteCount` | Int64 | 是 | 收藏数 |
| `Title` | String | 是 | 标题 |
| `Summary` | String | 是 | 摘要 |

### 请求示例

```bash
curl -G 'https://developer.zhihu.com/api/v1/user/contents' \
  -d 'ContentType=all' \
  -d 'Limit=20' \
  -H 'Authorization: Bearer <access_secret>' \
  -H "X-Request-Timestamp: $(date +%s)"
```

## 用户关注

获取用户公开范围内的关注用户列表。

```text
GET /api/v1/user/followees
```

### Query

| 参数 | 类型 | 必填 | 默认值 | 约束与说明 |
|---|---|---:|---|---|
| `Offset` | Int64 | 否 | `0` | 分页偏移量 |
| `Limit` | Int64 | 否 | `20` | 最大 `50` |

### `Data`

| 字段 | 类型 | 必返 | 说明 |
|---|---|---:|---|
| `Items` | Array[`FolloweeItem`] | 是 | 关注用户列表 |
| `Paging` | `Paging` | 是 | 分页信息 |

### `FolloweeItem`

| 字段 | 类型 | 必返 | 说明 |
|---|---|---:|---|
| `Fullname` | String | 是 | 用户名 |
| `UrlToken` | String | 是 | 用户主页标识 |
| `Url` | String | 是 | 用户主页 URL |
| `AvatarUrl` | String | 是 | 头像 URL |
| `Headline` | String | 是 | 一句话介绍 |
| `Gender` | Int16 | 是 | 性别：`0` 未知或保密，`1` 女性，`2` 男性 |
| `FollowerCount` | Int64 | 是 | 粉丝数 |

## 用户收藏夹列表

获取用户公开范围内的收藏夹列表。

```text
GET /api/v1/user/favlists
```

### Query

| 参数 | 类型 | 必填 | 默认值 | 约束与说明 |
|---|---|---:|---|---|
| `Limit` | Int64 | 否 | `20` | 最大 `50` |

### `Data`

| 字段 | 类型 | 必返 | 说明 |
|---|---|---:|---|
| `Items` | Array[`FavlistRecord`] | 是 | 收藏夹列表 |

### `FavlistRecord`

| 字段 | 类型 | 必返 | 说明 |
|---|---|---:|---|
| `UrlToken` | Int64 | 是 | 收藏夹 URL 标识，可用于查询收藏夹内容 |
| `Url` | String | 是 | 收藏夹链接 |
| `Title` | String | 是 | 收藏夹名称 |
| `Description` | String | 是 | 收藏夹描述 |
| `IsPublic` | Bool | 是 | 是否公开 |

`FavlistRecord` 是本文为避免重名使用的文档别名。服务端原文将它和收藏内容中的简版对象都命名为 `FavlistItem`，但两者字段不同。

## 收藏夹内容

获取指定收藏夹中的公开内容。

```text
GET /api/v1/user/favlist_contents
```

### Query

| 参数 | 类型 | 必填 | 默认值 | 约束与说明 |
|---|---|---:|---|---|
| `FavlistUrlToken` | Int64 | 是 | - | 收藏夹 URL 标识，由收藏夹列表的 `UrlToken` 获取 |
| `Offset` | Int64 | 否 | `0` | 分页偏移量 |
| `Limit` | Int64 | 否 | `20` | 最大 `50` |

### `Data`

| 字段 | 类型 | 必返 | 说明 |
|---|---|---:|---|
| `Items` | Array[`CollectionContentItem`] | 是 | 收藏夹内容列表 |
| `Paging` | `Paging` | 是 | 分页信息 |

## 近期收藏

获取用户公开范围内的近期收藏内容。该接口没有 Offset，也没有 Paging，只适合读取最近一批数据，不等于完整收藏历史。

```text
GET /api/v1/user/collections
```

### Query

| 参数 | 类型 | 必填 | 默认值 | 约束与说明 |
|---|---|---:|---|---|
| `Limit` | Int64 | 否 | `20` | 最大 `50` |

### `Data`

| 字段 | 类型 | 必返 | 说明 |
|---|---|---:|---|
| `Items` | Array[`CollectionContentItem`] | 是 | 近期收藏内容 |

## 收藏内容公共对象

### `CollectionContentItem`

| 字段 | 类型 | 必返 | 说明 |
|---|---|---:|---|
| `ContentType` | String | 是 | 内容类型，固定为小写：`answer`、`article`、`zvideo`、`pin`、`question` |
| `Url` | String | 是 | 内容链接 |
| `CreatedAt` | Int64 | 是 | 内容创建时间，秒级时间戳 |
| `FavTime` | Int64 | 是 | 收藏时间，秒级时间戳 |
| `LikeCount` | Int64 | 是 | 点赞数 |
| `CommentCount` | Int64 | 是 | 评论数 |
| `FavoriteCount` | Int64 | 是 | 收藏数 |
| `Title` | String | 是 | 标题 |
| `Summary` | String | 是 | 摘要 |
| `Favlists` | Array[`ContentFavlistItem`] | 是 | 内容所在收藏夹列表 |
| `Author` | `ContentAuthor` | 否 | 内容作者；下游未返回作者时不输出 |

### `ContentFavlistItem`

| 字段 | 类型 | 必返 | 说明 |
|---|---|---:|---|
| `UrlToken` | Int64 | 是 | 收藏夹 URL 标识；下游未返回时为 `0` |
| `Title` | String | 是 | 收藏夹名称 |
| `Url` | String | 是 | 收藏夹链接 |

`ContentFavlistItem` 同样是本文为避免与收藏夹列表对象重名而使用的文档别名，不改变服务端 JSON。

### `ContentAuthor`

| 字段 | 类型 | 必返 | 说明 |
|---|---|---:|---|
| `Name` | String | 是 | 作者名称 |
| `UrlToken` | String | 是 | 作者 URL 标识 |
| `Url` | String | 是 | 作者主页链接 |
| `Gender` | Int16 | 是 | 作者性别：`0` 未知，`1` 女性，`2` 男性 |
| `Headline` | String | 是 | 作者签名 |

## `Paging`

| 字段 | 类型 | 必返 | 说明 |
|---|---|---:|---|
| `IsEnd` | Bool | 是 | 是否到最后一页 |
| `NextOffset` | String | 否 | 下一页偏移量；调用方应原样回传为下一次 `Offset` |
| `Totals` | Int64 | 是 | 总数 |

注意：请求参数 `Offset` 标为 Int64，响应 `NextOffset` 却标为 String。CLI 下一次接收时应做严格 Int64 解析；解析失败返回协议错误，不静默截断。

## 公共响应与错误码

成功响应外层结构：

```json
{
  "Code": 0,
  "Message": "success",
  "Data": {}
}
```

| `Code` | 说明 |
|---:|---|
| `0` | 成功 |
| `10001` | 参数错误 |
| `20001` | 鉴权失败 |
| `30001` | 频率限制 |
| `30002` | 配额限制 |
| `90001` | 内部错误 |

## OAuth 调用示例

第三方应用代表已授权用户访问时，只在同一请求上增加 `X-OAuth-Token`：

```bash
curl -G 'https://developer.zhihu.com/api/v1/user/followees' \
  -d 'Limit=20' \
  -H 'Authorization: Bearer <access_secret>' \
  -H 'X-OAuth-Token: <oauth_access_token>' \
  -H "X-Request-Timestamp: $(date +%s)"
```

OAuth access token 的获取方式见 [OAuth 应用集成](oauth.md)。

## 协议待确认项

以下内容在当前资料中不闭合。实现时应按服务端实测或正式文档修订，不要由客户端猜测：

1. 资料没有给出 OAuth scope、token 撤销、刷新 token 或过期后的错误协议。
