#!/usr/bin/env node
import crypto from "node:crypto";
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

const FIXED_BASE_URL = "https://ilinkai.weixin.qq.com";
const BOT_TYPE = process.env.WECHAT_BOT_TYPE || "3";
const CHANNEL_VERSION = process.env.WECHAT_CHANNEL_VERSION || "2.4.3";
const ILINK_APP_ID = process.env.WECHAT_ILINK_APP_ID || "bot";
const CLIENT_VERSION = String(buildClientVersion(CHANNEL_VERSION));

const ROOT_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const ACCOUNT_PATH = path.resolve(process.env.WECHAT_ACCOUNT_PATH || path.join(ROOT_DIR, "conf/wechat/account.json"));
const SYNC_BUF_PATH = path.resolve(
  process.env.WECHAT_SYNC_BUF_PATH || path.join(ROOT_DIR, "runtime/wechat/get_updates_buf.txt"),
);
const CODEXCLAW_BASE_URL = (process.env.CODEXCLAW_BASE_URL || "http://127.0.0.1:8080").replace(/\/+$/, "");
const CODEXCLAW_WEBHOOK_PATH = process.env.CODEXCLAW_WECHAT_WEBHOOK_PATH || "/webhook/wechat";
const WEBHOOK_TOKEN = process.env.WECHAT_WEBHOOK_TOKEN || "";
const SIDECAR_HOST = process.env.WECHAT_SIDECAR_HOST || "127.0.0.1";
const SIDECAR_PORT = Number(process.env.WECHAT_SIDECAR_PORT || "8787");
const BOT_AGENT = process.env.WECHAT_BOT_AGENT || "CodexClaw/0.1";
const LONG_POLL_TIMEOUT_MS = Number(process.env.WECHAT_LONG_POLL_TIMEOUT_MS || "35000");
const CODEXCLAW_TIMEOUT_MS = Number(process.env.CODEXCLAW_WECHAT_TIMEOUT_MS || "300000");
const FILE_ARCHIVE_DIR = path.resolve(process.env.FILE_ARCHIVE_DIR || "/data/file");
const CDN_BASE_URL = (process.env.WECHAT_CDN_BASE_URL || "https://novac2c.cdn.weixin.qq.com/c2c").replace(/\/+$/, "");
const CDN_UPLOAD_TIMEOUT_MS = Number(process.env.WECHAT_CDN_TIMEOUT_MS || 120000);
const MAX_OUTBOUND_FILE_BYTES = Number(process.env.WECHAT_MAX_FILE_MB || 50) * 1024 * 1024;

// iLink uses two different enums for the same concept: getuploadurl takes
// UploadMediaType (FILE=3) while sendmessage takes MessageItemType (FILE=4).
const UPLOAD_MEDIA_TYPE_FILE = 3;
const MESSAGE_ITEM_TYPE_TEXT = 1;
const MESSAGE_ITEM_TYPE_IMAGE = 2;
const MESSAGE_ITEM_TYPE_FILE = 4;

const contextTokens = new Map();
const activeInbound = new Set();
const CONTEXT_TOKENS_PATH = path.resolve(
  process.env.WECHAT_CONTEXT_TOKENS_PATH || path.join(ROOT_DIR, "runtime/wechat/context_tokens.json"),
);

function loadContextTokens() {
  try {
    const raw = fs.readFileSync(CONTEXT_TOKENS_PATH, "utf8");
    const data = JSON.parse(raw);
    if (data && typeof data === "object") {
      for (const [key, token] of Object.entries(data)) {
        if (typeof token === "string" && token) contextTokens.set(key, token);
      }
      console.log(`context tokens loaded count=${contextTokens.size}`);
    }
  } catch (error) {
    if (error?.code !== "ENOENT") console.error(`failed to load context tokens: ${String(error)}`);
  }
}

let contextTokensSaveTimer = null;

function saveContextTokens() {
  if (contextTokensSaveTimer) return;
  contextTokensSaveTimer = setTimeout(() => {
    contextTokensSaveTimer = null;
    try {
      fs.mkdirSync(path.dirname(CONTEXT_TOKENS_PATH), { recursive: true });
      const tmpPath = `${CONTEXT_TOKENS_PATH}.tmp`;
      fs.writeFileSync(tmpPath, JSON.stringify(Object.fromEntries(contextTokens)), "utf8");
      fs.renameSync(tmpPath, CONTEXT_TOKENS_PATH);
    } catch (error) {
      console.error(`failed to save context tokens: ${String(error)}`);
    }
  }, 1000);
}

function setContextToken(key, token) {
  if (contextTokens.size > 1000) {
    const firstKey = contextTokens.keys().next().value;
    contextTokens.delete(firstKey);
  }
  contextTokens.set(key, token);
  saveContextTokens();
}

process.on("unhandledRejection", (error) => {
  console.error(`unhandledRejection: ${String(error)}`);
});
process.on("uncaughtException", (error) => {
  console.error(`uncaughtException: ${String(error)}`);
  process.exit(1);
});

function usage() {
  console.log(`Usage:
  node lib/js/wechat-sidecar.mjs login
  node lib/js/wechat-sidecar.mjs run

Environment:
  CODEXCLAW_BASE_URL=http://127.0.0.1:8080
  WECHAT_WEBHOOK_TOKEN=<same as CodexClaw conf/.env>
  WECHAT_ACCOUNT_PATH=./conf/wechat/account.json
  WECHAT_SIDECAR_HOST=127.0.0.1
  WECHAT_SIDECAR_PORT=8787`);
}

function buildClientVersion(version) {
  const parts = String(version).split(".").map((p) => Number.parseInt(p, 10) || 0);
  return ((parts[0] & 0xff) << 16) | ((parts[1] & 0xff) << 8) | (parts[2] & 0xff);
}

function ensureTrailingSlash(rawUrl) {
  return rawUrl.endsWith("/") ? rawUrl : `${rawUrl}/`;
}

function commonHeaders() {
  return {
    "iLink-App-Id": ILINK_APP_ID,
    "iLink-App-ClientVersion": CLIENT_VERSION,
  };
}

function randomWechatUin() {
  const uint32 = crypto.randomBytes(4).readUInt32BE(0);
  return Buffer.from(String(uint32), "utf8").toString("base64");
}

function jsonHeaders(token = "") {
  const headers = {
    "Content-Type": "application/json",
    AuthorizationType: "ilink_bot_token",
    "X-WECHAT-UIN": randomWechatUin(),
    ...commonHeaders(),
  };
  if (token) headers.Authorization = `Bearer ${token}`;
  return headers;
}

async function fetchJson(method, baseUrl, endpoint, body, opts = {}) {
  const url = new URL(endpoint, ensureTrailingSlash(baseUrl));
  const timeoutMs = opts.timeoutMs ?? 15000;
  const controller = timeoutMs > 0 ? new AbortController() : undefined;
  const timer = controller ? setTimeout(() => controller.abort(), timeoutMs) : undefined;
  try {
    const response = await fetch(url, {
      method,
      headers: method === "GET" ? commonHeaders() : jsonHeaders(opts.token || ""),
      body: method === "GET" ? undefined : JSON.stringify(body || {}),
      signal: controller?.signal,
    });
    const text = await response.text();
    if (!response.ok) {
      throw new Error(`${method} ${endpoint} ${response.status}: ${text.slice(0, 300)}`);
    }
    return text ? JSON.parse(text) : {};
  } finally {
    if (timer) clearTimeout(timer);
  }
}

function baseInfo() {
  return {
    channel_version: CHANNEL_VERSION,
    bot_agent: BOT_AGENT,
  };
}

function ensureParentDir(filePath) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
}

function readAccount() {
  if (!fs.existsSync(ACCOUNT_PATH)) {
    throw new Error(`missing account file: ${ACCOUNT_PATH}; run login first`);
  }
  const account = JSON.parse(fs.readFileSync(ACCOUNT_PATH, "utf8"));
  if (!account.token || !account.accountId || !account.baseUrl) {
    throw new Error(`invalid account file: ${ACCOUNT_PATH}`);
  }
  return account;
}

function saveAccount(account) {
  ensureParentDir(ACCOUNT_PATH);
  fs.writeFileSync(ACCOUNT_PATH, JSON.stringify(account, null, 2), { mode: 0o600 });
}

function readSyncBuf() {
  try {
    return fs.existsSync(SYNC_BUF_PATH) ? fs.readFileSync(SYNC_BUF_PATH, "utf8") : "";
  } catch {
    return "";
  }
}

function saveSyncBuf(value) {
  ensureParentDir(SYNC_BUF_PATH);
  fs.writeFileSync(SYNC_BUF_PATH, value || "", { mode: 0o600 });
}

function redact(value) {
  const text = String(value || "");
  if (text.length <= 14) return text ? "***" : "";
  return `${text.slice(0, 8)}...${text.slice(-6)}`;
}

async function login() {
  console.log("requesting QR code...");
  const qr = await fetchJson(
    "POST",
    FIXED_BASE_URL,
    `ilink/bot/get_bot_qrcode?bot_type=${encodeURIComponent(BOT_TYPE)}`,
    { local_token_list: [] },
    { timeoutMs: 35000 },
  );
  if (!qr.qrcode || !qr.qrcode_img_content) {
    throw new Error(`unexpected QR response: ${JSON.stringify(qr).slice(0, 300)}`);
  }

  console.log("\nQR URL:");
  console.log(qr.qrcode_img_content);
  console.log("\nUse WeChat to scan the QR code. This waits up to 8 minutes.");
  await tryPrintQr(qr.qrcode_img_content);

  let currentBaseUrl = FIXED_BASE_URL;
  let lastStatus = "";
  const deadline = Date.now() + 8 * 60_000;
  while (Date.now() < deadline) {
    try {
      const endpoint = `ilink/bot/get_qrcode_status?qrcode=${encodeURIComponent(qr.qrcode)}`;
      const status = await fetchJson("GET", currentBaseUrl, endpoint, undefined, { timeoutMs: 35000 });
      if (status.status !== lastStatus) {
        console.log(`status=${status.status}`);
        lastStatus = status.status;
      }
      if (status.status === "scaned_but_redirect" && status.redirect_host) {
        currentBaseUrl = `https://${status.redirect_host}`;
        console.log(`redirected_poll_host=${currentBaseUrl}`);
      }
      if (status.status === "confirmed") {
        if (!status.bot_token || !status.ilink_bot_id) {
          throw new Error(`confirmed but missing token/id: ${JSON.stringify(status)}`);
        }
        const account = {
          token: status.bot_token,
          accountId: status.ilink_bot_id,
          baseUrl: status.baseurl || currentBaseUrl,
          userId: status.ilink_user_id || "",
          savedAt: new Date().toISOString(),
        };
        saveAccount(account);
        console.log("confirmed");
        console.log(`accountId=${redact(account.accountId)}`);
        console.log(`baseUrl=${account.baseUrl}`);
        console.log(`userId=${redact(account.userId)}`);
        console.log(`saved=${ACCOUNT_PATH}`);
        return;
      }
      if (status.status === "expired") throw new Error("QR expired");
      if (status.status === "need_verifycode") {
        console.log("This account requires a verification code. Use the official OpenClaw login flow for now.");
      }
    } catch (error) {
      if (error?.name !== "AbortError") {
        console.log(`poll warning: ${String(error).slice(0, 300)}`);
      }
    }
    await sleep(1000);
  }
  throw new Error("login timed out");
}

async function tryPrintQr(qrUrl) {
  try {
    const qrterm = await import("qrcode-terminal");
    qrterm.default.generate(qrUrl, { small: true });
  } catch {
    console.log("(qrcode-terminal is not installed; open the QR URL above or convert it to a QR code.)");
  }
}

function firstText(msg) {
  for (const item of msg.item_list || []) {
    if (item.type === 1 && item.text_item?.text != null) return String(item.text_item.text);
    if (item.type === 3 && item.voice_item?.text) return String(item.voice_item.text);
  }
  return "";
}

// item_list[].type: 2 = image, 4 = file attachment, 5 = video
// (all three are stored AES-128-ECB encrypted on the CDN). Type 2 used to be
// matched by neither firstText nor this filter, so inbound images vanished.
// Images are split out from files: they ride pi's native `@file` multimodal
// input and must NOT trigger the "已收藏" text ack that plain files get.
function collectImageItems(msg) {
  return (msg.item_list || []).filter((item) => item.type === MESSAGE_ITEM_TYPE_IMAGE);
}

function collectFileItems(msg) {
  return (msg.item_list || []).filter((item) => item.type === 4 || item.type === 5);
}

function filePayloadOf(item) {
  return (
    item.file_item ||
    item.image_item ||
    item.video_item ||
    item.attach_item ||
    item.attachment_item ||
    item.media_item ||
    null
  );
}

function extractFilePayload(item) {
  const payload = filePayloadOf(item);
  if (!payload || typeof payload !== "object") return null;
  const media = payload.media && typeof payload.media === "object" ? payload.media : {};
  const url = String(
    payload.url || payload.cdn_url || payload.download_url || payload.file_url || media.full_url || media.url || ""
  ).trim();
  if (!url) return null;
  return {
    url,
    name: String(payload.file_name || payload.name || payload.title || "").trim(),
    aesKey: String(payload.aes_key || payload.aeskey || media.aes_key || media.aeskey || "").trim(),
    size: Number(payload.file_size || payload.size || payload.len || 0),
  };
}

// Schema-calibration log. Field names are the point; the CDN credential and
// aes_key are deliberately absent, since together they let anyone with read
// access to this log download and decrypt the archived file.
function describeFileItem(item) {
  const payload = filePayloadOf(item) || {};
  const media = payload.media && typeof payload.media === "object" ? payload.media : {};
  return {
    type: item.type,
    item_keys: Object.keys(item),
    payload_keys: Object.keys(payload),
    media_keys: Object.keys(media),
    file_name: String(payload.file_name || payload.name || payload.title || ""),
    len: payload.len ?? payload.file_size ?? payload.size ?? 0,
    md5: String(payload.md5 || ""),
    credential_present: Boolean(
      media.encrypt_query_param || media.aes_key || media.full_url || payload.url || payload.cdn_url,
    ),
  };
}

function decryptMediaBuffer(encrypted, aesKeyRaw, expectedSize) {
  if (!aesKeyRaw) return encrypted;
  let key;
  if (/^[0-9a-fA-F]{32}$/.test(aesKeyRaw)) {
    // Inbound image items carry a plain 32-char hex key at image_item.aeskey
    // (16 bytes as hex), unlike files whose media.aes_key is base64-of-hex.
    // Decode hex directly; the base64 path below would misread it as 24 bytes
    // and fall through to a 32-byte utf8 key, tripping the length guard.
    key = Buffer.from(aesKeyRaw, "hex");
  } else {
    key = Buffer.from(aesKeyRaw, "base64");
    if (key.length === 32 && /^[0-9a-fA-F]{32}$/.test(key.toString("utf8"))) {
      key = Buffer.from(key.toString("utf8"), "hex");
    }
    if (key.length !== 16) key = Buffer.from(aesKeyRaw, "utf8");
  }
  if (key.length !== 16) throw new Error(`unexpected aes key length: ${key.length}`);
  try {
    const decipher = crypto.createDecipheriv("aes-128-ecb", key, null);
    return Buffer.concat([decipher.update(encrypted), decipher.final()]);
  } catch {
    // Retry without PKCS7 padding, then trim to the declared size.
    const decipher = crypto.createDecipheriv("aes-128-ecb", key, null);
    decipher.setAutoPadding(false);
    const plain = Buffer.concat([decipher.update(encrypted), decipher.final()]);
    return expectedSize > 0 && expectedSize <= plain.length ? plain.subarray(0, expectedSize) : plain;
  }
}

// AES-128-ECB is dictated by the iLink CDN wire format (both upload and
// download sides), not a local choice — do not swap in a stronger mode.
function encryptAesEcb(plaintext, key) {
  const cipher = crypto.createCipheriv("aes-128-ecb", key, null);
  return Buffer.concat([cipher.update(plaintext), cipher.final()]);
}

// PKCS7 always appends padding, so an exact 16-byte multiple gains a full block.
function aesEcbPaddedSize(plaintextSize) {
  return Math.ceil((plaintextSize + 1) / 16) * 16;
}

function resolveSendableFile(rawPath) {
  const candidate = path.resolve(String(rawPath || "").trim());
  let stat;
  try {
    stat = fs.statSync(candidate);
  } catch {
    throw new Error(`file not found: ${candidate}`);
  }
  if (!stat.isFile()) throw new Error(`not a regular file: ${candidate}`);
  if (stat.size === 0) throw new Error(`refusing to send an empty file: ${candidate}`);
  if (stat.size > MAX_OUTBOUND_FILE_BYTES) {
    throw new Error(
      `file too large: ${stat.size} bytes exceeds limit ${MAX_OUTBOUND_FILE_BYTES}`,
    );
  }
  return { path: fs.realpathSync(candidate), size: stat.size };
}

async function getUploadUrl(account, params) {
  return fetchJson(
    "POST",
    account.baseUrl,
    "ilink/bot/getuploadurl",
    {
      filekey: params.filekey,
      media_type: UPLOAD_MEDIA_TYPE_FILE,
      to_user_id: params.toUserId,
      rawsize: params.rawsize,
      rawfilemd5: params.rawfilemd5,
      filesize: params.filesize,
      no_need_thumb: true,
      aeskey: params.aeskeyHex,
      base_info: baseInfo(),
    },
    { token: account.token, timeoutMs: 20000 },
  );
}

// The CDN returns the download credential in the x-encrypted-param response
// header, not in the body, and rejects with detail in x-error-message.
async function uploadBufferToCdn({ ciphertext, uploadFullUrl, uploadParam, filekey }) {
  const trimmedFull = String(uploadFullUrl || "").trim();
  const cdnUrl = trimmedFull
    || (uploadParam
      ? `${CDN_BASE_URL}/upload?encrypted_query_param=${encodeURIComponent(uploadParam)}&filekey=${encodeURIComponent(filekey)}`
      : "");
  if (!cdnUrl) throw new Error("getuploadurl returned neither upload_full_url nor upload_param");

  let lastError;
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), CDN_UPLOAD_TIMEOUT_MS);
    try {
      const response = await fetch(cdnUrl, {
        method: "POST",
        headers: { "Content-Type": "application/octet-stream" },
        body: new Uint8Array(ciphertext),
        signal: controller.signal,
      });
      if (response.status >= 400 && response.status < 500) {
        const detail = response.headers.get("x-error-message") || (await response.text());
        throw new Error(`cdn upload rejected ${response.status}: ${detail.slice(0, 300)}`);
      }
      if (response.status !== 200) {
        throw new Error(
          `cdn upload failed ${response.status}: ${response.headers.get("x-error-message") || ""}`,
        );
      }
      const downloadParam = response.headers.get("x-encrypted-param");
      if (!downloadParam) throw new Error("cdn upload response missing x-encrypted-param header");
      return downloadParam;
    } catch (error) {
      lastError = error;
      if (String(error?.message || "").includes("rejected")) throw error;
      console.error(`cdn upload attempt=${attempt} failed: ${String(error)}`);
    } finally {
      clearTimeout(timer);
    }
  }
  throw lastError instanceof Error ? lastError : new Error("cdn upload failed");
}

function sanitizeFileName(name) {
  const base = String(name || "").replace(/\\/g, "/").split("/").pop() || "";
  const cleaned = base.replace(/[\u0000-\u001f]/g, "").trim().replace(/^\.+/, "");
  return cleaned;
}

function uniqueTargetPath(dir, name) {
  const parsed = path.parse(name);
  let target = path.join(dir, name);
  let counter = 1;
  while (fs.existsSync(target)) {
    target = path.join(dir, `${parsed.name}-${counter}${parsed.ext}`);
    counter += 1;
  }
  return target;
}

// Inbound media carries no usable name for images/videos, so fall back by type.
const FALLBACK_EXTENSION_BY_TYPE = { 2: ".jpg", 5: ".mp4" };

async function archiveFileItem(msg, item) {
  // Field names only; never the CDN credential or aes_key.
  console.log(`file item schema: ${JSON.stringify(describeFileItem(item))}`);
  const payload = extractFilePayload(item);
  if (!payload) {
    throw new Error("unrecognized file item schema (see schema log above)");
  }

  const response = await fetch(payload.url, { headers: commonHeaders() });
  if (!response.ok) {
    throw new Error(`cdn download failed ${response.status}`);
  }
  const encrypted = Buffer.from(await response.arrayBuffer());
  const plain = decryptMediaBuffer(encrypted, payload.aesKey, payload.size);

  fs.mkdirSync(FILE_ARCHIVE_DIR, { recursive: true });
  const fallbackName = `wechat-${messageIdentity(msg)}${FALLBACK_EXTENSION_BY_TYPE[item.type] || ""}`;
  const name = sanitizeFileName(payload.name) || fallbackName;
  const target = uniqueTargetPath(FILE_ARCHIVE_DIR, name);
  fs.writeFileSync(target, plain);
  return { path: fs.realpathSync(target), name, size: plain.length };
}

async function handleFileItems(account, msg, fileItems) {
  const archived = [];
  for (const item of fileItems) {
    try {
      const saved = await archiveFileItem(msg, item);
      console.log(`file archived path=${saved.path} bytes=${saved.size}`);
      archived.push(saved);
      await sendText(account, msg.from_user_id, `已收藏\n${saved.path}`, msg.context_token || "");
    } catch (error) {
      console.error(`failed to archive file: ${String(error)}`);
      try {
        await sendText(account, msg.from_user_id, "文件收取失败，已记录日志。", msg.context_token || "");
      } catch (sendError) {
        console.error(`failed to send file failure reply: ${String(sendError)}`);
      }
    }
  }
  return archived;
}

// Images are archived silently (no "已收藏" ack) and forwarded to the handler,
// which passes them to pi as native `@file` multimodal input. A failure on one
// image must not block the rest, and must not wake the model with a bad path.
async function handleImageItems(account, msg, imageItems) {
  const archived = [];
  for (const item of imageItems) {
    try {
      const saved = await archiveFileItem(msg, item);
      console.log(`image archived path=${saved.path} bytes=${saved.size}`);
      archived.push(saved);
    } catch (error) {
      console.error(`failed to archive image: ${String(error)}`);
    }
  }
  return archived;
}

function messageIdentity(msg) {
  return String(msg.message_id || msg.client_id || `${msg.from_user_id || "unknown"}-${msg.create_time_ms || Date.now()}`);
}

async function getUpdates(account, getUpdatesBuf) {
  return fetchJson(
    "POST",
    account.baseUrl,
    "ilink/bot/getupdates",
    {
      get_updates_buf: getUpdatesBuf || "",
      base_info: baseInfo(),
    },
    {
      token: account.token,
      timeoutMs: LONG_POLL_TIMEOUT_MS,
    },
  );
}

async function sendMessageItem(account, toUserId, item, contextToken = "") {
  const clientId = `codexclaw-${Date.now()}-${crypto.randomBytes(4).toString("hex")}`;
  const resp = await fetchJson(
    "POST",
    account.baseUrl,
    "ilink/bot/sendmessage",
    {
      msg: {
        from_user_id: "",
        to_user_id: toUserId,
        client_id: clientId,
        message_type: 2,
        message_state: 2,
        item_list: [item],
        context_token: contextToken || undefined,
      },
      base_info: baseInfo(),
    },
    {
      token: account.token,
      timeoutMs: 15000,
    },
  );
  const ret = resp.ret ?? resp.errcode ?? 0;
  if (ret !== 0) {
    throw new Error(`sendmessage failed ret=${resp.ret} errcode=${resp.errcode} errmsg=${resp.errmsg || ""}`);
  }
  return clientId;
}

async function sendText(account, toUserId, text, contextToken = "") {
  if (!text) return;
  const clientId = await sendMessageItem(
    account,
    toUserId,
    { type: MESSAGE_ITEM_TYPE_TEXT, text_item: { text } },
    contextToken,
  );
  console.log(`sent to=${redact(toUserId)} clientId=${clientId} chars=${text.length}`);
}

function buildFileItem({ encryptQueryParam, aeskeyHex, fileName, rawsize }) {
  return {
    type: MESSAGE_ITEM_TYPE_FILE,
    file_item: {
      media: {
        encrypt_query_param: encryptQueryParam,
        // base64 of the hex-string key, mirroring how inbound aes_key arrives.
        aes_key: Buffer.from(aeskeyHex, "utf8").toString("base64"),
        encrypt_type: 1,
      },
      file_name: fileName,
      len: String(rawsize),
    },
  };
}

async function sendFileAttachment(account, toUserId, rawPath, contextToken = "", fileNameOverride = "") {
  const target = resolveSendableFile(rawPath);
  const plaintext = fs.readFileSync(target.path);
  const rawsize = plaintext.length;
  const aeskey = crypto.randomBytes(16);
  const filekey = crypto.randomBytes(16).toString("hex");

  const uploadResp = await getUploadUrl(account, {
    filekey,
    toUserId,
    rawsize,
    rawfilemd5: crypto.createHash("md5").update(plaintext).digest("hex"),
    filesize: aesEcbPaddedSize(rawsize),
    aeskeyHex: aeskey.toString("hex"),
  });
  const ret = uploadResp.ret ?? uploadResp.errcode ?? 0;
  if (ret !== 0) {
    throw new Error(
      `getuploadurl failed ret=${uploadResp.ret} errcode=${uploadResp.errcode} errmsg=${uploadResp.errmsg || ""}`,
    );
  }

  const encryptQueryParam = await uploadBufferToCdn({
    ciphertext: encryptAesEcb(plaintext, aeskey),
    uploadFullUrl: uploadResp.upload_full_url,
    uploadParam: uploadResp.upload_param,
    filekey,
  });

  const fileName = sanitizeFileName(fileNameOverride) || path.basename(target.path);
  const clientId = await sendMessageItem(
    account,
    toUserId,
    buildFileItem({
      encryptQueryParam,
      aeskeyHex: aeskey.toString("hex"),
      fileName,
      rawsize,
    }),
    contextToken,
  );
  console.log(
    `sent file to=${redact(toUserId)} clientId=${clientId} name=${fileName} bytes=${rawsize}`,
  );
  return { file_name: fileName, size: rawsize, client_id: clientId };
}

// Plain node:http POST without undici's implicit 5-minute headersTimeout,
// so long-running webhook calls are bounded only by CODEXCLAW_TIMEOUT_MS.
async function httpPostJson(url, headers, bodyText, timeoutMs) {
  const target = new URL(url);
  const mod = target.protocol === "https:" ? (await import("node:https")).default : http;
  return new Promise((resolve, reject) => {
    const req = mod.request(
      target,
      { method: "POST", headers: { ...headers, "Content-Length": Buffer.byteLength(bodyText) } },
      (res) => {
        const chunks = [];
        res.on("data", (chunk) => chunks.push(chunk));
        res.on("end", () =>
          resolve({ status: res.statusCode || 0, text: Buffer.concat(chunks).toString("utf8") }),
        );
        res.on("error", reject);
      },
    );
    if (timeoutMs > 0) {
      req.setTimeout(timeoutMs, () =>
        req.destroy(new Error(`codexclaw webhook timeout after ${timeoutMs}ms`)),
      );
    }
    req.on("error", reject);
    req.end(bodyText);
  });
}

async function postToCodexClaw(account, msg, text, files = [], images = []) {
  const contextToken = msg.context_token || "";
  if (contextToken) {
    setContextToken(`${account.accountId}:${msg.from_user_id}`, contextToken);
  }

  const headers = { "Content-Type": "application/json" };
  if (WEBHOOK_TOKEN) headers.Authorization = `Bearer ${WEBHOOK_TOKEN}`;

  const body = {
    message_id: messageIdentity(msg),
    account_id: account.accountId,
    user_id: msg.from_user_id,
    text,
    context_token: contextToken,
    timestamp: msg.create_time_ms || Date.now(),
  };
  if (files.length > 0) {
    body.files = files.map((file) => ({ name: file.name, path: file.path, size: file.size }));
  }
  if (images.length > 0) {
    body.images = images.map((image) => ({ name: image.name, path: image.path, size: image.size }));
  }

  const response = await httpPostJson(
    `${CODEXCLAW_BASE_URL}${CODEXCLAW_WEBHOOK_PATH}`,
    headers,
    JSON.stringify(body),
    CODEXCLAW_TIMEOUT_MS,
  );
  const raw = response.text;
  if (response.status < 200 || response.status >= 300) {
    throw new Error(`CodexClaw webhook ${response.status}: ${raw.slice(0, 300)}`);
  }
  const data = raw ? JSON.parse(raw) : {};
  const replies = Array.isArray(data.replies) ? data.replies : [];
  console.log(`codexclaw replied message_id=${body.message_id} chunks=${replies.length}`);
  for (const reply of replies) {
    const replyText = String(reply || "").trim();
    if (!replyText) continue;
    await sendText(account, msg.from_user_id, replyText, contextToken);
  }
}

async function handleInbound(account, msg) {
  const text = firstText(msg).trim();
  const imageItems = collectImageItems(msg);
  const fileItems = collectFileItems(msg);
  if (!text && imageItems.length === 0 && fileItems.length === 0) return;
  const id = messageIdentity(msg);
  if (activeInbound.has(id)) return;
  activeInbound.add(id);
  let archived = [];
  let archivedImages = [];
  try {
    if (imageItems.length > 0) {
      console.log(`inbound image from=${redact(msg.from_user_id)} id=${id} items=${imageItems.length}`);
      archivedImages = await handleImageItems(account, msg, imageItems);
    }
    if (fileItems.length > 0) {
      console.log(`inbound file from=${redact(msg.from_user_id)} id=${id} items=${fileItems.length}`);
      archived = await handleFileItems(account, msg, fileItems);
    }
    console.log(
      `inbound from=${redact(msg.from_user_id)} id=${id} text=${JSON.stringify(text)} files=${archived.length} images=${archivedImages.length}`,
    );
    await postToCodexClaw(account, msg, text, archived, archivedImages);
  } catch (error) {
    console.error(`failed to process inbound id=${id}: ${String(error)}`);
    if (!text && (archived.length > 0 || archivedImages.length > 0)) {
      // The media is safely on disk; only the session notice was lost, so
      // "服务繁忙" would misreport a successful receipt. Files were already
      // acked with "已收藏"; images ride the next model turn instead.
      console.warn(`media archived but webhook failed id=${id}; session notice lost`);
    } else {
      try {
        await sendText(account, msg.from_user_id, "服务繁忙，请稍后重试。", msg.context_token || "");
      } catch (sendError) {
        console.error(`failed to send fallback reply id=${id}: ${String(sendError)}`);
      }
    }
  } finally {
    activeInbound.delete(id);
  }
}

async function monitor(account) {
  let getUpdatesBuf = readSyncBuf();
  console.log(`wechat monitor started account=${redact(account.accountId)} baseUrl=${account.baseUrl}`);
  while (true) {
    try {
      const resp = await getUpdates(account, getUpdatesBuf);
      if (resp.get_updates_buf) {
        getUpdatesBuf = resp.get_updates_buf;
        saveSyncBuf(getUpdatesBuf);
      }
      const ret = resp.ret ?? 0;
      const errcode = resp.errcode ?? 0;
      if (ret !== 0 || errcode !== 0) {
        console.error(`getupdates api error ret=${ret} errcode=${errcode} errmsg=${resp.errmsg || ""}`);
        await sleep(2000);
        continue;
      }
      for (const msg of resp.msgs || []) {
        void handleInbound(account, msg);
      }
    } catch (error) {
      if (error?.name !== "AbortError") {
        console.error(`getupdates error: ${String(error)}`);
      }
      await sleep(2000);
    }
  }
}

function startHttpServer(account) {
  const server = http.createServer(async (req, res) => {
    try {
      if (req.method === "GET" && req.url === "/healthz") {
        respondJson(res, 200, { status: "ok" });
        return;
      }
      if (req.method === "POST" && req.url === "/send") {
        const body = await readJsonBody(req);
        const to = String(body.to || body.user_id || "").trim();
        const text = String(body.text || "").trim();
        const contextToken = String(body.context_token || contextTokens.get(`${account.accountId}:${to}`) || "");
        if (!to || !text) {
          respondJson(res, 400, { code: 400, msg: "missing to/text" });
          return;
        }
        await sendText(account, to, text, contextToken);
        respondJson(res, 200, { code: 0 });
        return;
      }
      if (req.method === "POST" && req.url === "/send_file") {
        const body = await readJsonBody(req);
        const to = String(body.to || body.user_id || "").trim();
        const filePath = String(body.path || body.file || body.file_path || "").trim();
        const contextToken = String(body.context_token || contextTokens.get(`${account.accountId}:${to}`) || "");
        if (!to || !filePath) {
          respondJson(res, 400, { code: 400, msg: "missing to/path" });
          return;
        }
        if (!contextToken) {
          console.warn(`send_file without context_token to=${redact(to)}; reply may not thread correctly`);
        }
        try {
          const info = await sendFileAttachment(
            account,
            to,
            filePath,
            contextToken,
            String(body.file_name || "").trim(),
          );
          respondJson(res, 200, { code: 0, data: info });
        } catch (error) {
          const msg = String(error?.message || error);
          console.error(`send_file failed to=${redact(to)} path=${filePath} error=${msg}`);
          respondJson(res, 502, { code: 502, msg });
        }
        return;
      }
      respondJson(res, 404, { code: 404, msg: "not found" });
    } catch (error) {
      respondJson(res, 500, { code: 500, msg: String(error) });
    }
  });
  server.listen(SIDECAR_PORT, SIDECAR_HOST, () => {
    console.log(`wechat sidecar HTTP listening on http://${SIDECAR_HOST}:${SIDECAR_PORT}`);
  });
}

function readJsonBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on("data", (chunk) => chunks.push(chunk));
    req.on("end", () => {
      try {
        const raw = Buffer.concat(chunks).toString("utf8");
        resolve(raw ? JSON.parse(raw) : {});
      } catch (error) {
        reject(error);
      }
    });
    req.on("error", reject);
  });
}

function respondJson(res, status, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(body),
  });
  res.end(body);
}

async function run() {
  const account = readAccount();
  loadContextTokens();
  startHttpServer(account);
  await monitor(account);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function isDirectRun() {
  const invoked = process.argv[1];
  if (!invoked) return false;
  try {
    return fs.realpathSync(invoked) === fs.realpathSync(fileURLToPath(import.meta.url));
  } catch {
    return false;
  }
}

if (isDirectRun()) {
  const command = process.argv[2] || "help";
  try {
    if (command === "login") {
      await login();
    } else if (command === "run") {
      await run();
    } else {
      usage();
      process.exit(command === "help" || command === "-h" || command === "--help" ? 0 : 1);
    }
  } catch (error) {
    console.error(String(error));
    process.exit(1);
  }
}

export {
  aesEcbPaddedSize,
  buildFileItem,
  collectFileItems,
  collectImageItems,
  decryptMediaBuffer,
  describeFileItem,
  encryptAesEcb,
  extractFilePayload,
  resolveSendableFile,
  sanitizeFileName,
};
