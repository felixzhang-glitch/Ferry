#!/usr/bin/env bun
// Natural-language mail assistant using SMTP (send) and POP3 (read).

import tls from "node:tls";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import crypto from "node:crypto";

const DEFAULT_SUBJECT = "Message from SMTP Mail Assistant";
const DEFAULT_BODY = "Sent by SMTP Mail Assistant.";
const DEFAULT_SMTP_HOST = "smtp.163.com";
const DEFAULT_SMTP_PORT = 465;
const DEFAULT_POP3_HOST = "pop.163.com";
const DEFAULT_POP3_PORT = 995;
const SOCKET_TIMEOUT_MS = 30_000;
// 163 Mail caps attachments around 50MB; keep headroom for base64 overhead.
const MAX_ATTACHMENT_TOTAL_BYTES = 45 * 1024 * 1024;

const MIME_TYPES = {
  pdf: "application/pdf",
  zip: "application/zip",
  png: "image/png",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  gif: "image/gif",
  txt: "text/plain",
  md: "text/markdown",
  csv: "text/csv",
  json: "application/json",
  xml: "application/xml",
  html: "text/html",
  xls: "application/vnd.ms-excel",
  xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  doc: "application/msword",
  docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  ppt: "application/vnd.ms-powerpoint",
  pptx: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  mp3: "audio/mpeg",
  mp4: "video/mp4",
};

const ENV_FILE =
  process.env.MAIL_ASSISTANT_ENV_FILE ||
  path.join(os.homedir(), ".env", "smtp-mail-assistant.env");

class MailAssistantError extends Error {}

// ---------- env loading ----------

function loadEnvFile(filePath) {
  if (!fs.existsSync(filePath)) return;
  for (const rawLine of fs.readFileSync(filePath, "utf-8").split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) continue;
    const match = line.match(/^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$/);
    if (!match) continue;
    let value = match[2].trim();
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }
    if (process.env[match[1]] === undefined) process.env[match[1]] = value;
  }
}

// ---------- instruction parsing ----------

const SEND_PATTERNS = [
  /^\s*给\s*(?<email>\S+@\S+)\s*发(?:一封)?邮件(?<rest>[\s\S]*)$/,
  /^\s*发(?:一封)?邮件给\s*(?<email>\S+@\S+)(?<rest>[\s\S]*)$/,
  /^\s*send\s+email\s+to\s+(?<email>\S+@\S+)(?<rest>[\s\S]*)$/i,
];

const LIST_PATTERN =
  /(查看|列出|显示|查收|查|看|拉|拉取|list|show|pull|fetch)[\s\S]*(邮件|收件箱|email|inbox)/i;
const LIMIT_PATTERNS = [
  /(最近|latest)\s*(?<count>\d+)\s*(封|条|emails?)?/i,
  /(?<count>\d+)\s*(封|条)\s*(邮件|email)/i,
];
const BODY_PATTERNS = [/(正文|详情|全文|完整|内容)/, /\b(body|detail|full)\b/i];

// Match the LAST standalone attachment keyword so body text like "见附件" stays intact.
const ATTACHMENT_SEGMENT_PATTERN =
  /^(?<before>[\s\S]*)(?:^|[\s，,。;；])(?:附件|attachments?|attach)[:：]?\s+(?<paths>\S[\s\S]*)$/i;

function extractAttachmentPaths(text) {
  const match = text.match(ATTACHMENT_SEGMENT_PATTERN);
  if (!match) return { remaining: text, paths: [] };
  const paths = match.groups.paths.split(/[\s,，]+/).filter(Boolean);
  return { remaining: match.groups.before.trim(), paths };
}

function parseSubjectAndBody(rest) {
  const text = rest.trim();
  if (!text) return { subject: DEFAULT_SUBJECT, body: DEFAULT_BODY };

  const subjectMatch = text.match(
    /主题[:：]?\s*(?<subject>.*?)(?:\s*(?:内容|正文|body)[:：]?\s*(?<body>[\s\S]+))?$/i,
  );
  if (subjectMatch) {
    const subject = (subjectMatch.groups.subject || "").trim() || DEFAULT_SUBJECT;
    const body = (subjectMatch.groups.body || "").trim() || DEFAULT_BODY;
    return { subject, body };
  }

  const bodyMatch = text.match(/(?:内容|正文|body)[:：]?\s*(?<body>[\s\S]+)$/i);
  let body = bodyMatch ? (bodyMatch.groups.body || "").trim() || DEFAULT_BODY : text;
  body = body.replace(/^[，,。.;； ]+/, "");
  return { subject: DEFAULT_SUBJECT, body };
}

function parseInstruction(instruction) {
  const text = instruction.trim();
  if (!text) {
    throw new MailAssistantError("命令为空。示例：给 a@b.com 发邮件 主题 测试 内容 你好");
  }

  for (const pattern of SEND_PATTERNS) {
    const match = text.match(pattern);
    if (!match) continue;
    const { remaining, paths } = extractAttachmentPaths(match.groups.rest || "");
    const { subject, body } = parseSubjectAndBody(remaining);
    return {
      type: "send",
      toEmail: match.groups.email.trim(),
      subject,
      body,
      attachments: paths,
    };
  }

  if (LIST_PATTERN.test(text)) {
    let limit = 10;
    for (const pattern of LIMIT_PATTERNS) {
      const match = text.match(pattern);
      if (!match) continue;
      const parsed = Number(match.groups.count);
      if (parsed > 0) limit = Math.min(parsed, 50);
      break;
    }
    const includeBody = BODY_PATTERNS.some((pattern) => pattern.test(text));
    return { type: "list", limit, includeBody };
  }

  throw new MailAssistantError(
    "无法识别命令。示例：\n" +
      "1) 给 user@example.com 发邮件 主题 会议 内容 明天 10 点开会\n" +
      "2) 给 user@example.com 发邮件 内容 报表见附件 附件 /tmp/report.pdf\n" +
      "3) 查看最近5封邮件",
  );
}

// ---------- TLS line-based client ----------

function connectTls(host, port) {
  return new Promise((resolve, reject) => {
    const socket = tls.connect({ host, port, servername: host }, () => resolve(socket));
    socket.setTimeout(SOCKET_TIMEOUT_MS, () =>
      socket.destroy(new Error(`连接 ${host}:${port} 超时`)),
    );
    socket.once("error", reject);
  });
}

class LineReader {
  constructor(socket) {
    this.buffer = Buffer.alloc(0);
    this.waiters = [];
    this.error = null;
    this.ended = false;
    socket.on("data", (chunk) => {
      this.buffer = Buffer.concat([this.buffer, chunk]);
      this.#drain();
    });
    socket.on("error", (err) => {
      this.error = err;
      this.#drain();
    });
    socket.on("close", () => {
      this.ended = true;
      this.#drain();
    });
  }

  readLine() {
    return new Promise((resolve, reject) => {
      this.waiters.push({ resolve, reject });
      this.#drain();
    });
  }

  #drain() {
    while (this.waiters.length > 0) {
      const idx = this.buffer.indexOf("\r\n");
      if (idx >= 0) {
        // latin1 keeps raw bytes intact so per-part charsets can be decoded later
        const line = this.buffer.subarray(0, idx).toString("latin1");
        this.buffer = this.buffer.subarray(idx + 2);
        this.waiters.shift().resolve(line);
        continue;
      }
      if (this.error) {
        this.waiters.shift().reject(this.error);
        continue;
      }
      if (this.ended) {
        this.waiters.shift().reject(new Error("连接已关闭"));
        continue;
      }
      break;
    }
  }
}

// ---------- SMTP ----------

async function smtpExpect(socket, reader, command, expectedCodes) {
  if (command !== null) socket.write(command + "\r\n");
  const lines = [];
  let code = 0;
  for (;;) {
    const line = await reader.readLine();
    lines.push(line);
    if (/^\d{3}-/.test(line)) continue;
    if (/^\d{3}(?: |$)/.test(line)) {
      code = Number(line.slice(0, 3));
      break;
    }
  }
  if (!expectedCodes.includes(code)) {
    throw new MailAssistantError(`SMTP 响应异常: ${lines.join(" / ")}`);
  }
}

function encodeHeaderValue(value) {
  // eslint-disable-next-line no-control-regex
  if (/^[\x00-\x7f]*$/.test(value)) return value;
  return `=?utf-8?B?${Buffer.from(value, "utf-8").toString("base64")}?=`;
}

function rfc2822Date(date = new Date()) {
  const days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const pad = (n) => String(n).padStart(2, "0");
  const offsetMinutes = -date.getTimezoneOffset();
  const sign = offsetMinutes >= 0 ? "+" : "-";
  const abs = Math.abs(offsetMinutes);
  const zone = `${sign}${pad(Math.floor(abs / 60))}${pad(abs % 60)}`;
  return (
    `${days[date.getDay()]}, ${pad(date.getDate())} ${months[date.getMonth()]} ` +
    `${date.getFullYear()} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())} ${zone}`
  );
}

// ---------- attachments ----------

function expandHomePath(rawPath) {
  if (rawPath === "~") return os.homedir();
  if (rawPath.startsWith("~/")) return path.join(os.homedir(), rawPath.slice(2));
  return rawPath;
}

function guessMimeType(filename) {
  const ext = path.extname(filename).slice(1).toLowerCase();
  return MIME_TYPES[ext] || "application/octet-stream";
}

function formatBytes(size) {
  if (size < 1024) return `${size}B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)}KB`;
  return `${(size / (1024 * 1024)).toFixed(1)}MB`;
}

function resolveAttachments(paths) {
  const attachments = [];
  const seen = new Set();
  let totalBytes = 0;
  for (const rawPath of paths) {
    const filePath = path.resolve(expandHomePath(rawPath));
    if (seen.has(filePath)) continue;
    seen.add(filePath);
    if (!fs.existsSync(filePath)) {
      throw new MailAssistantError(`附件不存在: ${rawPath}`);
    }
    const stat = fs.statSync(filePath);
    if (!stat.isFile()) {
      throw new MailAssistantError(`附件不是文件: ${rawPath}`);
    }
    totalBytes += stat.size;
    if (totalBytes > MAX_ATTACHMENT_TOTAL_BYTES) {
      throw new MailAssistantError(
        `附件总大小超过 ${formatBytes(MAX_ATTACHMENT_TOTAL_BYTES)} 限制`,
      );
    }
    attachments.push({
      filePath,
      filename: path.basename(filePath),
      size: stat.size,
      mimeType: guessMimeType(filePath),
    });
  }
  return attachments;
}

function encodeBase64Lines(buffer) {
  return buffer.toString("base64").replace(/(.{76})/g, "$1\r\n");
}

function buildMessage(config, action, attachments) {
  const bodyBase64 = encodeBase64Lines(Buffer.from(action.body, "utf-8"));
  const messageId = `<${Date.now()}.${crypto.randomBytes(8).toString("hex")}@mail-assistant>`;
  const headerLines = [
    `From: ${config.emailAddress}`,
    `To: ${action.toEmail}`,
    `Subject: ${encodeHeaderValue(action.subject)}`,
    `Date: ${rfc2822Date()}`,
    `Message-ID: ${messageId}`,
    "MIME-Version: 1.0",
  ];

  if (attachments.length === 0) {
    return [
      ...headerLines,
      'Content-Type: text/plain; charset=utf-8',
      "Content-Transfer-Encoding: base64",
      "",
      bodyBase64,
    ].join("\r\n");
  }

  const boundary = `=_mail-assistant_${crypto.randomBytes(12).toString("hex")}`;
  const lines = [
    ...headerLines,
    `Content-Type: multipart/mixed; boundary="${boundary}"`,
    "",
    `--${boundary}`,
    'Content-Type: text/plain; charset=utf-8',
    "Content-Transfer-Encoding: base64",
    "",
    bodyBase64,
  ];
  for (const attachment of attachments) {
    const encodedName = encodeHeaderValue(attachment.filename);
    lines.push(
      `--${boundary}`,
      `Content-Type: ${attachment.mimeType}; name="${encodedName}"`,
      `Content-Disposition: attachment; filename="${encodedName}"`,
      "Content-Transfer-Encoding: base64",
      "",
      encodeBase64Lines(fs.readFileSync(attachment.filePath)),
    );
  }
  lines.push(`--${boundary}--`);
  return lines.join("\r\n");
}

async function sendEmail(config, action, attachments) {
  const socket = await connectTls(config.smtpHost, config.smtpPort);
  const reader = new LineReader(socket);
  try {
    await smtpExpect(socket, reader, null, [220]);
    await smtpExpect(socket, reader, "EHLO mail-assistant", [250]);
    await smtpExpect(socket, reader, "AUTH LOGIN", [334]);
    await smtpExpect(socket, reader, Buffer.from(config.emailAddress).toString("base64"), [334]);
    await smtpExpect(socket, reader, Buffer.from(config.authCode).toString("base64"), [235]);
    await smtpExpect(socket, reader, `MAIL FROM:<${config.emailAddress}>`, [250]);
    await smtpExpect(socket, reader, `RCPT TO:<${action.toEmail}>`, [250, 251]);
    await smtpExpect(socket, reader, "DATA", [354]);
    await smtpExpect(socket, reader, buildMessage(config, action, attachments) + "\r\n.", [250]);
    socket.write("QUIT\r\n");
  } finally {
    socket.destroy();
  }
}

// ---------- MIME parsing ----------

function decodeCharsetBytes(buffer, charset) {
  const normalized = (charset || "utf-8").toLowerCase();
  try {
    return new TextDecoder(normalized).decode(buffer);
  } catch {
    return buffer.toString("utf-8");
  }
}

function decodeMimeHeader(value) {
  if (!value) return "";
  const collapsed = value.replace(/(\?=)\s+(=\?)/g, "$1$2");
  return collapsed
    .replace(/=\?([^?]+)\?([BbQq])\?([^?]*)\?=/g, (_all, charset, encoding, data) => {
      let bytes;
      if (encoding.toUpperCase() === "B") {
        bytes = Buffer.from(data, "base64");
      } else {
        const qp = data.replace(/_/g, " ").replace(/=([0-9A-Fa-f]{2})/g, (_m, hex) =>
          String.fromCharCode(parseInt(hex, 16)),
        );
        bytes = Buffer.from(qp, "latin1");
      }
      return decodeCharsetBytes(bytes, charset);
    })
    .trim();
}

function parseHeaders(headerLines) {
  const headers = {};
  let current = null;
  for (const line of headerLines) {
    if (/^[ \t]/.test(line) && current) {
      current.value += " " + line.trim();
      continue;
    }
    const idx = line.indexOf(":");
    if (idx < 0) continue;
    current = { name: line.slice(0, idx).trim().toLowerCase(), value: line.slice(idx + 1).trim() };
    if (headers[current.name] === undefined) headers[current.name] = current;
    else current = headers[current.name]; // keep first, fold ignored duplicates away
  }
  const result = {};
  for (const [name, entry] of Object.entries(headers)) result[name] = entry.value;
  return result;
}

function splitRawMessage(raw) {
  const idx = raw.indexOf("\r\n\r\n");
  if (idx < 0) return { headerLines: raw.split("\r\n"), body: "" };
  return { headerLines: raw.slice(0, idx).split("\r\n"), body: raw.slice(idx + 4) };
}

function parseContentType(value) {
  const [type, ...rest] = (value || "text/plain").split(";");
  const params = {};
  for (const piece of rest) {
    const match = piece.match(/^\s*([^=]+)=\s*"?([^"]*)"?\s*$/);
    if (match) params[match[1].trim().toLowerCase()] = match[2].trim();
  }
  return { type: type.trim().toLowerCase(), params };
}

function decodeQuotedPrintable(text) {
  return text
    .replace(/=\r?\n/g, "")
    .replace(/=([0-9A-Fa-f]{2})/g, (_m, hex) => String.fromCharCode(parseInt(hex, 16)));
}

function decodePartPayload(headers, body) {
  const encoding = (headers["content-transfer-encoding"] || "").toLowerCase().trim();
  const { params } = parseContentType(headers["content-type"]);
  let bytes;
  if (encoding === "base64") {
    bytes = Buffer.from(body.replace(/[\r\n\s]/g, ""), "base64");
  } else if (encoding === "quoted-printable") {
    bytes = Buffer.from(decodeQuotedPrintable(body), "latin1");
  } else {
    bytes = Buffer.from(body, "latin1");
  }
  return decodeCharsetBytes(bytes, params.charset);
}

function htmlToText(html) {
  let content = html.replace(/<head[^>]*>[\s\S]*?<\/head>/gi, " ");
  content = content.replace(/<(script|style)[^>]*>[\s\S]*?<\/\1>/gi, " ");
  content = content.replace(
    /<\/?(p|div|br|tr|li|h[1-6]|table|section|article|td|th|ul|ol)[^>]*>/gi,
    "\n",
  );
  content = content.replace(/<[^>]+>/g, " ");
  content = content
    .replace(/&nbsp;/gi, " ")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#(\d+);/g, (_m, code) => String.fromCodePoint(Number(code)))
    .replace(/&#x([0-9a-f]+);/gi, (_m, code) => String.fromCodePoint(parseInt(code, 16)))
    .replace(/&amp;/g, "&")
    .replace(/\xa0/g, " ");
  return content
    .split(/\r?\n/)
    .map((line) => line.replace(/\s+/g, " ").trim())
    .filter(Boolean)
    .join("\n");
}

function collectTextParts(headers, body, candidates) {
  const { type, params } = parseContentType(headers["content-type"]);
  const disposition = (headers["content-disposition"] || "").toLowerCase();

  if (type.startsWith("multipart/") && params.boundary) {
    const marker = `--${params.boundary}`;
    const segments = body.split(new RegExp(`(?:^|\\r\\n)${escapeRegExp(marker)}`));
    for (const segment of segments.slice(1)) {
      if (segment.startsWith("--")) break;
      const partRaw = segment.replace(/^\r\n/, "");
      const { headerLines, body: partBody } = splitRawMessage(partRaw);
      collectTextParts(parseHeaders(headerLines), partBody, candidates);
    }
    return;
  }

  if (disposition.includes("attachment")) return;
  if (type !== "text/plain" && type !== "text/html") return;

  let text = decodePartPayload(headers, body);
  if (type === "text/html") text = htmlToText(text);
  candidates.push(text.trim());
}

function escapeRegExp(text) {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function extractBody(headers, body) {
  const candidates = [];
  collectTextParts(headers, body, candidates);
  return candidates.find((item) => item) || "";
}

function formatDate(rawDate) {
  if (!rawDate) return "";
  const date = new Date(rawDate);
  if (Number.isNaN(date.getTime())) return rawDate;
  const pad = (n) => String(n).padStart(2, "0");
  const offsetMinutes = -date.getTimezoneOffset();
  const sign = offsetMinutes >= 0 ? "+" : "-";
  const abs = Math.abs(offsetMinutes);
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())} ` +
    `${sign}${pad(Math.floor(abs / 60))}${pad(abs % 60)}`
  );
}

// ---------- POP3 ----------

async function popExpectOk(socket, reader, command) {
  if (command !== null) socket.write(command + "\r\n");
  const line = await reader.readLine();
  if (!line.startsWith("+OK")) throw new MailAssistantError(`POP3 响应异常: ${line}`);
  return line;
}

async function popReadMultiline(reader) {
  const lines = [];
  for (;;) {
    let line = await reader.readLine();
    if (line === ".") break;
    if (line.startsWith("..")) line = line.slice(1);
    lines.push(line);
  }
  return lines.join("\r\n");
}

async function listRecentEmails(config, action) {
  const socket = await connectTls(config.pop3Host, config.pop3Port);
  const reader = new LineReader(socket);
  try {
    await popExpectOk(socket, reader, null);
    await popExpectOk(socket, reader, `USER ${config.emailAddress}`);
    await popExpectOk(socket, reader, `PASS ${config.authCode}`);
    const stat = await popExpectOk(socket, reader, "STAT");
    const total = Number(stat.split(/\s+/)[1] || 0);
    if (total <= 0) return [];

    const start = Math.max(1, total - action.limit + 1);
    const result = [];
    for (let index = total; index >= start; index -= 1) {
      await popExpectOk(socket, reader, `RETR ${index}`);
      const raw = await popReadMultiline(reader);
      const { headerLines, body } = splitRawMessage(raw);
      const headers = parseHeaders(headerLines);

      const fullBody = extractBody(headers, body);
      result.push({
        fromValue: decodeMimeHeader(headers["from"]),
        subject: decodeMimeHeader(headers["subject"]) || "(无主题)",
        dateText: formatDate(headers["date"]),
        preview: fullBody.length <= 120 ? fullBody : `${fullBody.slice(0, 120)}...`,
        body: action.includeBody ? fullBody : "",
      });
    }
    socket.write("QUIT\r\n");
    return result;
  } finally {
    socket.destroy();
  }
}

// ---------- CLI ----------

function parseArgs(argv) {
  const args = {
    instruction: null,
    email: process.env.MAIL_ASSISTANT_EMAIL || "",
    authCode: process.env.MAIL_ASSISTANT_AUTH_CODE || "",
    smtpHost: process.env.MAIL_ASSISTANT_SMTP_HOST || DEFAULT_SMTP_HOST,
    smtpPort: Number(process.env.MAIL_ASSISTANT_SMTP_PORT || DEFAULT_SMTP_PORT),
    pop3Host: process.env.MAIL_ASSISTANT_POP3_HOST || DEFAULT_POP3_HOST,
    pop3Port: Number(process.env.MAIL_ASSISTANT_POP3_PORT || DEFAULT_POP3_PORT),
    attachments: [],
    dryRun: false,
  };
  const valueFlags = {
    "--email": "email",
    "--auth-code": "authCode",
    "--smtp-host": "smtpHost",
    "--smtp-port": "smtpPort",
    "--pop3-host": "pop3Host",
    "--pop3-port": "pop3Port",
  };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--dry-run") {
      args.dryRun = true;
    } else if (arg === "--attach") {
      const value = argv[i + 1];
      if (value === undefined) throw new MailAssistantError(`缺少参数值: ${arg}`);
      args.attachments.push(value);
      i += 1;
    } else if (valueFlags[arg]) {
      const value = argv[i + 1];
      if (value === undefined) throw new MailAssistantError(`缺少参数值: ${arg}`);
      args[valueFlags[arg]] = arg.endsWith("-port") ? Number(value) : value;
      i += 1;
    } else if (args.instruction === null) {
      args.instruction = arg;
    } else {
      throw new MailAssistantError(`无法识别的参数: ${arg}`);
    }
  }
  if (args.instruction === null) {
    throw new MailAssistantError(
      '用法: bun scripts/mail_assistant.mjs "<自然语言指令>" [--dry-run] [--attach <文件>]... [--email ...] [--auth-code ...]',
    );
  }
  return args;
}

function buildConfig(args) {
  if (!args.email) {
    throw new MailAssistantError("缺少邮箱账号。请设置 --email 或环境变量 MAIL_ASSISTANT_EMAIL。");
  }
  if (!args.authCode) {
    throw new MailAssistantError(
      `缺少授权码。请设置 --auth-code、环境变量 MAIL_ASSISTANT_AUTH_CODE，或写入 ${ENV_FILE}。`,
    );
  }
  return {
    emailAddress: args.email,
    authCode: args.authCode,
    smtpHost: args.smtpHost,
    smtpPort: args.smtpPort,
    pop3Host: args.pop3Host,
    pop3Port: args.pop3Port,
  };
}

function printMailList(items, includeBody) {
  if (items.length === 0) {
    console.log("收件箱没有邮件。");
    return;
  }
  items.forEach((item, i) => {
    console.log(`[${i + 1}] From: ${item.fromValue}`);
    console.log(`    Subject: ${item.subject}`);
    console.log(`    Date: ${item.dateText}`);
    if (includeBody) {
      console.log("    Body:");
      for (const line of (item.body || "(正文为空)").split(/\r?\n/)) {
        console.log(`      ${line}`);
      }
    } else if (item.preview) {
      console.log(`    Preview: ${item.preview}`);
    }
    console.log();
  });
}

async function main() {
  loadEnvFile(ENV_FILE);
  try {
    const args = parseArgs(process.argv.slice(2));
    const action = parseInstruction(args.instruction);
    const attachments =
      action.type === "send"
        ? resolveAttachments([...action.attachments, ...args.attachments])
        : [];

    if (args.dryRun) {
      if (action.type === "send") {
        console.log("DRY RUN - SendAction");
        console.log(`From: ${args.email || "(from env)"}`);
        console.log(`To: ${action.toEmail}`);
        console.log(`Subject: ${action.subject}`);
        console.log(`Body: ${action.body}`);
        console.log(`Attachments: ${attachments.length}`);
        for (const item of attachments) {
          console.log(`  - ${item.filename} (${formatBytes(item.size)}, ${item.mimeType})`);
        }
      } else {
        console.log("DRY RUN - ListAction");
        console.log(`Limit: ${action.limit}`);
        console.log(`IncludeBody: ${action.includeBody}`);
      }
      return 0;
    }

    const config = buildConfig(args);
    if (action.type === "send") {
      await sendEmail(config, action, attachments);
      const pad = (n) => String(n).padStart(2, "0");
      const now = new Date();
      const stamp =
        `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ` +
        `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
      const suffix = attachments.length > 0 ? `（附件 ${attachments.length} 个）` : "";
      console.log(`[${stamp}] 邮件发送成功: ${action.toEmail}${suffix}`);
      return 0;
    }

    const summaries = await listRecentEmails(config, action);
    printMailList(summaries, action.includeBody);
    return 0;
  } catch (error) {
    if (error instanceof MailAssistantError) {
      console.log(`[ERROR] ${error.message}`);
    } else {
      console.log(`[ERROR] 邮件服务调用失败: ${error?.message || error}`);
    }
    return 1;
  }
}

process.exit(await main());
