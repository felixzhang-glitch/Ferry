# 文档索引

| 文档 | 主题 |
|------|------|
| [architecture.md](architecture.md) | 系统架构与技术栈 |
| [DESIGN.md](DESIGN.md) | Ferry 控制台视觉与交互规范（DeepSeek Harness 风格、明暗 token、PWA） |
| [channels.md](channels.md) | 渠道接入：飞书 WS 长连接 / 微信 sidecar |
| [routing.md](routing.md) | 单 pi 接入：接口、只读状态命令、配置迁移与数据兼容 |
| [sessions.md](sessions.md) | pi 原生会话、key 与附件通知、去重、队列、定时任务 |
| [memory.md](memory.md) | 长期记忆：NOTE-FORMAT 文件格式、写入触发、Dream 离线整理与撤销、常驻注入与审查回滚、旧格式迁移 |
| [core-beliefs.md](core-beliefs.md) | 核心设计信念与边界约束（CLI-native、只做桥接） |
| [functional-tests.md](functional-tests.md) | 核心功能回归测试清单（迭代验收必过） |
| [requirement-changes.md](requirement-changes.md) | 需求变更记录（每次需求变化追加，稳定维护） |
| [PRODUCT.md](PRODUCT.md) | 产品定位与功能项清单（开发方向锚点） |
| [TEST.md](TEST.md) | 测试要点与用例映射 |
| [SECURITY.md](SECURITY.md) | 安全要求：密钥扫描、签名校验、敏感数据 |
| [RELIABILITY.md](RELIABILITY.md) | 可靠性与运维：部署、Ferry 控制台与 pi 历史 Token 用量、Dream 与报告推送、故障处理 |
| [QUALITY_SCORE.md](QUALITY_SCORE.md) | 质量评分基线与复评记录 |
| [../.qoder/hooks/README.md](../.qoder/hooks/README.md) | 推送前密钥扫描钩子：安装、覆盖范围、误报处理（`.qoder` 不入库，此类文件仅本机可见） |
| [references/README.md](references/README.md) | 外部参考资料：[pi-cli.txt](references/pi-cli.txt)（唯一后端）、[feishu-bot-api.md](references/feishu-bot-api.md)、[wechat-ilink.md](references/wechat-ilink.md) |
