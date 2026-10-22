---
name: smtp-mail-assistant
description: Send and read emails through SMTP and POP3 from one-line natural language commands, including sending mail with file attachments. Use when users ask to quickly send a mail ("发邮件给..."), send files by mail ("把文件发给..."), or check inbox mail ("查看最近N封邮件"), or when a ready-to-run Bun/JS CLI mail assistant with 163 Mail compatible defaults is needed.
---

# SMTP Mail Assistant

## Overview

Run `scripts/mail_assistant.mjs` with Bun to parse one sentence into a send/list action and execute it with SMTP (send) + POP3 (read). Zero dependencies.

## Workflow

1. Credentials load automatically from `~/.env/smtp-mail-assistant.env` (KEY=VALUE lines):
```
MAIL_ASSISTANT_AUTH_CODE=xxxx
MAIL_ASSISTANT_EMAIL=your-address@example.com
```
Process env vars take precedence over the file. Override the file path with `MAIL_ASSISTANT_ENV_FILE`.
2. Run one sentence (see examples below).
3. Use `--dry-run` to verify parsing without network calls.

## Command Rules

- Parse send commands from `给 <email> 发邮件 ...` and `发邮件给 <email> ...`.
- Parse optional `主题` and `内容` segments; apply defaults when missing.
- Parse optional trailing `附件 <路径...>` segment (also `attach:`/`attachment:`); multiple paths split by spaces or commas, `~` expanded. Repeatable `--attach <file>` flag also adds attachments; both sources merge and dedupe. Total size limit: 45MB.
- Parse list commands from `查看/列出/显示/拉/拉取 ... 邮件`; parse optional `最近N封` with max `50`.
- If command contains `正文/详情/全文/body/detail/full`, include full plain-text body in output.

## Examples

### Send Mail

```bash
bun scripts/mail_assistant.mjs "给 user@example.com 发邮件 主题 项目更新 内容 今天已完成接口联调"
bun scripts/mail_assistant.mjs "发邮件给 user@example.com 内容 你好，这是测试邮件"
```

### Send Mail with Attachments

```bash
bun scripts/mail_assistant.mjs "给 user@example.com 发邮件 主题 周报 内容 请查收附件 附件 /tmp/report.pdf /tmp/data.xlsx"
bun scripts/mail_assistant.mjs "发邮件给 user@example.com 内容 文件见附件" --attach ~/docs/总结.docx --attach /tmp/photo.png
```

### List Inbox Mail

```bash
bun scripts/mail_assistant.mjs "查看邮件"
bun scripts/mail_assistant.mjs "查看最近8封邮件"
bun scripts/mail_assistant.mjs "拉最近5封邮件"
bun scripts/mail_assistant.mjs "查看最近2封邮件正文"
```

### Dry Run (No Network)

```bash
bun scripts/mail_assistant.mjs "给 user@example.com 发邮件 主题 测试 内容 仅解析不发送" --dry-run
bun scripts/mail_assistant.mjs "给 user@example.com 发邮件 内容 见附件 附件 /tmp/report.pdf" --dry-run
bun scripts/mail_assistant.mjs "查看最近3封邮件" --dry-run
```

### Optional Flags

- `--attach <file>`: add an attachment (repeatable); merged with paths parsed from the instruction. Dry-run still validates file existence and size.
- `--email`: override sender account; otherwise set `MAIL_ASSISTANT_EMAIL` in the environment or private credentials file. There is no built-in sender account.
- `--auth-code`: override mail auth code, fallback to `MAIL_ASSISTANT_AUTH_CODE`.
- `--smtp-host`, `--smtp-port`: override SMTP endpoint.
- `--pop3-host`, `--pop3-port`: override POP3 endpoint.

## Implementation Notes

- Use SMTP only for sending (`smtp.163.com:465` by default).
- Use POP3 for inbox reading (`pop.163.com:995` by default) because SMTP cannot fetch messages.
- Build `multipart/mixed` MIME when attachments exist (plain body part + base64 attachment parts); non-ASCII filenames use RFC 2047 encoded-words. MIME type guessed from extension, fallback `application/octet-stream`.
- Keep secrets out of source code; read credentials from CLI flags, environment variables, or `~/.env/smtp-mail-assistant.env`.
