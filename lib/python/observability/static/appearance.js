"use strict";
(() => {
  const API = "/api/observability/v1", PRIVATE = `${API}/chat/appearance`;
  const $ = (id) => document.getElementById(id);
  const SIZES = ["180", "192", "512", "maskable"];
  const DEFAULT_ICONS = Object.fromEntries(SIZES.map((size) => [size, `/static/console-icon-${size}.png`]));
  const MAX_AVATAR = 2 * 1024 * 1024, MAX_SOURCE = 20 * 1024 * 1024, MAX_PIXELS = 32_000_000, MAX_DIMENSION = 8192;
  const state = { brand: null, authenticated: false, csrf: "", generation: 0, epoch: 0, publicRead: 0,
    controllers: new Set(), ready: false, busy: false, dirty: false, file: null, preview: null, removeAvatar: false, decoding: false,
    haptics: true, draftHaptics: null, viewTitle: "", composing: false, lastHaptic: -Infinity };

  function validName(value) {
    return typeof value === "string" && Array.from(value.trim()).length >= 1 && Array.from(value.trim()).length <= 32;
  }
  function safeIcon(value, size) {
    // Never accept an arbitrary URL, even if a server response supplies one.
    return typeof value === "string" && new RegExp(`^/static/console-icon-${size}\\.png(?:\\?v=[a-f0-9]{1,128})?$`, "i").test(value)
      ? value : DEFAULT_ICONS[size];
  }
  function snapshot(data) {
    return { name: validName(data?.name) ? data.name.trim() : "Ferry",
      revision: typeof data?.revision === "string" ? data.revision.slice(0, 128) : "",
      avatar: data?.avatar === true,
      icons: Object.fromEntries(SIZES.map((size) => [size, safeIcon(data?.icons?.[size], size)])) };
  }
  function title() {
    document.title = `${state.viewTitle ? `${state.viewTitle} · ` : ""}${state.brand.name} 控制台`;
  }
  function setView(viewTitle = "") { state.viewTitle = viewTitle; title(); }
  function applyBrand(data) {
    state.brand = snapshot(data);
    const { name, icons } = state.brand;
    document.querySelectorAll("[data-app-name]:not(body), .brand-name, .login-brand > span").forEach((node) => { node.textContent = name; });
    $("login-title").textContent = `${name} 个人助手`;
    document.querySelectorAll(".brand > img, .login-brand > img").forEach((node) => { if (node.getAttribute("src") !== icons["192"]) node.src = icons["192"]; });
    document.querySelector('meta[name="apple-mobile-web-app-title"]').content = name;
    document.querySelector('link[rel="icon"]').href = icons["192"];
    document.querySelector('link[rel="apple-touch-icon"]').href = icons["180"];
    title();
    $("appearance-current").textContent = `当前生效：${name} · ${state.brand.avatar ? "自定义头像" : "默认图标"}`;
    if (!state.preview && !state.removeAvatar) $("appearance-preview").src = icons["192"];
    document.dispatchEvent(new CustomEvent("ferry:appearance", { detail: { ...state.brand, icons: { ...icons } } }));
  }
  function status(message = "", error = false) {
    $("appearance-status").textContent = message;
    $("appearance-status").dataset.error = String(error);
  }
  const current = (generation) => state.authenticated && generation === state.generation;
  function revokePreview() {
    if (state.preview) URL.revokeObjectURL(state.preview);
    state.preview = null;
  }
  function avatarDraft() {
    $("appearance-preview").src = state.preview || (state.removeAvatar ? DEFAULT_ICONS["192"] : state.brand.icons["192"]);
    $("appearance-file-note").textContent = state.file ? `${state.file.name} · ${Math.ceil(state.file.size / 1024)} KiB · 待保存` : state.removeAvatar ? "保存后恢复默认图标" : "源图最多 20 MiB / 3200 万像素 / 单边 8192；本地转为 PNG 或 JPEG，保存后才上传";
    $("appearance-remove-avatar").hidden = state.removeAvatar || (!state.file && !state.brand.avatar);
    $("appearance-undo-avatar").hidden = !state.file && !state.removeAvatar;
  }
  function controls() {
    const busy = state.busy || state.decoding;
    $("appearance-fields").disabled = !state.authenticated || !state.csrf || !state.ready || busy;
    $("appearance-refresh").disabled = !state.authenticated || !state.csrf || busy;
    $("appearance-form").setAttribute("aria-busy", String(busy));
    $("appearance-save").textContent = state.decoding ? "正在处理图片…" : state.busy ? "正在保存 / 读取…" : "保存外观";
    $("appearance-reset-accept").disabled = busy || !state.ready;
    $("appearance-reset-cancel").disabled = busy;
    $("appearance-test").disabled = busy || !state.ready || !$("appearance-haptics").checked;
  }
  function clearDraft() {
    revokePreview(); state.file = null; state.removeAvatar = false; state.dirty = false; state.draftHaptics = null;
    $("appearance-file").value = "";
    $("appearance-name").value = state.brand.name;
    $("appearance-haptics").checked = state.haptics;
    $("appearance-test-status").textContent = "";
    $("appearance-reset-confirm").hidden = true;
    avatarDraft();
  }
  function applyPrivate(data, resetDraft = false) {
    if (!data || !validName(data.name) || typeof data.avatar !== "boolean" || !data.icons) throw new Error("外观接口数据格式不兼容");
    state.epoch += 1; // A public read started before this commit must not overwrite it.
    state.haptics = typeof data.haptics_enabled === "boolean" ? data.haptics_enabled : true;
    state.ready = true; applyBrand(data);
    if (resetDraft || !state.dirty) clearDraft(); else avatarDraft();
    controls();
  }
  async function request(path, options = {}, privateRead = true, generation = state.generation) {
    if (privateRead && !current(generation)) throw new DOMException("Inactive appearance request", "AbortError");
    if (privateRead && !state.csrf) throw new Error("安全令牌缺失，请刷新页面后重试");
    const controller = new AbortController(), headers = new Headers(options.headers);
    if (privateRead) headers.set("X-Ferry-CSRF", state.csrf);
    state.controllers.add(controller);
    const timeout = setTimeout(() => controller.abort(), 30000);
    try {
      const response = await fetch(path, { ...options, headers, mode: "same-origin", credentials: "same-origin", cache: "no-store", redirect: "error", signal: controller.signal });
      const data = await response.json().catch(() => null);
      if (generation !== state.generation || (privateRead && !current(generation))) throw new DOMException("Stale appearance response", "AbortError");
      if (!response.ok) {
        const error = new Error(typeof data?.detail === "string" ? data.detail : typeof data?.message === "string" ? data.message : ({ 401: "登录已失效", 403: "安全校验失败，请刷新页面重试", 413: "头像不能超过 2 MiB", 422: "名称或图片格式无效" }[response.status] || "外观请求失败，请重试"));
        error.status = response.status;
        if (privateRead && response.status === 401) window.FerryMonitor?.signOut();
        throw error;
      }
      if (!data) throw new Error("外观接口没有返回有效快照");
      return data;
    } finally { clearTimeout(timeout); state.controllers.delete(controller); }
  }
  async function refreshBrand() {
    const read = ++state.publicRead, epoch = state.epoch, generation = state.generation;
    try {
      const data = await request(`${API}/brand`, {}, false, generation);
      if (read === state.publicRead && epoch === state.epoch && generation === state.generation) applyBrand(data);
    } catch { /* Escaped server bootstrap/default icons remain usable, including offline. */ }
  }
  async function loadPrivate() {
    if (!state.authenticated || !state.csrf || state.busy || state.decoding) return;
    const generation = state.generation;
    state.busy = true; controls(); status("正在读取外观…");
    try { applyPrivate(await request(PRIVATE, {}, true, generation)); if (current(generation)) status(state.dirty ? "已读取当前生效外观，未保存的草稿保留" : ""); }
    catch (error) { if (current(generation)) status(error.name === "AbortError" ? "读取超时，请重新读取" : error.message, true); }
    finally { if (current(generation)) { state.busy = false; controls(); } }
  }
  const json = (body) => ({ method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  async function save(event) {
    event.preventDefault();
    if (!state.ready || state.busy || state.decoding || !state.authenticated) return;
    const name = $("appearance-name").value.trim();
    if (!validName(name)) { status("请输入 1–32 个字符的助手名称；草稿未改动", true); return; }
    const generation = state.generation, file = state.file, remove = state.removeAvatar;
    let stage = "名称与触感", committed = false;
    state.busy = true; controls(); status("正在保存名称与触感…");
    try {
      applyPrivate(await request(PRIVATE, json({ name, haptics_enabled: $("appearance-haptics").checked }), true, generation));
      committed = true;
      if (file || remove) {
        stage = "头像"; status(file ? "名称与触感已保存，正在上传头像…" : "名称与触感已保存，正在移除头像…");
        const options = file ? { method: "PUT", headers: { "Content-Type": file.type }, body: file } : { method: "DELETE" };
        applyPrivate(await request(`${PRIVATE}/avatar`, options, true, generation));
      }
      clearDraft(); status("已保存外观");
    } catch (error) {
      if (!current(generation)) return;
      const failure = `${committed ? "名称与触感已保存；" : ""}${stage}未确认：${error.name === "AbortError" ? "请求超时" : error.message}。草稿保留，未自动重试。`;
      // A network failure can follow a successful commit; show canonical state, not a half-success claim.
      let reconciled = false;
      if (error.status !== 401) {
        try { applyPrivate(await request(PRIVATE, {}, true, generation)); reconciled = true; } catch { /* Keep the last confirmed snapshot and dirty draft. */ }
      }
      if (current(generation)) status(`${failure}${reconciled ? "上方为重新读取的当前生效外观。" : "无法重新确认当前外观，请稍后重新读取。"}`, true);
    } finally { if (current(generation)) { state.busy = false; controls(); } }
  }
  async function reset() {
    if (!state.ready || state.busy || state.decoding || !state.authenticated || $("appearance-reset-confirm").hidden) return;
    const generation = state.generation;
    state.busy = true; controls(); status("正在恢复默认…");
    try { applyPrivate(await request(PRIVATE, { method: "DELETE" }, true, generation), true); status("已恢复默认名称、图标与触感设置"); }
    catch (error) {
      if (!current(generation)) return;
      let reconciled = false;
      if (error.status !== 401) { try { applyPrivate(await request(PRIVATE, {}, true, generation)); reconciled = true; } catch { /* Draft remains available. */ } }
      if (current(generation)) status(`恢复默认未确认：${error.name === "AbortError" ? "请求超时" : error.message}；草稿保留，未自动重试。${reconciled ? "上方为当前生效外观。" : "请重新读取确认。"}`, true);
    } finally { if (current(generation)) { state.busy = false; controls(); } }
  }

  // iOS has no generic vibrate API; the visible switch is not a hidden haptic proxy.
  function haptic(event) {
    if (!event?.isTrusted || !(event.detail > 0) || event.button !== 0 || event.isComposing || state.composing) return "gesture";
    if (!state.authenticated || !state.ready || !(state.draftHaptics ?? state.haptics)) return "disabled";
    const now = performance.now();
    if (now - state.lastHaptic < 80) return "limited";
    state.lastHaptic = now;
    if (typeof navigator.vibrate === "function") {
      try { if (navigator.vibrate(10) !== false) return "attempted"; } catch { /* Unsupported or blocked by the browser. */ }
    }
    return "unsupported";
  }
  const ACTIONS = "[data-haptic], [data-nav], [data-tab], [data-usage-range], [data-heat-span], #chat-send, #chat-stop, #chat-add-files, #chat-new, #chat-new-header, #chat-new-mobile, #chat-open-drawer, #chat-close-drawer, #chat-bottom, #chat-older, #chat-dialog-confirm, #view-refresh, #usage-refresh, #refresh, #sheet-confirm";
  document.addEventListener("click", (event) => {
    const action = event.target instanceof Element ? event.target.closest(ACTIONS) : null;
    if (!action || action.disabled || action.getAttribute("aria-disabled") === "true") return;
    haptic(event);
  }, true);
  document.addEventListener("compositionstart", () => { state.composing = true; }, true);
  document.addEventListener("compositionend", () => { state.composing = false; }, true);
  $("appearance-test").addEventListener("click", (event) => {
    const result = haptic(event);
    $("appearance-test-status").textContent = { attempted: "已尝试，不代表设备马达实际反馈", unsupported: "当前浏览器不支持通用振动（iOS 无此 API）；可见开关的反馈由系统决定", disabled: "触感已关闭或尚未登录", limited: "轻点过于频繁，请稍后手动再试", gesture: "请直接轻点按钮；键盘或脚本不会触发" }[result];
  });
  $("appearance-name").addEventListener("input", () => { state.dirty = true; });
  $("appearance-haptics").addEventListener("change", () => {
    state.dirty = true; state.draftHaptics = $("appearance-haptics").checked;
    $("appearance-test-status").textContent = state.draftHaptics ? "保存后保留偏好；可轻点手动尝试" : "已停止本页的触感尝试；保存后保留关闭偏好";
    controls();
  });
  function checkPixels(width, height) {
    if (!width || !height || width > MAX_DIMENSION || height > MAX_DIMENSION || width * height > MAX_PIXELS) throw new Error("源图不能超过 3200 万像素或单边 8192，请先缩小图片");
  }
  function sourceImage(bytes) {
    const view = new DataView(bytes.buffer), text = (offset, size) => String.fromCharCode(...bytes.subarray(offset, offset + size));
    // Inspect compressed headers before allocating a raster for common formats.
    if (bytes.length >= 24 && view.getUint32(0) === 0x89504e47 && view.getUint32(4) === 0x0d0a1a0a) return { type: "image/png", width: view.getUint32(16), height: view.getUint32(20) };
    if (bytes[0] === 0xff && bytes[1] === 0xd8) {
      let offset = 2;
      while (offset + 4 <= bytes.length && bytes[offset] === 0xff) {
        while (bytes[offset] === 0xff) offset += 1;
        const marker = bytes[offset++];
        if (offset + 2 > bytes.length || marker === 0xda || marker === 0xd9) break;
        const length = view.getUint16(offset);
        if (length < 2 || offset + length > bytes.length) break;
        if (marker >= 0xc0 && marker <= 0xcf && ![0xc4, 0xc8, 0xcc].includes(marker) && length >= 7) return { type: "image/jpeg", width: view.getUint16(offset + 5), height: view.getUint16(offset + 3) };
        offset += length;
      }
    }
    if (bytes.length >= 30 && text(0, 4) === "RIFF" && text(8, 4) === "WEBP") {
      const kind = text(12, 4), u24 = (offset) => bytes[offset] + (bytes[offset + 1] << 8) + (bytes[offset + 2] << 16);
      if (kind === "VP8X") return { type: "image/webp", width: u24(24) + 1, height: u24(27) + 1 };
      if (kind === "VP8 ") return { type: "image/webp", width: view.getUint16(26, true) & 0x3fff, height: view.getUint16(28, true) & 0x3fff };
      if (kind === "VP8L" && bytes[20] === 0x2f) {
        const bits = view.getUint32(21, true);
        return { type: "image/webp", width: (bits & 0x3fff) + 1, height: ((bits >>> 14) & 0x3fff) + 1 };
      }
    }
    if (bytes.length >= 16 && text(4, 4) === "ftyp" && /^(heic|heix|hevc|hevx|mif1|msf1)$/.test(text(8, 4))) return { type: "image/heic" };
    throw new Error("请选择有效的 PNG / JPEG / WebP 或 HEIC 图片");
  }
  async function prepareAvatar(file) {
    if (!file.size || file.size > MAX_SOURCE) throw new Error("请选择非空图片，源文件最多 20 MiB");
    const source = sourceImage(new Uint8Array(await file.arrayBuffer()));
    if (source.width !== undefined) checkPixels(source.width, source.height);
    const image = new Image(), url = URL.createObjectURL(file), canvas = document.createElement("canvas");
    let timeout;
    try {
      await new Promise((resolve, reject) => {
        const failure = () => reject(new Error(source.type === "image/heic" ? "此浏览器无法解码 HEIC / HEIF，请先在照片中导出 JPEG 或 PNG" : "图片无法解码，请重新导出 PNG 或 JPEG"));
        image.onload = resolve; image.onerror = failure; timeout = setTimeout(failure, 30000); image.src = url;
      });
      checkPixels(image.naturalWidth, image.naturalHeight);
      const type = source.type === "image/png" ? "image/png" : "image/jpeg";
      let edge = 1024;
      for (let attempt = 0; attempt < 5; attempt += 1) {
        const scale = Math.min(1, edge / Math.max(image.naturalWidth, image.naturalHeight));
        canvas.width = Math.max(1, Math.round(image.naturalWidth * scale)); canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
        const context = canvas.getContext("2d");
        if (!context) throw new Error("浏览器无法处理图片，请先缩小并导出 PNG 或 JPEG");
        if (type === "image/jpeg") { context.fillStyle = "#fff"; context.fillRect(0, 0, canvas.width, canvas.height); }
        context.drawImage(image, 0, 0, canvas.width, canvas.height);
        const blob = await new Promise((resolve) => canvas.toBlob(resolve, type, .86));
        if (!blob || !["image/png", "image/jpeg"].includes(blob.type)) throw new Error("图片转换失败，请先导出 PNG 或 JPEG");
        if (blob.size > 0 && blob.size <= MAX_AVATAR) return new File([blob], `${file.name.replace(/\.[^.]+$/, "") || "avatar"}.${blob.type === "image/png" ? "png" : "jpg"}`, { type: blob.type });
        edge = Math.floor(edge * .75);
      }
      throw new Error("图片缩小后仍超过 2 MiB，请另选图片");
    } finally {
      clearTimeout(timeout); image.onload = null; image.onerror = null; image.src = ""; URL.revokeObjectURL(url); canvas.width = canvas.height = 1;
    }
  }
  $("appearance-file").addEventListener("change", async () => {
    if (state.busy || state.decoding || !state.ready) return;
    const file = $("appearance-file").files[0]; $("appearance-file").value = "";
    if (!file) return;
    const generation = state.generation;
    state.decoding = true; controls(); status("正在本地解码并缩小图片，尚未上传…");
    try {
      const prepared = await prepareAvatar(file);
      if (!current(generation)) return;
      const preview = URL.createObjectURL(prepared);
      revokePreview(); state.file = prepared; state.preview = preview; state.removeAvatar = false; state.dirty = true;
      avatarDraft(); status("图片已在本地处理，点击保存后才会公开");
    } catch (error) { if (current(generation)) status(`${error.message}；原草稿保留`, true); }
    finally { if (current(generation)) { state.decoding = false; controls(); } }
  });
  $("appearance-remove-avatar").addEventListener("click", () => {
    if (state.busy || state.decoding || !state.ready) return;
    revokePreview(); state.file = null; state.removeAvatar = true; state.dirty = true; avatarDraft(); status("点击保存后移除头像");
  });
  $("appearance-undo-avatar").addEventListener("click", () => {
    if (state.busy || state.decoding || !state.ready) return;
    revokePreview(); state.file = null; state.removeAvatar = false; avatarDraft(); status("头像修改已撤销，名称与触感草稿保留");
  });
  $("appearance-form").addEventListener("submit", save);
  $("appearance-refresh").addEventListener("click", loadPrivate);
  $("appearance-reset").addEventListener("click", () => { if (!state.busy) $("appearance-reset-confirm").hidden = false; });
  $("appearance-reset-cancel").addEventListener("click", () => { $("appearance-reset-confirm").hidden = true; });
  $("appearance-reset-accept").addEventListener("click", reset);
  document.addEventListener("ferry:auth", (event) => {
    state.generation += 1; state.epoch += 1;
    state.controllers.forEach((controller) => controller.abort()); state.controllers.clear();
    state.authenticated = Boolean(event.detail?.authenticated);
    state.csrf = state.authenticated ? event.detail?.session?.chat_csrf_token || "" : "";
    state.ready = false; state.busy = false; state.decoding = false; state.haptics = true; state.composing = false; state.lastHaptic = -Infinity;
    clearDraft(); controls();
    if (!state.authenticated) { setView(); status(""); refreshBrand(); }
    else if (state.csrf) loadPrivate();
    else status("外观编辑需要安全令牌，请刷新页面后重试", true);
  });
  const attrs = document.body.dataset;
  state.brand = snapshot({ name: attrs.appName, revision: attrs.appRevision, avatar: attrs.appAvatar === "true",
    icons: Object.fromEntries(SIZES.map((size) => [size, document.body.getAttribute(`data-icon-${size}`)])) });
  window.FerryAppearance = { name: () => state.brand.name, icon: (size = "192") => state.brand.icons[String(size)] || DEFAULT_ICONS["192"], avatar: () => state.brand.avatar,
    refresh: () => state.authenticated && state.csrf ? loadPrivate() : refreshBrand(), setView };
  applyBrand(state.brand); clearDraft(); controls(); refreshBrand();
})();
