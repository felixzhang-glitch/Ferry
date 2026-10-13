from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class FeishuTextMessageEvent:
    event_id: str
    message_id: str
    chat_id: str
    user_id: str
    text: str
    chat_type: str
    message_type: str = "text"
    image_key: str = ""
    image_keys: tuple[str, ...] = ()
    file_key: str = ""
    file_name: str = ""


def is_url_verification(payload: dict[str, Any]) -> bool:
    return payload.get("type") == "url_verification" and "challenge" in payload


def extract_token(payload: dict[str, Any]) -> str:
    if payload.get("token"):
        return str(payload.get("token"))

    header = payload.get("header")
    if isinstance(header, dict) and header.get("token"):
        return str(header.get("token"))

    return ""


def parse_text_message_event(
    payload: dict[str, Any],
    bot_open_id: str = "",
    group_require_mention: bool = True,
) -> FeishuTextMessageEvent | None:
    event = parse_message_event(
        payload=payload,
        bot_open_id=bot_open_id,
        group_require_mention=group_require_mention,
    )
    if event is None or event.message_type != "text":
        return None
    return event


def parse_message_event(
    payload: dict[str, Any],
    bot_open_id: str = "",
    group_require_mention: bool = True,
) -> FeishuTextMessageEvent | None:
    header = payload.get("header") or {}
    if header.get("event_type") != "im.message.receive_v1":
        return None

    event = payload.get("event")
    if not isinstance(event, dict):
        return None

    message = event.get("message")
    if not isinstance(message, dict):
        return None

    message_type = str(message.get("message_type", "")).strip()
    if message_type not in {"text", "image", "post", "file", "audio", "media"}:
        return None

    chat_type = str(message.get("chat_type", "")).strip()
    if chat_type not in {"p2p", "group", "chat"}:
        return None

    content_raw = message.get("content")
    if not isinstance(content_raw, str):
        return None

    try:
        content = json.loads(content_raw)
    except json.JSONDecodeError:
        return None

    text = ""
    image_key = ""
    image_keys: tuple[str, ...] = ()
    file_key = ""
    file_name = ""
    if message_type == "text":
        text = str(content.get("text", "")).strip()
        if not text:
            return None
    elif message_type == "image":
        image_key = str(content.get("image_key", "")).strip()
        if not image_key:
            return None
        image_keys = (image_key,)
        text = "用户发送了一张图片。"
    elif message_type in {"file", "audio", "media"}:
        file_key = str(content.get("file_key", "")).strip()
        if not file_key:
            return None
        file_name = str(content.get("file_name", "")).strip()
        text = "用户发送了一个文件。"
    else:
        text = _extract_post_text(content)
        image_keys = tuple(_extract_post_image_keys(content))
        image_key = image_keys[0] if image_keys else ""
        if not text and not image_keys:
            return None
        if not text:
            text = "用户发送了一张图片。"

    if chat_type != "p2p":
        mention_keys = _matching_mention_keys(message=message, bot_open_id=bot_open_id)
        if group_require_mention and not mention_keys:
            return None
        if text:
            text = _strip_mention_keys(text=text, mention_keys=mention_keys)
        if not text:
            # A bare @mention with no body: claiming an image was sent would
            # make the model answer a photo that does not exist.
            text = "用户发送了一张图片。" if image_keys else "（用户仅@了机器人，没有其他内容）"

    sender = event.get("sender") or {}
    sender_id = sender.get("sender_id") or {}
    user_id = sender_id.get("open_id") or sender_id.get("user_id") or sender_id.get("union_id")
    if not user_id:
        return None

    message_id = str(message.get("message_id", "")).strip()
    chat_id = str(message.get("chat_id", "")).strip()
    event_id = str(header.get("event_id", "")).strip()

    if not message_id or not chat_id:
        return None

    return FeishuTextMessageEvent(
        event_id=event_id,
        message_id=message_id,
        chat_id=chat_id,
        user_id=str(user_id),
        text=text,
        chat_type=chat_type,
        message_type=message_type,
        image_key=image_key,
        image_keys=image_keys,
        file_key=file_key,
        file_name=file_name,
    )


def _extract_post_text(content: dict[str, Any]) -> str:
    parts: list[str] = []
    for element in _iter_post_elements(content):
        tag = str(element.get("tag", "")).strip()
        if tag in {"text", "a", "at"}:
            text = str(element.get("text", "") or element.get("name", "")).strip()
            if text:
                parts.append(text)
    return " ".join(" ".join(parts).split())


def _extract_post_image_keys(content: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    seen: set[str] = set()
    for element in _iter_post_elements(content):
        if str(element.get("tag", "")).strip() not in {"img", "image"}:
            continue
        key = str(element.get("image_key", "") or element.get("file_key", "")).strip()
        if key and key not in seen:
            seen.add(key)
            keys.append(key)
    return keys


def _iter_post_elements(content: dict[str, Any]) -> list[dict[str, Any]]:
    root: Any = content
    if isinstance(content.get("post"), dict):
        post = content["post"]
        if isinstance(post.get("zh_cn"), dict):
            root = post["zh_cn"]
        elif isinstance(post.get("en_us"), dict):
            root = post["en_us"]
        else:
            root = post

    blocks = root.get("content") if isinstance(root, dict) else None
    if not isinstance(blocks, list):
        return []

    elements: list[dict[str, Any]] = []
    for block in blocks:
        if isinstance(block, dict):
            elements.append(block)
            continue
        if not isinstance(block, list):
            continue
        for element in block:
            if isinstance(element, dict):
                elements.append(element)
    return elements


def _matching_mention_keys(message: dict[str, Any], bot_open_id: str = "") -> list[str]:
    mentions = message.get("mentions")
    if not isinstance(mentions, list):
        return []

    expected_open_id = bot_open_id.strip()
    keys: list[str] = []
    for mention in mentions:
        if not isinstance(mention, dict):
            continue
        key = str(mention.get("key", "")).strip()
        if not key:
            continue
        if not expected_open_id:
            keys.append(key)
            continue

        mention_id = mention.get("id")
        if not isinstance(mention_id, dict):
            continue
        mention_ids = {
            str(mention_id.get("open_id", "")).strip(),
            str(mention_id.get("user_id", "")).strip(),
            str(mention_id.get("union_id", "")).strip(),
        }
        if expected_open_id in mention_ids:
            keys.append(key)
    return keys


def _strip_mention_keys(text: str, mention_keys: list[str]) -> str:
    cleaned = text
    for key in mention_keys:
        cleaned = cleaned.replace(key, " ")
    return " ".join(cleaned.strip().split())
