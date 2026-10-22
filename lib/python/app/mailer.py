"""Plain-text mail over SMTP SSL (163 Mail by default).

The auth code is read from MAIL_ENV_FILE at send time and never placed in the
process environment, which every pi child inherits.
"""

from __future__ import annotations

import asyncio
import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from app.config import Settings

AUTH_CODE_KEY = "MAIL_ASSISTANT_AUTH_CODE"
SENDER_KEY = "MAIL_ASSISTANT_EMAIL"
_ADDRESS = re.compile(r"^[^@\s,;<>\"']+@[^@\s,;<>\"']+\.[^@\s,;<>\"']+$")


class MailError(RuntimeError):
    pass


def valid_address(value: str) -> bool:
    return bool(_ADDRESS.match(value or ""))


def recipients(settings: Settings) -> list[str]:
    """The fixed list the console may choose from; anything malformed is dropped."""
    seen: list[str] = []
    for item in (settings.mail_recipients or "").split(","):
        address = item.strip()
        if valid_address(address) and address not in seen:
            seen.append(address)
    return seen


def _read_env_file(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].strip()
                key, sep, value = line.partition("=")
                if sep:
                    values[key.strip()] = value.strip().strip("'\"")
    except OSError:
        return {}
    return values


def _send_sync(settings: Settings, to: str, subject: str, body: str) -> None:
    if not valid_address(to):
        raise MailError("invalid recipient address")
    values = _read_env_file(settings.mail_env_file)
    auth_code = values.get(AUTH_CODE_KEY, "")
    if not auth_code:
        raise MailError(f"{AUTH_CODE_KEY} missing in MAIL_ENV_FILE")
    sender = settings.mail_sender.strip() or values.get(SENDER_KEY, "").strip()
    if not valid_address(sender):
        raise MailError("MAIL_SENDER is not configured")

    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1])
    message.set_content(body)

    context = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL(settings.mail_smtp_host, settings.mail_smtp_port, timeout=30, context=context) as smtp:
            smtp.login(sender, auth_code)
            smtp.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        # Never echo server replies verbatim: some include the login identity.
        raise MailError(f"smtp send failed: {type(exc).__name__}") from exc


async def send_text(settings: Settings, to: str, subject: str, body: str) -> None:
    await asyncio.to_thread(_send_sync, settings, to, subject, body)


def subject_for(text: str) -> str:
    first = next((line.strip() for line in text.splitlines() if line.strip()), "Ferry 通知")
    return f"Ferry · {first[:60]}"
