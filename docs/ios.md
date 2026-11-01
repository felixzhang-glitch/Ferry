# iOS 轻客户端：设备只读数据与 macOS 签名

## 定位与数据范围

iOS App 是现有 Ferry 控制台的原生轻壳：聊天、设置与任务继续由服务端执行，原生页面仅负责授权、读取、手动同步和查看服务端副本。最低 iOS 17，优先验收 iPhone 17 Pro；PWA 保持独立使用，网页不能读取 Apple 健康库。

| 分项 | 手动读取范围 | 上传字段 |
|---|---|---|
| 活动 | 最近 30 天每日统计 | 步数、步行/跑步距离（m）、活动能量（kcal）、时间/来源 |
| 心率 | 最近 30 天样本 | bpm、时间/来源 |
| 体重 | 最近 30 天样本 | kg、时间/来源 |
| 血氧 | 最近 30 天样本 | 百分比、时间/来源 |
| 日历 | 所选日历，过去 30 天至未来 30 天 | 标题、日历、起止日期、全天标志及原时区 |
| 提醒事项 | 所选列表全部未完成项 + 最近 30 天完成项 | 标题、列表、到期日期组件、完成状态及完成时间 |

未默认上传备注、位置、参与者、URL、附件；不读取睡眠或运动路线。每次同步前确认服务地址及选中分项。此处的同步开关不是系统权限设置；撤销系统授权应在 iPhone 设置/健康 App 中操作。

健康数据读取被拒绝和没有数据通常不可区分，“无可读取数据”不能证明权限已获准。活动只读到部分指标、其他指标无统计值时明确标为部分结果，不填零。血氧来自健康库已有记录（例如兼容设备或其他数据来源），不是 iPhone 自身测量。系统可能仅允许有限历史，查询成功不代表可以读到所有记录。

iOS 17+ EventKit 读取日历和提醒需要完整访问授权，系统权限包括读写；Ferry 代码仅查询，不保存、修改、完成或删除设备上的项目。Apple 提醒事项与 Ferry `/remind` 调度是两套数据，不会导入为自动任务。

## 数据副本与隐私

- 手动前台同步；没有后台自动同步、通知或医疗判断。进入后台、登出、切换服务端或登录失效会取消操作。
- 手机仅在内存中缓冲待上传数据，服务端地址及随机设备标识可保留；密码不保存。WKWebView 使用系统管理的会话 Cookie，原生请求使用短暂内存会话，不保存服务端内部 Token。
- 公开接口只接受有效登录 Cookie；上传、取消及清除副本须绑定会话 CSRF。独立观测只读 Bearer 无权查看设备数据。
- 服务端私有目录默认 `runtime/server/device-data/`（0700/0600、gitignored），与观测事件、聊天、memory/Dream 分离，不自动注入 pi。
- 0700/0600 是文件权限，不是磁盘加密；同 OS 用户的工具进程仍可能读取，部署时须信任主机与运行身份。备份副本亦需自行保护和清理。
- 同步只合并已读取数据，不因空结果、权限变化、取消选择或缺失项目抹除旧数据。已从设备删除的项目可能继续出现在旧副本中，不能视为当前设备状态；只有显式“清除服务端副本”会删除保存的数据，绝不删除手机原件。
- 协议每批最多 200 条/256KiB，原生客户端按 240KiB 分批，为服务端数值规范化留余量；每分项事务最多 256 批/64MiB。每分项保留记录最多 51,200 条，整体私有存储预算 512MiB、设备标识最多 32 个。由于保留旧记录，达到历史预算时提交会拒绝，可确认后清除该分项副本再同步，不能静默淘汰旧数据。到达读取限额或读取失败明确显示部分结果；不宣称已完整同步。事务提交前数据不会进入正式查询。

## 服务端准备

按正常方式安装项目依赖并使用既有主服务/控制台。可选 `DEVICE_DATA_STORE_DIR` 指定独立私有数据目录；不要把数据目录放入公开静态目录或版本库。新增模板配置不会自行修改当前部署。

原生客户端要求有效 HTTPS 证书；不要关闭证书校验或通过裸 HTTP 登录。沿用现有控制台用户名与密码，公网入口需 `OBSERVABILITY_COOKIE_SECURE=true`。

已有反向代理必须在 macOS 联调前由部署者更新 `conf/nginx-observability.locations.conf` 对应内容：设备批次需要 256KiB 上限，普通控制台仍保持 4KB；`/internal/device-data/` 不得对外暴露。本次代码交付不自动改线上 Nginx、重启服务或发布。代理配置应检查后按自身运维流程应用。

公开接口前缀 `/api/observability/v1/device-data`，内部前缀 `/internal/device-data/v1`：

1. `POST /syncs` 固定设备/分项/范围，获得服务端事务 ID；相同设备和完全相同范围的活跃事务返回同一个 ID，支持开启响应丢失后的原请求重试，不同范围仍冲突。
2. `PUT /syncs/{id}/batches/{batch_id}` 分批上传，批次序号从 0 开始连续；同序号同内容重试成功，内容不同返回冲突。
3. `POST /syncs/{id}/commit` 校验数量，原子发布 complete/partial；空读取标记不确定并保留旧数据。
4. `DELETE /syncs/{id}` 取消事务；未完成事务有一小时 TTL。原生网络重试有界，若开启响应连续丢失且始终未取得事务 ID，换范围重试可能冲突，需等待旧事务到期；不保证后台续传。
5. `GET /summary` 和 `GET /records` 仅登录用户查看摘要及有界分页。
6. `DELETE /data` 明确设备与分项后清除服务端副本；迟到的旧事务不得恢复已清除数据。

数据字段及严格约束见 `lib/python/core/device_data_contract.py`。日期时间传 Unix 秒，同时保留全天日期和提醒事项日期组件语义。查询在条数上限之外按实际编码字节缩页，客户端须使用服务端返回的 `next_offset`，不能按请求 limit 推断下一页。

EventKit 提醒查询没有系统级分页/条数参数，底层会返回所选列表的查询结果；Ferry 仅在内存中处理并限制序列化/上传数量，不保证系统查询本身的内存占用有固定上限。特别大的提醒列表需用户减小选择范围。

## macOS：打开工程、编译与真机签名

本环境没有 Xcode/Swift Apple SDK；以下是用户在 macOS 需要执行的步骤，不是已成功签名的记录。

1. 把包含 `ios/` 的工作副本复制或拉取到 Mac（不要为此提交任何私有配置/健康数据）。安装能够支持实际 iPhone 系统版本的 Xcode，首次打开完成额外组件安装。
2. 打开 `ios/Ferry.xcodeproj`，选择共享 `Ferry` scheme。Xcode → Settings → Accounts 添加自己的 Apple ID。
3. App target → Signing & Capabilities：保持 Automatically manage signing，选择自己的 Team，把 `PRODUCT_BUNDLE_IDENTIFIER` 改成自己唯一的反向域名标识。测试 target 若提示签名问题，同样选择 Team。
4. 确认 HealthKit capability 与 `Ferry.entitlements` 的 HealthKit entitlement 一致；不启用 Background Delivery。确认 Info.plist 的 `NSHealthShareUsageDescription`、`NSCalendarsFullAccessUsageDescription`、`NSRemindersFullAccessUsageDescription` 文案存在；健康写入集合必须为空。
5. 让 Xcode 生成并下载匹配 Bundle ID/Team/设备和 HealthKit capability 的 provisioning profile。免费 Personal Team 的可用 capability、有效期及设备数量有实际限制，不能预先保证 HealthKit 签名可用；若 Xcode 提示 Team 不支持该能力，需换成支持的 Apple Developer Program Team，不能靠删除 entitlement 伪装接入成功。
6. USB 连接 iPhone 17 Pro，手机选择“信任这台电脑”；按系统/Xcode 提示启用 设置 → 隐私与安全性 → 开发者模式并完成重启确认。选择该手机为运行目标，先 Product → Build，再 Run。
7. 若手机提示开发者不受信任，按系统提示在 设置 → 通用 → VPN 与设备管理 中信任相应开发者；是否出现此步骤取决于实际签名方式。
8. App 输入自己的 HTTPS 服务地址，在内嵌控制台登录。进入设备数据页，逐项触发系统授权，选择日历/列表；先同步一项小样本，核对服务端记录、时间/单位及状态，再扩大分项。
9. 验证“清除服务端副本”只影响副本；登出/过期/切站/进入后台后不再继续上传。签名过期需重新构建安装，长期 TestFlight/App Store 分发另按账号与审核流程处理，本次不代为发布。

可在终端核对工程及模拟器测试（设备名称按本机可用列表替换）：

```bash
xcodebuild -list -project ios/Ferry.xcodeproj
xcodebuild -showdestinations -project ios/Ferry.xcodeproj -scheme Ferry
xcodebuild -project ios/Ferry.xcodeproj -scheme Ferry \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' \
  CODE_SIGNING_ALLOWED=NO build test
```

模拟器构建/协议单元测试不能代替真机健康授权、读取及签名验证。不要把 Apple 凭证、私钥、provisioning profile 或自动签名账户信息提交仓库。

## iPhone 17 Pro 验收清单

- 冷启动、退出重登、旋转及前后台恢复时，控制台标题不被状态栏遮住，不出现双层安全区空白；原生设备页支持动态字体。
- 中文键盘、多行消息、历史滚动、附件选择和发送可用；既有 PWA 仍可独立运行。
- 健康四类权限逐项允许/拒绝、健康库无血氧/体重、日历/提醒拒绝、不选列表均不误报成功，不清空旧数据。
- 日历跨时区/夏令时、全天及重复事件正确区分；提醒无到期日、仅日期到期、已完成时间处理正确。
- 网络中断、重试、到达限额、重复点击、注销和切换服务器，不重复记录或跨站上传；清除后迟到提交不能恢复副本。
- 摘要显示保存记录数、范围、最新同步及完整性；能分页看记录并清除分项；无需模型即可使用。

## 平台依据

- [Apple：健康数据授权与读取隐私](https://developer.apple.com/documentation/healthkit/authorizing-access-to-health-data)
- [Apple：HealthKit](https://developer.apple.com/documentation/healthkit/)
- [Apple：EventKit](https://developer.apple.com/documentation/eventkit)
- [Apple WWDC23：日历访问级别](https://developer.apple.com/cn/videos/play/wwdc2023/10052/)
- [Apple：设置 HealthKit](https://developer.apple.com/documentation/healthkit/setting-up-healthkit)
- [Apple：在 Xcode 中添加 capability 与团队限制](https://developer.apple.com/cn/documentation/xcode/adding_capabilities_to_your_app/)

以上官方材料已用于核对读取隐私、iOS 17 日历授权边界与 capability 的团队限制；实际工程能否在用户 Xcode/Team 下构建和签名，仍以 macOS 验收为准。
