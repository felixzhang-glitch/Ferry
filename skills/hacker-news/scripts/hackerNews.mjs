#!/usr/bin/env node
// hackerNews — Hacker News API 客户端(CLI + 库)
// 零依赖纯 ESM,Node 18+ / Bun 均可运行:
//   node scripts/hackerNews.mjs top 10
//   bun  scripts/hackerNews.mjs top 10 --comments 3
//   import { topStories, search } from './hackerNews.mjs'
//
// 数据源:
//   Firebase 官方 API  https://hacker-news.firebaseio.com/v0   (榜单/item/user/updates,无鉴权无限流)
//   Algolia 搜索 API   https://hn.algolia.com/api/v1           (全文搜索/评论树/历史检索)
//
// Algolia 分页硬上限:paginationLimitedTo=1000,即任意查询最多只能取回前 1000 条
// 更早的历史数据需用 numericFilters=created_at_i>...&created_at_i<... 切时间窗分段翻

const FB = 'https://hacker-news.firebaseio.com/v0'
const ALG = 'https://hn.algolia.com/api/v1'
const UA = 'hackerNews-cli/1.0'
const TIMEOUT_MS = 15000
const CONCURRENCY = 8

// ---------- 基础工具 ----------

async function httpJson(url, { retries = 1 } = {}) {
  for (let attempt = 0; attempt <= retries; attempt++) {
    const ctrl = new AbortController()
    const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS)
    try {
      const res = await fetch(url, {
        headers: { 'User-Agent': UA },
        signal: ctrl.signal,
      })
      if (!res.ok) throw new Error(`HTTP ${res.status} ${url}`)
      return await res.json()
    } catch (err) {
      if (attempt === retries) throw err
    } finally {
      clearTimeout(timer)
    }
  }
}

const fb = (path) => httpJson(`${FB}/${path}.json`)
const alg = (path) => httpJson(`${ALG}/${path}`)

// 并发映射,上限 CONCURRENCY
export async function pMap(items, fn, concurrency = CONCURRENCY) {
  const out = new Array(items.length)
  let i = 0
  async function worker() {
    while (i < items.length) {
      const idx = i++
      out[idx] = await fn(items[idx], idx)
    }
  }
  await Promise.all(Array.from({ length: Math.min(concurrency, items.length) }, worker))
  return out
}

const HTML_ENTITIES = {
  '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"',
  '&#x27;': "'", '&#x2F;': '/', '&#39;': "'", '&apos;': "'",
  '&nbsp;': ' ', '&mdash;': '—', '&ndash;': '–',
}

// HN 的 text/title 是 HTML 实体转义 + <p>/<a> 标签,剥成纯文本
export function stripHtml(s) {
  if (!s) return ''
  let t = String(s)
  t = t.replace(/<p>/gi, '\n\n')
  t = t.replace(/<a\s+[^>]*href="([^"]*)"[^>]*>(.*?)<\/a>/gis, '$2 ($1)')
  t = t.replace(/<[^>]+>/g, '')
  for (const [k, v] of Object.entries(HTML_ENTITIES)) t = t.split(k).join(v)
  t = t.replace(/&#x([0-9a-f]+);/gi, (_, h) => String.fromCodePoint(parseInt(h, 16)))
  t = t.replace(/&#(\d+);/g, (_, d) => String.fromCodePoint(parseInt(d, 10)))
  return t.replace(/\n{3,}/g, '\n\n').trim()
}

export function ageStr(unixSec) {
  const m = Math.max(0, Math.floor(Date.now() / 1000 - unixSec) / 60)
  if (m < 60) return `${Math.floor(m)}m`
  if (m < 60 * 24) return `${(m / 60).toFixed(1)}h`
  return `${(m / 60 / 24).toFixed(1)}d`
}

const hnUrl = (id) => `https://news.ycombinator.com/item?id=${id}`

// 规整 story/job/poll 条目
function summarize(it) {
  return {
    id: it.id,
    type: it.type,
    title: stripHtml(it.title),
    url: it.url ?? null,
    by: it.by ?? null,
    score: it.score ?? null,
    comments: it.descendants ?? 0,
    time: it.time,
    age: ageStr(it.time),
    hn: hnUrl(it.id),
    ...(it.text ? { text: stripHtml(it.text) } : {}),
  }
}

// ---------- Firebase:榜单 / item / user ----------

const LIST_ENDPOINTS = {
  top: 'topstories', new: 'newstories', best: 'beststories',
  ask: 'askstories', show: 'showstories', job: 'jobstories',
}

async function storyList(kind, n = 10) {
  const ids = await fb(LIST_ENDPOINTS[kind])
  const picked = ids.slice(0, n)
  let skipped = 0
  const items = await pMap(picked, async (id) => {
    const it = await fb(`item/${id}`)
    if (!it || it.deleted || it.dead) { skipped++; return null }
    return summarize(it)
  })
  return { kind, total: ids.length, skipped, items: items.filter(Boolean) }
}

export const topStories = (n) => storyList('top', n)
export const newStories = (n) => storyList('new', n)
export const bestStories = (n) => storyList('best', n)
export const askStories = (n) => storyList('ask', n)
export const showStories = (n) => storyList('show', n)
export const jobStories = (n) => storyList('job', n)

export async function item(id) {
  const it = await fb(`item/${id}`)
  if (!it) return null
  return { ...it, title: stripHtml(it.title), text: stripHtml(it.text) }
}

export async function user(name) {
  const u = await fb(`user/${name}`)
  if (!u) return null
  return {
    id: u.id, created: u.created, karma: u.karma,
    about: stripHtml(u.about),
    submittedCount: u.submitted?.length ?? 0,
    submittedHead: u.submitted?.slice(0, 20) ?? [],
  }
}

export const maxItem = () => fb('maxitem')
export const updates = () => fb('updates')

// ---------- Algolia:评论树 / 搜索 / front page ----------

function mapAlgComment(c, depth, maxDepth) {
  const node = {
    id: c.id, by: c.author ?? null, time: c.created_at_i,
    age: c.created_at_i ? ageStr(c.created_at_i) : null,
    text: stripHtml(c.text),
  }
  if (depth < maxDepth && c.children?.length) {
    node.children = c.children.map((k) => mapAlgComment(k, depth + 1, maxDepth))
  } else if (c.children?.length) {
    node.moreChildren = c.children.length
  }
  return node
}

// 一次请求拿整棵评论树(Firebase 需要 N+1 次),depth 控制展开层数
export async function itemTree(id, depth = 2) {
  const d = await alg(`items/${id}`)
  if (!d || d.error) return null
  return {
    id: d.id, type: d.type, title: stripHtml(d.title), url: d.url ?? null,
    by: d.author, points: d.points, text: stripHtml(d.text),
    hn: hnUrl(d.id),
    comments: (d.children ?? []).map((c) => mapAlgComment(c, 1, depth)),
  }
}

// 顶层评论前 k 条(给榜单 --comments 用)
export async function topComments(id, k = 3) {
  const d = await alg(`items/${id}`)
  if (!d || d.error) return []
  return (d.children ?? []).slice(0, k).map((c) => ({
    by: c.author ?? null,
    age: c.created_at_i ? ageStr(c.created_at_i) : null,
    replies: c.children?.length ?? 0,
    text: stripHtml(c.text).slice(0, 300),
  }))
}

function mapAlgHit(h) {
  return {
    id: Number(h.objectID),
    title: stripHtml(h.title ?? h.story_title),
    url: h.url ?? null,
    by: h.author ?? null,
    points: h.points ?? null,
    comments: h.num_comments ?? null,
    created_at: h.created_at ?? null,
    hn: hnUrl(h.objectID),
    ...(h.comment_text ? { comment_text: stripHtml(h.comment_text) } : {}),
    ...(h.story_text ? { story_text: stripHtml(h.story_text).slice(0, 500) } : {}),
  }
}

// tags 示例:'story' | 'comment' | 'show_hn' | 'ask_hn' | 'job' | 'poll' | 'front_page'
//             'author_pg' | 'story_8863' | 组合 'story,author_pg'
export async function search(query, {
  tags = '', sort = 'relevance', page = 0, hitsPerPage = 20, numericFilters = '',
} = {}) {
  const ep = sort === 'date' ? 'search_by_date' : 'search'
  const params = new URLSearchParams({ query, page: String(page), hitsPerPage: String(hitsPerPage) })
  if (tags) params.set('tags', tags)
  if (numericFilters) params.set('numericFilters', numericFilters)
  const r = await alg(`${ep}?${params}`)
  return {
    query, total: r.nbHits, page: r.page, pages: r.nbPages,
    cappedNote: 'Algolia 分页上限 1000 条,更早数据用 numericFilters=created_at_i 分段',
    hits: (r.hits ?? []).map(mapAlgHit),
  }
}

export async function frontPage() {
  const r = await alg('search?tags=front_page')
  return (r.hits ?? []).map(mapAlgHit)
}

// ---------- 文章正文提取(给 LLM 喂原文用,非摘要) ----------

export async function fetchArticleText(url) {
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS)
  try {
    const res = await fetch(url, {
      headers: { 'User-Agent': UA, Accept: 'text/html' },
      signal: ctrl.signal,
      redirect: 'follow',
    })
    if (!res.ok) throw new Error(`HTTP ${res.status} ${url}`)
    const html = await res.text()
    let t = html
      .replace(/<script[\s\S]*?<\/script>/gi, '')
      .replace(/<style[\s\S]*?<\/style>/gi, '')
      .replace(/<(nav|header|footer|aside|form|noscript)[\s\S]*?<\/\1>/gi, '')
    // 优先 <article>/<main> 区块
    const m = t.match(/<(article|main)[^>]*>([\s\S]*?)<\/\1>/i)
    if (m) t = m[2]
    return stripHtml(t.replace(/<\/(p|div|h[1-6]|li|br)>/gi, '\n'))
  } finally {
    clearTimeout(timer)
  }
}

// ---------- CLI ----------

function parseArgs(argv) {
  const args = [], opts = {}
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i]
    if (a.startsWith('--')) {
      const [k, v] = a.slice(2).split('=')
      if (v !== undefined) opts[k] = v
      else if (argv[i + 1] && !argv[i + 1].startsWith('--')) opts[k] = argv[++i]
      else opts[k] = true
    } else args.push(a)
  }
  return { args, opts }
}

function printTable(items) {
  const w = { n: 3, score: 5, cmt: 5, age: 6 }
  console.log(`${'#'.padStart(w.n)} ${'pts'.padStart(w.score)} ${'cmt'.padStart(w.cmt)}  ${'age'.padEnd(w.age)}  title`)
  console.log('-'.repeat(100))
  items.forEach((it, i) => {
    console.log(
      `${String(i + 1).padStart(w.n)} ${String(it.score ?? '').padStart(w.score)} ${String(it.comments).padStart(w.cmt)}  ${it.age.padEnd(w.age)}  ${it.title}`
    )
    console.log(`     by ${it.by ?? '?'}  id=${it.id}  ${it.url ?? '(self/text post)'}`)
    if (it.topComments?.length) {
      for (const c of it.topComments) {
        console.log(`       └ ${c.by} (+${c.replies}): ${c.text.replace(/\n/g, ' ')}`)
      }
    }
    if (it.text) console.log(`       text: ${it.text.slice(0, 200).replace(/\n/g, ' ')}`)
  })
}

function printCommentTree(nodes, indent = '') {
  for (const c of nodes) {
    const first = c.text.split('\n')[0].slice(0, 200)
    console.log(`${indent}- ${c.by ?? '?'} (${c.age}): ${first}`)
    if (c.children) printCommentTree(c.children, indent + '  ')
    else if (c.moreChildren) console.log(`${indent}  … 还有 ${c.moreChildren} 条子回复,加深 --depth 查看`)
  }
}

const HELP = `hackerNews — Hacker News API CLI(Node 18+ / Bun,零依赖)

用法:
  top|new|best|ask|show|job [n=10] [--comments k] [--json]   榜单+详情,--comments 附顶层评论
  item <id> [--tree] [--depth d=2] [--json]                  单条详情 / 评论树
  user <name> [--json]                                       用户信息(karma/about/提交数)
  search <query> [--tags t] [--sort relevance|date]          Algolia 全文搜索
                 [--page p] [--hits n] [--numericFilters f]
  front [--json]                                             当前首页(Algolia front_page)
  maxitem                                                    当前最大 item id
  updates [--json]                                           最近变更的 items/profiles
  body <url>                                                 抓文章正文并剥成纯文本
  help

tags 参考: story | comment | show_hn | ask_hn | job | poll | front_page
           author_<name> | story_<id> | 逗号组合 'story,author_pg'
注意: Algolia 分页上限 1000 条,更早历史用 --numericFilters 'created_at_i>1690000000' 分段

示例:
  hackerNews.mjs top 10 --comments 3
  hackerNews.mjs search 'local first' --tags story --sort date
  hackerNews.mjs item 8863 --tree --depth 3
  hackerNews.mjs top 30 --json > today.json
`

async function main() {
  const { args, opts } = parseArgs(process.argv.slice(2))
  const cmd = args[0] ?? 'help'
  const asJson = Boolean(opts.json)
  const out = (data) => console.log(JSON.stringify(data, null, 2))

  switch (cmd) {
    case 'top': case 'new': case 'best': case 'ask': case 'show': case 'job': {
      const n = Number(args[1] ?? 10)
      const r = await storyList(cmd, n)
      if (opts.comments) {
        const k = Number(opts.comments)
        await pMap(r.items, async (it) => {
          it.topComments = await topComments(it.id, k).catch(() => [])
        })
      }
      if (asJson) out(r)
      else {
        printTable(r.items)
        console.error(`[${cmd}] 榜单共 ${r.total} 条,返回 ${r.items.length} 条` +
          (r.skipped ? `,跳过已删除 ${r.skipped} 条` : ''))
      }
      break
    }
    case 'item': {
      const id = Number(args[1])
      if (!id) throw new Error('用法: item <id> [--tree] [--depth d]')
      if (opts.tree) {
        const t = await itemTree(id, Number(opts.depth ?? 2))
        if (!t) { console.error(`item ${id} 不存在或已删除`); process.exitCode = 1; break }
        if (asJson) out(t)
        else {
          console.log(`${t.title ?? ''}  by ${t.by ?? '?'}  ${t.points ?? ''}pts  ${t.hn}`)
          if (t.text) console.log(stripHtml(t.text).slice(0, 300))
          printCommentTree(t.comments)
        }
      } else {
        const it = await item(id)
        if (!it) { console.error(`item ${id} 不存在或已删除`); process.exitCode = 1; break }
        if (asJson) out(it)
        else printTable([summarize(it)])
      }
      break
    }
    case 'user': {
      const name = args[1]
      if (!name) throw new Error('用法: user <name>')
      const u = await user(name)
      if (!u) { console.error(`user ${name} 不存在或无公开活动`); process.exitCode = 1; break }
      if (asJson) out(u)
      else {
        console.log(`${u.id}  karma=${u.karma}  created=${new Date(u.created * 1000).toISOString().slice(0, 10)}`)
        if (u.about) console.log(`about: ${u.about.slice(0, 200)}`)
        console.log(`submitted: ${u.submittedCount} 条,最近: ${u.submittedHead.slice(0, 5).join(', ')}`)
      }
      break
    }
    case 'search': {
      const query = args.slice(1).join(' ')
      if (!query) throw new Error('用法: search <query> [--tags t] [--sort date]')
      const r = await search(query, {
        tags: opts.tags ?? '',
        sort: opts.sort ?? 'relevance',
        page: Number(opts.page ?? 0),
        hitsPerPage: Number(opts.hits ?? 20),
        numericFilters: opts.numericFilters ?? '',
      })
      if (asJson) out(r)
      else {
        printTable(r.hits.map((h) => ({
          ...h, score: h.points, comments: h.comments ?? 0,
          age: h.created_at ? ageStr(Date.parse(h.created_at) / 1000) : '?',
        })))
        console.error(`共 ${r.total} 条,第 ${r.page + 1}/${r.pages} 页(${r.cappedNote})`)
      }
      break
    }
    case 'front': {
      const items = await frontPage()
      if (asJson) out(items)
      else printTable(items.map((h) => ({
        ...h, score: h.points, comments: h.comments ?? 0,
        age: h.created_at ? ageStr(Date.parse(h.created_at) / 1000) : '?',
      })))
      break
    }
    case 'maxitem': {
      const id = await maxItem()
      if (asJson) out({ maxitem: id })
      else console.log(id)
      break
    }
    case 'updates': {
      const u = await updates()
      if (asJson) out(u)
      else {
        console.log(`items (${u.items.length}): ${u.items.slice(0, 10).join(', ')}${u.items.length > 10 ? ' …' : ''}`)
        console.log(`profiles (${u.profiles.length}): ${u.profiles.slice(0, 10).join(', ')}${u.profiles.length > 10 ? ' …' : ''}`)
      }
      break
    }
    case 'body': {
      const url = args[1]
      if (!url) throw new Error('用法: body <url>')
      const text = await fetchArticleText(url)
      if (asJson) out({ url, chars: text.length, text })
      else console.log(text)
      break
    }
    case 'help': default:
      console.log(HELP)
  }
}

// 直接执行时跑 CLI;被 import 时只导出函数
const isMain = process.argv[1] && import.meta.url === new URL(`file://${process.argv[1]}`).href
if (isMain) {
  main().catch((err) => {
    console.error(`error: ${err.message}`)
    process.exitCode = 1
  })
}
