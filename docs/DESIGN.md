---
version: 2
name: Ferry Console — DeepSeek Harness style
description: Ferry 控制台的视觉与交互规范。风格取自 DeepSeek Harness（deepseek.com/harness）：中性浅灰底、墨色文字、细灰边框、单一蓝色强调、药丸控件与 16px 圆角面板、极轻阴影；跟随系统明暗。面向 iOS 桌面 PWA 与桌面浏览器。
source: https://www.deepseek.com/harness/ （CSS 抽样：#F9FAFB / #202124 / #353638 / #81858C / #ADB2B8 / #CFD3D6 / #E7E9EC / 强调蓝 #617BEB / 成功 #22C55E / 危险 #F25A5A；深色 #0F0F0F / #151517 / #2C2C2E）
---

# Ferry 控制台设计规范

实现：`lib/python/observability/static/dashboard.css`（全部 token 定义在 `:root`，深色覆盖在 `@media (prefers-color-scheme: dark)`）。

## 颜色 token

| Token | 浅色 | 深色 | 用途 |
|---|---|---|---|
| `--bg` | `#F9FAFB` | `#0F0F0F` | 页面底色 |
| `--sidebar` | `#F3F4F6` | `#121214` | 桌面侧边栏 |
| `--panel` | `#FFFFFF` | `#151517` | 卡片 / 面板 |
| `--panel-2` | `#F5F6F8` | `#1E1E20` | 次级面、分段控件底、表头 |
| `--line` / `--line-strong` | `#E7E9EC` / `#CFD3D6` | `#2C2C2E` / `#3A3A3D` | 边框、分割线 |
| `--ink` / `--ink-2` | `#202124` / `#353638` | `#F9FAFB` / `#E2E3E6` | 标题 / 正文 |
| `--muted` / `--faint` | `#81858C` / `#ADB2B8` | `#ADB2B8` / `#81858C` | 说明文字 / 弱提示 |
| `--accent` | `#617BEB` | `#7C93F2` | 唯一强调色：主按钮、选中、开关、焦点环 |
| `--accent-soft` / `--accent-line` | `#EEF2FF` / `#D2E0FF` | `#1C2340` / `#2D3A6B` | 选中底、徽标、chip |
| `--ok` / `--warn` / `--danger` | `#22C55E` / `#C9821F` / `#F25A5A` | `#34D399` / `#F2AF63` / `#F87171` | 状态色，配同名 `-soft` 底 |
| `--heat-0..5` | 灰 → `#D2E0FF` → `#3F55C7` | `#1E1E20` → `#A6B8FF` | 用量热力图分级 |

模型系列配色（`dashboard.js` `usageColors`）：`#617BEB` `#22C55E` `#F2AF63` `#A797FC` `#58BCC8`，「其他」`#ADB2B8`。

## 形状、字体、阴影

- 圆角：面板 `16px`（`--r-panel`），控件 `10px`（`--r-control`），chip / 徽标 / 分段按钮 `999px`（`--r-pill`）
- 阴影：卡片 `--shadow-sm`（0 1px 2px rgba(0,0,0,.04)），浮层 `--shadow-md`（0 8px 24px rgba(0,0,0,.08)）
- 字体：系统字体栈 `-apple-system, "SF Pro Text", "PingFang SC", Inter…`；数字与时间用 `ui-monospace`；不加载任何 Web 字体（CSP 只允许 self）
- 字号：页面标题 26px（移动端 20px），卡片标题 16px，正文 14px，说明 12px，eyebrow 11px 大写字距 .08em

## 布局

- 桌面（> 900px）：左侧 244px 侧边栏（品牌、分组导航「监控 / 控制」、采集新鲜度、账号与退出）+ 右侧内容区（sticky 毛玻璃顶栏、最大 1240px）
- 移动（≤ 900px）：隐藏侧边栏，底部 5 项标签栏（聊天 / 概览 / 用量 / 记忆 / 设置）；聊天线程使用抽屉，性能 / 可靠性 / 运行状态走概览页内的分段切换；顶栏、标签栏与聊天输入区使用 `env(safe-area-inset-*)` 适配刘海与 Home 指示条
- 任何视图在 320px 宽下不得出现横向滚动（Playwright 检查）

## 组件

| 组件 | 规则 |
|---|---|
| 主按钮 `.primary` | 强调蓝实底白字；危险操作用 `.danger` / `.danger-outline` / `.danger-quiet` |
| 开关 `.switch` | 46×28 药丸，`role="switch"` + `aria-checked`，开为强调蓝 |
| 选择 | 原生 `<select>`：iOS 弹出滚轮，最友好；时间 = 时 + 分（5 分钟步长）两个选择器，报告时间额外有「整理完立即」 |
| 多选 `.chip` | 药丸按钮 `aria-pressed`，选中为强调浅底 + 强调字 |
| 分段控件 `.tabs` | 灰底胶囊，选中项白底 + 轻阴影 |
| 卡片 `.card` / `.surface` / `.metric` | 白底、细边、16px 圆角、极轻阴影；`.hero` 为强调浅色渐变 |
| 徽标 `.badge` | `data-tone` = ok / accent / muted |
| 提示条 `#toast` | 底部居中药丸，成功 2.6s、失败 5.2s 自动消失；`role="status"` |
| 确认层 `#sheet` | 桌面居中对话框，移动端底部抽屉；Esc / 点遮罩取消；用于撤销、删除、取消提醒 |

## 交互原则

- **模型配置只选不写，外观可自定义**：模型/系统配置保持白名单；App 外观用独立稳定表单，允许有界名称、本地头像、轻触偏好，不扩大通用配置写面。聊天允许消息文本、附件和线程重命名；可选命令目录由主服务下发
- 改动即生效：控件变更立即提交，行内置灰表示保存中；成功提示「已更新：…」，失败提示原因并回到服务端状态
- 破坏性操作二次确认（撤销整理、删除任务、取消提醒）；立即整理不确认（可撤销）
- 每项被控制台改过的配置显示「控制台已调整」徽标与「恢复默认」
- 控制视图每 20 秒、回到前台时静默刷新；监控视图沿用 15 秒刷新，切到控制视图时暂停
- 监控/控制不展示消息正文与记忆正文；聊天模块展示独立保存的可见历史；定时任务与提醒只显示前 20 字摘要
- 聊天：紧凑单行线程标题，左侧列表图标、右侧新建加号，不放刷新/改名/重置（列表改名、`/reset` 重置）；移动端右滑打开左侧抽屉、左滑关闭，保留原生选字与代码横滚。列表不显示“我的对话”标题，改名/复制使用统一线性图标和至少 44px 热区、无障碍标签，删除仍确认。首条有效输入自动命名，手改优先。创建反馈与草稿保持，流式进度、停止/重试、后台继续生成及按快照重连；斜杠菜单区分轻点与拖动。安全 DOM 渲染，不用 `innerHTML`
- 工具活动：放在助手消息内，原生 details 默认折叠，弱边框/紧凑间距；摘要直接显示当前实际操作（脚本/文件/搜索词的短脱敏参数）与次数，工具名为辅助信息；展开看名称、相同参数摘要、状态和耗时。流式更新保留展开状态，不显示原始参数/结果/思考；完成后保留，刷新默认折叠
- 移动输入：纯文本编辑区，粘贴不带 HTML；回车换行、按钮发送，中文组合输入提交完成再发送；发送期间可编辑下一条草稿，accepted 不清后来输入。焦点反馈集中于输入外框，保留键盘可见焦点。键盘模式跟随 visualViewport 高度/偏移，安全区不重复占位，输入区与附件区高度有界；仅消息区滚动，阅读旧消息保留锚点，明确发送后才跟随最新回复；退出聊天恢复外层滚动。iOS 原生辅助栏不能用 CSS 隐藏，输入方式调整的效果须真机验证
- Markdown 表格使用安全 DOM，列对齐/转义管道/行内代码可用；流式表头完整后再成表，手机表格自身横向滚动不撑宽页面
- App 外观表单不因 20 秒轮询被替换，未保存名称/图片预览和焦点保持；保存同步品牌/助手/安装资源，但不改登录用户名和 Pi 身份。已安装 iPhone 图标/名称可能需重新添加
- 轻触只在真实用户操作时尽力尝试、可关闭；不在输入/组合输入/流式完成/后台触发。Safari 无通用 Vibration API，原生 switch 尝试不保证硬件反馈，界面不声称震动已成功
- `prefers-reduced-motion` 关闭全部动画；所有可交互元素有 `:focus-visible` 焦点环

## PWA

- `manifest.webmanifest`：Ferry 个人 Agent 助手，`start_url` = `/observability/#chat`，`scope` = `/observability/`，`display: standalone`，图标 192 / 512 / maskable
- iOS：`apple-mobile-web-app-capable`、`status-bar-style=default`、`apple-touch-icon` 180px、`viewport-fit=cover`；standalone 限制误缩放、手机输入至少 16px，保留系统辅助功能边界
- 通知：用户主动授权，设置可关闭/测试；后台回复、提醒与每日任务使用标准 Web Push，锁屏只显示固定简短文案，点击同源会话；前台已读回复确认后取消提醒，短可见租约不直接吞掉未读完成事件
- Service Worker `/observability/sw.js`：只缓存外壳（html / css / js / 图标），网络优先、离线回退；`/api/` 与 `/metrics` 一律直通不缓存
- 图标：强调蓝渐变底 + 白色渡船与波浪线（`static/console-icon-*.png`）
