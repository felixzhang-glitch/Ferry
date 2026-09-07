# 知乎 OAuth 应用集成

资料整理时间：2026-07-22
适用对象：需要在 Web 应用中集成知乎登录，并代表已授权知乎用户访问数据的开发者

OAuth 是开发者集成能力，不是 `zhihu-cli` 的普通用户鉴权方式。CLI 使用 Access Secret 查询该凭证所属账号自己的数据，不发起 OAuth、不接收用户 OAuth token。

## 目录

- [凭证与角色](#凭证与角色)
- [前置申请](#前置申请)
- [Authorization Code Flow](#authorization-code-flow)
- [授权页面](#授权页面)
- [换取 Access Token](#换取-access-token)
- [调用用户数据 API](#调用用户数据-api)
- [安全要求](#安全要求)
- [协议待确认项](#协议待确认项)

## 凭证与角色

| 凭证 | 代表谁 | 用在哪里 |
|---|---|---|
| `app_id` | 第三方应用 | 发起授权、换取 token |
| `app_key` | 第三方应用密钥 | 仅应用后端换取 token |
| `authorization_code` | 用户一次授权结果 | 应用后端换取 access token |
| OAuth `access_token` | 已授权知乎用户 | 用户数据 API 的 `X-OAuth-Token` |
| 开放平台 Access Secret | 开放平台调用方 | 用户数据 API 的 `Authorization: Bearer ...` |

代表其他用户调用用户数据 API 时，需要同时提供最后两项：Access Secret 鉴权调用方，OAuth access token 指明当前被代表的用户。

## 前置申请

向 `product-platform@zhihu.com` 申请 `app_id` 和 `app_key`。

邮件主题：

```text
<公司名称>申请接入知乎 OAuth 服务
```

申请材料：

- 应用名称
- 应用简介
- OAuth 授权完成后的回调地址 `redirect_uri`
- 应用申请人名称
- 应用申请人手机号

## Authorization Code Flow

1. Web 应用将用户跳转到知乎授权页面。
2. 用户登录知乎并确认授权。
3. 知乎重定向回已登记的 `redirect_uri`，附带授权码。
4. 应用后端用授权码、`app_id` 和 `app_key` 换取 access token。
5. 应用后端将 access token 作为 `X-OAuth-Token` 调用知乎用户数据 API。

## 授权页面

```text
GET https://openapi.zhihu.com/authorize?redirect_uri={redirect_uri}&app_id={app_id}&response_type=code
```

知乎 2077 项目在 2026-05-14 的线上实测回调形态：

```text
{redirect_uri}?authorization_code={authorization_code}
```

回调参数实际为 `authorization_code`。应用后端把它的值作为 token 接口的 `code` 表单参数提交，即 `code = callback.authorization_code`。为兼容可能的协议修订，接收端可以同时接受 `authorization_code` 和 `code`，但以 `authorization_code` 为当前实测主路径。

`redirect_uri` 应进行 URL 编码，并且必须与申请时登记的地址一致。知乎 2077 同期实测还发现授权回调不返 `state`。当前资料也没有定义 PKCE、scope 和用户拒绝授权时的回调参数，正式集成前必须向平台确认。

## 换取 Access Token

```text
POST https://openapi.zhihu.com/access_token
Content-Type: application/x-www-form-urlencoded
```

### 请求参数

| 参数 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `app_id` | String | 是 | 第三方应用 ID |
| `app_key` | String | 是 | 第三方应用密钥 |
| `grant_type` | String | 是 | 固定为 `authorization_code` |
| `redirect_uri` | String | 是 | 申请应用时登记的回调地址 |
| `code` | String | 是 | 用户授权后获得的 authorization code |

### cURL

```bash
curl -sS -X POST 'https://openapi.zhihu.com/access_token' \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode "app_id=${APP_ID}" \
  --data-urlencode "app_key=${APP_KEY}" \
  --data-urlencode 'grant_type=authorization_code' \
  --data-urlencode "redirect_uri=${REDIRECT_URI}" \
  --data-urlencode "code=${CODE}"
```

### 成功响应

```json
{
  "access_token": "xxx",
  "token_type": "Bearer",
  "expires_in": 3600
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `access_token` | String | 用户 OAuth 访问令牌 |
| `token_type` | String | 令牌类型，如 `Bearer` |
| `expires_in` | Int64 | 有效期，单位为秒 |

## 调用用户数据 API

应用后端用开放平台 Access Secret 和用户 OAuth token 共同调用：

```bash
curl -G 'https://developer.zhihu.com/api/v1/user/contents' \
  -d 'ContentType=all' \
  -H 'Authorization: Bearer <access_secret>' \
  -H 'X-OAuth-Token: <oauth_access_token>' \
  -H "X-Request-Timestamp: $(date +%s)"
```

完整接口见 [用户数据 API](user-api.md)。

## 安全要求

- `app_key`、authorization code 的交换和 OAuth access token 的使用都在应用后端完成。
- 不把 `app_key` 或 OAuth access token 放进浏览器、移动端包、URL、前端日志或 Agent 输出。
- 回调必须校验请求关联性；正式集成至少需要平台确认并支持 `state` 后再上线。
- OAuth access token 与开放平台 Access Secret 分开存储、分开审计、分开撤销。
- 用户取消授权、token 过期或接口返回鉴权失败时，停止访问，不静默切换到 Access Secret 所属账号。

## 协议待确认项

1. 授权回调实测不返 `state`。没有可靠的 `state` 回传就无法完成标准登录 CSRF 校验，不能直接作为生产安全能力定稿。
2. 文档没有 PKCE、scope、用户拒绝授权、错误响应和回调错误参数。
3. 只返回 `access_token` 与 `expires_in`，没有 refresh token；需确认过期后是否必须重新授权。
4. 没有 token 撤销、授权查询或解绑接口。
5. 文档提到“获取用户信息”，但没有提供对应 endpoint 和响应 schema。
6. 需确认 `app_key` 是否允许直接作为表单参数传输，以及是否另有签名要求。

## 已验证的协议偏差

知乎 2077 项目在 2026-05-14 真实跑通 OAuth 时确认：

- 授权回调使用 `authorization_code`，不是旧文档中的 `code`。
- token 交换接口的表单字段仍使用 `code`。
- 授权回调没有带回请求中的 `state`。
- `/access_token` 和 `/user` 响应中的业务字段 `code: 20000` 表示成功；客户端应优先检查 `access_token` 或用户对象是否存在，不把 `20000` 当错误。

实现证据：`../zhihu2077/src/routes/auth.js`；项目记录：`../zhihu2077/CLAUDE.md`、`../zhihu2077/specs/ARCHITECTURE.md`。
