---
name: hacker-news
description: 查询 Hacker News 数据:top/new/best/ask/show/job 榜单及详情、帖子评论树、全文搜索、用户信息、文章正文提取。当用户提到 HN、Hacker News、科技圈热点、HN 热帖、某条 HN 新闻的内容或评论时使用。数据源为官方 Firebase API + Algolia 搜索 API,免密钥。
---

# hackerNews

零依赖 ESM 脚本,Node 18+ / Bun 通用。bun 不在默认 PATH,用绝对路径调用

## 执行方式

`<skill-dir>` 指本 skill 目录,即仓库内 `skills/hacker-news/`(pi 运行机上为所在仓库的绝对路径)

```bash
~/.bun/bin/bun <skill-dir>/scripts/hackerNews.mjs <cmd> [args] [--flags]
# 或 node <skill-dir>/scripts/hackerNews.mjs <cmd>
# 本机全局副本(可选): ~/.bun/bin/bun ~/.agents/skills/hacker-news/scripts/hackerNews.mjs
```

## 子命令

| 命令 | 说明 |
|---|---|
| `top [n]` | Top 榜单 + 详情(n 默认 10);`new/best/ask/show/job` 同理 |
| `top n --comments k` | 每帖附带 k 条顶层评论(已剥 HTML) |
| `item <id>` | 单条详情;`--tree --depth d` 输出评论树 |
| `user <name>` | karma / about / 提交数 |
| `search <q>` | Algolia 全文搜索;`--tags t --sort date --page p --numericFilters f` |
| `front` | 当前首页(Algolia front_page) |
| `body <url>` | 抓文章正文剥成纯文本,供喂给 LLM 总结 |
| `maxitem` / `updates` | 最大 item id / 变更轮询快照 |

全局 flag:`--json` 输出完整结构化数据(默认是对齐表格)

## 典型用法

```bash
# 今日热点速览(带讨论焦点)
~/.bun/bin/bun <skill-dir>/scripts/hackerNews.mjs top 10 --comments 3

# 读某帖完整评论
~/.bun/bin/bun <skill-dir>/scripts/hackerNews.mjs item 49844786 --tree --depth 3

# 拿文章原文交给 LLM 总结
~/.bun/bin/bun <skill-dir>/scripts/hackerNews.mjs body https://go.dev/blog/simd-experiment

# 按时间搜某主题
~/.bun/bin/bun <skill-dir>/scripts/hackerNews.mjs search 'local first' --tags story --sort date
```

## tags 参考

`story` | `comment` | `show_hn` | `ask_hn` | `job` | `poll` | `front_page` | `author_<name>` | `story_<id>` | 逗号组合如 `story,author_pg`

## 注意

- Algolia 分页硬上限 1000 条(paginationLimitedTo),更早历史用 `--numericFilters 'created_at_i>1690000000&created_at_i<1700000000'` 分段翻
- ask/show/job 榜单实际条数浮动(几十到 200),`n` 超量自动截断
- 已删除/不存在的 item 返回 null,脚本会跳过并在 stderr 计数
- 脚本做正文提取与评论拉取,不做摘要;总结交由 LLM 完成

## 库用法

`<skill-dir>/scripts/hackerNews.mjs` 可直接 import(零依赖,已导出下列函数)

```javascript
import { topStories, search, itemTree, fetchArticleText } from '<skill-dir>/scripts/hackerNews.mjs'
```

导出:`topStories/newStories/bestStories/askStories/showStories/jobStories(n)`、`item(id)`、`itemTree(id, depth)`、`topComments(id, k)`、`user(name)`、`search(q, opts)`、`frontPage()`、`maxItem()`、`updates()`、`fetchArticleText(url)`、`stripHtml(s)`、`pMap(items, fn)`
