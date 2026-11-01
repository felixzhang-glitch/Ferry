"use strict";
(() => {
  const API = "/api/observability/v1/chat";
  const $ = (id) => document.getElementById(id);
  const MAX_FILE = 10 * 1024 * 1024, MAX_TOTAL = 25 * 1024 * 1024, MAX_FILES = 5;
  const IMAGE_MIMES = new Set(["image/png", "image/jpeg", "image/gif", "image/webp"]);
  const FILE_MIMES = { png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", gif: "image/gif", webp: "image/webp", pdf: "application/pdf", txt: "text/plain", md: "text/markdown", json: "application/json", csv: "text/csv", docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document", xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", pptx: "application/vnd.openxmlformats-officedocument.presentationml.presentation" };
  const CORE = new Set(["/help", "/new", "/reset", "/stop", "/backend", "/skills", "/memory", "/dream", "/remind", "/daily"]);
  const NEW_BUTTONS = ["chat-new", "chat-new-mobile", "chat-new-header"];
  // All private state is memory-only. A generation owns every read, upload and stream.
  const app = { authenticated: false, userDisplayName: "我", visible: false, generation: 0, controllers: new Set(),
    threads: [], initialized: false, thread: null, messages: [], hasMore: false, loading: false,
    drafts: new Map(), files: [], commands: [], suggestions: [], selected: 0, composing: false,
    stream: null, failed: null, stick: true, images: new Map(), urls: new Set(), dialog: null, busy: false,
    csrf: "", pendingSubmit: false, creationPending: false, recovering: null, uncertain: false, reconnectTimer: 0, reconnectDelay: 1000 };
  let commandGesture = null, commandTap = null, commandInteraction = false, drawerGesture = null;
  const layout = { frame: 0, releasing: 0, settling: 0, anchor: null, bottom: true, updating: false, baseline: 0, orientation: "", signature: "" };
  const mobileInput = () => matchMedia("(max-width: 900px)").matches && (matchMedia("(pointer: coarse)").matches || navigator.standalone);

  function el(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = String(text);
    if (className) node.className = className;
    return node;
  }
  function button(text, className, action) {
    const node = el("button", text, className); node.type = "button";
    node.addEventListener("click", action); return node;
  }
  function iconButton(label, symbol, className, action) {
    const node = button(null, `chat-icon-button ${className}`, action);
    node.setAttribute("aria-label", label); node.title = label;
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "icon"); svg.setAttribute("aria-hidden", "true"); svg.setAttribute("focusable", "false");
    const use = document.createElementNS(svg.namespaceURI, "use"); use.setAttribute("href", `#i-${symbol}`);
    svg.append(use); node.append(svg); return node;
  }
  const current = (generation) => app.authenticated && app.visible && generation === app.generation;
  const notice = (message = "") => {
    $("chat-notice").textContent = message; $("chat-drawer-notice").textContent = message;
  };
  const summary = (id) => app.threads.find((thread) => thread.id === id);
  function threadPath(thread = app.thread) {
    // Only IDs found in server responses ever become routes; hash text is never a URL.
    if (!thread || !summary(thread.id)) throw new Error("请先选择对话");
    return `${API}/threads/${encodeURIComponent(thread.id)}`;
  }
  function attachmentPath(meta, thread = app.thread) {
    if (!meta || typeof meta.id !== "string" || !meta.id) throw new Error("附件数据无效");
    return `${threadPath(thread)}/attachments/${encodeURIComponent(meta.id)}`;
  }
  function rememberURL(blob) { const url = URL.createObjectURL(blob); app.urls.add(url); return url; }
  function revoke(url) { if (url) { URL.revokeObjectURL(url); app.urls.delete(url); } }
  function clearImages() { app.images.forEach((image) => revoke(image.url)); app.images.clear(); }
  function releaseDraft(draft) { draft?.files.forEach((item) => revoke(item.preview)); }

  async function request(path, options = {}, generation = app.generation, controller = new AbortController(), stream = false) {
    if (!current(generation)) throw new DOMException("Inactive request", "AbortError");
    if (!app.csrf) throw new Error("聊天页面需要更新，请刷新后重试；草稿会保留在当前页");
    const headers = new Headers(options.headers);
    headers.set("X-Ferry-CSRF", app.csrf);
    app.controllers.add(controller);
    const timeout = stream ? null : setTimeout(() => controller.abort(), 30000);
    try {
      const response = await fetch(path, { ...options, headers, mode: "same-origin", credentials: "same-origin", cache: "no-store", redirect: "error", signal: controller.signal });
      if (!current(generation)) throw new DOMException("Stale request", "AbortError");
      if (!response.ok) {
        let data; try { data = await response.json(); } catch { data = null; }
        if (!current(generation)) throw new DOMException("Stale response", "AbortError");
        const error = new Error(typeof data?.detail === "string" ? data.detail : typeof data?.message === "string" ? data.message : ({ 401: "登录已失效", 403: "来源或安全校验失败，请刷新页面后重试", 409: "对话正在处理，请稍后重试", 413: "输入或附件过大", 429: "已达到请求或对话数量上限，请稍后重试或整理对话列表", 503: "聊天服务暂不可用" }[response.status] || "请求失败，请重试"));
        error.status = response.status;
        if (response.status === 401) window.FerryMonitor?.signOut();
        throw error;
      }
      if (stream) return response;
      const data = options.binary ? await response.blob() : await response.json();
      if (!current(generation)) throw new DOMException("Stale response", "AbortError");
      return data;
    } finally {
      clearTimeout(timeout);
      if (!stream) app.controllers.delete(controller);
    }
  }
  function json(method, body) {
    return { method, headers: { "Content-Type": "application/json" }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) };
  }
  function fail(error, generation) {
    if (current(generation) && error.name !== "AbortError" && error.status !== 401) notice(error.message);
  }

  const MAX_TEXT = 32000;
  const editorProbe = document.createElement("div"); editorProbe.setAttribute("contenteditable", "plaintext-only");
  const editorMode = editorProbe.contentEditable === "plaintext-only" ? "plaintext-only" : "true";
  let inputRevision = 0;
  const normalizeText = (text) => String(text).replace(/\r\n?/g, "\n");
  const textLength = (text) => Array.from(text).length;
  // Read native line nodes without disturbing the selection, undo history or IME.
  function editorText(root = $("chat-input")) {
    let text = "", previousBlock = false;
    [...root.childNodes].forEach((node, index) => {
      const block = node.nodeName === "DIV" || node.nodeName === "P";
      if (index && (previousBlock || (block && !text.endsWith("\n")))) text += "\n";
      if (node.nodeType === Node.TEXT_NODE) text += node.textContent;
      else if (node.nodeName === "BR") { if (node.nextSibling) text += "\n"; }
      else text += editorText(node);
      previousBlock = block;
    });
    return normalizeText(text);
  }
  function writeEditor(text) {
    $("chat-input").textContent = normalizeText(text); inputRevision += 1;
    $("chat-input").dataset.empty = String(!text);
  }
  function editorEnd() {
    const input = $("chat-input"), range = document.createRange(), selection = window.getSelection();
    input.focus({ preventScroll: true }); range.selectNodeContents(input); range.collapse(false);
    selection.removeAllRanges(); selection.addRange(range);
  }
  function insertionFits(text) {
    const input = $("chat-input"), selection = window.getSelection();
    const selected = selection?.rangeCount && input.contains(selection.anchorNode) && input.contains(selection.focusNode) ? selection.toString() : "";
    return textLength(editorText()) - textLength(normalizeText(selected)) + textLength(text) <= MAX_TEXT;
  }
  function insertPlainText(text) {
    const input = $("chat-input"); text = normalizeText(text);
    if (!input.isContentEditable || app.composing || !text) return;
    if (!insertionFits(text)) { notice("消息最多 32000 字符，请缩短后再输入"); return; }
    // The native edit transaction preserves undo across plain-text paste.
    input.focus({ preventScroll: true });
    if (!document.execCommand("insertText", false, text)) notice("浏览器未能插入文字，请重新输入");
  }
  function editorChanged() {
    inputRevision += 1;
    autosize(); saveDraft(); updateCommands(); updateComposer();
  }
  function saveDraft() {
    if (app.thread) app.drafts.set(app.thread.id, { text: editorText(), files: app.files });
  }
  function leave() {
    saveDraft(); app.visible = false; app.generation += 1;
    clearTimeout(layout.settling); layout.settling = 0;
    app.pendingSubmit = false; app.composing = false;
    app.controllers.forEach((controller) => controller.abort()); app.controllers.clear();
    app.files.forEach((item) => { if (item.state === "uploading" || item.state === "removing") item.state = "error"; });
    app.stream = null; app.busy = false; app.loading = false; app.recovering = null; app.uncertain = false;
    clearTimeout(app.reconnectTimer); app.reconnectTimer = 0; app.reconnectDelay = 1000;
    drawerGesture = null;
    clearImages(); closeCommands();
    $("chat-drawer").close(); $("chat-dialog").close(); app.dialog = null;
    $("chat-messages").replaceChildren(); $("chat-attachments").replaceChildren();
    writeEditor(""); $("chat-progress").textContent = "";
    app.messages = []; app.files = []; app.thread = null; app.failed = null; app.hasMore = false;
    viewport();
  }

  function upsertThread(thread) {
    if (!thread || typeof thread.id !== "string" || !thread.id) throw new Error("对话数据无效");
    const index = app.threads.findIndex((item) => item.id === thread.id);
    if (index < 0) app.threads.unshift(thread); else app.threads[index] = thread;
    if (app.thread?.id === thread.id) app.thread = thread;
  }
  function renderThreads() {
    ["chat-thread-list", "chat-drawer-list"].forEach((id) => {
      const nodes = app.threads.map((thread) => {
        const row = el("div", null, "chat-thread");
        const select = button("", "chat-thread-select", () => { $("chat-drawer").close(); window.FerryConsole?.navigate(`chat/${encodeURIComponent(thread.id)}`); });
        select.append(el("span", thread.title || "新对话", "chat-thread-title"), el("small", `${thread.message_count || 0} 条消息 · ${timeLabel(thread.updated_at).day}`, "caption"));
        if (thread.id === app.thread?.id) select.setAttribute("aria-current", "page");
        const rename = iconButton(`重命名 ${thread.title || "新对话"}`, "pencil", "chat-thread-rename", () => showDialog("rename", thread));
        const remove = button("删除", "danger-quiet", () => showDialog("delete", thread)); remove.setAttribute("aria-label", `删除 ${thread.title || "新对话"}`);
        row.append(select, rename, remove); return row;
      });
      $(id).replaceChildren(...(nodes.length ? nodes : [el("p", "还没有对话，点击新对话开始", "empty")]));
    });
    $("chat-title").textContent = app.thread?.title || "个人助手";
  }

  async function loadThreads(generation) {
    const data = await request(`${API}/threads`, {}, generation);
    app.threads = Array.isArray(data.threads) ? data.threads.filter((t) => typeof t.id === "string" && t.id) : [];
    if (app.thread && summary(app.thread.id)) app.thread = summary(app.thread.id);
    app.initialized = true; renderThreads();
  }
  async function loadCommands(generation) {
    try {
      const data = await request(`${API}/commands`, {}, generation);
      app.commands = (data.commands || []).filter((item) => CORE.has(item.command));
      updateCommands();
    } catch (error) { fail(error, generation); }
  }
  async function open(rawId, historyMode = "push", force = false) {
    let id; try { id = rawId ? decodeURIComponent(rawId) : app.thread?.id; } catch { id = ""; }
    if (!app.authenticated) return;
    if (app.visible && app.thread?.id === id && !force) return;
    const oldId = app.thread?.id;
    leave(); app.visible = true; const generation = app.generation;
    layout.anchor = null; layout.bottom = true; layout.signature = "";
    $("chat-input-help").textContent = mobileInput() ? "回车换行 · 点发送按钮发送" : "Enter 发送 · Shift+Enter 换行";
    let opened = false;
    app.loading = true; app.stick = true; notice("正在读取对话…"); renderMessages(); updateComposer(); viewport();
    settleStandaloneViewport();
    try {
      if (!app.initialized || force) await loadThreads(generation);
      if (!current(generation)) return;
      if (!app.commands.length) loadCommands(generation);
      app.thread = summary(id) || summary(oldId) || app.threads[0] || null;
      if (id && !summary(id)) notice("该对话不存在，已返回对话列表"); else notice();
      const replace = historyMode !== "push" || !id || !summary(id);
      window.FerryConsole?.chatRoute(app.thread?.id, replace);
      renderThreads();
      if (app.thread) {
        const draft = app.drafts.get(app.thread.id);
        writeEditor(draft?.text || ""); app.files = draft?.files || [];
        renderAttachments(); autosize();
        await loadHistory(generation);
        if (app.thread.history_truncated && !$("chat-notice").textContent) notice("较早历史已截断，当前展示保留的消息");
        await recover(generation, false);
      }
      opened = true;
    } catch (error) { fail(error, generation); }
    finally { if (current(generation)) { app.loading = false; $("chat-empty").hidden = Boolean(app.messages.length); updateComposer(); } }
    return current(generation) && opened;
  }
  function newThreadBlockReason() {
    if (app.loading) return "正在读取对话…";
    const hasMessages = app.thread?.message_count > 0 || app.messages.some((message) =>
      message.role === "user" || Boolean(message.text || message.attachments?.length));
    return app.thread && !hasMessages ? "当前对话还没有消息，请先发送消息" : "";
  }
  function updateNewButtons() {
    const reason = app.creationPending ? "正在创建…" : newThreadBlockReason();
    NEW_BUTTONS.forEach((id) => {
      $(id).disabled = Boolean(reason);
      $(id).setAttribute("aria-label", reason || "新对话");
      $(id).title = reason || "新对话";
      $(id).setAttribute("aria-busy", String(app.creationPending));
    });
  }
  async function createThread() {
    if (app.creationPending) { notice("正在创建…"); return; }
    if (!app.authenticated) {
      notice("请先登录后再新建对话");
      if (!$("login-view").hidden) $("login-error").textContent = "请先登录后再新建对话";
      return;
    }
    if (!app.visible) { notice("请先打开对话页面后再新建"); return; }
    if (!navigator.onLine) { notice("当前离线，无法新建对话；草稿已保留，联网后请手动新建"); return; }
    if (app.busy) { notice("当前操作尚未完成，请完成后再新建对话"); return; }
    const blocked = newThreadBlockReason();
    if (blocked) { notice(blocked); return; }
    const generation = app.generation;
    // Navigation cannot release a pending POST or admit duplicate creation.
    app.creationPending = true; notice("正在创建…"); updateNewButtons();
    try {
      const data = await request(`${API}/threads`, json("POST", {}), generation);
      if (!current(generation)) return;
      const created = data.thread; upsertThread(created);
      // Keep creation locked through navigation into the new, empty thread.
      // Only successful creation closes the drawer; opening preserves the old draft.
      $("chat-drawer").close();
      const opening = open(encodeURIComponent(created.id)), navigationGeneration = app.generation;
      const opened = await opening;
      if (current(navigationGeneration) && app.thread?.id === created.id) {
        notice(opened ? "已新建对话" : `已新建对话；${$("chat-notice").textContent || "消息读取失败，请刷新后重试"}`);
      }
    } catch (error) {
      if (current(generation) && error.status !== 401) {
        notice(error.status || !app.csrf ? `新建对话失败：${error.message}` : "新建结果未确认，请刷新对话列表确认后再手动新建；不会自动重试，草稿已保留");
      }
    } finally {
      app.creationPending = false; updateNewButtons();
    }
  }

  function showDialog(kind, thread = app.thread) {
    if (!thread || !["rename", "delete"].includes(kind) || app.busy || app.loading) return;
    $("chat-drawer").close();
    app.dialog = { kind, thread, generation: app.generation };
    const rename = kind === "rename";
    $("chat-dialog-title").textContent = rename ? "重命名对话" : "删除这个对话？";
    $("chat-dialog-message").textContent = rename ? "名称仅用于整理对话列表" : `「${thread.title || "新对话"}」的消息与附件将删除，无法恢复；关联提醒和每日任务也将停止。`;
    $("chat-name").hidden = !rename; $("chat-name-label").hidden = !rename;
    $("chat-name").required = rename; $("chat-name").value = rename ? thread.title || "新对话" : "";
    $("chat-dialog-error").textContent = "";
    $("chat-dialog-confirm").textContent = rename ? "保存" : "删除";
    $("chat-dialog-confirm").className = rename ? "primary" : "danger";
    $("chat-dialog-confirm").disabled = false;
    $("chat-dialog").showModal();
    if (rename) $("chat-name").select();
  }
  $("chat-dialog-form").addEventListener("submit", async (event) => {
    event.preventDefault(); const dialog = app.dialog;
    if (!dialog || app.busy || !current(dialog.generation)) return;
    const { kind, thread, generation } = dialog;
    const title = $("chat-name").value.trim();
    if (kind === "rename" && !title) { $("chat-dialog-error").textContent = "请输入对话名称"; return; }
    app.busy = true; $("chat-dialog-confirm").disabled = true; updateComposer();
    try {
      const path = threadPath(thread);
      const data = await request(path, json(kind === "rename" ? "PATCH" : "DELETE", kind === "rename" ? { title } : undefined), generation);
      $("chat-dialog").close(); app.dialog = null;
      if (kind === "delete") {
        releaseDraft(app.drafts.get(thread.id)); app.drafts.delete(thread.id);
        app.threads = data.threads || [];
        if (data.replacement) upsertThread(data.replacement);
        if (app.thread?.id === thread.id) {
          // Do not save the deleted draft again in leave().
          app.thread = null; app.files = []; writeEditor("");
          await open(data.replacement?.id || app.threads[0]?.id, "replace", true);
        } else renderThreads();
      } else {
        upsertThread(data.thread); renderThreads();
      }
    } catch (error) { if (current(generation)) $("chat-dialog-error").textContent = error.message; }
    finally { if (current(generation)) { app.busy = false; $("chat-dialog-confirm").disabled = false; updateComposer(); } }
  });

  // Small, deliberately non-HTML Markdown renderer. No parsing of tags or attributes.
  // Index equal-length tick runs once: repeated unmatched ticks never search a suffix per run.
  function codeSpans(source) {
    const runs = [], starts = new Map(), next = new Map(), spans = [];
    for (let i = 0; i < source.length;) {
      if (source[i] !== "`") { i += 1; continue; }
      const start = i; while (source[i] === "`") i += 1;
      const run = { start, end: i, length: i - start, close: null };
      runs.push(run); starts.set(start, run);
    }
    for (let i = runs.length - 1; i >= 0; i -= 1) {
      const run = runs[i]; run.close = next.get(run.length) || null; next.set(run.length, run);
    }
    for (let i = 0; i < source.length;) {
      if (source[i] === "\\") { i += 2; continue; }
      const run = starts.get(i);
      if (!run) { i += 1; continue; }
      if (run.close) { spans.push({ start: i, end: run.close.end, text: source.slice(run.end, run.close.start) }); i = run.close.end; }
      else i = run.end;
    }
    return spans;
  }
  const unescapeInline = (source) => source.replace(/\\([\\`*{}\[\]()#+.!_|>~\-])/g, "$1");
  function inlineLine(parent, source, depth) {
    const spans = new Map(codeSpans(source).map((span) => [span.start, span]));
    const square = new Int32Array(source.length + 1), stop = new Int32Array(source.length + 1), star = new Int32Array(source.length + 1);
    square[source.length] = stop[source.length] = star[source.length] = source.length;
    // Constant-time closing lookups also keep many unmatched '[' / '*' characters linear.
    for (let i = source.length - 1; i >= 0; i -= 1) {
      square[i] = source[i] === "]" ? i : square[i + 1];
      stop[i] = /[\s)]/.test(source[i]) ? i : stop[i + 1];
      star[i] = source[i] === "*" ? i : star[i + 1];
    }
    let start = 0;
    const plain = (end) => { parent.append(document.createTextNode(unescapeInline(source.slice(start, end)))); };
    for (let i = 0; i < source.length;) {
      if (source[i] === "\\") { i += 2; continue; }
      const span = spans.get(i);
      if (span) {
        plain(i); let text = span.text;
        if (text.startsWith(" ") && text.endsWith(" ") && text.trim()) text = text.slice(1, -1);
        parent.append(el("code", text)); i = span.end; start = i; continue;
      }
      if (source.startsWith("**", i) && i + 2 < source.length) {
        const end = star[i + 2];
        if (end > i + 2 && source.startsWith("**", end)) {
          plain(i); const strong = el("strong"); inline(strong, source.slice(i + 2, end), depth + 1); parent.append(strong);
          i = end + 2; start = i; continue;
        }
      }
      if (source[i] === "[") {
        const close = square[i + 1];
        if (close > i + 1 && source[close + 1] === "(" && close + 2 < source.length) {
          const end = stop[close + 2];
          if (end > close + 2 && source[end] === ")") {
            plain(i); let url; try { url = new URL(source.slice(close + 2, end)); } catch { url = null; }
            if (url && ["http:", "https:"].includes(url.protocol)) {
              const link = el("a", unescapeInline(source.slice(i + 1, close))); link.href = url.href; link.target = "_blank"; link.rel = "noopener noreferrer"; parent.append(link);
            } else parent.append(document.createTextNode(unescapeInline(source.slice(i, end + 1))));
            i = end + 1; start = i; continue;
          }
        }
      }
      i += 1;
    }
    plain(source.length);
  }
  function inline(parent, source, depth = 0) {
    if (depth > 3) { parent.append(document.createTextNode(source)); return; }
    // Code spans cannot consume a later paragraph line or a table delimiter.
    source.split("\n").forEach((line, index) => {
      if (index) parent.append(document.createTextNode("\n"));
      inlineLine(parent, line, depth);
    });
  }
  function pipeCells(line) {
    const spans = codeSpans(line), cells = []; let spanIndex = 0, start = 0, pipes = 0;
    for (let i = 0; i < line.length;) {
      const span = spans[spanIndex];
      if (span?.start === i) { i = span.end; spanIndex += 1; continue; }
      if (line[i] === "\\") { i += 2; continue; }
      if (line[i] === "|") { cells.push(line.slice(start, i).trim()); start = i + 1; pipes += 1; }
      i += 1;
    }
    if (!pipes) return null;
    cells.push(line.slice(start).trim());
    if (!cells[0]) cells.shift(); // Optional outer pipes, but not interior empty cells.
    if (!cells.at(-1)) cells.pop();
    return cells.length ? cells : null;
  }
  function tableAlignment(cells) {
    if (!cells || !cells.length || cells.length > 64 || !cells.every((cell) => /^:?-{3,}:?$/.test(cell))) return null;
    return cells.map((cell) => cell.startsWith(":") ? cell.endsWith(":") ? "center" : "left" : cell.endsWith(":") ? "right" : "left");
  }
  function tableNode(header, alignment, lineIndex) {
    const wrap = el("div", null, "chat-table-scroll"), table = el("table"), head = el("thead"), row = el("tr"), body = el("tbody");
    wrap.dataset.markdownKey = `table:${lineIndex}`;
    wrap.tabIndex = 0; wrap.setAttribute("role", "region"); wrap.setAttribute("aria-label", "回复表格，可横向滚动");
    header.forEach((text, index) => { const cell = el("th"); cell.scope = "col"; cell.dataset.align = alignment[index]; inline(cell, text); row.append(cell); });
    head.append(row); table.append(head, body); wrap.append(table); return { wrap, body };
  }
  function markdown(source, streaming = false) {
    const fragment = document.createDocumentFragment(), lines = String(source || "").replace(/\r\n?/g, "\n").split("\n");
    const tokenized = new Map(), cellsAt = (index) => {
      if (!tokenized.has(index)) tokenized.set(index, pipeCells(lines[index]));
      return tokenized.get(index);
    };
    const complete = (index) => index < lines.length - 1 || !streaming;
    let paragraph = [], list = null, code = null;
    const flush = () => { if (paragraph.length) { const node = el("p"); inline(node, paragraph.join("\n")); fragment.append(node); paragraph = []; } list = null; };
    const flushCode = () => { const pre = el("pre"); pre.append(el("code", code.lines.join("\n"))); fragment.append(pre); code = null; };
    for (let index = 0; index < lines.length; index += 1) {
      const line = lines[index], fence = /^\s*(`{3,}|~{3,})(.*)$/.exec(line);
      if (code !== null) {
        if (fence && fence[1][0] === code.marker && fence[1].length >= code.length && !fence[2].trim()) flushCode();
        else code.lines.push(line);
        continue;
      }
      if (fence) { flush(); code = { marker: fence[1][0], length: fence[1].length, lines: [] }; continue; }
      if (!line.trim()) { flush(); continue; }
      // Streamed unterminated header/delimiter/body lines remain literal until complete.
      const header = complete(index) && index + 1 < lines.length && complete(index + 1) ? cellsAt(index) : null;
      const alignment = header && tableAlignment(cellsAt(index + 1));
      if (alignment && header.length === alignment.length) {
        flush(); const { wrap, body } = tableNode(header, alignment, index); fragment.append(wrap); index += 1;
        // Bound columns, so padding many short rows cannot expand into quadratic DOM size.
        while (index + 1 < lines.length && complete(index + 1) && lines[index + 1].trim() && !/^\s*(?:`{3,}|~{3,})/.test(lines[index + 1])) {
          const cells = cellsAt(index + 1); if (!cells) break;
          const row = el("tr");
          alignment.forEach((align, column) => { const cell = el("td"); cell.dataset.align = align; inline(cell, cells[column] || ""); row.append(cell); });
          body.append(row); index += 1;
        }
        continue;
      }
      const heading = /^(#{1,6})\s+(.+)$/.exec(line), item = /^\s*(?:([-*])|\d+\.)\s+(.+)$/.exec(line);
      if (heading) { flush(); const node = el(`h${Math.min(6, heading[1].length + 1)}`); inline(node, heading[2]); fragment.append(node); }
      else if (item) {
        if (paragraph.length) flush();
        const tag = item[1] ? "ul" : "ol";
        if (!list || list.tagName.toLowerCase() !== tag) { list = el(tag); fragment.append(list); }
        const node = el("li"); inline(node, item[2]); list.append(node);
      } else { if (list) flush(); paragraph.push(line); }
    }
    if (code !== null) flushCode();
    flush(); return fragment;
  }
  function timeLabel(value) {
    const date = typeof value === "number" ? new Date(value > 1e12 ? value : value * 1000) : new Date(value);
    if (!value || Number.isNaN(date.getTime())) return { day: "日期未知", clock: "" };
    return { day: date.toLocaleDateString("zh-CN", { timeZone: "Asia/Shanghai" }), clock: date.toLocaleTimeString("zh-CN", { timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", hour12: false }) };
  }
  const nearBottom = () => { const node = $("chat-scroll"); return node.scrollHeight - node.scrollTop - node.clientHeight < 80; };
  function scrollBottom() { $("chat-scroll").scrollTop = $("chat-scroll").scrollHeight; app.stick = true; layout.bottom = true; $("chat-bottom").hidden = true; }
  function follow() { if (app.stick) scrollBottom(); else $("chat-bottom").hidden = false; }
  const TOOL_STATUS = Object.freeze({ running: "进行中", completed: "已完成", error: "失败", cancelled: "已取消", interrupted: "已中断" });
  const messageViews = new WeakMap();
  function toolText(value, limit) {
    return typeof value === "string" ? value.replace(/[\u0000-\u001f\u007f-\u009f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]/g, " ").replace(/\s+/g, " ").trim().slice(0, limit) : "";
  }
  function toolSnapshot(value) {
    if (!Array.isArray(value)) return [];
    const ids = new Set(), tools = [];
    for (const item of value.slice(0, 30)) {
      if (!item || typeof item.id !== "string" || !/^[a-f0-9]{32}$/i.test(item.id) || ids.has(item.id)
        || typeof item.status !== "string" || !Object.hasOwn(TOOL_STATUS, item.status)) continue;
      ids.add(item.id);
      tools.push({ id: item.id, name: toolText(item.name, 96) || "工具", summary: toolText(item.summary, 160), status: item.status,
        started_at: Number.isFinite(item.started_at) && item.started_at > 0 ? item.started_at : null,
        duration_seconds: Number.isFinite(item.duration_seconds) && item.duration_seconds >= 0 ? item.duration_seconds : null });
    }
    return tools;
  }
  function applyToolSnapshot(message, source, previous = message) {
    const before = toolSnapshot(previous?.tool_calls);
    if (Array.isArray(source.tool_calls)) {
      const tools = toolSnapshot(source.tool_calls), old = new Map(before.map((tool) => [tool.id, tool]));
      const changed = tools.filter((tool) => {
        const prior = old.get(tool.id);
        return !prior || prior.status !== tool.status || prior.name !== tool.name || prior.summary !== tool.summary;
      });
      message.tool_calls = tools;
      message._latestToolId = changed.at(-1)?.id || tools.find((tool) => tool.id === previous?._latestToolId)?.id || tools.at(-1)?.id;
    } else if (Array.isArray(previous?.tool_calls)) {
      message.tool_calls = before; message._latestToolId = previous._latestToolId;
    }
    message.tools_truncated = typeof source.tools_truncated === "boolean" ? source.tools_truncated : Boolean(previous?.tools_truncated);
    if (Array.isArray(source.tool_calls) && source.tool_calls.length > 30) message.tools_truncated = true;
  }
  function upsertMessage(message) {
    if (!message || typeof message.id !== "string") throw new Error("消息数据无效");
    const index = app.messages.findIndex((item) => item.id === message.id), next = { ...message };
    if (message.role === "assistant") applyToolSnapshot(next, message, index < 0 ? null : app.messages[index]);
    if (index < 0) app.messages.push(next); else app.messages[index] = next;
    return next;
  }
  function messageAttachment(meta, generation) {
    const node = el("div", null, "chat-message-attachment");
    const thread = app.thread;
    const download = button(`${meta.name || "附件"} · ${sizeLabel(meta.size)}`, "chat-download", async () => {
      try {
        const blob = await request(`${attachmentPath(meta, thread)}/content`, { binary: true }, generation);
        const url = rememberURL(blob), link = el("a"); link.href = url; link.download = meta.name || "attachment";
        link.click(); setTimeout(() => revoke(url), 1000);
      } catch (error) { fail(error, generation); }
    });
    node.append(download);
    if (meta.kind === "image" && IMAGE_MIMES.has(meta.mime)) {
      const image = el("img"); image.alt = meta.name || "图片附件"; image.className = "chat-image";
      const key = `${thread.id}/${meta.id}`;
      let cached = app.images.get(key);
      if (!cached) {
        cached = { url: null, promise: null };
        cached.promise = request(`${attachmentPath(meta, thread)}/content`, { binary: true }, generation).then((blob) => {
          cached.url = rememberURL(blob); return cached.url;
        });
        app.images.set(key, cached);
      }
      if (cached.url) image.src = cached.url;
      else cached.promise.then((url) => { if (current(generation)) { image.src = url; image.addEventListener("load", follow, { once: true }); } }).catch(() => { image.alt = "图片加载失败，可点击附件下载"; });
      node.prepend(image);
    }
    return node;
  }
  // Connected keyed nodes preserve disclosure state and focus during streaming.
  function syncNodes(parent, nodes) {
    nodes.forEach((node, index) => {
      const old = parent.children[index];
      if (old !== node) parent.insertBefore(node, old || null);
    });
    while (parent.children.length > nodes.length) parent.lastElementChild.remove();
  }
  function patchMarkdown(parent, fragment) {
    const nodes = [...fragment.childNodes], keyed = new Map([...parent.children].filter((node) => node.dataset.markdownKey).map((node) => [node.dataset.markdownKey, node]));
    nodes.forEach((fresh, index) => {
      const key = fresh.nodeType === Node.ELEMENT_NODE ? fresh.dataset.markdownKey : null;
      let old = parent.childNodes[index];
      if (key && keyed.has(key) && old !== keyed.get(key)) { parent.insertBefore(keyed.get(key), old || null); old = keyed.get(key); }
      if (!old) parent.append(fresh);
      else if (!old.isEqualNode(fresh)) {
        if (old.nodeType === fresh.nodeType && old.nodeName === fresh.nodeName && (!key || old.dataset.markdownKey === key)) {
          if (old.nodeType === Node.TEXT_NODE) old.textContent = fresh.textContent;
          else {
            const horizontal = old.classList.contains("chat-table-scroll") ? old.scrollLeft : null;
            [...old.attributes].forEach((attr) => { if (!fresh.hasAttribute(attr.name)) old.removeAttribute(attr.name); });
            [...fresh.attributes].forEach((attr) => { if (old.getAttribute(attr.name) !== attr.value) old.setAttribute(attr.name, attr.value); });
            patchMarkdown(old, fresh);
            if (horizontal !== null) old.scrollLeft = horizontal;
          }
        } else old.replaceWith(fresh);
      }
    });
    while (parent.childNodes.length > nodes.length) parent.lastChild.remove();
  }
  function retainMessageScroll(update) {
    const horizontal = [...$("chat-messages").querySelectorAll(".chat-table-scroll")].map((node) => [node, node.scrollLeft]);
    const scroll = $("chat-scroll"), bottom = app.stick, top = scroll.scrollTop, box = scroll.getBoundingClientRect();
    const article = bottom ? null : [...$("chat-messages").children].find((node) => node.dataset.messageId && node.getBoundingClientRect().bottom > box.top);
    const anchor = article && ([...article.querySelectorAll(".chat-message-meta, .chat-tools, .chat-markdown > *, .chat-message-attachment, .chat-message-actions")].find((node) => {
      const rect = node.getBoundingClientRect(); return rect.height && rect.bottom > box.top;
    }) || article);
    const offset = anchor?.getBoundingClientRect().top - box.top, articleOffset = article?.getBoundingClientRect().top - box.top;
    update();
    horizontal.forEach(([node, left]) => { if (node.isConnected) node.scrollLeft = left; });
    if (bottom) scrollBottom();
    else {
      const kept = anchor?.isConnected ? anchor : article?.isConnected ? article : null;
      scroll.scrollTop = kept ? scroll.scrollTop + kept.getBoundingClientRect().top - scroll.getBoundingClientRect().top - (kept === anchor ? offset : articleOffset) : top;
      app.stick = false; layout.bottom = false; $("chat-bottom").hidden = false;
    }
    rememberAnchor();
  }
  function toolPanel() {
    const details = el("details", null, "chat-tools"), heading = el("summary"), line = el("span", null, "chat-tools-heading");
    const latest = el("span", null, "chat-tools-latest"), status = el("span", null, "chat-tools-status");
    const meta = el("span", null, "chat-tools-meta"), name = el("span", null, "chat-tools-name"), count = el("span", null, "chat-tools-count");
    line.append(latest, status); meta.append(name, count); heading.append(line, meta);
    const panel = el("div", null, "chat-tools-panel"), list = el("ol", null, "chat-tools-list"), truncated = el("p", "工具活动过多，仅展示前 30 条。", "chat-tools-truncated");
    panel.tabIndex = 0; panel.setAttribute("role", "region"); panel.setAttribute("aria-label", "工具活动明细，可滚动");
    panel.append(list, truncated); details.append(heading, panel);
    // Never set open here or during updates; a newly loaded article is closed.
    return { details, latest, status, name, count, list, truncated, rows: new Map() };
  }
  function renderTools(view, message) {
    const tools = toolSnapshot(message.tool_calls), truncated = Boolean(message.tools_truncated || message.tool_calls?.length > 30);
    view.details.hidden = !tools.length && !truncated;
    const active = tools.filter((tool) => tool.status === "running");
    const latest = active.at(-1) || tools.find((tool) => tool.id === message._latestToolId) || tools.at(-1);
    const running = active.length;
    view.latest.textContent = latest ? latest.summary || latest.name : "工具活动";
    view.latest.title = view.latest.textContent;
    view.status.textContent = latest ? TOOL_STATUS[latest.status] : "";
    view.name.textContent = latest ? `工具 · ${latest.name}` : "工具活动";
    const stage = running ? ` · ${running} 进行中` : ["running", "streaming"].includes(message.status) ? " · 生成回复中" : "";
    view.count.textContent = tools.length ? `${tools.length}${truncated ? "+" : ""} 次${stage}` : "记录已截断";
    const ids = new Set(), nodes = tools.map((tool) => {
      ids.add(tool.id); let row = view.rows.get(tool.id);
      if (!row) {
        const node = el("li", null, "chat-tool"), head = el("div", null, "chat-tool-head"); node.dataset.toolId = tool.id;
        const name = el("span", null, "chat-tool-name"), status = el("span", null, "chat-tool-status"), duration = el("span", null, "chat-tool-duration"), description = el("p", null, "chat-tool-summary");
        head.append(name, status, duration); node.append(head, description);
        row = { node, name, status, duration, description }; view.rows.set(tool.id, row);
      }
      row.name.textContent = tool.name; row.status.textContent = TOOL_STATUS[tool.status];
      row.duration.textContent = tool.duration_seconds === null ? "耗时 —" : `耗时 ${tool.duration_seconds < 10 ? tool.duration_seconds.toFixed(1) : Math.round(tool.duration_seconds)} 秒`;
      row.description.textContent = tool.summary; row.description.hidden = !tool.summary;
      return row.node;
    });
    syncNodes(view.list, nodes);
    view.rows.forEach((row, id) => { if (!ids.has(id)) view.rows.delete(id); });
    view.truncated.hidden = !truncated;
  }
  function updateMessageBrand(view) {
    const message = view.message, assistant = message.role !== "user";
    const status = { running: "回复中", streaming: "回复中", failed: "失败", error: "失败", interrupted: "已中断", truncated: "回复已截断", cancelled: "已停止", stopped: "已停止" }[message.status] || "";
    view.metaText.textContent = `${assistant ? window.FerryAppearance?.name() || "Ferry" : app.userDisplayName} · ${timeLabel(message.time ?? message.created_at).clock}${status ? ` · ${status}` : ""}`;
    view.avatar.hidden = !assistant || !window.FerryAppearance?.avatar();
    if (view.avatar.hidden) view.avatar.removeAttribute("src");
    else {
      const icon = window.FerryAppearance.icon("192");
      if (view.avatar.getAttribute("src") !== icon) view.avatar.src = icon;
    }
  }
  function updateMessageNode(node, message) {
    const view = messageViews.get(node); view.message = message;
    updateMessageBrand(view);
    if (view.tools) renderTools(view.tools, message);
    const text = String(message.text || ""), streaming = ["running", "streaming"].includes(message.status);
    if (view.text !== text || view.streaming !== streaming) { patchMarkdown(view.body, markdown(text, streaming)); view.text = text; view.streaming = streaming; }
    view.body.hidden = !text;
    const attachments = message.attachments || [], key = JSON.stringify(attachments);
    if (view.attachmentKey !== key) {
      view.attachments.replaceChildren(...attachments.map((meta) => messageAttachment(meta, app.generation))); view.attachmentKey = key;
    }
    const retryable = Boolean(app.failed?.retryable && app.failed.turnId === message.turn_id && message.role === "assistant");
    view.copy.hidden = !text; view.retry.hidden = !retryable; view.actions.hidden = !text && !retryable;
    view.retry.disabled = Boolean(app.stream || app.loading || app.files.length || editorText().trim());
  }
  function messageNode(message) {
    const node = el("article", null, `chat-message chat-message-${message.role === "user" ? "user" : "assistant"}`);
    node.dataset.messageId = message.id;
    const view = { message, meta: el("div", null, "chat-message-meta caption"), metaText: el("span"), avatar: el("img", null, "chat-assistant-avatar"), body: el("div", null, "chat-markdown"),
      attachments: el("div", null, "chat-message-attachments"), actions: el("div", null, "chat-message-actions"), tools: message.role === "assistant" ? toolPanel() : null };
    view.avatar.alt = ""; view.avatar.width = 20; view.avatar.height = 20; view.avatar.hidden = true;
    view.meta.append(view.avatar, view.metaText);
    view.copy = iconButton("复制消息", "copy", "chat-copy", async () => {
      const generation = app.generation;
      try { await navigator.clipboard.writeText(view.message.text); if (current(generation)) notice("已复制"); }
      catch { if (current(generation)) notice("无法访问剪贴板，请选择文字复制"); }
    });
    view.retry = button("重试", "chat-retry", () => send(view.message.turn_id));
    view.actions.append(view.copy, view.retry); node.append(view.meta);
    if (view.tools) node.append(view.tools.details);
    node.append(view.body, view.attachments, view.actions); messageViews.set(node, view); updateMessageNode(node, message);
    return node;
  }
  function renderMessages() {
    retainMessageScroll(() => {
      const container = $("chat-messages"), messages = new Map(), dates = new Map(), nodes = []; let previous = "";
      [...container.children].forEach((node) => {
        if (node.dataset.messageId) messages.set(node.dataset.messageId, node);
        else if (node.dataset.day) dates.set(node.dataset.day, node);
      });
      app.messages.forEach((message) => {
        const day = timeLabel(message.time ?? message.created_at).day;
        if (day !== previous) {
          const date = dates.get(day) || el("div", day, "chat-date"); date.dataset.day = day;
          nodes.push(date); dates.delete(day); previous = day;
        }
        const node = messages.get(message.id);
        if (node) { updateMessageNode(node, message); nodes.push(node); }
        else nodes.push(messageNode(message));
      });
      syncNodes(container, nodes);
      $("chat-empty").hidden = Boolean(app.messages.length || app.loading);
      $("chat-older").hidden = !app.hasMore; $("chat-older").disabled = app.loading;
    });
    updateNewButtons();
  }
  function renderMessage(message) {
    const old = [...$("chat-messages").children].find((node) => node.dataset.messageId === message.id);
    if (old) retainMessageScroll(() => updateMessageNode(old, message)); else renderMessages();
  }
  async function older() {
    if (app.loading || !app.hasMore || !app.messages.length || turnPending()) return;
    const generation = app.generation, scroll = $("chat-scroll"), height = scroll.scrollHeight, top = scroll.scrollTop;
    app.loading = true; updateComposer(); $("chat-older").disabled = true;
    try {
      const data = await request(`${threadPath()}/messages?${new URLSearchParams({ limit: "50", before: app.messages[0].id })}`, {}, generation);
      upsertThread(data.thread); const ids = new Set(app.messages.map((message) => message.id));
      app.messages = [...data.messages.filter((message) => !ids.has(message.id)), ...app.messages]; app.hasMore = Boolean(data.has_more);
      app.stick = false; renderMessages(); scroll.scrollTop = top + scroll.scrollHeight - height;
    } catch (error) { fail(error, generation); }
    finally { if (current(generation)) { app.loading = false; $("chat-older").disabled = false; updateComposer(); } }
  }

  // Attachments reserve metadata first, then upload raw bytes. Never send pending IDs.
  const sizeLabel = (bytes) => Number.isFinite(bytes) ? bytes >= 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(1)} MiB` : `${Math.ceil(bytes / 1024)} KiB` : "";
  function renderAttachments() {
    $("chat-attachments").replaceChildren(...app.files.map((item) => {
      const node = el("div", null, "chat-attachment-chip");
      if (item.preview) { const image = el("img"); image.src = item.preview; image.alt = item.file.name; node.append(image); }
      node.append(el("span", item.file.name, "chat-file-name"), el("small", item.state === "ready" ? sizeLabel(item.file.size) : item.state === "uploading" ? "上传中…" : item.state === "removing" ? "删除中…" : "上传失败", "caption"));
      if (item.state === "error") node.append(button("重新上传", "link-button", () => upload(item)));
      const remove = button("×", "chat-remove-file", () => removeFile(item)); remove.setAttribute("aria-label", `移除 ${item.file.name}`); remove.disabled = Boolean(app.stream || item.state === "removing"); node.append(remove); return node;
    }));
    updateComposer();
  }
  async function addFiles(files) {
    if (!app.thread || turnPending() || app.loading || app.busy) return;
    for (const file of files) {
      const total = app.files.reduce((sum, item) => sum + item.file.size, 0);
      if (!file.size) { notice("不能上传空文件"); continue; }
      if (file.size > MAX_FILE) { notice("单个附件不能超过 10 MiB"); continue; }
      if (app.files.length >= MAX_FILES) { notice("每条消息最多 5 个附件"); break; }
      if (total + file.size > MAX_TOTAL) { notice("附件合计不能超过 25 MiB"); continue; }
      const item = { file, meta: null, state: "uploading", preview: IMAGE_MIMES.has(file.type) ? rememberURL(file) : null, controller: null };
      app.files.push(item); saveDraft(); renderAttachments(); upload(item);
    }
  }
  async function upload(item) {
    if (turnPending() || !app.files.includes(item)) return;
    const generation = app.generation, thread = app.thread, controller = new AbortController(); item.controller = controller; item.state = "uploading"; renderAttachments();
    try {
      if (item.meta) await request(attachmentPath(item.meta, thread), { method: "DELETE" }, generation, controller);
      const extension = item.file.name.split(".").pop().toLowerCase();
      const mime = !item.file.type || item.file.type === "application/octet-stream" ? FILE_MIMES[extension] || "application/octet-stream" : item.file.type;
      const data = await request(`${threadPath(thread)}/attachments`, json("POST", { name: item.file.name, mime, size: item.file.size }), generation, controller);
      item.meta = data.attachment;
      const uploaded = await request(`${attachmentPath(item.meta, thread)}/content`, { method: "PUT", headers: { "Content-Type": "application/octet-stream" }, body: item.file }, generation, controller);
      item.meta = uploaded.attachment; item.state = "ready";
    } catch (error) { item.state = "error"; fail(error, generation); }
    finally { if (current(generation) && app.files.includes(item)) { renderAttachments(); saveDraft(); } }
  }
  async function removeFile(item) {
    if (turnPending()) return;
    const generation = app.generation; item.controller?.abort(); item.state = "removing"; renderAttachments();
    try {
      if (item.meta) await request(attachmentPath(item.meta), { method: "DELETE" }, generation);
      if (!current(generation)) return;
      app.files = app.files.filter((other) => other !== item); revoke(item.preview); saveDraft(); renderAttachments();
    } catch (error) { if (current(generation)) { item.state = "error"; renderAttachments(); fail(error, generation); } }
  }

  function autosize() {
    const input = $("chat-input");
    const cap = Number.parseFloat(getComputedStyle(input).maxHeight) || 150;
    input.style.height = "auto"; input.style.height = `${Math.min(cap, input.scrollHeight)}px`;
    viewport();
  }
  const turnPending = () => Boolean(app.stream || app.recovering || app.uncertain);
  function updateComposer() {
    const blocked = !app.authenticated || !app.thread || app.loading || app.busy;
    const running = turnPending(), input = $("chat-input"), text = editorText();
    const tooLong = !app.composing && textLength(text) > MAX_TEXT;
    const mode = blocked ? "false" : editorMode;
    if (input.contentEditable !== mode) input.contentEditable = mode;
    input.tabIndex = blocked ? -1 : 0; input.setAttribute("aria-disabled", String(blocked));
    input.dataset.empty = String(!text); input.setAttribute("aria-invalid", String(tooLong));
    if (blocked) closeCommands();
    if (tooLong) notice("消息最多 32000 字符；草稿已保留，请缩短后发送");
    else if (!app.composing && $("chat-notice").textContent.startsWith("消息最多 32000 字符")) notice();
    $("chat-add-files").disabled = blocked || running;
    $("chat-send").disabled = blocked || running || tooLong || app.files.some((item) => item.state !== "ready") || (!text.trim() && !app.files.length);
    $("chat-stop").hidden = !running; $("chat-stop").disabled = !app.stream || Boolean(app.stream.stopping);
    $("chat-older").disabled = app.loading || running;
    updateNewButtons();
    $("chat-composer").setAttribute("aria-busy", String(running));
    document.querySelectorAll(".chat-retry").forEach((node) => { node.disabled = blocked || running || Boolean(app.files.length || text.trim()); });
  }
  function closeCommands() {
    commandGesture = null; commandTap = null; commandInteraction = false;
    app.suggestions = []; $("chat-commands").hidden = true; $("chat-commands").replaceChildren();
    $("chat-input").setAttribute("aria-expanded", "false"); $("chat-input").removeAttribute("aria-activedescendant");
  }
  function updateCommands() {
    const value = editorText();
    if (app.composing || !$("chat-input").isContentEditable || !value.startsWith("/") || value.includes("\n") || app.stream) { closeCommands(); return; }
    const candidates = [];
    app.commands.forEach((command) => {
      candidates.push({ value: command.command, usage: command.usage, description: command.description });
      (command.aliases || []).filter((alias) => /^\/[a-z]+$/.test(alias)).forEach((alias) => candidates.push({ value: alias, usage: alias, description: command.description }));
      (command.subcommands || []).filter((name) => /^[a-z]+$/.test(name)).forEach((name) => {
        const value = `${command.command} ${name}`;
        candidates.push({ value, usage: value, description: command.description });
      });
    });
    app.suggestions = candidates.filter((item) => item.value.startsWith(value.trim().toLowerCase()));
    app.selected = 0; renderCommands();
  }
  function renderCommands() {
    if (!app.suggestions.length) { closeCommands(); return; }
    $("chat-commands").hidden = false; $("chat-input").setAttribute("aria-expanded", "true");
    $("chat-commands").replaceChildren(...app.suggestions.map((item, index) => {
      const option = el("div", null, "chat-command"); option.id = `chat-command-${index}`; option.setAttribute("role", "option"); option.setAttribute("aria-selected", String(index === app.selected));
      option.append(el("strong", item.usage || item.value), el("span", item.description, "caption"));
      option.dataset.index = String(index); return option;
    }));
    $("chat-input").setAttribute("aria-activedescendant", `chat-command-${app.selected}`);
    $(`chat-command-${app.selected}`)?.scrollIntoView({ block: "nearest" });
  }
  function chooseCommand() {
    const item = app.suggestions[app.selected]; if (!item) return;
    editorEnd();
    const range = document.createRange(); range.selectNodeContents($("chat-input"));
    const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
    insertPlainText(`${item.value} `); closeCommands(); autosize(); saveDraft(); updateComposer(); editorEnd();
  }

  const FAILED_STATUSES = new Set(["error", "failed", "interrupted", "truncated", "cancelled", "stopped"]);
  async function loadHistory(generation, preserveOlder = false) {
    const data = await request(`${threadPath()}/messages?limit=50`, {}, generation);
    upsertThread(data.thread);
    const messages = Array.isArray(data.messages) ? data.messages : [];
    const first = preserveOlder && data.has_more ? app.messages.findIndex((message) => message.id === messages[0]?.id) : -1;
    app.messages = first > 0 ? [...app.messages.slice(0, first), ...messages] : messages;
    if (first <= 0) app.hasMore = Boolean(data.has_more);
    historyStatus(); renderThreads(); renderMessages();
  }
  function historyStatus() {
    const last = [...app.messages].reverse().find((message) => message.role === "assistant");
    app.failed = last?.turn_id && FAILED_STATUSES.has(last.status) ? { turnId: last.turn_id, retryable: true } : null;
    $("chat-progress").textContent = ["cancelled", "stopped"].includes(last?.status) ? "已停止" : app.failed ? "回复失败，可重试" : "";
  }
  function newStream(turnId = null) {
    return { controller: new AbortController(), accepted: false, turnId, messageId: null, seq: -1, terminal: false, stopping: false, error: null, created: null };
  }
  function scheduleRecovery(generation) {
    if (!current(generation) || app.reconnectTimer || !navigator.onLine || document.hidden) return;
    app.reconnectTimer = setTimeout(() => { app.reconnectTimer = 0; recover(generation); }, app.reconnectDelay);
    app.reconnectDelay = Math.min(app.reconnectDelay * 2, 30000);
  }
  async function recover(generation = app.generation, history = true) {
    if (!current(generation) || !app.thread || app.recovering) return;
    clearTimeout(app.reconnectTimer); app.reconnectTimer = 0;
    const token = {}, thread = app.thread, old = app.stream;
    app.recovering = token; app.stream = null; app.uncertain = true;
    old?.controller.abort(); updateComposer();
    try {
      if (history) await loadHistory(generation, true);
      const data = await request(`${threadPath(thread)}/active`, {}, generation);
      if (!current(generation) || app.recovering !== token) return;
      if (typeof data.active !== "boolean") throw new Error("聊天恢复接口数据无效");
      // Completion may race the history read; active also carries the latest durable snapshot.
      if (!data.active) {
        if (data.assistant_message && data.user_message) {
          upsertMessage(data.user_message); upsertMessage(data.assistant_message); historyStatus(); renderMessages();
          if (["complete", "completed"].includes(data.assistant_message.status)) document.dispatchEvent(new CustomEvent("ferry:chat-read", { detail: { thread_id: thread.id, finished_turn_id: data.turn?.id } }));
        } else if (app.messages.some((message) => ["running", "streaming"].includes(message.status))) await loadHistory(generation, true);
        if (app.messages.some((message) => ["running", "streaming"].includes(message.status))) throw new Error("轮次状态正在确认");
      }
      if (!current(generation) || app.recovering !== token) return;
      if (data.active) {
        if (typeof data.turn?.id !== "string" || !data.turn.id) throw new Error("聊天恢复轮次无效");
        upsertMessage(data.user_message); upsertMessage(data.assistant_message);
        const stream = newStream(data.turn.id);
        app.stream = stream; app.failed = null; closeCommands(); renderMessages();
        $("chat-progress").textContent = "正在恢复回复…";
        subscribe(`${threadPath(thread)}/turns/${encodeURIComponent(data.turn.id)}/events`, { headers: { Accept: "application/x-ndjson" } }, stream, generation, thread);
      }
      app.uncertain = false;
    } catch (error) {
      if (current(generation) && error.status !== 401) {
        notice("暂时无法确认回复状态，联网或回到页面后继续恢复；不会重发消息");
        scheduleRecovery(generation);
      }
    } finally {
      if (current(generation) && app.recovering === token) { app.recovering = null; updateComposer(); }
    }
  }
  function consume(line, stream, generation, thread, submission) {
      if (!line.trim() || !current(generation) || app.stream !== stream) return;
      const { text = "", retryOf = null } = submission || {};
      const event = JSON.parse(line);
      if (event.v !== 1) throw new Error("聊天流版本不兼容");
      const type = event.type || event.event;
      if (type === "accepted") {
        if (event.thread_id !== thread.id) throw new Error("对话流标识不一致");
        if (stream.turnId && stream.turnId !== event.turn_id) throw new Error("恢复轮次标识不一致");
        stream.accepted = true; stream.turnId = event.turn_id; stream.messageId = event.assistant_message.id;
        stream.seq = Number.isInteger(event.seq) ? event.seq : -1;
        upsertMessage(event.user_message); upsertMessage(event.assistant_message);
        if (event.thread?.id === thread.id) { upsertThread(event.thread); renderThreads(); }
        else loadThreads(generation).catch((error) => fail(error, generation));
        if (submission && !submission.acknowledged && !retryOf) {
          submission.acknowledged = true;
          if (!text.startsWith("/")) { app.files.forEach((item) => revoke(item.preview)); app.files = []; }
          if (!app.composing && inputRevision === submission.inputRevision && editorText().trim() === text) writeEditor("");
          saveDraft(); renderAttachments(); autosize();
        }
        renderMessages(); $("chat-progress").textContent = "正在回复…";
        // Only a new local send claims the bottom; replay/recovery must not move history readers.
        if (submission && !retryOf && !submission.scrolledToAccepted) {
          submission.scrolledToAccepted = true;
          scrollBottom();
        }
      } else if (type === "progress") {
        const label = toolText(event.label, 160) || toolText(event.stage, 96) || "正在处理…";
        const snapshot = Array.isArray(event.tool_calls) || typeof event.tools_truncated === "boolean";
        if (stream.accepted && snapshot) {
          if (event.message_id !== undefined && event.message_id !== stream.messageId) throw new Error("工具活动消息标识无效");
          const message = app.messages.find((item) => item.id === stream.messageId);
          applyToolSnapshot(message, event); renderMessage(message);
          // Activity belongs to its assistant article, not a second loading card.
          $("chat-progress").textContent = message.tool_calls?.length || message.tools_truncated ? "" : label;
        } else $("chat-progress").textContent = label;
      } else if (type === "delta") {
        if (!stream.accepted || event.message_id !== stream.messageId || !Number.isInteger(event.seq)) throw new Error("聊天增量标识无效");
        if (event.seq <= stream.seq) return;
        stream.seq = event.seq; app.reconnectDelay = 1000;
        const message = app.messages.find((item) => item.id === stream.messageId);
        message.text = (message.text || "") + String(event.text || ""); renderMessage(message);
      } else if (type === "thread_created") { upsertThread(event.thread); stream.created = event.thread; renderThreads(); }
      else if (type === "done") {
        if (event.turn_id !== stream.turnId) throw new Error("聊天结束标识不一致");
        const message = upsertMessage(event.message); stream.terminal = true;
        if (["complete", "completed"].includes(event.status)) document.dispatchEvent(new CustomEvent("ferry:chat-read", { detail: { thread_id: thread.id, finished_turn_id: stream.turnId } }));
        if (text.toLowerCase() === "/reset" && event.status === "complete") {
          app.files.forEach((item) => revoke(item.preview)); app.files = []; saveDraft(); renderAttachments();
        }
        if (["error", "failed", "interrupted", "truncated", "cancelled", "stopped"].includes(event.status)) app.failed = { turnId: stream.turnId, retryable: stream.error?.retryable ?? true };
        renderMessage(message);
        $("chat-progress").textContent = ["cancelled", "stopped"].includes(event.status) ? "已停止" : app.failed ? "回复失败，可重试" : "";
      } else if (type === "error") {
        stream.error = { message: String(event.message || "回复失败"), retryable: Boolean(event.retryable) };
        notice(stream.error.message);
      } else if (type !== "heartbeat") throw new Error("未知聊天流事件");
  }
  async function subscribe(path, options, stream, generation, thread, submission = null) {
    const controller = stream.controller;
    let reader;
    try {
      const response = await request(path, options, generation, controller, true);
      if (!response.body) throw new Error("浏览器不支持流式回复");
      reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = "";
      while (current(generation) && app.stream === stream && !stream.terminal) {
        const { value, done } = await reader.read();
        if (!current(generation) || app.stream !== stream) break;
        buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
        let newline;
        while ((newline = buffer.indexOf("\n")) >= 0) { const line = buffer.slice(0, newline); buffer = buffer.slice(newline + 1); consume(line, stream, generation, thread, submission); if (stream.terminal) break; }
        if (done) { if (buffer.trim() && !stream.terminal) consume(buffer, stream, generation, thread, submission); break; }
      }
    } catch (error) {
      if (current(generation) && app.stream === stream && error.status && error.status !== 401) notice(error.message);
    } finally {
      controller.abort(); app.controllers.delete(controller);
      if (reader) { try { await reader.cancel(); } catch { /* already closed */ } }
      if (current(generation) && app.stream === stream) {
        app.stream = null;
        if (!stream.terminal) {
          // Losing an HTTP subscription does not stop or fail the background turn.
          app.uncertain = true; $("chat-progress").textContent = "正在确认回复状态…";
          if (submission || stream.stopping) recover(generation);
          else scheduleRecovery(generation);
        } else {
          app.reconnectDelay = 1000;
          if (stream.created) window.FerryConsole?.navigate(`chat/${encodeURIComponent(stream.created.id)}`);
          else loadThreads(generation).catch((error) => fail(error, generation));
        }
        updateComposer();
      }
    }
  }
  async function send(retryOf = null) {
    if (!app.thread || turnPending() || app.loading || app.busy || !app.authenticated || app.composing) return;
    const raw = editorText(), text = raw.trim();
    if (textLength(raw) > MAX_TEXT) { notice("消息最多 32000 字符；草稿已保留，请缩短后发送"); return; }
    if (!retryOf && ((!text && !app.files.length) || app.files.some((item) => item.state !== "ready"))) return;
    if (retryOf && (!app.failed?.retryable || app.failed.turnId !== retryOf || text || app.files.length)) return;
    if (!navigator.onLine) { notice("当前离线，草稿留在本页内存中；联网后手动发送"); return; }
    let clientId; try { clientId = crypto.randomUUID(); } catch { notice("发送需要 HTTPS 安全上下文"); return; }
    const generation = app.generation, thread = app.thread, stream = newStream();
    app.stream = stream; app.failed = null; closeCommands(); notice(); $("chat-progress").textContent = "正在提交…"; updateComposer();
    if (!retryOf) scrollBottom();
    const body = retryOf ? { client_turn_id: clientId, retry_of: retryOf } : { client_turn_id: clientId, text, attachment_ids: app.files.map((item) => item.meta.id), retry_of: null };
    await subscribe(`${threadPath(thread)}/turns`, { ...json("POST", body), headers: { "Content-Type": "application/json", Accept: "application/x-ndjson" } }, stream, generation, thread, { text, retryOf, inputRevision, acknowledged: false });
  }
  async function stop() {
    const stream = app.stream; if (!stream || stream.stopping) return;
    const generation = app.generation; stream.stopping = true; updateComposer();
    try {
      const result = await request(`${threadPath()}/stop`, json("POST"), generation);
      if (app.stream === stream) { $("chat-progress").textContent = result.stopped ? "正在停止…" : "轮次已结束"; stream.controller.abort(); }
    } catch (error) { if (current(generation)) { stream.stopping = false; updateComposer(); fail(error, generation); } }
  }

  function rememberAnchor() {
    if (layout.updating || !app.visible) return;
    const box = $("chat-scroll").getBoundingClientRect();
    const anchor = [...$("chat-messages").children].find((node) => node.dataset.messageId && node.getBoundingClientRect().bottom > box.top);
    layout.anchor = anchor ? { id: anchor.dataset.messageId, offset: anchor.getBoundingClientRect().top - box.top } : null;
    layout.bottom = app.stick;
  }
  function settleStandaloneViewport() {
    clearTimeout(layout.settling); layout.settling = 0;
    if (!app.visible || document.hidden || !matchMedia("(max-width: 900px)").matches
        || !(navigator.standalone || matchMedia("(display-mode: standalone)").matches)) return;
    const generation = app.generation, delays = [100, 200, 400, 800];
    let step = 0;
    const sample = () => {
      layout.settling = 0;
      if (!current(generation) || document.hidden) return;
      // iOS cold-launch viewport/safe-area changes may arrive without resize events.
      // Keep sampling through the launch window even when the first measurements agree.
      viewport();
      if (step < delays.length) layout.settling = setTimeout(sample, delays[step++]);
    };
    layout.settling = setTimeout(sample, delays[step++]);
  }
  function viewport(resampleAfterReset = true) {
    if (layout.frame) return;
    layout.frame = requestAnimationFrame(() => {
      layout.frame = 0;
      const pane = $("dashboard"), vv = window.visualViewport;
      const active = app.visible && matchMedia("(max-width: 900px)").matches;
      const height = vv?.height || window.innerHeight;
      const orientation = screen.orientation?.type || (window.innerWidth > window.innerHeight ? "landscape" : "portrait");
      const focused = document.activeElement === $("chat-input");
      if (orientation !== layout.orientation) { layout.orientation = orientation; layout.baseline = 0; }
      if (!focused || !layout.baseline) layout.baseline = Math.max(layout.baseline, height, window.innerHeight);
      const keyboard = active && focused && (vv?.scale || 1) < 1.05
        && layout.baseline - height > Math.max(80, layout.baseline * .15);
      // iOS keeps the document scrolled after a keyboard (e.g. login) closes, shifting the fixed chat pane under the status bar.
      if (active && !keyboard && window.scrollY) {
        window.scrollTo(0, 0);
        // The visual viewport can settle one frame later without another resize/scroll event.
        // Queue before the signature check; the extra pass must not recursively resample.
        if (resampleAfterReset !== false) viewport(false);
      }
      const offset = vv?.offsetTop || 0;
      const standaloneActive = Boolean(navigator.standalone || matchMedia("(display-mode: standalone)").matches);
      const topPadding = active ? getComputedStyle($("chat-room").closest("main")).paddingTop : "";
      const signature = [active, keyboard, standaloneActive, height, offset, topPadding, $("chat-title").parentElement.offsetHeight,
        $("chat-composer").offsetHeight, $("chat-progress").offsetHeight].join(":");
      if (signature === layout.signature) return;
      const anchor = layout.anchor, bottom = layout.bottom;
      layout.signature = signature; layout.updating = true;
      document.documentElement.classList.toggle("chat-viewport", active);
      pane.classList.toggle("chat-mobile", active);
      pane.classList.toggle("chat-keyboard", keyboard);
      pane.classList.toggle("chat-standalone", standaloneActive);
      pane.style.setProperty("--chat-vh", `${height}px`);
      pane.style.setProperty("--chat-offset-top", `${active ? offset : 0}px`);
      const input = $("chat-input"), cap = Number.parseFloat(getComputedStyle(input).maxHeight) || 150;
      input.style.height = "auto"; input.style.height = `${Math.min(cap, input.scrollHeight)}px`;
      if (app.visible) {
        if (bottom) scrollBottom();
        else if (anchor) {
          const node = [...$("chat-messages").children].find((item) => item.dataset.messageId === anchor.id);
          if (node) $("chat-scroll").scrollTop += node.getBoundingClientRect().top - $("chat-scroll").getBoundingClientRect().top - anchor.offset;
        }
        $("chat-bottom").style.bottom = `${$("chat-composer").offsetHeight + $("chat-progress").offsetHeight + 12}px`;
      }
      cancelAnimationFrame(layout.releasing);
      layout.releasing = requestAnimationFrame(() => {
        layout.updating = false;
        if (app.visible) {
          // A send/accepted event may have changed the bottom intent since this layout began.
          app.stick = layout.bottom;
          if (app.stick) scrollBottom();
          rememberAnchor();
        }
      });
    });
  }
  function submitMessage() {
    if (app.composing) { app.pendingSubmit = true; return; }
    app.pendingSubmit = false; send();
  }
  $("chat-composer").addEventListener("submit", (event) => { event.preventDefault(); submitMessage(); });
  $("chat-send").addEventListener("pointerdown", (event) => {
    if (event.button === 0 && document.activeElement === $("chat-input") && !app.composing) event.preventDefault();
  });
  $("chat-input").addEventListener("beforeinput", (event) => {
    if (!$("chat-input").isContentEditable || event.inputType.startsWith("format")) { event.preventDefault(); return; }
    if (app.composing || event.isComposing || /Composition/.test(event.inputType)) return;
    if (["insertFromPaste", "insertFromDrop"].includes(event.inputType)) {
      event.preventDefault(); insertPlainText(event.dataTransfer?.getData("text/plain") || event.data || ""); return;
    }
    const text = ["insertParagraph", "insertLineBreak"].includes(event.inputType) ? "\n" : event.data;
    if (event.inputType.startsWith("insert") && text && !insertionFits(normalizeText(text))) {
      event.preventDefault(); notice("消息最多 32000 字符，请缩短后再输入");
    }
  });
  $("chat-input").addEventListener("paste", (event) => {
    event.preventDefault(); insertPlainText(event.clipboardData?.getData("text/plain") || "");
  });
  $("chat-input").addEventListener("dragover", (event) => {
    if (!event.dataTransfer.types.includes("Files")) { event.preventDefault(); event.dataTransfer.dropEffect = "copy"; }
  });
  $("chat-input").addEventListener("drop", (event) => {
    if (event.dataTransfer.files.length) return; // The room's attachment handler owns files.
    event.preventDefault();
    const input = $("chat-input"), selection = window.getSelection();
    if (!input.isContentEditable || app.composing) return;
    const point = document.caretPositionFromPoint?.(event.clientX, event.clientY);
    let range = document.caretRangeFromPoint?.(event.clientX, event.clientY);
    if (point) { range = document.createRange(); range.setStart(point.offsetNode, point.offset); range.collapse(true); }
    if (range && input.contains(range.startContainer)) { input.focus({ preventScroll: true }); selection.removeAllRanges(); selection.addRange(range); }
    else editorEnd();
    insertPlainText(event.dataTransfer.getData("text/plain"));
  });
  $("chat-input").addEventListener("input", editorChanged);
  $("chat-input").addEventListener("focus", viewport);
  $("chat-input").addEventListener("compositionstart", () => { app.composing = true; closeCommands(); });
  $("chat-input").addEventListener("compositionend", () => {
    app.composing = false;
    const generation = app.generation;
    setTimeout(() => {
      if (!current(generation) || app.composing) return;
      // Keep oversized IME results intact until the user shortens them.
      autosize(); saveDraft(); updateCommands(); updateComposer();
      if (app.pendingSubmit) submitMessage();
    }, 0);
  });
  $("chat-input").addEventListener("keydown", (event) => {
    if (!$("chat-input").isContentEditable || app.composing || event.isComposing || event.keyCode === 229) return;
    if (!$("chat-commands").hidden) {
      if (["ArrowDown", "ArrowUp"].includes(event.key)) { event.preventDefault(); app.selected = (app.selected + (event.key === "ArrowDown" ? 1 : app.suggestions.length - 1)) % app.suggestions.length; renderCommands(); return; }
      if (event.key === "Escape") { event.preventDefault(); closeCommands(); return; }
      if (event.key === "Tab" || (event.key === "Enter" && !event.shiftKey && !mobileInput())) { event.preventDefault(); chooseCommand(); return; }
    }
    if (event.key === "Enter" && !event.shiftKey && !mobileInput()) { event.preventDefault(); submitMessage(); }
  });
  $("chat-commands").addEventListener("pointerdown", (event) => {
    const option = event.target.closest(".chat-command");
    commandTap = null;
    if (!option || event.button !== 0 || event.isPrimary === false) { commandGesture = null; return; }
    commandInteraction = true;
    commandGesture = { id: event.pointerId, x: event.clientX, y: event.clientY, index: Number(option.dataset.index), top: $("chat-commands").scrollTop, moved: false };
    // Only mouse focus needs suppressing. Touch must retain its native scroll gesture.
    if (event.pointerType === "mouse") event.preventDefault();
  });
  document.addEventListener("pointermove", (event) => {
    if (commandGesture?.id === event.pointerId && Math.hypot(event.clientX - commandGesture.x, event.clientY - commandGesture.y) > 10) commandGesture.moved = true;
  }, { passive: true });
  document.addEventListener("pointerup", (event) => {
    const gesture = commandGesture;
    if (!gesture || gesture.id !== event.pointerId) return;
    commandGesture = null;
    if (!gesture.moved && Math.hypot(event.clientX - gesture.x, event.clientY - gesture.y) <= 10 && Math.abs($("chat-commands").scrollTop - gesture.top) < 2) {
      commandTap = gesture.index;
    }
  });
  $("chat-commands").addEventListener("click", (event) => {
    const option = event.target.closest(".chat-command");
    if (option && (event.detail === 0 || commandTap === Number(option.dataset.index))) {
      // Wait for the native click so compatibility mouse events cannot focus
      // the message pane underneath a menu removed on pointerup.
      event.preventDefault(); app.selected = Number(option.dataset.index); chooseCommand();
    }
    commandTap = null;
  });
  document.addEventListener("pointercancel", () => { commandGesture = null; commandTap = null; }, { passive: true });
  document.addEventListener("pointerdown", (event) => {
    if (!(event.target instanceof Element) || !event.target.closest(".chat-input-wrap")) closeCommands();
    if (event.isPrimary === false && commandGesture) commandGesture.moved = true;
  }, true);
  $("chat-input").addEventListener("blur", () => {
    setTimeout(() => { if (!commandInteraction && document.activeElement !== $("chat-input")) closeCommands(); }, 0);
    viewport();
  });
  $("chat-add-files").addEventListener("click", () => $("chat-files").click());
  $("chat-files").addEventListener("change", () => { addFiles([...$("chat-files").files]); $("chat-files").value = ""; });
  $("chat-room").addEventListener("dragover", (event) => { event.preventDefault(); if (!app.stream) $("chat-room").classList.add("chat-dropping"); });
  $("chat-room").addEventListener("dragleave", (event) => { if (!$("chat-room").contains(event.relatedTarget)) $("chat-room").classList.remove("chat-dropping"); });
  $("chat-room").addEventListener("drop", (event) => { event.preventDefault(); $("chat-room").classList.remove("chat-dropping"); addFiles([...event.dataTransfer.files]); });
  $("chat-scroll").addEventListener("scroll", () => {
    if (layout.updating) return;
    app.stick = nearBottom(); $("chat-bottom").hidden = app.stick; rememberAnchor();
  });
  $("chat-scroll").addEventListener("pointerdown", closeCommands);
  $("chat-bottom").addEventListener("click", scrollBottom);
  $("chat-older").addEventListener("click", older);
  $("chat-stop").addEventListener("click", stop);
  NEW_BUTTONS.forEach((id) => $(id).addEventListener("click", createThread));
  $("chat-open-drawer").addEventListener("click", () => $("chat-drawer").showModal());
  $("chat-threads-toggle").addEventListener("click", () => {
    const view = $("view-chat"), toggle = $("chat-threads-toggle");
    const open = view.dataset.threads === "collapsed";
    const label = open ? "收起对话列表" : "展开对话列表";
    view.dataset.threads = open ? "open" : "collapsed";
    toggle.setAttribute("aria-expanded", String(open));
    toggle.setAttribute("aria-label", label); toggle.title = label;
  });
  $("chat-close-drawer").addEventListener("click", () => $("chat-drawer").close());
  $("chat-dialog-cancel").addEventListener("click", () => $("chat-dialog").close());
  $("chat-dialog").addEventListener("close", () => { app.dialog = null; $("chat-name").value = ""; });
  ["chat-drawer", "chat-dialog"].forEach((id) => $(id).addEventListener("click", (event) => {
    const bounds = $(id).getBoundingClientRect();
    if (event.target === $(id) && (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom)) $(id).close();
  }));
  // Swipe only on non-interactive surfaces. No preventDefault: vertical scroll,
  // long-press selection, code/table horizontal scrolling and multi-touch stay native.
  const hasSelection = () => Boolean(window.getSelection()?.toString());
  document.addEventListener("touchstart", (event) => {
    drawerGesture = null;
    if (!app.visible || !matchMedia("(max-width: 900px)").matches || event.touches.length !== 1 || $("chat-dialog").open || hasSelection()) return;
    const target = event.target;
    if (!(target instanceof Element) || !target.closest("#chat-room, #chat-drawer") || target.closest("input, textarea, select, button, a, summary, [contenteditable], pre, code, .chat-table-scroll, .chat-commands")) return;
    const touch = event.touches[0];
    drawerGesture = { x: touch.clientX, y: touch.clientY, id: touch.identifier, open: $("chat-drawer").open, time: performance.now() };
  }, { passive: true });
  document.addEventListener("touchmove", (event) => {
    const gesture = drawerGesture, touch = event.touches[0];
    if (!gesture) return;
    if (event.touches.length !== 1 || touch.identifier !== gesture.id || Math.abs(touch.clientY - gesture.y) > 24 || hasSelection()) drawerGesture = null;
  }, { passive: true });
  document.addEventListener("touchend", (event) => {
    const gesture = drawerGesture; drawerGesture = null;
    if (!gesture || event.touches.length || performance.now() - gesture.time > 500 || hasSelection()) return;
    const touch = [...event.changedTouches].find((item) => item.identifier === gesture.id);
    if (!touch) return;
    const dx = touch.clientX - gesture.x, dy = touch.clientY - gesture.y;
    if (Math.abs(dx) < 64 || Math.abs(dx) < Math.abs(dy) * 2 || Math.abs(dy) > 24 || $("chat-drawer").open !== gesture.open) return;
    if (dx > 0 && !gesture.open) $("chat-drawer").showModal();
    else if (dx < 0 && gesture.open) $("chat-drawer").close();
  }, { passive: true });
  document.addEventListener("touchcancel", () => { drawerGesture = null; }, { passive: true });
  document.addEventListener("selectionchange", () => { if (hasSelection()) drawerGesture = null; });
  const standalone = matchMedia("(display-mode: standalone)");
  const viewportMeta = document.querySelector('meta[name="viewport"]'), baseViewport = viewportMeta.content;
  function standaloneMode() {
    const active = Boolean(navigator.standalone || standalone.matches);
    document.documentElement.classList.toggle("pwa-standalone", active);
    viewportMeta.content = baseViewport + (active ? ", maximum-scale=1, user-scalable=no" : "");
  }
  standaloneMode(); standalone.addEventListener("change", standaloneMode);
  ["gesturestart", "gesturechange", "touchstart", "touchmove"].forEach((type) => document.addEventListener(type, (event) => {
    if (document.documentElement.classList.contains("pwa-standalone") && (type.startsWith("gesture") || event.touches.length > 1) && event.cancelable) event.preventDefault();
  }, { passive: false }));
  function restoreSubscription(event) {
    if (document.hidden) { clearTimeout(layout.settling); layout.settling = 0; }
    if (app.visible && !document.hidden) {
      viewport();
      if (event?.type !== "online") settleStandaloneViewport();
    }
    if (app.visible && app.authenticated && !document.hidden && !app.loading && !app.busy) recover();
  }
  document.addEventListener("visibilitychange", restoreSubscription);
  window.addEventListener("pageshow", restoreSubscription);
  window.addEventListener("online", restoreSubscription);
  window.addEventListener("resize", viewport);
  window.addEventListener("scroll", viewport, { passive: true });
  window.addEventListener("orientationchange", () => { layout.baseline = 0; viewport(); });
  window.visualViewport?.addEventListener("resize", viewport);
  window.visualViewport?.addEventListener("scroll", viewport);
  // Clearing input/chips/progress after a send shrinks the composer without any resize event.
  const composerObserver = new ResizeObserver(viewport);
  composerObserver.observe($("chat-composer")); composerObserver.observe($("chat-progress"));
  window.FerryChat = { open, leave, refresh() {
    if (app.creationPending) notice("正在创建…，请创建完成后再刷新");
    else if (!app.busy) open(app.thread?.id, "replace", true);
    else notice("当前操作尚未完成，请稍后刷新");
  } };
  document.addEventListener("ferry:auth", (event) => {
    leave(); app.authenticated = Boolean(event.detail?.authenticated && event.detail?.session?.chat);
    const displayName = app.authenticated ? event.detail?.session?.user_display_name : null;
    app.userDisplayName = typeof displayName === "string" && displayName.trim() ? displayName : "我";
    app.csrf = app.authenticated ? event.detail?.session?.chat_csrf_token || "" : "";
    if (!app.authenticated) {
      app.drafts.forEach(releaseDraft); app.drafts.clear(); app.urls.forEach((url) => URL.revokeObjectURL(url)); app.urls.clear();
      app.threads = []; app.commands = []; app.initialized = false; app.hasMore = false; app.composing = false;
      ["chat-title", "chat-notice", "chat-drawer-notice", "chat-thread-list", "chat-drawer-list", "chat-dialog-title", "chat-dialog-message", "chat-dialog-error"].forEach((id) => $(id).replaceChildren());
      $("chat-files").value = ""; $("chat-name").value = ""; $("chat-bottom").hidden = true; $("chat-older").hidden = true;
      $("chat-room").classList.remove("chat-dropping"); updateComposer();
    }
  });
  document.addEventListener("ferry:appearance", () => {
    if (!app.visible) return;
    [...$("chat-messages").children].forEach((node) => { const view = messageViews.get(node); if (view) updateMessageBrand(view); });
  });
})();
