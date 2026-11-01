# 佳明 Garmin Connect MCP

> 接入方式：pi 全局 MCP（stdio），Ferry 侧零代码改动。服务端为社区项目 [Taxuspt/garmin_mcp](https://github.com/Taxuspt/garmin_mcp)，直连 Garmin Connect 私有接口，非官方。

## 现状（2026-10-30）

| 项 | 值 |
|---|---|
| 服务端 | `uv tool install` 到 `/root/.local/share/uv/tools/garmin-mcp`，入口 `/root/.local/bin/garmin-mcp` |
| 依赖覆盖 | `garminconnect==0.3.20`（`/root/.config/garmin-mcp/overrides.txt`）；项目原本钉 `==0.3.2`，该版本对中国区登录无效 |
| pi 配置 | `~/.pi/agent/mcp.json` → `mcpServers.garmin`（`codemode` 暴露，58 工具） |
| 区域 | 中国区 `garmin.cn`（`GARMIN_IS_CN=true`） |
| token | `/root/.garminconnect/garmin_tokens.json`（`GARMINTOKENS`），目录 700 / 文件 600 |
| 认证命令 | `garmin-auth`（= `garmin-mcp-auth --is-cn`，带 `GARMINTOKENS`） |

## 安装与升级

```bash
uv tool install --force --python 3.12 \
  --overrides /root/.config/garmin-mcp/overrides.txt \
  "git+https://github.com/Taxuspt/garmin_mcp"
# 校验覆盖版本
ls -d /root/.local/share/uv/tools/garmin-mcp/lib/python3.12/site-packages/garminconnect-*
```

升级后重启 Ferry（`./bin/server restart`）：pi 在进程启动时装配 MCP。

## 认证

```bash
/root/.local/bin/garmin-auth            # 手机号或邮箱 + 密码 + 验证码
/root/.local/bin/garmin-auth --verify   # 验证 token
/root/.local/bin/garmin-auth --diagnose # 打印 token 路径与可用性
/root/.local/bin/garmin-auth --force-reauth  # token 失效后重认证
```

token 有效期约 6 个月，过期症状是工具调用报 401 / login failed，重跑认证即可。密码与验证码只在交互式提示里输入，不写入配置、不入库。

区域判定：`garmin-region`（同组凭证分别打 garmin.cn / garmin.com）用于排查 `INVALID_USERNAME_PASSWORD`；`garmin-matrix` 探测 CN 移动 SSO 可接受的 clientId/locale 组合。

## 工具档位

pi 会把 MCP 工具描述放进上下文，全量 155 个太多。当前配置 `GARMIN_TOOL_PROFILE=minimal,health` → 58 个：活动列表/详情/分段、每日统计与步数、睡眠、HRV、压力、心率、Body Battery、训练准备度与状态、体重体脂与身体成分、饮水、血氧。

| profile | 工具数 |
|---|---|
| `minimal` | 20 |
| `health` | 53 |
| `training` | 38 |
| `activities` | 41 |
| `minimal,health` | 58 |

改档位后 `pi mcp add garmin ...` 重写配置，再重启 Ferry。

## 验证

```bash
pi mcp list | sed -n '/^garmin/,/^$/p'                # 期望 connected，58 tools
/root/.local/share/uv/tools/garmin-mcp/bin/python /data/tmp/garmin_probe.py  # 真取数
```

`garmin_probe.py` 走 `garmin-mcp` 的 stdio 接口，验证 `get_full_name` / `get_stats` / `get_activities` 返回真实数据。探针脚本本身不入库。

## 故障

| 现象 | 处置 |
|---|---|
| `Garmin login failed. Run 'garmin-mcp-auth'` | token 缺失或过期，跑 `garmin-auth` |
| `DI token exchange failed ... falling back to JWT_WEB` 后卡住 | `garminconnect` 版本过旧（0.3.2 走 `mobile.integration.garmin.com` 内网域名，公网不解析）→ 按上面的 overrides 重装 |
| `INVALID_USERNAME_PASSWORD` | 手机号未被 SSO 接受或密码错，改用账号绑定邮箱；先 `garmin-region` 确认区域 |
| `429 — IP rate limited` | 停手 30 分钟以上；密码错两次就停 |
| pi 里没有 garmin 工具 | 未重启 Ferry，或 `mcp.json` 被改坏（`pi mcp list` 会报错） |