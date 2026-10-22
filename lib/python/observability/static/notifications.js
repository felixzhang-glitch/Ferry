"use strict";
(() => {
  const API = "/api/observability/v1/chat/notifications";
  const $ = (id) => document.getElementById(id);
  const state = { authenticated: false, csrf: "", enabled: false, subscribed: false, key: "", busy: false, generation: 0 };
  const capable = () => window.isSecureContext && "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
  function status(text) { $("notification-status").textContent = text; }
  function controls() {
    $("notification-enable").disabled = state.busy || !state.authenticated || !state.enabled || !capable();
    $("notification-disable").disabled = state.busy || !state.subscribed;
    $("notification-test").disabled = state.busy || !state.subscribed;
    $("notification-scheduled").disabled = state.busy || !state.authenticated;
  }
  async function request(path = "", method = "GET", body) {
    const generation = state.generation;
    if (!state.authenticated || !state.csrf) throw new Error("请先登录");
    const headers = { "X-Ferry-CSRF": state.csrf };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const response = await fetch(API + path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "same-origin", mode: "same-origin", cache: "no-store", redirect: "error", signal: AbortSignal.timeout(15000) });
    if (generation !== state.generation) throw new DOMException("Inactive request", "AbortError");
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "通知操作失败，请重试");
    return data;
  }
  async function registration() {
    const existing = await navigator.serviceWorker.getRegistration("/observability/");
    if (!existing?.active) throw new Error("应用尚未完成离线组件安装，请稍后重新打开");
    return existing;
  }
  function keyBytes(value) {
    const raw = atob(value.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - value.length % 4) % 4));
    return Uint8Array.from(raw, (char) => char.charCodeAt(0));
  }
  async function saveSubscription(subscription) {
    const result = await request("/subscription", "PUT", { subscription: subscription.toJSON(), scheduled: $("notification-scheduled").checked });
    state.subscribed = result.subscribed;
    status("通知已开启；锁屏显示简短提醒，点击回到会话查看全文。");
  }
  async function refresh() {
    const generation = state.generation;
    try {
      const data = await request();
      state.enabled = data.enabled === true; state.key = data.public_key || ""; state.subscribed = data.subscribed === true;
      if (state.subscribed) $("notification-scheduled").checked = data.scheduled === true;
      if (!state.enabled) { status("服务器尚未配置 Web Push，请配置后重启主服务。"); return; }
      if (!capable()) { status("当前环境不支持通知；iPhone 请通过 Safari 添加到主屏幕后打开，并使用 HTTPS。"); return; }
      if (Notification.permission === "denied") { status("通知权限已被拒绝，请在系统通知设置中重新允许。"); return; }
      const reg = await registration(), subscription = await reg.pushManager.getSubscription();
      if (generation !== state.generation) return;
      if (subscription && Notification.permission === "granted") await saveSubscription(subscription);
      else { state.subscribed = false; status("点击“启用通知”授权，接收后台回复、提醒与每日任务。"); }
    } catch (error) { if (generation === state.generation) status(error.message); }
    finally { if (generation === state.generation) controls(); }
  }
  async function enable() {
    if (state.busy || !state.enabled || !capable()) return;
    const generation = state.generation;
    state.busy = true; controls();
    try {
      const permission = await Notification.requestPermission();
      if (generation !== state.generation) return;
      if (permission !== "granted") { status(permission === "denied" ? "通知已拒绝，请到系统设置中允许。" : "尚未授权，可稍后再开启。"); return; }
      const reg = await registration();
      let subscription = await reg.pushManager.getSubscription();
      if (generation !== state.generation) return;
      subscription ||= await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(state.key) });
      if (generation !== state.generation) { await subscription.unsubscribe(); return; }
      await saveSubscription(subscription);
    } catch (error) { if (generation === state.generation) status(`未能开启通知：${error.message}`); }
    finally { if (generation === state.generation) { state.busy = false; controls(); } }
  }
  $("notification-enable").addEventListener("click", enable);
  $("notification-disable").addEventListener("click", async () => {
    if (state.busy) return;
    const generation = state.generation;
    state.busy = true; controls();
    try {
      await request("/subscription", "DELETE");
      const subscription = capable() ? await (await registration()).pushManager.getSubscription() : null;
      if (subscription) await subscription.unsubscribe();
      if (generation === state.generation) { state.subscribed = false; status("此设备通知已关闭。"); }
    } catch (error) { if (generation === state.generation) status(error.message); }
    finally { if (generation === state.generation) { state.busy = false; controls(); } }
  });
  $("notification-test").addEventListener("click", async () => {
    try { await request("/test", "POST"); status("测试通知已排队；是否显示取决于系统通知与专注模式设置。"); }
    catch (error) { status(error.message); }
  });
  $("notification-scheduled").addEventListener("change", async () => {
    if (!state.subscribed) return;
    try {
      const subscription = await (await registration()).pushManager.getSubscription();
      if (subscription) await saveSubscription(subscription);
    } catch (error) { status(error.message); }
  });
  function presence() {
    if (!state.subscribed || !state.authenticated) return;
    const match = /^#chat\/([a-f0-9]{32})$/.exec(location.hash);
    const thread_id = document.visibilityState === "visible" && match ? match[1] : null;
    request("/presence", "POST", { thread_id }).catch(() => {});
  }
  document.addEventListener("ferry:chat-read", (event) => {
    const detail = event.detail;
    if (!state.enabled || !state.authenticated || document.hidden || !/^[a-f0-9]{32}$/.test(detail?.thread_id) || !/^[a-f0-9]{32}$/.test(detail?.finished_turn_id)) return;
    request("/presence", "POST", detail).catch(() => {});
  });
  document.addEventListener("visibilitychange", presence);
  window.addEventListener("hashchange", presence);
  window.addEventListener("pageshow", presence);
  window.addEventListener("online", () => { if (state.authenticated) refresh(); });
  setInterval(presence, 20000);
  navigator.serviceWorker?.addEventListener("message", (event) => {
    if (event.data?.type === "ferry-notification" && /^[a-f0-9]{32}$/.test(event.data.thread_id)) {
      location.hash = `chat/${event.data.thread_id}`;
    }
  });
  document.addEventListener("ferry:auth", (event) => {
    state.generation += 1;
    state.authenticated = Boolean(event.detail?.authenticated && event.detail?.session?.chat);
    state.csrf = state.authenticated ? event.detail.session.chat_csrf_token || "" : "";
    state.enabled = false; state.subscribed = false; state.busy = false; state.key = "";
    controls(); status(state.authenticated ? "正在读取通知状态…" : "登录后可配置通知。");
    if (state.authenticated) refresh();
  });
  controls();
})();
