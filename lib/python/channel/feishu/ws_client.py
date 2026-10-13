"""Feishu long-connection (WebSocket) client using lark-oapi SDK."""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import TYPE_CHECKING, Any

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

from channel.feishu.models import FeishuTextMessageEvent

if TYPE_CHECKING:
    from channel.feishu.handler import FeishuWebhookHandler

logger = logging.getLogger(__name__)


class FeishuWsClient:
    """Wraps lark.ws.Client and bridges SDK events to the async handler."""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        handler: FeishuWebhookHandler,
        loop: asyncio.AbstractEventLoop,
        *,
        bot_open_id: str = "",
        group_require_mention: bool = True,
        log_level: lark.LogLevel = lark.LogLevel.INFO,
    ) -> None:
        self._handler = handler
        self._loop = loop
        self._bot_open_id = bot_open_id
        self._group_require_mention = group_require_mention

        event_handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message_receive)
            .register_p2_im_message_message_read_v1(self._on_ignored_event)
            .register_p2_im_message_reaction_created_v1(self._on_ignored_event)
            .register_p2_im_message_reaction_deleted_v1(self._on_ignored_event)
            .build()
        )
        self._client = lark.ws.Client(
            app_id,
            app_secret,
            event_handler=event_handler,
            log_level=log_level,
        )
        self._stopped = False

    def start_in_thread(self) -> None:
        """Start the WebSocket client in a daemon thread (blocking call)."""
        thread = threading.Thread(target=self._run, daemon=True, name="feishu-ws")
        thread.start()
        logger.info("feishu long-connection client thread started")

    def stop(self) -> None:
        """Refuse new events during shutdown; the daemon thread dies with the process.

        The SDK offers no graceful disconnect, so this is a delivery gate, not
        a socket close: events that arrive while the app is tearing down would
        otherwise hit an already-closed agent client.
        """
        self._stopped = True

    def _run(self) -> None:
        # The SDK raises out of start() on credential/limit errors instead of
        # reconnecting, and a dead thread silently stops all Feishu events
        # while the HTTP healthz stays green. Supervise with backoff: quick
        # failures back off exponentially, a long-lived connection resets it.
        backoff = 1.0
        while not self._stopped:
            started = time.monotonic()
            try:
                self._client.start()
                logger.warning("feishu ws client exited; restarting")
            except Exception:
                logger.exception("feishu ws client crashed")
            if time.monotonic() - started > 60.0:
                backoff = 1.0
            time.sleep(backoff)
            backoff = min(backoff * 2.0, 60.0)

    @staticmethod
    def _on_ignored_event(_data: Any) -> None:
        """No-op handler for subscribed events that need no processing."""

    def _on_message_receive(self, data: P2ImMessageReceiveV1) -> None:
        """Synchronous callback invoked by lark SDK in its own thread."""
        if self._stopped:
            logger.info("dropping feishu event received during shutdown")
            return
        try:
            event = self._convert_event(data)
            if event is None:
                return
            asyncio.run_coroutine_threadsafe(
                self._handler.handle_event(event),
                self._loop,
            )
        except Exception:
            logger.exception("error converting SDK event to internal event")

    def _convert_event(self, data: P2ImMessageReceiveV1) -> FeishuTextMessageEvent | None:
        """Convert lark SDK P2ImMessageReceiveV1 to internal FeishuTextMessageEvent."""
        header = data.header
        event = data.event
        if event is None or header is None:
            return None

        message = event.message
        sender = event.sender
        if message is None or sender is None:
            return None

        message_type = (message.message_type or "").strip()
        if message_type not in {"text", "image", "post", "file", "audio", "media"}:
            return None

        chat_type = (message.chat_type or "").strip()
        if chat_type not in {"p2p", "group", "chat"}:
            return None

        content_raw = message.content or ""
        try:
            content: dict[str, Any] = json.loads(content_raw)
        except (json.JSONDecodeError, TypeError):
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
            # post type
            text = self._extract_post_text(content)
            extracted_keys = self._extract_post_image_keys(content)
            image_keys = tuple(extracted_keys)
            image_key = image_keys[0] if image_keys else ""
            if not text and not image_keys:
                return None
            if not text:
                text = "用户发送了一张图片。"

        # Group mention check
        if chat_type != "p2p":
            mention_keys = self._get_mention_keys(message, self._bot_open_id)
            if self._group_require_mention and not mention_keys:
                return None
            if text:
                for key in mention_keys:
                    text = text.replace(key, " ")
                text = " ".join(text.strip().split())
            if not text:
                # Bare @mention: no body and no image; keep the caption honest.
                text = "用户发送了一张图片。" if image_keys else "（用户仅@了机器人，没有其他内容）"

        # Extract sender info
        sender_id = sender.sender_id
        if sender_id is None:
            return None
        user_id = sender_id.open_id or sender_id.user_id or sender_id.union_id
        if not user_id:
            return None

        message_id = (message.message_id or "").strip()
        chat_id = (message.chat_id or "").strip()
        event_id = (header.event_id or "").strip()

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

    @staticmethod
    def _get_mention_keys(message: Any, bot_open_id: str) -> list[str]:
        """Extract mention keys matching the bot from SDK message object."""
        mentions = getattr(message, "mentions", None)
        if not mentions:
            return []

        expected = bot_open_id.strip()
        keys: list[str] = []
        for mention in mentions:
            key = getattr(mention, "key", "") or ""
            if not key:
                continue
            if not expected:
                keys.append(key)
                continue
            mention_id = getattr(mention, "id", None)
            if mention_id is None:
                continue
            ids = {
                getattr(mention_id, "open_id", "") or "",
                getattr(mention_id, "user_id", "") or "",
                getattr(mention_id, "union_id", "") or "",
            }
            if expected in ids:
                keys.append(key)
        return keys

    @staticmethod
    def _extract_post_text(content: dict[str, Any]) -> str:
        """Extract text from rich-text (post) message content."""
        parts: list[str] = []
        for element in FeishuWsClient._iter_post_elements(content):
            tag = str(element.get("tag", "")).strip()
            if tag in {"text", "a", "at"}:
                t = str(element.get("text", "") or element.get("name", "")).strip()
                if t:
                    parts.append(t)
        return " ".join(" ".join(parts).split())

    @staticmethod
    def _extract_post_image_keys(content: dict[str, Any]) -> list[str]:
        """Extract image keys from rich-text (post) message content."""
        keys: list[str] = []
        seen: set[str] = set()
        for element in FeishuWsClient._iter_post_elements(content):
            if str(element.get("tag", "")).strip() not in {"img", "image"}:
                continue
            key = str(element.get("image_key", "") or element.get("file_key", "")).strip()
            if key and key not in seen:
                seen.add(key)
                keys.append(key)
        return keys

    @staticmethod
    def _iter_post_elements(content: dict[str, Any]) -> list[dict[str, Any]]:
        """Iterate through elements of a rich-text post."""
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
