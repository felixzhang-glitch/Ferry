"use strict";
(() => {
  const API = "/api/observability/v1";
  const $ = (id) => document.getElementById(id);
  const numberFormat = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 });
  const state = { authenticated: false, data: null, receivedAt: 0, page: 0, controller: null, revision: 0, failed: false };
  // pi history has its own lifecycle: never share aborts, filters or cached data with Ferry metrics.
  const usage = { data: null, meta: null, controller: null, revision: 0, loading: false, error: "", stale: false, range: "30d", start: "", end: "", heatSpan: 180, heatRendered: 0, scanTimer: null };
  const usageActive = () => state.authenticated && !$("panel-usage").hidden;
  const known = (value) => typeof value === "number" && Number.isFinite(value);
  const number = (value) => known(value) ? numberFormat.format(value) : "—";
  const seconds = (value) => known(value) ? `${number(value)} s` : "—";
  const percent = (value) => known(value) ? `${number(value * 100)}%` : "—";
  const text = (id, value) => { $(id).textContent = value; };
  const date = (value) => known(value) ? new Date(value * 1000).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false }) : "—";
  const shortDate = (value) => known(value) ? new Date(value * 1000).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }) : "—";
  const names = { feishu: "飞书", wechat: "微信", system: "系统", interactive: "交互", scheduled: "定时", push: "推送", success: "成功", error: "错误", cancelled: "已取消", partial: "部分失败", unknown: "未知", running: "执行中", rejected: "已拒绝", cli: "CLI", worker: "常驻 Worker" };
  const stageNames = { pipeline: "轮次处理", agent: "执行", attempt: "单次尝试", queue_wait: "排队", delivery: "回发", first_text: "首响应", tool: "工具", worker_wait: "Worker 等待", worker_setup: "Worker 准备", task_generation: "任务生成", task_delivery: "任务推送", channel_io: "渠道辅助传输" };
  const label = (value) => typeof value === "string" ? (names[value] || value) : "—";

  function el(tag, content, className) {
    const node = document.createElement(tag);
    if (content !== undefined && content !== null) node.textContent = String(content);
    if (className) node.className = className;
    return node;
  }

  function empty(target, message) {
    $(target).replaceChildren(el("p", message, "empty"));
  }

  function cards(target, items) {
    const nodes = items.map(([title, value, note, tone]) => {
      const card = el("article", null, "metric");
      if (tone) card.dataset.tone = tone;
      card.append(el("h3", title), el("span", value, "metric-value"), el("p", note, "metric-note"));
      return card;
    });
    $(target).replaceChildren(...nodes);
  }

  function table(target, headings, rows, options = {}) {
    if (!rows.length) {
      empty(target, options.empty || "当前筛选下没有记录。可扩大时间窗口，或选择全部渠道与模型");
      return;
    }
    const wrapper = el("div", null, "table-scroll");
    wrapper.tabIndex = 0;
    wrapper.setAttribute("role", "region");
    wrapper.setAttribute("aria-label", options.title || "数据表，可横向滚动");
    const node = el("table");
    const head = el("thead");
    const headerRow = el("tr");
    headings.forEach((heading, index) => {
      const cell = el("th", heading, options.numeric?.includes(index) ? "numeric" : "");
      cell.scope = "col";
      headerRow.append(cell);
    });
    head.append(headerRow);
    const body = el("tbody");
    rows.forEach((row) => {
      const tr = el("tr");
      row.forEach((value, index) => {
        const cell = el("td", null, options.numeric?.includes(index) ? "numeric" : "");
        if (value instanceof Node) cell.append(value);
        else cell.textContent = value === null || value === undefined ? "—" : String(value);
        tr.append(cell);
      });
      body.append(tr);
    });
    node.append(head, body);
    wrapper.append(node);
    $(target).replaceChildren(wrapper);
  }

  function breakdown(target, rows, countLabel = "计数") {
    table(target, ["名称", countLabel, "已知 Token", "错误"], (Array.isArray(rows) ? rows : []).map((row) => [label(row.name), number(row.count), number(row.tokens), number(row.errors)]), { numeric: [1, 2, 3], title: "分类指标表" });
  }

  function svgNode(tag, attributes, content) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, String(value)));
    if (content !== undefined) node.textContent = content;
    return node;
  }

  function chart(target, raw, series, title) {
    const points = (Array.isArray(raw) ? raw : []).filter((p) => known(p.at)).sort((a, b) => a.at - b.at);
    if (!points.length || !points.some((p) => series.some((key) => known(p[key])))) {
      empty(target, "尚无可绘制的趋势。选择更大的时间窗口，或确认主服务已启用采集");
      return;
    }
    const width = 900, height = 225, left = 58, right = 16, top = 16, bottom = 24;
    const maximum = Math.max(1, ...points.flatMap((p) => series.map((key) => known(p[key]) ? p[key] : 0)));
    const start = points[0].at, end = points[points.length - 1].at;
    const x = (at) => start === end ? (width + left - right) / 2 : left + (at - start) / (end - start) * (width - left - right);
    const y = (value) => height - bottom - value / maximum * (height - top - bottom);
    const svg = svgNode("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": title });
    svg.append(svgNode("title", {}, `${title}；${points.length} 个时间桶。缺失值断线，零值位于基线`));
    for (let index = 0; index <= 4; index += 1) {
      const value = maximum * index / 4;
      svg.append(svgNode("line", { x1: left, x2: width - right, y1: y(value), y2: y(value), class: "chart-grid" }));
      svg.append(svgNode("text", { x: left - 10, y: y(value) + 4, "text-anchor": "end", class: "chart-label" }, number(value)));
    }
    series.forEach((key) => {
      let path = "", penDown = false;
      points.forEach((point) => {
        if (!known(point[key])) { penDown = false; return; }
        path += `${penDown ? "L" : "M"}${x(point.at).toFixed(2)},${y(point[key]).toFixed(2)} `;
        penDown = true;
      });
      svg.append(svgNode("path", { d: path.trim(), class: `chart-line chart-${key}` }));
      // Dots make single samples and all-zero observations visible as real data.
      points.forEach((point, index) => {
        if (!known(point[key])) return;
        const isolated = !known(points[index - 1]?.[key]) && !known(points[index + 1]?.[key]);
        if (points.length > 100 && !isolated && index !== points.length - 1) return;
        const dot = svgNode("circle", { cx: x(point.at), cy: y(point[key]), r: points.length < 10 || isolated ? 3 : 1.5, class: `chart-point chart-${key}` });
        dot.append(svgNode("title", {}, `${shortDate(point.at)} · ${key}: ${number(point[key])}`));
        svg.append(dot);
      });
    });
    const range = el("div", null, "chart-range");
    range.append(el("span", shortDate(start)), el("span", shortDate(end)));
    $(target).replaceChildren(svg, range);
  }

  function renderRuns() {
    const runs = Array.isArray(state.data?.runs) ? state.data.runs : [];
    const size = 10;
    state.page = Math.min(state.page, Math.max(0, Math.ceil(runs.length / size) - 1));
    const rows = runs.slice(state.page * size, (state.page + 1) * size).map((run) => {
      const id = el("span", typeof run.id === "string" ? run.id.slice(0, 10) : "—", "run-id");
      if (typeof run.id === "string") id.title = run.id;
      const status = el("span", label(run.status), "status");
      if (["success", "error", "partial", "unknown", "running", "cancelled"].includes(run.status)) status.dataset.status = run.status;
      return [id, shortDate(run.started_at), label(run.channel), label(run.source), label(run.model), status, seconds(run.duration_seconds), seconds(run.ttft_seconds), number(run.tokens_total), number(run.tool_calls), number(run.attempts), number(run.usage_missing)];
    });
    table("runs-table", ["轮次 ID", "开始时间", "渠道", "来源", "模型", "状态", "总耗时", "首响应", "已知 Token", "工具", "尝试", "缺失用量"], rows, { numeric: [6, 7, 8, 9, 10, 11], title: "最近轮次，只包含元数据" });
    $("runs-prev").disabled = state.page === 0;
    $("runs-next").disabled = (state.page + 1) * size >= runs.length;
    text("runs-page", runs.length ? `${state.page + 1} / ${Math.ceil(runs.length / size)} 页 · 已返回 ${runs.length} 轮` : "0 条轮次记录");
  }

  function freshness() {
    if (usageActive()) { usageFreshness(); return; }
    if (!state.authenticated || !state.data) return;
    const quality = state.data.quality || {};
    const age = known(quality.age_seconds) ? Math.max(0, quality.age_seconds + (performance.now() - state.receivedAt) / 1000) : null;
    let status = quality.state;
    let title = { ok: "采集数据新鲜", stale: "采集心跳已过期", empty: "尚无采集数据", partial: "采集数据不完整" }[status] || "采集质量未知";
    if (known(age) && age > 15 && status !== "empty") { status = "stale"; title = "采集心跳已过期"; }
    if (!known(age) && status === "ok") { status = "partial"; title = "采集新鲜度未知"; }
    if (state.failed) { status = "error"; title = "读取失败 · 保留上次数据"; }
    $("freshness-state").dataset.state = status || "unknown";
    text("freshness-state", title);
    text("freshness-time", known(age) ? `${Math.floor(age)} 秒前采集 · ${date(state.data.data_until)}` : `数据时间 ${date(state.data.data_until)}`);
    const runtime = state.data.runtime || {};
    const runtimeAge = known(runtime.timestamp) && known(state.data.generated_at) ? Math.max(0, state.data.generated_at - runtime.timestamp + (performance.now() - state.receivedAt) / 1000) : null;
    text("runtime-notice", !known(runtimeAge) ? "尚未收到主服务快照。确认主服务已启用采集，并在有数据后刷新；Web 可访问不代表主服务正常" : runtimeAge > 15 || state.failed ? "以下是历史快照，不能用于判断当前运行状态。检查主服务采集后刷新" : "以下为最近采集的主服务快照，不是实时健康保证；数值不会随上方筛选变化");
  }

  function renderRuntime(runtime) {
    const entries = [["运行方式", label(runtime.mode)], ["主服务启动", date(runtime.started_at)], ["快照采集", date(runtime.timestamp)]];
    $("runtime-meta").replaceChildren(...entries.map(([key, value]) => {
      const group = el("div"); group.append(el("dt", key), el("dd", value)); return group;
    }));
    const gaugeNames = {
      queue_feishu: ["飞书排队", "项"], queue_wechat: ["微信排队", "项"], active_tasks: ["活动任务", "项"], worker_total: ["Worker 总数", "个"], worker_busy: ["忙碌 Worker", "个"], worker_starting: ["启动中 Worker", "个"], circuit_open: ["熔断开启", "0 否 / 1 是"], session_mappings: ["会话映射", "个"], daily_tasks: ["每日任务", "项"], reminders: ["提醒", "项"], rss_bytes: ["进程常驻内存", "byte"], cpu_seconds: ["累计 CPU 时间", "秒"], disk_free_bytes: ["磁盘可用空间", "byte"], event_loop_lag_seconds: ["事件循环延迟", "秒"], pi_active: ["活动 pi", "个"]
    };
    const gauges = runtime.gauges || {};
    const rows = Object.entries(gaugeNames).filter(([key]) => Object.hasOwn(gauges, key)).map(([key, [name, unit]]) => [name, number(gauges[key]), unit]);
    table("runtime-table", ["指标", "最近快照值", "单位"], rows, { numeric: [1], title: "主服务运行指标", empty: "尚未上报运行指标。启用主服务采集后刷新；未上报不表示数值为零" });
  }

  function render(data) {
    const s = data.summary || {}, p = data.performance || {}, q = data.quality || {}, b = data.breakdown || {};
    text("track-inbound", known(s.messages) ? `${number(s.messages)} 条` : "—");
    text("track-inbound-note", `去重 ${number(s.duplicates)} 条`);
    text("track-queue", seconds(p.queue_wait?.p95));
    text("track-queue-note", `P95 等待 · 拒绝 ${number(s.queue_rejected)} 次`);
    text("track-execution", known(s.turns) ? `${number(s.turns)} 轮` : "—");
    text("track-execution-note", `已结束 · 错误 ${number(s.errors)} 轮`);
    text("track-delivery", known(s.delivery_success) ? `${number(s.delivery_success)} 次` : "—");
    text("track-delivery-note", `平台接受 · 失败 ${number(s.delivery_error)} 次`);
    cards("overview-cards", [["结束轮次", number(s.turns), `窗口内 ${number(s.active_sessions)} 个被观测会话`], ["轮次成功率", percent(s.success_rate), "成功 / (成功 + 错误)，不含取消"], ["处理耗时 P95", seconds(p.pipeline?.p95), `${number(p.pipeline?.count)} 个有效样本`], ["已知 Token", number(s.tokens_total), `Ferry运行事件口径 · 缺失用量 ${number(s.usage_missing)} 次 · 非账单`]]);
    cards("reliability-cards", [["错误轮次", number(s.errors), "结束状态为错误", known(s.errors) && s.errors > 0 ? "error" : ""], ["取消轮次", number(s.cancelled), "不计入成功率分母"], ["重试", number(s.retries), "可见的重试事件"], ["回发失败", number(s.delivery_error), "含部分失败；不是已读状态", known(s.delivery_error) && s.delivery_error > 0 ? "error" : ""]]);
    cards("quality-cards", [["丢弃事件", number(q.dropped_events), "采集端计数"], ["写入错误", number(q.write_errors), "采集端计数"], ["无效行", number(q.invalid_lines), "存储读取计数"], ["缺失用量", number(s.usage_missing), "不能将缺失值计为零"]]);
    const warnings = Array.isArray(q.warnings) ? q.warnings.filter((item) => typeof item === "string") : [];
    $("quality-warnings").replaceChildren(...warnings.map((warning) => el("li", warning)));
    if (!warnings.length) $("quality-warnings").append(el("li", "暂无额外质量提示。未报告异常不等于已验证健康"));
    table("performance-table", ["阶段", "样本", "P50 / s", "P95 / s", "P99 / s", "均值 / s"], Object.entries(p).filter(([, value]) => value && typeof value === "object").map(([stage, value]) => [stageNames[stage] || stage, number(value.count), number(value.p50), number(value.p95), number(value.p99), number(value.mean)]), { numeric: [1, 2, 3, 4, 5], title: "阶段耗时分位数", empty: "当前窗口没有阶段耗时样本。可扩大时间窗口，或确认对应阶段已接入采集" });
    breakdown("channel-breakdown", b.channels, "轮次");
    breakdown("status-breakdown", b.statuses, "轮次");
    table("tool-breakdown", ["工具", "调用", "错误"], (Array.isArray(b.tools) ? b.tools : []).map((row) => [label(row.name), number(row.count), number(row.errors)]), { numeric: [1, 2], title: "工具调用计数；不提供工具用量归因" });
    chart("turns-trend", data.timeseries, ["turns", "errors"], "轮次与错误趋势");
    renderRuns();
    renderRuntime(data.runtime || {});
    const options = (Array.isArray(b.models) ? b.models : []).filter((row) => typeof row.name === "string" && row.name.length <= 96).map((row) => { const option = el("option"); option.value = row.name; return option; });
    $("model-options").replaceChildren(...options);
    const noEvents = !Array.isArray(data.timeseries) || data.timeseries.length === 0;
    $("empty-notice").hidden = usageActive() || (q.state !== "empty" && !noEvents);
    text("empty-notice", q.state === "empty" ? "尚无主服务采集数据。确认主服务已启用可观测采集，再刷新；这里的空白不代表服务正常" : "此筛选下没有观测事件。尝试选择全部渠道、全部模型或更大的时间窗口");
    freshness();
  }

  const usageColors = ["#276fbf", "#548893", "#718ed0", "#445775", "#ab802e", "#a2b3c3"];
  const dayMs = 86400000;
  const todayCN = () => new Date(Date.now() + 8 * 3600000).toISOString().slice(0, 10);
  const validDay = (d) => typeof d === "string" && /^\d{4}-\d{2}-\d{2}$/.test(d) && Number.isFinite(Date.parse(d)) && new Date(d).toISOString().slice(0, 10) === d;
  const shiftDay = (d, offset) => new Date(Date.parse(d) + offset * dayMs).toISOString().slice(0, 10);
  const msDate = (value) => known(value) ? date(value / 1000) : "尚未完成同步";
  const compactNumber = (value) => !known(value) ? "—" : value >= 1e9 ? `${number(value / 1e9)}B` : value >= 1e6 ? `${number(value / 1e6)}M` : value >= 1e3 ? `${number(value / 1e3)}K` : number(value);
  const modelKey = (row) => JSON.stringify([row.p || "", row.m || ""]);
  const modelName = (row) => {
    const maintenance = row.compactionReq > 0 && !row.assistantReq;
    let name = !row.m || row.m === "unknown" ? (maintenance ? "未标记/压缩" : "未标记模型") : row.m;
    if (maintenance && row.m === "compaction") name = "未标记/压缩";
    if (maintenance && row.m === "branch_summary") name = "未标记/分支摘要";
    return `${row.p && row.p !== "unknown" ? row.p + " / " : ""}${name}`;
  };
  function daySequence(start, end) {
    if (!validDay(start) || !validDay(end) || start > end) return [];
    const days = [];
    for (let d = start; d <= end; d = shiftDay(d, 1)) days.push(d);
    return days;
  }
  function usageBounds(data) {
    const r = data?.range;
    return r?.preset === "all" ? { start: data?.scan?.firstDay, end: data?.scan?.lastDay } : r || {};
  }
  function usageRangeLabel(data) {
    const r = usageBounds(data);
    return validDay(r.start) && validDay(r.end) ? `${r.start} — ${r.end}${data?.range?.preset === "all" ? " · 全部历史" : ""}` : "暂无日期记录";
  }
  function usageScanFailed(data) {
    return data?.quality?.state === "stale" || data?.failed > 0 || data?.scan?.failed > 0;
  }
  function usageStateLabel() {
    if (usage.error) return [usage.data ? "stale" : "error", usage.data ? "pi 读取失败 · 数据已过期" : "pi 读取失败"];
    if (usage.stale) return ["stale", "pi 同步失败 · 数据已过期"];
    if (usage.meta?.scan?.scanning || usage.meta?.quality?.state === "scanning") return ["scanning", "pi 正在后台同步"];
    if (usage.loading) return ["scanning", "正在读取 pi 用量"];
    const status = usage.meta?.quality?.state;
    return [status || "unknown", { ok: "pi 同步完成", partial: "pi 数据不完整", empty: "尚无 pi 用量记录", stale: "pi 数据已过期" }[status] || "尚未读取 pi 用量"];
  }
  function usageFreshness() {
    if (!usageActive()) return;
    const [status, title] = usageStateLabel();
    $("freshness-state").dataset.state = status;
    text("freshness-state", title);
    text("freshness-time", `pi 最近同步 · ${msDate(usage.meta?.lastScanAt ?? usage.meta?.scan?.lastScanAt)}`);
  }
  function usageStatus() {
    const data = usage.meta, scan = data?.scan || {}, q = data?.quality || {};
    const [status, title] = usageStateLabel();
    text("usage-sync-state", title);
    $("usage-sync-state").dataset.state = status;
    const message = usage.error || (usage.stale ? "扫描失败" : usage.loading ? "正在读取所选日期…" : scan.scanning ? "增量扫描进行中，将自动读取完成结果" : q.state === "empty" ? "没有可用记录，可点击「刷新并同步」重试或扩大日期范围" : "仅扫描 pi 原生记录，不修改会话历史");
    text("usage-status", `${message}${(usage.error || usage.stale) && usage.data ? `；保留上次可用图表（${usageRangeLabel(usage.data)}）` : ""} · 最近同步 ${msDate(data?.lastScanAt ?? scan.lastScanAt)}`);
    text("usage-scan-meta", data ? `会话 ${number(data.sessions)} · 扫描文件 ${number(scan.files)} · 本次变更 ${number(scan.changedFiles)} · usage 记录 ${number(scan.records)} · 来源目录 ${number(Array.isArray(scan.sourceDirectories) ? scan.sourceDirectories.length : scan.sourceDirectories)} · 扫描覆盖 ${scan.firstDay || "—"} — ${scan.lastDay || "—"}` : "等待 pi 用量 API 返回数据");
    $("usage-refresh").disabled = usage.loading;
    text("usage-refresh", usage.loading ? "正在读取…" : "刷新并同步");
    const qualityLabels = [["missingUsage", "缺失用量"], ["invalidLines", "无效行"], ["pendingLines", "待完整行"], ["duplicateRecords", "已去重记录"], ["weakIdentities", "弱身份记录"], ["unattributedTurns", "未归属轮次"], ["totalMismatches", "总量不一致"]];
    $("usage-quality").hidden = !data && !usage.error;
    text("usage-quality-summary", data ? `失败 ${number(data.failed)} / 扫描失败 ${number(scan.failed)} · ${qualityLabels.map(([key, name]) => `${name} ${number(q[key])}`).join(" · ")}` : "尚未成功读取用量，缺失数据不补零");
    $("usage-warnings").replaceChildren(...(Array.isArray(q.warnings) ? q.warnings : []).filter((w) => typeof w === "string").map((w) => el("li", w)));
    usageFreshness();
  }
  function usageLegend(target, series, withCache = false) {
    const nodes = series.map((row) => {
      const node = el("span");
      const swatch = el("i", null, "usage-swatch");
      swatch.style.backgroundColor = row.color;
      node.append(swatch, el("span", row.name));
      return node;
    });
    if (withCache) nodes.push(el("span", "缓存读取率", "usage-cache-legend"));
    $(target).replaceChildren(...nodes);
  }
  function renderUsageCalendar() {
    const data = usage.data;
    if (!data) return;
    const end = todayCN(), start = shiftDay(end, 1 - usage.heatSpan), days = daySequence(start, end);
    const rows = new Map(data.calendar.filter((row) => validDay(row.d)).map((row) => [row.d, row]));
    const offset = (new Date(start).getUTCDay() + 6) % 7;
    const weeks = Math.ceil((days.length + offset) / 7);
    const maximum = days.reduce((max, d) => Math.max(max, rows.get(d)?.total || 0), 1);
    const grid = el("div", null, "usage-heat-grid");
    grid.style.setProperty("--weeks", weeks);
    grid.style.minWidth = `${34 + weeks * 18}px`;
    const recorded = days.filter((d) => known(rows.get(d)?.total));
    const active = recorded.filter((d) => rows.get(d).total > 0);
    text("usage-calendar-summary", `${start} — ${end} · ${recorded.length} 天有记录 / ${active.length} 天有 Token 用量 · 不随上方日期筛选；斜线表示未采集或无日记录`);
    ["一", "二", "三", "四", "五", "六", "日"].forEach((name, i) => {
      const node = el("span", name, "usage-weekday"); node.style.gridRow = i + 2; node.style.gridColumn = 1; grid.append(node);
    });
    let lastMonth = "";
    const focusedDay = document.activeElement?.dataset?.day;
    const previousDay = focusedDay || document.querySelector("#usage-heatmap [tabindex='0']")?.dataset?.day;
    const selectedDay = days.includes(previousDay) ? previousDay : end;
    const buttons = [];
    days.forEach((d, i) => {
      const column = Math.floor((i + offset) / 7) + 2, row = rows.get(d), value = row?.total;
      const month = d.slice(0, 7);
      if (month !== lastMonth && (i === 0 || (i + offset) % 7 === 0)) {
        const caption = el("span", `${Number(d.slice(5, 7))}月`, "usage-month");
        caption.style.gridColumn = column; caption.style.gridRow = 1; caption.title = month; grid.append(caption); lastMonth = month;
      }
      const level = !known(value) ? "missing" : value === 0 ? "0" : String(Math.min(5, Math.ceil(value / maximum * 5)));
      const description = `${d} · ${known(value) ? `${number(value)} Token · ${number(row.req)} 请求 · 缓存读取率 ${percent(row.cacheRate)}` : "未采集 / 无日记录（不是 0）"}`;
      const cell = el("button", null, "usage-heat-cell");
      cell.type = "button"; cell.dataset.level = level; cell.dataset.day = d;
      cell.style.gridRow = (i + offset) % 7 + 2; cell.style.gridColumn = column;
      cell.title = description; cell.setAttribute("aria-label", description);
      cell.tabIndex = d === selectedDay ? 0 : -1;
      const describe = () => text("usage-heat-tooltip", description);
      cell.addEventListener("mouseenter", describe);
      cell.addEventListener("focus", () => { buttons.forEach((b) => { b.tabIndex = b === cell ? 0 : -1; }); describe(); });
      cell.addEventListener("click", describe);
      cell.addEventListener("keydown", (event) => {
        const move = { ArrowLeft: -7, ArrowRight: 7, ArrowUp: -1, ArrowDown: 1, Home: -i, End: days.length - 1 - i }[event.key];
        if (move === undefined) return;
        event.preventDefault(); buttons[Math.max(0, Math.min(days.length - 1, i + move))].focus();
      });
      buttons.push(cell); grid.append(cell);
    });
    $("usage-heatmap").replaceChildren(grid);
    if (focusedDay) buttons.find((b) => b.dataset.day === focusedDay)?.focus({ preventScroll: true });
    else text("usage-heat-tooltip", "悬停、点击或用方向键查看每日用量");
    if (usage.heatRendered !== usage.heatSpan) $("usage-heat-scroll").scrollLeft = $("usage-heat-scroll").scrollWidth;
    usage.heatRendered = usage.heatSpan;
    table("usage-calendar-table", ["日期", "采集状态", "Token", "请求", "缓存读取率"], days.slice().reverse().map((d) => {
      const row = rows.get(d); return [d, known(row?.total) ? "有日记录" : "未采集 / 无日记录", number(row?.total), number(row?.req), percent(row?.cacheRate)];
    }), { numeric: [2, 3, 4], title: "热力图每日明细" });
  }
  function renderUsageTrend(data, series) {
    const rows = new Map(data.byDay.filter((row) => validDay(row.d)).map((row) => [row.d, row]));
    const bounds = usageBounds(data);
    const seq = daySequence(bounds.start, bounds.end);
    usageLegend("usage-trend-legend", series, true);
    if (!seq.length) { empty("usage-trend", "所选日期没有日记录，请扩大日期范围或同步历史"); empty("usage-daily-table", "暂无每日数据"); return; }
    const perDay = new Map();
    data.byDayModel.forEach((row) => {
      if (!perDay.has(row.d)) perDay.set(row.d, new Map());
      const per = perDay.get(row.d), key = modelKey(row);
      per.set(key, (per.get(key) || 0) + (known(row.total) ? row.total : 0));
    });
    const width = Math.max(720, seq.length * 12 + 114), height = 270, left = 64, right = 48, top = 22, base = 230;
    const plotWidth = width - left - right, step = plotWidth / seq.length;
    const maximum = seq.reduce((max, d) => Math.max(max, rows.get(d)?.total || 0), 1);
    const y = (v) => base - v / maximum * (base - top);
    const svg = svgNode("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": "每日 Top 5 模型及其他 Token 堆叠柱，叠加缓存读取率；完整数据见下方表格" });
    svg.style.minWidth = `${width}px`;
    svg.append(svgNode("title", {}, `每日模型用量 · ${usageRangeLabel(data)}；无日记录不补零，无缓存率则断线`));
    for (let i = 0; i <= 4; i++) {
      const value = maximum * i / 4, position = y(value);
      svg.append(svgNode("line", { x1: left, x2: width - right, y1: position, y2: position, class: "chart-grid" }));
      svg.append(svgNode("text", { x: left - 8, y: position + 4, "text-anchor": "end", class: "chart-label" }, compactNumber(value)));
      svg.append(svgNode("text", { x: width - right + 8, y: position + 4, class: "chart-label" }, `${i * 25}%`));
    }
    let path = "", penDown = false;
    const dots = [], tableRows = [];
    seq.forEach((d, i) => {
      const row = rows.get(d), per = perDay.get(d), values = series.slice(0, -1).map((s) => per?.get(s.key) || 0);
      const observed = known(row?.total);
      values.push(observed ? Math.max(0, row.total - values.reduce((a, b) => a + b, 0)) : 0);
      const x = left + (i + .5) * step;
      let stack = 0;
      values.forEach((v, index) => {
        if (!observed || v <= 0) return;
        stack += v;
        const rect = svgNode("rect", { x: x - step * .36, y: y(stack), width: step * .72, height: v / maximum * (base - top), fill: series[index].color });
        rect.append(svgNode("title", {}, `${d} · ${series[index].name} · ${number(v)} Token`)); svg.append(rect);
      });
      if (observed && row.total === 0) svg.append(svgNode("line", { x1: x - step * .36, x2: x + step * .36, y1: base, y2: base, stroke: usageColors[5], "stroke-width": 2 }));
      if (known(row?.cacheRate)) {
        const cy = base - row.cacheRate * (base - top);
        path += `${penDown ? "L" : "M"}${x},${cy} `; penDown = true;
        const dot = svgNode("circle", { cx: x, cy, r: 2.5, class: "usage-cache-point" });
        dot.append(svgNode("title", {}, `${d} · 缓存读取率 ${percent(row.cacheRate)}`)); dots.push(dot);
      } else penDown = false;
      if (i % Math.max(1, Math.ceil(seq.length / 8)) === 0 || i === seq.length - 1) svg.append(svgNode("text", { x, y: height - 12, "text-anchor": "middle", class: "chart-label" }, d.slice(5)));
      tableRows.push([d, observed ? "有日记录" : "未采集 / 无日记录", number(row?.total), ...values.map((v) => observed ? number(v) : "—"), percent(row?.cacheRate), number(row?.req), number(row?.turns)]);
    });
    svg.append(svgNode("path", { d: path, class: "usage-cache-line" }), ...dots);
    $("usage-trend").replaceChildren(svg);
    table("usage-daily-table", ["日期", "采集状态", "总 Token", ...series.map((s) => s.name), "缓存读取率", "请求", "日内去重轮次（不可相加）"], tableRows.reverse(), { numeric: Array.from({ length: series.length + 4 }, (_, i) => i + 2), title: "每日模型堆叠与缓存率明细" });
  }
  function renderUsageModels(data, models, series) {
    const grand = data.totals.total, radius = 70, circumference = 2 * Math.PI * radius;
    const svg = svgNode("svg", { viewBox: "0 0 220 220", role: "img", "aria-label": "模型 Token 用量占比；右侧排名与下方明细提供完整数值" });
    svg.append(svgNode("circle", { cx: 110, cy: 110, r: radius, fill: "none", stroke: "#e4eef9", "stroke-width": 24 }));
    let offset = 0;
    const ranking = [];
    series.forEach((row, index) => {
      const share = grand > 0 ? row.total / grand : null;
      if (share > 0) {
        const circle = svgNode("circle", { cx: 110, cy: 110, r: radius, fill: "none", stroke: row.color, "stroke-width": 24, "stroke-dasharray": `${share * circumference} ${circumference}`, "stroke-dashoffset": -offset, transform: "rotate(-90 110 110)" });
        circle.append(svgNode("title", {}, `${row.name} · ${number(row.total)} Token · ${percent(share)}`)); svg.append(circle);
        offset += share * circumference;
      }
      const item = el("li"), order = el("span", row.key === null ? "…" : String(index + 1).padStart(2, "0"), "usage-rank-number");
      const main = el("div", null, "usage-rank-main"), name = el("span", row.name, "usage-model-name");
      const track = el("div", null, "usage-rank-track"), bar = el("i");
      bar.style.width = `${known(share) ? share * 100 : 0}%`; bar.style.backgroundColor = row.color; track.append(bar); main.append(name, track);
      const value = el("span", `${number(row.total)} Token`, "usage-rank-value"); value.append(el("small", percent(share)));
      item.append(order, main, value); ranking.push(item);
    });
    svg.append(svgNode("text", { x: 110, y: 106, "text-anchor": "middle", class: "usage-donut-total" }, compactNumber(grand)), svgNode("text", { x: 110, y: 131, "text-anchor": "middle", class: "chart-label" }, grand > 0 ? "总 Token" : "暂无 Token 用量"));
    $("usage-donut").replaceChildren(svg);
    $("usage-model-ranking").replaceChildren(...(models.length ? ranking : [el("li", "所选日期暂无模型记录", "empty")]));
    table("usage-model-table", ["模型 / 提供方", "总 Token", "占比", "输入", "缓存读", "缓存写", "输出（含推理）", "请求", "助手请求", "压缩/分支摘要", "去重轮次", "缓存读取率"], models.map((row) => [modelName(row), number(row.total), percent(grand > 0 ? row.total / grand : null), number(row.in), number(row.cr), number(row.cw), number(row.out), number(row.req), number(row.assistantReq), number(row.compactionReq), number(row.turns), percent(row.cacheRate)]), { numeric: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11], title: "全部模型用量明细，压缩记录包含在总量中", empty: "所选日期没有模型用量记录，可扩大日期范围或同步历史" });
  }
  function renderUsage(data) {
    const totals = data.totals, models = data.byModel.slice().sort((a, b) => (b.total || 0) - (a.total || 0));
    const series = models.slice(0, 5).map((row, i) => ({ key: modelKey(row), name: modelName(row), total: row.total, color: usageColors[i] }));
    series.push({ key: null, name: "其他", total: models.slice(5).reduce((sum, row) => sum + (row.total || 0), 0), color: usageColors[5] });
    text("usage-shown-range", `图表日期 · ${usageRangeLabel(data)} · 查询生成 ${msDate(data.generatedAt)}`);
    cards("usage-cards", [["总 Token", number(totals.total), "含压缩/分支摘要 · 推理不重复计"], ["轮次", number(totals.turns), "所选日期内去重 · 不按日相加"], ["请求", number(totals.req), `usage 记录 · 上下文维护 ${number(totals.compactionReq)} 条`], ["活跃天", number(totals.activeDays), "所选日期内活跃日"], ["缓存读取率", percent(totals.cacheRate), "缓存读 /（输入 + 缓存读）"], ["用量最高模型", models.length && models[0].total > 0 ? modelName(models[0]) : "—", models[0]?.total > 0 ? `${number(models[0].total)} Token` : "暂无模型用量"]]);
    const parts = [["in", "输入", "未缓存输入"], ["cr", "缓存读取", "复用的输入"], ["cw", "缓存写入", "不计入缓存率分母"], ["out", "输出", `含推理 ${number(totals.reason)}`]];
    $("usage-components").replaceChildren(...parts.map(([key, title, note]) => {
      const node = el("div"); node.append(el("span", title), el("strong", number(totals[key])), el("small", note)); return node;
    }));
    renderUsageCalendar(); renderUsageTrend(data, series); renderUsageModels(data, models, series);
  }
  async function loadUsage(force = false) {
    if (!usageActive()) return;
    clearTimeout(usage.scanTimer);
    const revision = ++usage.revision;
    usage.controller?.abort();
    const controller = new AbortController(); usage.controller = controller;
    const timeout = setTimeout(() => controller.abort(), 12000);
    const params = new URLSearchParams({ range: usage.range });
    if (usage.range === "custom") { params.set("start", usage.start); params.set("end", usage.end); }
    if (force) params.set("refresh", "1");
    usage.loading = true; usage.error = ""; usageStatus();
    if (!usage.data) {
      cards("usage-cards", ["总 Token", "轮次", "请求", "活跃天", "缓存读取率", "用量最高模型"].map((name) => [name, "—", "等待采集数据"]));
      ["usage-heatmap", "usage-trend", "usage-donut"].forEach((id) => empty(id, "尚未读取数据；读取失败时可点击「刷新并同步」重试"));
    }
    try {
      const data = await request(`${API}/usage?${params}`, { signal: controller.signal });
      if (revision !== usage.revision || !usageActive()) return;
      if (data.schema_version !== 1 || data.source !== "pi" || !data.totals || ![data.byDay, data.byModel, data.byDayModel, data.calendar].every(Array.isArray)) throw new Error("pi 用量数据格式不兼容，请检查接口版本");
      usage.meta = data;
      usage.stale = usageScanFailed(data);
      // A failed scan must never wipe the last good chart, even on a different filter.
      if (!usage.stale || !usage.data) { usage.data = data; renderUsage(data); }
    } catch (error) {
      if (revision !== usage.revision || !usageActive()) return;
      if (error.status === 401) { signedOut("登录已失效，请重新登录"); return; }
      usage.error = error.name === "AbortError" ? "pi 用量读取超时，请重试" : error.message;
    } finally {
      clearTimeout(timeout);
      if (revision === usage.revision) {
        usage.loading = false; usage.controller = null; usageStatus();
        // Follow a background scan even when regular auto-refresh is off.
        if (usageActive() && (usage.meta?.scan?.scanning || usage.meta?.quality?.state === "scanning")) usage.scanTimer = setTimeout(() => { if (!document.hidden && !usage.loading) loadUsage(); }, 2500);
      }
    }
  }
  function resetUsage() {
    usage.revision += 1; usage.controller?.abort(); clearTimeout(usage.scanTimer);
    Object.assign(usage, { data: null, meta: null, controller: null, scanTimer: null, loading: false, error: "", stale: false, range: "30d", start: "", end: "", heatSpan: 180, heatRendered: 0 });
    ["usage-cards", "usage-components", "usage-shown-range", "usage-quality-summary", "usage-warnings", "usage-status", "usage-scan-meta", "usage-heatmap", "usage-calendar-table", "usage-calendar-summary", "usage-heat-tooltip", "usage-trend", "usage-trend-legend", "usage-daily-table", "usage-donut", "usage-model-ranking", "usage-model-table"].forEach((id) => $(id).replaceChildren());
    $("usage-start").value = ""; $("usage-end").value = "";
    $("usage-custom").hidden = true; $("usage-date-error").hidden = true; text("usage-date-error", "");
    $("usage-quality").hidden = true; $("usage-refresh").disabled = false; text("usage-refresh", "刷新并同步");
    text("usage-sync-state", "尚未读取 pi 用量"); $("usage-sync-state").dataset.state = "unknown";
    document.querySelectorAll("[data-usage-range]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.usageRange === "30d")));
    document.querySelectorAll("[data-heat-span]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.heatSpan === "180")));
  }

  async function request(path, options = {}) {
    const response = await fetch(path, { credentials: "same-origin", cache: "no-store", ...options });
    if (!response.ok) {
      const error = new Error({ 401: "身份验证失效，请重新登录", 403: "来源或安全配置不允许此操作，请检查访问地址与 HTTPS 配置", 413: "请求过大", 422: "输入格式无效，请检查筛选或密码长度", 429: "登录尝试过于频繁，请至少等待 60 秒", 503: "服务未启用、密码未配置或采集暂不可用，请检查配置后重试" }[response.status] || "读取失败，请稍后重试");
      error.status = response.status;
      throw error;
    }
    return response.json();
  }

  function signedIn() {
    state.authenticated = true;
    $("login-view").hidden = true;
    $("dashboard").hidden = false;
    text("login-error", "");
  }

  function signedOut(message = "") {
    state.revision += 1;
    state.controller?.abort();
    state.authenticated = false;
    resetUsage();
    activateTab($("tab-overview"));
    state.data = null;
    state.failed = false;
    state.page = 0;
    $("dashboard").hidden = true;
    $("login-view").hidden = false;
    $("password").value = "";
    text("login-error", message);
    text("freshness-state", "尚未读取数据");
    $("freshness-state").dataset.state = "unknown";
    text("freshness-time", "登录后查看采集时间");
    // Discard data-bearing DOM too; a logout does not leave the previous tables
    // visible through a future login if the next read fails.
    document.querySelectorAll(".metric-grid, .trend, .table-scroll, #quality-warnings, #runtime-meta, #model-options").forEach((node) => node.replaceChildren());
    ["inbound", "queue", "execution", "delivery"].forEach((stage) => { text(`track-${stage}`, "—"); text(`track-${stage}-note`, "未观测"); });
    $("model").value = "";
    $("channel").value = "all";
    $("empty-notice").hidden = true;
    text("request-status", "");
    text("runtime-notice", "尚未收到主服务快照");
    text("runs-page", "尚未读取轮次");
    $("runs-prev").disabled = true;
    $("runs-next").disabled = true;
    $("refresh").disabled = false;
    $("filters").setAttribute("aria-busy", "false");
    $("password").focus();
  }

  async function refresh(resetPage = false) {
    if (!state.authenticated) return;
    const revision = ++state.revision;
    state.controller?.abort();
    const controller = new AbortController();
    state.controller = controller;
    const timeout = setTimeout(() => controller.abort(), 12000);
    $("refresh").disabled = true;
    $("filters").setAttribute("aria-busy", "true");
    $("request-status").dataset.error = "false";
    text("request-status", "正在读取观测数据…");
    const params = new URLSearchParams({ window: $("window").value, channel: $("channel").value, model: $("model").value.trim() || "all" });
    try {
      const data = await request(`${API}/summary?${params}`, { signal: controller.signal });
      if (revision !== state.revision) return;
      if (data.schema_version !== 1 || typeof data.summary !== "object" || !data.summary) throw new Error("数据格式不兼容，请检查主服务与仪表盘版本");
      state.data = data;
      state.receivedAt = performance.now();
      state.failed = false;
      if (resetPage) state.page = 0;
      render(data);
      text("request-status", `${shortDate(data.window?.start)} — ${shortDate(data.window?.end)} · 查询生成 ${date(data.generated_at)}`);
    } catch (error) {
      if (revision !== state.revision) return;
      if (error.status === 401) { signedOut("登录已失效，请重新登录"); return; }
      state.failed = true;
      $("request-status").dataset.error = "true";
      text("request-status", `${error.name === "AbortError" ? "读取超时，请重试" : error.message}${state.data ? "；当前仍显示上次读取的筛选结果" : "；尚未读取任何指标"}`);
      if (!state.data && !usageActive()) { text("freshness-state", "尚未成功读取数据"); $("freshness-state").dataset.state = "error"; }
      freshness();
    } finally {
      clearTimeout(timeout);
      if (revision === state.revision) { $("refresh").disabled = false; $("filters").setAttribute("aria-busy", "false"); }
    }
  }

  $("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    state.revision += 1; // A late initial session check must not undo this login.
    const body = JSON.stringify({ password: $("password").value });
    $("password").value = "";
    $("login-button").disabled = true;
    text("login-error", "");
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 12000);
    try {
      await request(`${API}/login`, { method: "POST", headers: { "Content-Type": "application/json" }, body, signal: controller.signal });
      signedIn();
      $("refresh").focus();
      await refresh(true);
    } catch (error) {
      text("login-error", error.status === 401 ? "密码无效，请重试" : error.name === "AbortError" ? "登录超时，请重试" : error.message);
      $("password").focus();
    } finally { clearTimeout(timeout); $("login-button").disabled = false; }
  });

  $("logout").addEventListener("click", async () => {
    $("logout").disabled = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 12000);
    try { await request(`${API}/logout`, { method: "POST", signal: controller.signal }); signedOut(); }
    catch (error) {
      if (error.status === 401) signedOut();
      else if (usageActive()) { usage.error = "退出未完成，请检查连接后重试"; usageStatus(); }
      else { text("request-status", "退出未完成，请检查连接后重试"); $("request-status").dataset.error = "true"; }
    } finally { clearTimeout(timeout); $("logout").disabled = false; }
  });
  $("filters").addEventListener("submit", (event) => { event.preventDefault(); refresh(true); });
  ["window", "channel"].forEach((id) => $(id).addEventListener("change", () => refresh(true)));
  $("model").addEventListener("change", () => refresh(true));
  $("runs-prev").addEventListener("click", () => { state.page -= 1; renderRuns(); });
  $("runs-next").addEventListener("click", () => { state.page += 1; renderRuns(); });

  function updateUsageDateLimits() {
    $("usage-start").max = $("usage-end").value && $("usage-end").value < todayCN() ? $("usage-end").value : todayCN();
    $("usage-end").min = $("usage-start").value;
    $("usage-end").max = todayCN();
  }
  function selectUsageRange(preset) {
    usage.range = preset;
    $("usage-custom").hidden = preset !== "custom";
    $("usage-date-error").hidden = true;
    document.querySelectorAll("[data-usage-range]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.usageRange === preset)));
    if (preset === "custom") {
      usage.start = usage.start || shiftDay(todayCN(), -29);
      usage.end = usage.end || todayCN();
      $("usage-start").value = usage.start; $("usage-end").value = usage.end;
      updateUsageDateLimits();
    }
    loadUsage();
  }
  document.querySelectorAll("[data-usage-range]").forEach((b) => b.addEventListener("click", () => selectUsageRange(b.dataset.usageRange)));
  $("usage-filters").addEventListener("submit", (event) => {
    event.preventDefault();
    if (usage.range !== "custom") return;
    const start = $("usage-start").value, end = $("usage-end").value;
    const error = !validDay(start) || !validDay(end) ? "请选择有效的开始和结束日期" : start > end ? "开始日期不能晚于结束日期" : end > todayCN() ? "结束日期不能晚于上海时区的今天" : "";
    text("usage-date-error", error); $("usage-date-error").hidden = !error;
    if (error) return;
    usage.start = start; usage.end = end;
    loadUsage();
  });
  ["usage-start", "usage-end"].forEach((id) => $(id).addEventListener("input", updateUsageDateLimits));
  $("usage-refresh").addEventListener("click", () => loadUsage(true));
  document.querySelectorAll("[data-heat-span]").forEach((button) => button.addEventListener("click", () => {
    usage.heatSpan = Number(button.dataset.heatSpan);
    document.querySelectorAll("[data-heat-span]").forEach((b) => b.setAttribute("aria-pressed", String(b === button)));
    renderUsageCalendar();
  }));

  let heatResizeTimer;
  window.addEventListener("resize", () => {
    clearTimeout(heatResizeTimer);
    heatResizeTimer = setTimeout(() => {
      if (usageActive()) {
        const scroll = $("usage-heat-scroll");
        scroll.scrollLeft = scroll.scrollWidth;
      }
    }, 100);
  });
  const tabs = [...document.querySelectorAll("[role=tab]")];
  function activateTab(tab, focus = false) {
    tabs.forEach((item) => {
      const selected = item === tab;
      item.setAttribute("aria-selected", String(selected));
      item.tabIndex = selected ? 0 : -1;
      $(`panel-${item.dataset.tab}`).hidden = !selected;
    });
    const active = usageActive();
    $("filters").hidden = active;
    $("request-status").hidden = active;
    document.querySelector(".transit").hidden = active;
    $("empty-notice").hidden = active || !state.data || (state.data.quality?.state !== "empty" && !!state.data.timeseries?.length);
    document.querySelector(".page-heading h1").textContent = active ? "Token 用量" : "运行全貌";
    if (active) loadUsage();
    else {
      usage.revision += 1;
      usage.controller?.abort();
      usage.controller = null;
      usage.loading = false;
      clearTimeout(usage.scanTimer);
    }
    freshness();
    if (focus) tab.focus();
  }
  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => activateTab(tab));
    tab.addEventListener("keydown", (event) => {
      const positions = { ArrowRight: (index + 1) % tabs.length, ArrowLeft: (index + tabs.length - 1) % tabs.length, Home: 0, End: tabs.length - 1 };
      if (positions[event.key] !== undefined) { event.preventDefault(); activateTab(tabs[positions[event.key]], true); }
    });
  });
  setInterval(() => {
    if (!state.authenticated || document.hidden) return;
    if ($("auto-refresh").checked) refresh();
    if (usageActive() && $("usage-auto-refresh").checked && !usage.loading) loadUsage();
  }, 15000);
  setInterval(() => { if (!document.hidden) freshness(); }, 1000);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden || !state.authenticated) return;
    if ($("auto-refresh").checked) refresh();
    if (usageActive() && ($("usage-auto-refresh").checked || usage.meta?.scan?.scanning)) loadUsage();
  });

  (async () => {
    const revision = state.revision;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 12000);
    try {
      await request(`${API}/session`, { signal: controller.signal });
      if (revision !== state.revision) return;
      signedIn();
      await refresh();
    } catch (error) {
      if (revision === state.revision) signedOut(error.status === 401 ? "" : "暂时无法确认登录状态，请检查连接后重试");
    } finally { clearTimeout(timeout); }
  })();
})();
