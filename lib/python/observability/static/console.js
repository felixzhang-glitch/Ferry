"use strict";
(() => {
  const API = "/api/observability/v1";
  const CONSOLE = `${API}/console`;
  const $ = (id) => document.getElementById(id);
  const MONITOR = ["overview", "performance", "usage", "reliability", "runtime"];
  const VIEWS = {
    overview: ["监控", "概览"], performance: ["监控", "性能"], usage: ["监控", "Token 用量"],
    reliability: ["监控", "可靠性"], runtime: ["监控", "运行状态"],
    chat: ["个人助手", "对话"],
    memory: ["控制", "记忆整理"], tasks: ["控制", "定时任务"], settings: ["控制", "系统设置"],
  };
  const STATUS = { applied: "已生效", empty: "无变更", failed: "失败", conflict: "有冲突" };
  const TRIGGERS = { scheduled: "定时", manual: "IM 手动", console: "控制台" };
  const OPS = [["add", "新增"], ["update", "更新"], ["merge", "合并"], ["archive", "归档"]];
  const app = { authenticated: false, console: false, chat: false, csrf: "", revision: 0, view: "overview", state: null, loading: false, controller: null, timer: null, toastTimer: null, busy: new Set() };

  // ---------------------------------------------------------------- dom helpers

  function el(tag, content, className) {
    const node = document.createElement(tag);
    if (content !== undefined && content !== null) node.textContent = String(content);
    if (className) node.className = className;
    return node;
  }

  function button(label, className, onClick) {
    const node = el("button", label, className);
    node.type = "button";
    if (onClick) node.addEventListener("click", onClick);
    return node;
  }

  function badge(label, tone) {
    const node = el("span", label, "badge");
    if (tone) node.dataset.tone = tone;
    return node;
  }

  function card(title, subtitle, className) {
    const node = el("section", null, `card ${className || ""}`.trim());
    if (title) {
      const head = el("div", null, "card-head");
      const titles = el("div");
      titles.append(el("h2", title));
      if (subtitle) titles.append(el("p", subtitle, "caption"));
      head.append(titles);
      node.append(head);
    }
    return node;
  }

  function pad(value) { return String(value).padStart(2, "0"); }

  const shanghai = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
  function parts(date) {
    const out = {};
    shanghai.formatToParts(date).forEach((part) => { out[part.type] = part.value; });
    return out;
  }
  function when(value) {
    if (value === null || value === undefined || value === "") return "—";
    const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
    if (Number.isNaN(date.getTime())) return "—";
    const target = parts(date), today = parts(new Date()), tomorrow = parts(new Date(Date.now() + 86400000)), yesterday = parts(new Date(Date.now() - 86400000));
    const day = `${target.year}-${target.month}-${target.day}`;
    const clock = `${target.hour}:${target.minute}`;
    if (day === `${today.year}-${today.month}-${today.day}`) return `今天 ${clock}`;
    if (day === `${tomorrow.year}-${tomorrow.month}-${tomorrow.day}`) return `明天 ${clock}`;
    if (day === `${yesterday.year}-${yesterday.month}-${yesterday.day}`) return `昨天 ${clock}`;
    return `${Number(target.month)}月${Number(target.day)}日 ${clock}`;
  }

  // ---------------------------------------------------------------- feedback

  function toast(message, tone = "ok") {
    const node = $("toast");
    node.textContent = message;
    node.dataset.tone = tone;
    node.hidden = false;
    clearTimeout(app.toastTimer);
    app.toastTimer = setTimeout(() => { node.hidden = true; }, tone === "error" ? 5200 : 2600);
  }

  function confirmSheet(title, message, confirmLabel = "确认", tone = "danger") {
    return new Promise((resolve) => {
      const backdrop = $("sheet-backdrop"), ok = $("sheet-confirm"), cancel = $("sheet-cancel");
      $("sheet-title").textContent = title;
      $("sheet-message").textContent = message;
      ok.textContent = confirmLabel;
      ok.className = tone === "danger" ? "danger" : "primary";
      const previous = document.activeElement;
      backdrop.hidden = false;
      ok.focus();
      const close = (result) => {
        backdrop.hidden = true;
        ok.removeEventListener("click", onOk); cancel.removeEventListener("click", onCancel);
        backdrop.removeEventListener("click", onBackdrop); document.removeEventListener("keydown", onKey);
        if (previous instanceof HTMLElement) previous.focus();
        resolve(result);
      };
      const onOk = () => close(true);
      const onCancel = () => close(false);
      const onBackdrop = (event) => { if (event.target === backdrop) close(false); };
      const onKey = (event) => { if (event.key === "Escape") close(false); };
      ok.addEventListener("click", onOk); cancel.addEventListener("click", onCancel);
      backdrop.addEventListener("click", onBackdrop); document.addEventListener("keydown", onKey);
    });
  }

  // ---------------------------------------------------------------- requests

  async function call(path, options = {}) {
    if (path.startsWith(`${CONSOLE}/`) && !["GET", "HEAD"].includes((options.method || "GET").toUpperCase())) {
      // Never refresh credentials and replay a mutation: its outcome may be unknown.
      if (!app.csrf) throw new Error("控制台安全令牌缺失，请刷新页面后重试");
      const headers = new Headers(options.headers);
      headers.set("X-Ferry-CSRF", app.csrf);
      options = { ...options, headers };
    }
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(path, { credentials: "same-origin", cache: "no-store", mode: "same-origin", redirect: "error", signal: controller.signal, ...options });
      let data = null;
      try { data = await response.json(); } catch { data = null; }
      if (!response.ok) {
        const error = new Error((data && (data.detail || data.message)) || { 401: "登录已失效，请重新登录", 403: "来源校验失败，请从控制台页面操作", 503: "服务暂不可用" }[response.status] || "请求失败，请稍后重试");
        error.status = response.status;
        throw error;
      }
      return data;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("请求超时，请重试");
      throw error;
    } finally { clearTimeout(timeout); }
  }

  async function loadState(silent = false) {
    if (!app.authenticated || !app.console || app.view === "chat" || app.loading) return;
    const revision = app.revision;
    app.loading = true;
    if (!silent) $("console-sync").textContent = "正在读取…";
    try {
      const data = await call(`${CONSOLE}/state`);
      if (revision !== app.revision || !app.authenticated) return;
      app.state = data;
      $("console-sync").textContent = `已同步 ${when(Date.now() / 1000)}`;
      render();
    } catch (error) {
      if (revision !== app.revision) return;
      if (error.status === 401) { window.FerryMonitor?.signOut(); return; }
      $("console-sync").textContent = error.message;
      if (!app.state) renderUnavailable(error.message);
    } finally { if (revision === app.revision) app.loading = false; }
  }

  async function mutate(key, path, method = "POST", body) {
    if (app.busy.has(key)) return null;
    app.busy.add(key);
    const revision = app.revision;
    render();
    try {
      const options = { method, headers: { "Content-Type": "application/json" } };
      if (body !== undefined) options.body = JSON.stringify(body);
      const data = await call(`${CONSOLE}${path}`, options);
      if (revision !== app.revision || !app.authenticated) return null;
      if (data && data.state) app.state = data.state;
      toast(data?.message || "已保存", "ok");
      return data;
    } catch (error) {
      if (revision !== app.revision || !app.authenticated) return null;
      if (error.status === 401) { window.FerryMonitor?.signOut(); return null; }
      toast(error.message, "error");
      return null;
    } finally {
      if (revision === app.revision) { app.busy.delete(key); render(); }
    }
  }

  // ---------------------------------------------------------------- controls

  function switchControl(checked, label, onChange, disabled) {
    const node = button("", "switch", () => onChange(!checked));
    node.setAttribute("role", "switch");
    node.setAttribute("aria-checked", String(Boolean(checked)));
    node.setAttribute("aria-label", label);
    node.disabled = Boolean(disabled);
    node.append(el("span", null, "switch-thumb"));
    return node;
  }

  function selectControl(options, value, label, onChange, disabled) {
    const node = el("select", null, "select");
    node.setAttribute("aria-label", label);
    options.forEach((option) => {
      const item = el("option", option.label);
      item.value = option.value;
      if (option.value === value) item.selected = true;
      node.append(item);
    });
    node.disabled = Boolean(disabled);
    node.addEventListener("change", () => onChange(node.value));
    return node;
  }

  function chips(options, values, label, onChange, disabled) {
    const group = el("div", null, "chips");
    group.setAttribute("role", "group");
    group.setAttribute("aria-label", label);
    if (!options.length) group.append(el("span", "暂无可选项", "caption"));
    options.forEach((option) => {
      const selected = values.includes(option.value);
      const chip = button(option.label, "chip", () => onChange(selected ? values.filter((v) => v !== option.value) : [...values, option.value]));
      chip.setAttribute("aria-pressed", String(selected));
      chip.disabled = Boolean(disabled);
      group.append(chip);
    });
    return group;
  }

  function hourOptions(allowImmediate) {
    const options = allowImmediate ? [{ value: "immediate", label: "整理完立即" }] : [];
    for (let hour = 0; hour < 24; hour += 1) options.push({ value: pad(hour), label: `${pad(hour)} 时` });
    return options;
  }

  function minuteOptions(step, current) {
    const options = [];
    for (let minute = 0; minute < 60; minute += step) options.push({ value: pad(minute), label: `${pad(minute)} 分` });
    if (current && !options.some((option) => option.value === current)) options.push({ value: current, label: `${current} 分` });
    return options.sort((a, b) => a.value.localeCompare(b.value));
  }

  function timeControl(value, step, allowImmediate, label, onChange, disabled) {
    const wrap = el("div", null, "time-picker");
    const immediate = value === "immediate";
    const [hour, minute] = immediate ? ["immediate", "00"] : String(value || "00:00").split(":");
    const hourSelect = selectControl(hourOptions(allowImmediate), immediate ? "immediate" : hour, `${label} 小时`, (next) => {
      onChange(next === "immediate" ? "immediate" : `${next}:${immediate ? "00" : minute}`);
    }, disabled);
    const minuteSelect = selectControl(minuteOptions(step || 5, immediate ? "" : minute), minute, `${label} 分钟`, (next) => onChange(`${hour}:${next}`), disabled || immediate);
    wrap.append(hourSelect, el("span", ":", "time-sep"), minuteSelect);
    return wrap;
  }

  function settingRow(setting) {
    const key = `setting:${setting.key}`;
    const busy = app.busy.has(key);
    const row = el("div", null, "setting-row");
    row.dataset.kind = setting.kind;
    if (busy) row.setAttribute("aria-busy", "true");
    const info = el("div", null, "setting-info");
    const title = el("div", null, "setting-title");
    title.append(el("span", setting.label));
    if (setting.overridden) title.append(badge("控制台已调整", "accent"));
    info.append(title);
    if (setting.hint) info.append(el("p", setting.hint, "caption"));
    const control = el("div", null, "setting-control");
    const save = (value) => mutate(key, "/settings", "POST", { key: setting.key, value });
    if (setting.kind === "bool") control.append(switchControl(setting.value, setting.label, save, busy));
    else if (setting.kind === "choice") control.append(selectControl(setting.choices || [], String(setting.value), setting.label, save, busy));
    else if (setting.kind === "multi") control.append(chips(setting.choices || [], setting.value || [], setting.label, save, busy));
    else if (setting.kind === "time") control.append(timeControl(String(setting.value), setting.step, setting.allow_immediate, setting.label, save, busy));
    if (setting.overridden) {
      const reset = button("恢复默认", "link-button", () => mutate(key, "/settings/reset", "POST", { key: setting.key }));
      reset.disabled = busy;
      control.append(reset);
    }
    row.append(info, control);
    return row;
  }

  // ---------------------------------------------------------------- views

  function setting(key) { return app.state?.settings?.find((item) => item.key === key); }

  function renderUnavailable(message) {
    ["memory-root", "tasks-root", "settings-root"].forEach((id) => {
      const box = card("控制台暂不可用", message || "主服务未连接或未配置控制台令牌");
      box.append(el("p", "监控页面不受影响。确认主服务已启动，且 conf/.env.observability 中配置了 OBSERVABILITY_CONSOLE_TOKEN 后刷新", "caption"));
      $(id).replaceChildren(box);
    });
  }

  function countsText(counts) {
    const items = OPS.filter(([op]) => counts?.[op]).map(([op, label]) => `${label} ${counts[op]}`);
    return items.length ? items.join(" · ") : "无变更";
  }

  function renderMemory() {
    const state = app.state, dream = state.dream, root = $("memory-root");
    if (!dream) { root.replaceChildren(card("Dream 未启用", "主服务没有装配记忆整理组件")); return; }
    const hero = card(null, null, "hero");
    const head = el("div", null, "hero-head");
    const titles = el("div");
    titles.append(el("p", "DREAM · 离线记忆整理", "eyebrow"), el("h2", dream.running ? "正在整理记忆…" : dream.enabled ? "每天自动整理长期记忆" : "Dream 已停用"));
    head.append(titles, badge(dream.running ? "运行中" : dream.enabled ? "已启用" : "已停用", dream.running ? "accent" : dream.enabled ? "ok" : "muted"));
    const facts = el("dl", null, "facts");
    const fact = (label, value) => { const item = el("div"); item.append(el("dt", label), el("dd", value)); facts.append(item); };
    fact("下次整理", dream.enabled ? when(dream.next_run) : "—");
    fact("报告推送", dream.report_time === "immediate" ? "整理完立即" : `每天 ${dream.report_time}${dream.pending_report ? " · 有待发报告" : ""}`);
    fact("推送目标", dream.targets.length ? dream.targets.map((t) => t.label).join("、") : "未设置");
    const last = dream.last_run;
    fact("最近一次", last ? `${when(last.at)} · ${TRIGGERS[last.trigger] || last.trigger} · ${STATUS[last.status] || last.status}${last.undone ? "（已撤销）" : ""}` : "尚未运行");
    if (last) fact("最近结果", countsText(last.counts));
    const actions = el("div", null, "actions");
    const run = button(app.busy.has("dream:run") ? "正在提交…" : "立即整理", "primary", () => mutate("dream:run", "/dream/run"));
    run.disabled = !dream.enabled || dream.running || app.busy.has("dream:run");
    const undo = button("撤销最近一次", "danger-outline", async () => {
      if (await confirmSheet("撤销最近一次整理？", "会用 git 回滚最近一次 Dream 的全部改动；之后若记忆又被修改导致冲突，会自动放弃并提示手工处理。", "撤销")) mutate("dream:undo", "/dream/undo");
    });
    undo.disabled = !dream.undo_available || dream.running || app.busy.has("dream:undo");
    const tune = button("调整整理设置", "quiet", () => navigate("settings", "group-dream"));
    actions.append(run, undo, tune);
    hero.append(head, facts, actions);

    const categories = card("记忆类别", "条目数来自 NOTE-FORMAT 投影；不展示记忆正文");
    const grid = el("div", null, "category-grid");
    (state.memory || []).forEach((item) => {
      const tile = el("article", null, "category");
      const top = el("div", null, "category-top");
      top.append(el("span", item.label, "category-name"));
      if (item.resident) top.append(badge("常驻", "accent"));
      tile.append(top, el("span", item.active, "category-count"), el("span", `${item.archived ? `已归档 ${item.archived} · ` : ""}${item.last_date ? `最近 ${item.last_date}` : "暂无日期"}`, "caption"));
      grid.append(tile);
    });
    categories.append(grid);
    root.replaceChildren(hero, categories);
  }

  function renderTasks() {
    const state = app.state, root = $("tasks-root");
    const daily = card("每日任务", "可启停、改时间与推送目标；新建或修改提示词请在 IM 里发 /daily");
    if (!state.daily.length) daily.append(el("p", "还没有每日任务。在飞书或微信发「/daily 08:00 提示词」创建，之后可在这里管理", "empty"));
    const list = el("div", null, "task-list");
    state.daily.forEach((task) => {
      const key = `daily:${task.id}`, busy = app.busy.has(key);
      const item = el("article", null, "task");
      if (!task.enabled) item.dataset.disabled = "true";
      const head = el("div", null, "task-head");
      const time = el("div", null, "task-time");
      time.append(el("strong", `${pad(task.hour)}:${pad(task.minute)}`), el("span", task.enabled ? "每天" : "已停用", "caption"));
      head.append(time, switchControl(task.enabled, "启用任务", (enabled) => mutate(key, `/daily/${task.id}`, "POST", { enabled }), busy));
      const summary = el("p", task.summary, "task-summary");
      const meta = el("p", `#${task.short_id} · ${task.last_run_date ? `上次执行 ${task.last_run_date}` : "尚未执行"}`, "caption");
      const controls = el("div", null, "task-controls");
      const timeField = el("label", null, "field");
      timeField.append(el("span", "时间", "field-label"), timeControl(`${pad(task.hour)}:${pad(task.minute)}`, 5, false, "执行时间", (value) => {
        const [hour, minute] = value.split(":").map(Number);
        const body = {};
        if (hour !== task.hour) body.hour = hour;
        if (minute !== task.minute) body.minute = minute;
        mutate(key, `/daily/${task.id}`, "POST", body);
      }, busy));
      const targetField = el("label", null, "field");
      targetField.append(el("span", "推送到", "field-label"), selectControl(state.targets, task.target, "推送目标", (target) => mutate(key, `/daily/${task.id}`, "POST", { target }), busy));
      controls.append(timeField, targetField);
      const remove = button("删除", "danger-quiet", async () => {
        if (await confirmSheet("删除这个每日任务？", `#${task.short_id}「${task.summary}」删除后不可恢复，需要时请在 IM 里重新创建。`, "删除")) mutate(key, `/daily/${task.id}`, "DELETE");
      });
      remove.disabled = busy;
      item.append(head, summary, meta, controls, remove);
      list.append(item);
    });
    daily.append(list);

    const reminders = card("提醒", "飞书 /remind 创建的一次性提醒；可在这里取消");
    if (!state.reminders.length) reminders.append(el("p", "暂无待触发的提醒", "empty"));
    state.reminders.forEach((item) => {
      const key = `reminder:${item.id}`;
      const row = el("div", null, "reminder");
      const info = el("div");
      info.append(el("strong", when(item.due_at)), el("p", item.summary, "caption"));
      const cancel = button("取消", "danger-quiet", async () => {
        if (await confirmSheet("取消这个提醒？", `「${item.summary}」将不再发送。`, "取消提醒")) mutate(key, `/reminders/${item.id}`, "DELETE");
      });
      cancel.disabled = app.busy.has(key);
      row.append(info, cancel);
      reminders.append(row);
    });
    root.replaceChildren(daily, reminders);
  }

  function renderSettings() {
    const state = app.state, root = $("settings-root");
    const nodes = state.groups.map((group) => {
      const items = state.settings.filter((item) => item.group === group.id);
      const box = card(group.label, group.id === "dream" ? "报告推送时间与目标立即生效；整理时间修改后自动重新排期" : "修改后自动保存，重启后仍保留");
      box.id = `group-${group.id}`;
      items.forEach((item) => box.append(settingRow(item)));
      if (group.id === "dream" && state.dream) {
        const row = el("div", null, "setting-row");
        row.dataset.kind = "action";
        const info = el("div", null, "setting-info");
        info.append(el("div", "立即整理一次", "setting-title"), el("p", "后台运行，完成后推送到报告目标", "caption"));
        const run = button("立即整理", "primary", () => mutate("dream:run", "/dream/run"));
        run.disabled = !state.dream.enabled || state.dream.running || app.busy.has("dream:run");
        const control = el("div", null, "setting-control");
        control.append(run);
        row.append(info, control);
        box.append(row);
      }
      return box;
    });
    root.replaceChildren(...nodes);
  }

  function render() {
    if (!app.state) return;
    renderMemory();
    renderTasks();
    renderSettings();
  }

  // ---------------------------------------------------------------- navigation

  function routeHash(route, replace = false) {
    if (location.hash !== `#${route}`) history[replace ? "replaceState" : "pushState"](null, "", `#${route}`);
  }

  function navigate(route, anchor, historyMode = "push") {
    if (!app.authenticated) return;
    let [name, threadId] = String(route || "").split("/");
    if (!VIEWS[name] || (name === "chat" && !app.chat)) { name = "overview"; threadId = ""; }
    const previous = app.view;
    app.view = name;
    $("dashboard").dataset.view = name;
    const monitor = MONITOR.includes(name);
    $("view-refresh").hidden = name === "chat";
    $("view-monitor").hidden = !monitor;
    ["chat", "memory", "tasks", "settings"].forEach((view) => { $(`view-${view}`).hidden = view !== name; });
    document.querySelectorAll("[data-nav]").forEach((item) => {
      if (item.dataset.nav === name) item.setAttribute("aria-current", "page");
      else item.removeAttribute("aria-current");
    });
    if (monitor && name !== "usage" && name !== "overview") document.querySelector(".tabbar [data-nav=overview]")?.setAttribute("aria-current", "page");
    const [eyebrow, title] = VIEWS[name];
    $("view-eyebrow").textContent = eyebrow; $("view-title").textContent = title;
    window.FerryAppearance?.setView(title);
    // Thread hashes are written by FerryChat only after matching a server summary.
    if (name !== "chat" && historyMode !== "none") routeHash(name, historyMode === "replace");
    if (previous === "chat" && name !== "chat") window.FerryChat?.leave();
    if (monitor) window.FerryMonitor?.showTab(name);
    else {
      window.FerryMonitor?.pause();
      if (name === "chat") { $("console-sync").textContent = ""; window.FerryChat?.open(threadId, historyMode); }
      else loadState(Boolean(app.state));
    }
    schedulePoll();
    if (anchor) $(anchor)?.scrollIntoView({ behavior: "smooth", block: "start" });
    else if (name !== "chat") window.scrollTo({ top: 0 });
  }

  function schedulePoll() {
    clearInterval(app.timer);
    if (!app.authenticated || !app.console || MONITOR.includes(app.view) || app.view === "chat") return;
    app.timer = setInterval(() => { if (!document.hidden && !app.busy.size) loadState(true); }, 20000);
  }

  window.FerryConsole = { navigate, chatRoute(id, replace) { routeHash(id ? `chat/${encodeURIComponent(id)}` : "chat", replace); } };
  document.querySelectorAll("[data-nav]").forEach((item) => item.addEventListener("click", () => navigate(item.dataset.nav)));
  $("view-refresh").addEventListener("click", () => {
    if (app.view === "chat") return;
    if (MONITOR.includes(app.view)) window.FerryMonitor?.refresh();
    else loadState();
  });
  $("logout-mobile").addEventListener("click", () => window.FerryMonitor?.signOut());
  document.addEventListener("ferry:tab", (event) => {
    const tab = event.detail?.tab;
    if (!MONITOR.includes(app.view) || !MONITOR.includes(tab) || tab === app.view) return;
    // The monitor already activated this tab; do not start a duplicate usage read.
    app.view = tab; $("dashboard").dataset.view = tab;
    document.querySelectorAll("[data-nav]").forEach((item) => {
      if (item.dataset.nav === tab) item.setAttribute("aria-current", "page"); else item.removeAttribute("aria-current");
    });
    if (tab !== "overview" && tab !== "usage") document.querySelector(".tabbar [data-nav=overview]")?.setAttribute("aria-current", "page");
    const [eyebrow, title] = VIEWS[tab];
    $("view-eyebrow").textContent = eyebrow; $("view-title").textContent = title;
    document.title = `${title} · ${window.FerryAppearance?.name() || "Ferry"} 控制台`; routeHash(tab); schedulePoll();
  });
  const fromHistory = () => navigate(location.hash.slice(1) || (app.chat ? "chat" : "overview"), null, "none");
  window.addEventListener("popstate", fromHistory);
  window.addEventListener("hashchange", fromHistory);
  document.addEventListener("visibilitychange", () => { if (!document.hidden && app.authenticated && app.view !== "chat" && !MONITOR.includes(app.view)) loadState(true); });

  document.addEventListener("ferry:auth", async (event) => {
    const revision = ++app.revision;
    app.authenticated = Boolean(event.detail?.authenticated);
    app.csrf = "";
    if (!app.authenticated) {
      clearInterval(app.timer); clearTimeout(app.toastTimer);
      app.state = null; app.console = false; app.chat = false; app.loading = false; app.busy.clear();
      ["memory-root", "tasks-root", "settings-root", "account-name", "account-name-mobile", "console-sync", "toast", "sheet-title", "sheet-message"].forEach((id) => $(id).replaceChildren());
      $("toast").hidden = true; $("sheet-backdrop").hidden = true;
      document.querySelectorAll('[data-nav="chat"]').forEach((item) => { item.hidden = true; });
      return;
    }
    try {
      // Yield until all auth listeners (including FerryChat) have consumed capabilities.
      const session = await Promise.resolve(event.detail?.session || call(`${API}/session`));
      if (revision !== app.revision || !app.authenticated) return;
      app.console = Boolean(session.console); app.chat = Boolean(session.chat);
      app.csrf = app.console && typeof session.console_csrf_token === "string" && /^[a-f0-9]{64}$/.test(session.console_csrf_token) ? session.console_csrf_token : "";
      document.querySelectorAll('[data-nav="chat"]').forEach((item) => { item.hidden = !app.chat; });
      $("account-name").textContent = session.username || "—";
      $("account-name-mobile").textContent = session.username ? `已登录：${session.username}` : "";
      document.querySelector(".avatar").textContent = (session.username || "F").slice(0, 1).toUpperCase();
    } catch { if (revision !== app.revision) return; app.console = false; app.chat = false; app.csrf = ""; }
    if (!app.console) renderUnavailable("控制台未配置");
    navigate(location.hash.slice(1) || (app.chat ? "chat" : "overview"), null, "replace");
  });

  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("/observability/sw.js", { scope: "/observability/" }).catch(() => {});
    });
  }
})();
