from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
from pathlib import Path
import time
import uuid
from collections.abc import Mapping
from typing import Any

from fastapi import HTTPException

from app.commands import execute_daily_command, parse_daily_command, parse_reminder_command, process_command
from app.config import Settings
from app.skills import build_skill_summary
from channel.feishu.client import FeishuClient, FeishuClientError
from channel.feishu.formatting import normalize_reply_text, split_message_text
from channel.feishu.media import (
    extract_local_image_paths,
    find_recent_generated_images,
    remove_local_image_references,
)
from channel.feishu.models import (
    FeishuTextMessageEvent,
    extract_token,
    is_url_verification,
    parse_message_event,
)
from channel.feishu.security import (
    FeishuSecurityError,
    decrypt_event_payload,
    verify_request_signature,
)
from core.agent.types import AgentClient, AgentClientCancelled
from core.session.daily_scheduler import DailyTaskScheduler
from core.session.deduplicator import MessageDeduplicator
from core.session.manager import SessionManager, apply_pending_notices, format_file_notice
from core.session.message_queue import SessionMessageQueue
from core.session.reminder_scheduler import ReminderScheduler
from core.session.task_registry import ActiveTaskRegistry

logger = logging.getLogger(__name__)


class FeishuWebhookHandler:
    def __init__(
        self,
        settings: Settings,
        feishu_client: FeishuClient,
        agent_client: AgentClient,
        session_manager: SessionManager,
        deduplicator: MessageDeduplicator,
        task_registry: ActiveTaskRegistry,
        reminder_scheduler: ReminderScheduler | None = None,
        daily_scheduler: DailyTaskScheduler | None = None,
        message_queue: SessionMessageQueue | None = None,
    ) -> None:
        self._settings = settings
        self._feishu_client = feishu_client
        self._agent_client = agent_client
        self._sessions = session_manager
        self._deduplicator = deduplicator
        self._task_registry = task_registry
        self._reminder_scheduler = reminder_scheduler
        self._daily_scheduler = daily_scheduler
        self._message_queue = message_queue or SessionMessageQueue(max_pending=10)
        self._downloaded_image_paths: list[str] = []
        # trace_id -> message_id of an in-flight streaming card, so the error /
        # cancel paths can overwrite the partial card instead of posting anew.
        self._active_stream_cards: dict[str, str] = {}

    async def handle_event(self, event: FeishuTextMessageEvent) -> None:
        """Entry point for SDK long-connection mode (no verification needed)."""
        trace_id = uuid.uuid4().hex
        task = asyncio.create_task(self._handle_text_event(event=event, trace_id=trace_id))
        task.add_done_callback(lambda done: self._log_background_task_result(done, trace_id))

    async def handle_webhook(self, headers: Mapping[str, str], raw_body: bytes) -> dict[str, Any]:
        trace_id = uuid.uuid4().hex

        payload = self._parse_payload(raw_body)
        payload = self._verify_and_decrypt_payload(headers=headers, raw_body=raw_body, payload=payload)

        if is_url_verification(payload):
            self._verify_token(payload=payload)
            return {"challenge": payload.get("challenge")}

        event = parse_message_event(
            payload,
            bot_open_id=self._settings.feishu_bot_open_id,
            group_require_mention=self._settings.feishu_group_require_mention,
        )
        if event is None:
            logger.info("ignored unsupported event", extra={"trace_id": trace_id, "event": "feishu.ignore"})
            return {"code": 0}

        self._verify_token(payload=payload)

        task = asyncio.create_task(self._handle_text_event(event=event, trace_id=trace_id))
        task.add_done_callback(lambda done: self._log_background_task_result(done, trace_id))
        return {"code": 0}

    async def _handle_text_event(self, event: Any, trace_id: str) -> None:
        if self._deduplicator.seen(event.message_id):
            logger.info("duplicate message ignored", extra={"trace_id": trace_id, "event": "feishu.deduplicate"})
            return

        session_key = SessionManager.build_key(user_id=event.user_id, chat_id=event.chat_id)
        normalized_text = event.text.strip().lower()

        if str(getattr(event, "file_key", "") or "").strip():
            await self._handle_file_event(event=event, session_key=session_key, trace_id=trace_id)
            return

        if normalized_text == "/stop":
            await self._handle_stop_command(
                message_id=event.message_id,
                chat_id=event.chat_id,
                session_key=session_key,
                trace_id=trace_id,
            )
            return

        await self._send_quick_ack(message_id=event.message_id, trace_id=trace_id)

        daily = parse_daily_command(event.text)
        if daily is not None:
            await self._handle_daily_command(
                message_id=event.message_id,
                chat_id=event.chat_id,
                daily=daily,
                trace_id=trace_id,
            )
            return

        reminder = parse_reminder_command(event.text)
        if reminder is not None:
            await self._handle_reminder_command(
                message_id=event.message_id,
                chat_id=event.chat_id,
                reminder_text=reminder.text,
                delay_seconds=reminder.delay_seconds,
                trace_id=trace_id,
            )
            return

        command = process_command(
            event.text,
            session_manager=self._sessions,
            session_key=session_key,
            agent_client=self._agent_client,
        )
        if command is not None:
            await self._safe_reply(
                message_id=event.message_id,
                chat_id=event.chat_id,
                text=command.reply_text,
                trace_id=trace_id,
                request_uuid=f"{event.message_id}-cmd",
            )
            return

        if normalized_text == "/skills":
            skills = await asyncio.to_thread(build_skill_summary)
            if not skills:
                reply = "当前本机未发现可用 skills。"
            else:
                reply = f"当前本机可用 skills:\n{skills}"
            await self._safe_reply(
                message_id=event.message_id,
                chat_id=event.chat_id,
                text=reply,
                trace_id=trace_id,
                request_uuid=f"{event.message_id}-skills",
            )
            return

        done_future: asyncio.Future[None] = asyncio.get_running_loop().create_future()

        async def job() -> None:
            try:
                await self._run_llm_job(event=event, session_key=session_key, trace_id=trace_id)
            finally:
                if not done_future.done():
                    done_future.set_result(None)

        def on_drop() -> None:
            if not done_future.done():
                done_future.set_result(None)

        status = await self._message_queue.submit(session_key, job, on_drop=on_drop)
        if not status.accepted:
            await self._safe_reply(
                message_id=event.message_id,
                chat_id=event.chat_id,
                text="当前会话排队消息已满，请稍后再发，或发送 /stop 终止当前任务并清空队列。",
                trace_id=trace_id,
                request_uuid=f"{event.message_id}-busy",
            )
            return
        if status.position > 0:
            await self._safe_reply(
                message_id=event.message_id,
                chat_id=event.chat_id,
                text=f"已排队，前面还有 {status.position} 条消息，将按顺序处理。",
                trace_id=trace_id,
                request_uuid=f"{event.message_id}-queued",
            )
        await done_future

    async def _run_llm_job(self, event: Any, session_key: str, trace_id: str) -> None:
        try:
            user_text, inbound_image_paths = await self._build_user_text(event=event, trace_id=trace_id)
        except FeishuClientError:
            logger.exception(
                "failed to download received image",
                extra={"trace_id": trace_id, "event": "feishu.image_download"},
            )
            await self._safe_reply(
                message_id=event.message_id,
                chat_id=event.chat_id,
                text="图片下载失败，请稍后重试。",
                trace_id=trace_id,
                request_uuid=f"{event.message_id}-image-download-failed",
            )
            return

        started = self._task_registry.start(
            key=session_key,
            trace_id=trace_id,
            message_id=event.message_id,
            cancel_callback=lambda: self._agent_client.cancel(trace_id),
        )
        if not started:
            await self._safe_reply(
                message_id=event.message_id,
                chat_id=event.chat_id,
                text="当前已有任务在运行中。发送 /stop 可强制终止后再试。",
                trace_id=trace_id,
                request_uuid=f"{event.message_id}-busy",
            )
            return
        # Drain only once the turn is committed, so a rejected turn leaves the
        # notices queued for the next one instead of dropping them.
        user_text = apply_pending_notices(user_text, self._sessions.take_pending_files(session_key))
        messages = [{"role": "user", "content": user_text}]
        generated_since = time.time()
        delivered_image_paths: list[str] = []
        auto_complete_on_image = self._looks_like_image_request(event.text)
        image_watch_task: asyncio.Task[None] | None = None
        if auto_complete_on_image:
            image_watch_task = asyncio.create_task(
                self._watch_generated_images(
                    message_id=event.message_id,
                    chat_id=event.chat_id,
                    trace_id=trace_id,
                    generated_since=generated_since,
                    delivered_image_paths=delivered_image_paths,
                    auto_complete_on_image=auto_complete_on_image,
                )
            )

        try:
            if self._settings.streaming_enabled:
                await self._stream_to_feishu(
                    message_id=event.message_id,
                    chat_id=event.chat_id,
                    messages=messages,
                    trace_id=trace_id,
                    generated_since=generated_since,
                    already_sent_image_paths=delivered_image_paths,
                    include_recent_generated_images=auto_complete_on_image,
                    session_key=session_key,
                    image_paths=inbound_image_paths,
                )
            else:
                answer = await self._agent_client.chat(
                    messages=messages,
                    trace_id=trace_id,
                    session_key=session_key,
                    image_paths=inbound_image_paths,
                )
                generated_images = []
                if auto_complete_on_image:
                    generated_images = self._filter_unsent_images(
                        await asyncio.to_thread(self._find_generated_images_since, generated_since),
                        delivered_image_paths,
                    )
                if not answer.strip() and generated_images:
                    answer = self._format_generated_image_refs(generated_images)
                if not answer.strip() and delivered_image_paths:
                    answer = self._format_generated_image_refs(delivered_image_paths)
                await self._safe_reply_with_images(
                    message_id=event.message_id,
                    chat_id=event.chat_id,
                    text=answer,
                    trace_id=trace_id,
                    extra_image_paths=generated_images,
                    already_sent_image_paths=delivered_image_paths,
                )

        except AgentClientCancelled:
            if auto_complete_on_image and delivered_image_paths:
                self._active_stream_cards.pop(trace_id, None)
                logger.info(
                    "image generation completed before pi final response",
                    extra={"trace_id": trace_id, "event": "pipeline.image_auto_complete"},
                )
                return
            logger.info("message cancelled by user", extra={"trace_id": trace_id, "event": "pipeline.cancel"})
            await self._finalize_or_reply(
                trace_id=trace_id,
                message_id=event.message_id,
                chat_id=event.chat_id,
                text="当前任务已终止。",
            )
        except Exception:
            logger.exception("failed to process message", extra={"trace_id": trace_id, "event": "pipeline.error"})
            await self._finalize_or_reply(
                trace_id=trace_id,
                message_id=event.message_id,
                chat_id=event.chat_id,
                text="服务繁忙，请稍后重试。",
            )
        finally:
            if image_watch_task is not None:
                await self._finish_image_watch_task(image_watch_task, delivered_image_paths)
            self._task_registry.finish(key=session_key, trace_id=trace_id)
            self._cleanup_downloaded_images()

    async def _stream_to_feishu(
        self,
        message_id: str,
        chat_id: str,
        messages: list[dict[str, str]],
        trace_id: str,
        generated_since: float,
        already_sent_image_paths: list[str] | None = None,
        include_recent_generated_images: bool = False,
        session_key: str | None = None,
        image_paths: list[str] | None = None,
    ) -> str:
        start = time.monotonic()
        full_text_parts: list[str] = []

        streaming = getattr(self._settings, "feishu_streaming_edit_enabled", False)
        min_interval = getattr(self._settings, "feishu_stream_min_interval_seconds", 0.7)
        min_chars = getattr(self._settings, "feishu_stream_min_chars", 40)
        card_message_id: str | None = None
        stream_broken = False
        last_update = 0.0
        last_len = 0
        last_rendered = ""
        update_count = 0
        body = ""
        footer: str | None = None
        placeholder = "> 🌿 正在处理…"

        async def render(*, force: bool) -> None:
            """Push body + footer into the card, creating it on first render.

            `force` bypasses the interval/char throttle. Tool progress and the
            final text have to land even when the answer itself has not grown,
            otherwise the card freezes for the whole length of a tool call.
            """
            nonlocal card_message_id, stream_broken
            nonlocal last_update, last_len, last_rendered, update_count
            if not streaming or stream_broken:
                return
            hint = footer if footer is not None else ("" if body else placeholder)
            text = "\n\n".join(part for part in (body, hint) if part)
            if not text or text == last_rendered:
                return
            now = time.monotonic()
            if card_message_id is not None and not force:
                if (now - last_update) < min_interval or (len(body) - last_len) < min_chars:
                    return
            try:
                if card_message_id is None:
                    card_message_id = await self._feishu_client.reply_markdown(
                        message_id=message_id,
                        markdown=text,
                        trace_id=trace_id,
                        request_uuid=f"{message_id}-stream",
                    )
                    if card_message_id is None:
                        # No message_id returned -> cannot update; fall back at the end.
                        stream_broken = True
                        return
                    self._active_stream_cards[trace_id] = card_message_id
                else:
                    await self._feishu_client.update_markdown_card(
                        message_id=card_message_id,
                        markdown=text,
                        trace_id=trace_id,
                    )
            except FeishuClientError:
                stream_broken = True
                logger.warning(
                    "feishu streaming update failed, falling back to single reply",
                    extra={"trace_id": trace_id, "event": "feishu.stream_fallback"},
                )
                return
            last_update, last_len, last_rendered = now, len(body), text
            update_count += 1

        async def on_progress(label: str | None) -> None:
            nonlocal footer
            footer = f"> 🔧 {label}" if label else None
            await render(force=True)

        # The card exists before the first token: on tool-heavy turns most of the
        # wall time passes before pi emits any text at all (measured median 77%).
        await render(force=True)

        async for piece in self._agent_client.chat_stream(
            messages=messages,
            trace_id=trace_id,
            session_key=session_key,
            image_paths=image_paths,
            on_progress=on_progress if streaming else None,
        ):
            full_text_parts.append(piece)
            body = normalize_reply_text("".join(full_text_parts))
            await render(force=False)

        answer = "".join(full_text_parts).strip()
        already_sent = already_sent_image_paths or []
        generated_images = []
        if include_recent_generated_images:
            generated_images = self._filter_unsent_images(
                await asyncio.to_thread(self._find_generated_images_since, generated_since),
                already_sent,
            )
        if not answer:
            if generated_images:
                answer = self._format_generated_image_refs(generated_images)
            elif already_sent:
                answer = self._format_generated_image_refs(already_sent)
            else:
                answer = "(空响应)"

        # Finalize the card with the clean, complete text when streaming held up.
        if streaming and card_message_id is not None and not stream_broken:
            has_images = bool(extract_local_image_paths(answer)) or bool(generated_images)
            final_text = remove_local_image_references(answer) if has_images else answer
            final_text = normalize_reply_text(final_text)
            if not final_text:
                final_text = "(见下图)" if has_images else "(空响应)"
            body, footer = final_text, None
            await render(force=True)

        streamed_ok = streaming and card_message_id is not None and not stream_broken
        if streamed_ok:
            # Text already lives in the card; only deliver images (if any) as follow-ups.
            await self._safe_reply_with_images(
                message_id=message_id,
                chat_id=chat_id,
                text=answer,
                trace_id=trace_id,
                request_uuid=f"{message_id}-stream-img",
                extra_image_paths=generated_images,
                already_sent_image_paths=already_sent,
                send_text=False,
            )
        else:
            # Streaming disabled or broken: deliver the full answer as one message.
            await self._safe_reply_with_images(
                message_id=message_id,
                chat_id=chat_id,
                text=answer,
                trace_id=trace_id,
                request_uuid=f"{message_id}-final",
                extra_image_paths=generated_images,
                already_sent_image_paths=already_sent,
            )

        logger.info(
            "streaming response finished",
            extra={
                "trace_id": trace_id,
                "event": "pipeline.streaming",
                "duration_ms": int((time.monotonic() - start) * 1000),
                "feishu_stream_updates": update_count,
                "feishu_streamed": streamed_ok,
            },
        )
        self._active_stream_cards.pop(trace_id, None)
        return answer

    async def _handle_stop_command(self, message_id: str, chat_id: str, session_key: str, trace_id: str) -> None:
        dropped = await self._message_queue.clear(session_key)
        task = self._task_registry.cancel(session_key)
        if task is None and dropped == 0:
            await self._safe_reply(
                message_id=message_id,
                chat_id=chat_id,
                text="当前没有可终止的运行中任务。",
                trace_id=trace_id,
                request_uuid=f"{message_id}-stop-idle",
            )
            return

        parts = []
        if task is not None:
            parts.append("已收到停止请求，正在强制终止当前任务。")
        if dropped > 0:
            parts.append(f"已清空排队消息 {dropped} 条。")
        await self._safe_reply(
            message_id=message_id,
            chat_id=chat_id,
            text=" ".join(parts),
            trace_id=trace_id,
            request_uuid=f"{message_id}-stop",
        )

    async def _handle_daily_command(self, message_id: str, chat_id: str, daily: Any, trace_id: str) -> None:
        if self._daily_scheduler is None:
            await self._safe_reply(
                message_id=message_id,
                chat_id=chat_id,
                text="每日简报组件未启用。",
                trace_id=trace_id,
                request_uuid=f"{message_id}-daily-disabled",
            )
            return

        reply = await execute_daily_command(
            daily=daily,
            scheduler=self._daily_scheduler,
            channel="feishu",
            target_id=chat_id,
        )
        await self._safe_reply(
            message_id=message_id,
            chat_id=chat_id,
            text=reply,
            trace_id=trace_id,
            request_uuid=f"{message_id}-daily",
        )

    async def _handle_reminder_command(
        self,
        message_id: str,
        chat_id: str,
        reminder_text: str,
        delay_seconds: float,
        trace_id: str,
    ) -> None:
        if self._reminder_scheduler is None:
            await self._safe_reply(
                message_id=message_id,
                chat_id=chat_id,
                text="定时提醒组件未启用。",
                trace_id=trace_id,
                request_uuid=f"{message_id}-remind-disabled",
            )
            return

        reminder = await self._reminder_scheduler.schedule(
            chat_id=chat_id,
            text=reminder_text,
            delay_seconds=delay_seconds,
        )
        await self._safe_reply(
            message_id=message_id,
            chat_id=chat_id,
            text=f"已设置提醒: {reminder.reminder_id[:8]}",
            trace_id=trace_id,
            request_uuid=f"{message_id}-remind",
        )

    async def _handle_file_event(self, event: Any, session_key: str, trace_id: str) -> None:
        """Archive a received file and queue it as a notice for the next turn.

        The arrival turn deliberately does not wake the agent: the file is
        saved, the user gets 已收藏, and the notice rides the next user message
        so the agent can read it only if that message actually asks for it.
        """
        try:
            file_bytes, content_type = await self._feishu_client.download_message_file(
                message_id=event.message_id,
                file_key=event.file_key,
                trace_id=trace_id,
            )
            saved_path = await asyncio.to_thread(
                self._archive_file_bytes,
                file_bytes=file_bytes,
                content_type=content_type,
                file_key=event.file_key,
                file_name=str(getattr(event, "file_name", "") or ""),
            )
        except (FeishuClientError, OSError):
            logger.exception(
                "failed to archive received file",
                extra={"trace_id": trace_id, "event": "feishu.file_archive"},
            )
            await self._safe_reply(
                message_id=event.message_id,
                chat_id=event.chat_id,
                text="文件收藏失败，请稍后重试。",
                trace_id=trace_id,
                request_uuid=f"{event.message_id}-file-failed",
            )
            return

        file_name = str(getattr(event, "file_name", "") or "") or Path(saved_path).name
        self._sessions.note_incoming_file(
            session_key,
            format_file_notice(file_name, saved_path, len(file_bytes)),
        )
        logger.info(
            "received file archived",
            extra={
                "trace_id": trace_id,
                "event": "feishu.file_archive",
                "file_name": file_name,
                "size": len(file_bytes),
            },
        )
        await self._safe_reply(
            message_id=event.message_id,
            chat_id=event.chat_id,
            text=f"已收藏\n{saved_path}",
            trace_id=trace_id,
            request_uuid=f"{event.message_id}-file",
        )

    def _archive_file_bytes(
        self,
        file_bytes: bytes,
        content_type: str,
        file_key: str,
        file_name: str,
    ) -> str:
        directory = Path(str(getattr(self._settings, "file_archive_dir", "/data/file"))).expanduser()
        directory.mkdir(parents=True, exist_ok=True)

        name = self._sanitize_file_name(file_name)
        if not name:
            suffix = mimetypes.guess_extension(content_type.split(";", 1)[0].strip().lower()) or ""
            name = f"{self._safe_filename_part(file_key)[:32]}{suffix}"

        target = directory / name
        stem, suffix = os.path.splitext(name)
        counter = 1
        while target.exists():
            target = directory / f"{stem}-{counter}{suffix}"
            counter += 1
        target.write_bytes(file_bytes)
        return os.path.realpath(target)

    @staticmethod
    def _sanitize_file_name(file_name: str) -> str:
        # Keep the basename only and strip path separators / control chars.
        name = os.path.basename(file_name.strip().replace("\\", "/"))
        cleaned = "".join(char for char in name if char.isprintable() and char not in {"/", "\0"})
        cleaned = cleaned.strip(". ")
        return cleaned

    async def _build_user_text(self, event: Any, trace_id: str) -> tuple[str, list[str]]:
        """Return (user_text, image_paths) for pi native multimodal input.

        Images are downloaded to local disk and passed to pi via the `@file`
        positional argument syntax, so the model receives them as real vision
        input instead of a text hint. Text stays as-is; when there's no text
        but there are images, a fallback caption keeps the prompt non-empty.
        """
        text = str(getattr(event, "text", "") or "").strip()
        image_keys = self._event_image_keys(event)
        if not image_keys:
            return text, []

        image_paths: list[str] = []
        failed_keys: list[str] = []
        for image_key in image_keys:
            try:
                image_paths.append(
                    await self._download_received_image(
                        message_id=event.message_id,
                        image_key=image_key,
                        trace_id=trace_id,
                    )
                )
            except FeishuClientError:
                logger.warning(
                    "failed to download one image, continuing with the rest",
                    extra={
                        "trace_id": trace_id,
                        "event": "feishu.image_download_partial",
                        "image_key": image_key,
                    },
                )
                failed_keys.append(image_key)

        notes: list[str] = []
        if failed_keys:
            notes.append("[图片下载失败: " + ", ".join(failed_keys) + "]")
        if text:
            user_text = "\n\n".join([text, *notes]) if notes else text
        elif image_paths:
            user_text = "用户发送了一张图片。"
            if notes:
                user_text = f"{user_text}\n\n{notes[0]}"
        else:
            # All images failed to download and there was no text.
            user_text = "\n\n".join(["用户发送了一张图片。", *notes])
        return user_text, image_paths

    @staticmethod
    def _event_image_keys(event: Any) -> list[str]:
        seen: set[str] = set()
        keys: list[str] = []
        raw_keys = getattr(event, "image_keys", ()) or ()
        if isinstance(raw_keys, str):
            raw_keys = (raw_keys,)
        for raw_key in raw_keys:
            key = str(raw_key).strip()
            if key and key not in seen:
                seen.add(key)
                keys.append(key)

        image_key = str(getattr(event, "image_key", "") or "").strip()
        if image_key and image_key not in seen:
            keys.append(image_key)
        return keys

    async def _download_received_image(self, message_id: str, image_key: str, trace_id: str) -> str:
        image_bytes, content_type = await self._feishu_client.download_message_image(
            message_id=message_id,
            image_key=image_key,
            trace_id=trace_id,
        )
        suffix = self._image_suffix_from_content_type(content_type)
        directory = Path(str(getattr(self._settings, "feishu_received_images_dir", "./runtime/feishu-images")))
        directory.mkdir(parents=True, exist_ok=True)
        safe_message_id = self._safe_filename_part(message_id)
        safe_image_key = self._safe_filename_part(image_key)[:32]
        image_path = directory / f"{safe_message_id}-{safe_image_key}{suffix}"
        image_path.write_bytes(image_bytes)
        absolute_path = os.path.abspath(image_path)
        self._downloaded_image_paths.append(absolute_path)
        return absolute_path

    @staticmethod
    def _image_suffix_from_content_type(content_type: str) -> str:
        media_type = content_type.split(";", 1)[0].strip().lower()
        suffix = mimetypes.guess_extension(media_type) if media_type else None
        if suffix in {".jpe", ".jpeg"}:
            return ".jpg"
        if suffix:
            return suffix
        return ".jpg"

    @staticmethod
    def _safe_filename_part(value: str) -> str:
        cleaned = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in value)
        return cleaned.strip("._") or uuid.uuid4().hex

    async def send_chat_text(self, chat_id: str, text: str, trace_id: str, request_uuid: str | None = None) -> None:
        await self._send_chat_text(chat_id=chat_id, text=text, trace_id=trace_id, request_uuid=request_uuid)

    async def _safe_reply_with_images(
        self,
        message_id: str,
        chat_id: str,
        text: str,
        trace_id: str,
        request_uuid: str | None = None,
        extra_image_paths: list[str] | None = None,
        already_sent_image_paths: list[str] | None = None,
        send_text: bool = True,
    ) -> None:
        image_paths = self._merge_image_paths(extract_local_image_paths(text), extra_image_paths or [])
        shared_sent_paths = already_sent_image_paths
        already_sent = set(shared_sent_paths or [])
        image_paths_to_send = [path for path in image_paths if path not in already_sent]
        text_without_images = remove_local_image_references(text) if image_paths else text

        if send_text and normalize_reply_text(text_without_images):
            await self._safe_reply(
                message_id=message_id,
                chat_id=chat_id,
                text=text_without_images,
                trace_id=trace_id,
                request_uuid=request_uuid,
            )

        for idx, image_path in enumerate(image_paths_to_send, start=1):
            image_uuid = f"{request_uuid or message_id}-image-{idx}"
            if shared_sent_paths is not None:
                if image_path in shared_sent_paths:
                    continue
                shared_sent_paths.append(image_path)
            try:
                await self._safe_reply_image(
                    message_id=message_id,
                    chat_id=chat_id,
                    image_path=image_path,
                    trace_id=trace_id,
                    request_uuid=image_uuid,
                )
            except FeishuClientError:
                if shared_sent_paths is not None and image_path in shared_sent_paths:
                    shared_sent_paths.remove(image_path)
                logger.exception(
                    "failed to send generated image",
                    extra={"trace_id": trace_id, "event": "feishu.image_pipeline"},
                )
                await self._safe_reply(
                    message_id=message_id,
                    chat_id=chat_id,
                    text="图片已生成，但上传到飞书失败。请稍后重试。",
                    trace_id=trace_id,
                    request_uuid=f"{image_uuid}-failed",
                )

        if send_text and not image_paths and not normalize_reply_text(text_without_images):
            await self._safe_reply(
                message_id=message_id,
                chat_id=chat_id,
                text="(空响应)",
                trace_id=trace_id,
                request_uuid=request_uuid,
            )

    async def _watch_generated_images(
        self,
        message_id: str,
        chat_id: str,
        trace_id: str,
        generated_since: float,
        delivered_image_paths: list[str],
        auto_complete_on_image: bool,
    ) -> None:
        try:
            while True:
                await asyncio.sleep(1.0)
                image_paths = self._filter_unsent_images(
                    await asyncio.to_thread(self._find_generated_images_since, generated_since),
                    delivered_image_paths,
                )
                for image_path in image_paths:
                    image_index = len(delivered_image_paths) + 1
                    delivered_image_paths.append(image_path)
                    try:
                        await self._safe_reply_image(
                            message_id=message_id,
                            chat_id=chat_id,
                            image_path=image_path,
                            trace_id=trace_id,
                            request_uuid=f"{message_id}-watch-image-{image_index}",
                        )
                    except Exception:
                        if image_path in delivered_image_paths:
                            delivered_image_paths.remove(image_path)
                        raise
                    logger.info(
                        "generated image delivered while pi is still running",
                        extra={"trace_id": trace_id, "event": "feishu.generated_image_watch"},
                    )

                if auto_complete_on_image and delivered_image_paths:
                    await asyncio.sleep(0.5)
                    self._agent_client.cancel(trace_id)
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "generated image watcher failed",
                extra={"trace_id": trace_id, "event": "feishu.generated_image_watch_error"},
            )

    @staticmethod
    async def _finish_image_watch_task(image_watch_task: asyncio.Task[None], delivered_image_paths: list[str]) -> None:
        if delivered_image_paths and not image_watch_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(image_watch_task), timeout=10.0)
                return
            except asyncio.TimeoutError:
                pass

        if not image_watch_task.done():
            image_watch_task.cancel()
        await asyncio.gather(image_watch_task, return_exceptions=True)

    def _find_generated_images_since(self, since: float) -> list[str]:
        directory = self._settings.generated_images_dir
        # Allow slight clock/order skew between process start and image file write.
        return find_recent_generated_images(directory=directory, since=max(0.0, since - 2.0), until=time.time() + 2.0)

    @staticmethod
    def _filter_unsent_images(image_paths: list[str], delivered_image_paths: list[str]) -> list[str]:
        delivered = set(delivered_image_paths)
        return [path for path in image_paths if path not in delivered]

    @staticmethod
    def _merge_image_paths(*groups: list[str]) -> list[str]:
        seen: set[str] = set()
        paths: list[str] = []
        for group in groups:
            for path in group:
                if path not in seen:
                    seen.add(path)
                    paths.append(path)
        return paths

    @staticmethod
    def _format_generated_image_refs(image_paths: list[str]) -> str:
        return "\n".join(f"file://{path}" for path in image_paths)

    @staticmethod
    def _looks_like_image_request(text: str) -> bool:
        lowered = text.lower()
        keywords = ("画", "图", "图片", "插图", "漫画", "海报", "生成图", "image", "draw", "illustration")
        return any(keyword in lowered for keyword in keywords)

    async def _safe_reply_image(
        self,
        message_id: str,
        chat_id: str,
        image_path: str,
        trace_id: str,
        request_uuid: str | None = None,
    ) -> None:
        image_key = await self._feishu_client.upload_image(image_path=image_path, trace_id=trace_id)
        try:
            await self._feishu_client.reply_image(
                message_id=message_id,
                image_key=image_key,
                trace_id=trace_id,
                request_uuid=request_uuid,
            )
            return
        except FeishuClientError as exc:
            logger.warning(
                "image reply failed, falling back to chat send",
                extra={"trace_id": trace_id, "event": "feishu.image_reply_fallback", "error_code": type(exc).__name__},
            )

        await self._feishu_client.send_image(
            receive_id=chat_id,
            receive_id_type="chat_id",
            image_key=image_key,
            trace_id=trace_id,
            request_uuid=f"{request_uuid}-send" if request_uuid else None,
        )

    def _cleanup_downloaded_images(self) -> None:
        for image_path in self._downloaded_image_paths:
            try:
                os.remove(image_path)
            except OSError:
                pass
        self._downloaded_image_paths.clear()

    async def _finalize_or_reply(
        self,
        trace_id: str,
        message_id: str,
        chat_id: str,
        text: str,
    ) -> None:
        """Overwrite the in-flight streaming card with `text` when one exists,
        so a mid-stream failure/cancel replaces the partial output instead of
        leaving it visible and appending a second message. Falls back to a
        normal reply when there is no card or the update fails."""
        card_id = self._active_stream_cards.pop(trace_id, None)
        if card_id is not None:
            try:
                await self._feishu_client.update_markdown_card(
                    message_id=card_id,
                    markdown=text,
                    trace_id=trace_id,
                )
                return
            except FeishuClientError:
                logger.warning(
                    "failed to overwrite streaming card, falling back to reply",
                    extra={"trace_id": trace_id, "event": "feishu.stream_finalize_fallback"},
                )
        await self._safe_reply(
            message_id=message_id,
            chat_id=chat_id,
            text=text,
            trace_id=trace_id,
        )

    async def _safe_reply(
        self,
        message_id: str,
        chat_id: str,
        text: str,
        trace_id: str,
        request_uuid: str | None = None,
    ) -> None:
        content = normalize_reply_text(text)
        if not content:
            return

        try:
            await self._feishu_client.reply_markdown(
                message_id=message_id,
                markdown=content,
                trace_id=trace_id,
                request_uuid=request_uuid,
            )
            return
        except FeishuClientError as exc:
            logger.warning(
                "markdown reply failed, falling back to text reply",
                extra={
                    "trace_id": trace_id,
                    "event": "feishu.reply_markdown_fallback",
                    "error_code": type(exc).__name__,
                },
            )
        try:
            await self._feishu_client.reply_text(
                message_id=message_id,
                text=content,
                trace_id=trace_id,
                request_uuid=request_uuid,
            )
            return
        except FeishuClientError as exc:
            logger.warning(
                "text reply failed, falling back to chat send",
                extra={"trace_id": trace_id, "event": "feishu.reply_fallback", "error_code": type(exc).__name__},
            )
            chunks = self._split_chunks(content)

        for idx, chunk in enumerate(chunks, start=1):
            chunk_uuid = request_uuid
            if request_uuid:
                chunk_uuid = f"{request_uuid}-part-{idx}"
            try:
                await self._send_chat_text(chat_id=chat_id, text=chunk, trace_id=trace_id, request_uuid=chunk_uuid)
            except FeishuClientError:
                logger.warning(
                    "all reply methods failed for chunk",
                    extra={
                        "trace_id": trace_id,
                        "event": "feishu.reply_all_failed",
                        "request_uuid": chunk_uuid,
                    },
                )

    async def _send_chat_text(
        self,
        chat_id: str,
        text: str,
        trace_id: str,
        request_uuid: str | None = None,
    ) -> None:
        content = normalize_reply_text(text)
        if not content:
            return

        chunks = self._split_chunks(content)
        for idx, chunk in enumerate(chunks, start=1):
            chunk_uuid = request_uuid
            if request_uuid and len(chunks) > 1:
                chunk_uuid = f"{request_uuid}-send-part-{idx}"
            try:
                await self._feishu_client.send_markdown(
                    receive_id=chat_id,
                    receive_id_type="chat_id",
                    markdown=chunk,
                    trace_id=trace_id,
                    request_uuid=chunk_uuid,
                )
            except FeishuClientError as exc:
                logger.warning(
                    "markdown send failed, falling back to text send",
                    extra={
                        "trace_id": trace_id,
                        "event": "feishu.send_markdown_fallback",
                        "error_code": type(exc).__name__,
                    },
                )
                try:
                    await self._feishu_client.send_text(
                        receive_id=chat_id,
                        receive_id_type="chat_id",
                        text=chunk,
                        trace_id=trace_id,
                        request_uuid=chunk_uuid,
                    )
                except FeishuClientError:
                    logger.warning(
                        "text send also failed",
                        extra={
                            "trace_id": trace_id,
                            "event": "feishu.send_text_failed",
                            "error_code": type(exc).__name__,
                        },
                    )

    async def _send_quick_ack(self, message_id: str, trace_id: str) -> None:
        try:
            await self._feishu_client.create_reaction(
                message_id=message_id,
                trace_id=trace_id,
                emoji_type="Typing",
            )
        except FeishuClientError:
            logger.warning(
                "failed to send quick ack",
                extra={"trace_id": trace_id, "event": "feishu.quick_ack"},
            )

    def _verify_and_decrypt_payload(
        self,
        headers: Mapping[str, str],
        raw_body: bytes,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        has_signature = any(key.lower() == "x-lark-signature" for key in headers)
        if not has_signature and "encrypt" not in payload:
            if not self._settings.feishu_verification_token:
                raise HTTPException(
                    status_code=401,
                    detail="no signature, encryption, or verification token configured",
                )
        if has_signature:
            if not self._settings.feishu_encrypt_key:
                raise HTTPException(status_code=401, detail="missing FEISHU_ENCRYPT_KEY for signature validation")
            is_valid = verify_request_signature(
                headers=headers,
                raw_body=raw_body,
                encrypt_key=self._settings.feishu_encrypt_key,
            )
            if not is_valid:
                raise HTTPException(status_code=401, detail="invalid feishu signature")

        if "encrypt" in payload:
            if not self._settings.feishu_encrypt_key:
                raise HTTPException(status_code=401, detail="missing FEISHU_ENCRYPT_KEY for encrypted payload")
            try:
                return decrypt_event_payload(payload["encrypt"], self._settings.feishu_encrypt_key)
            except FeishuSecurityError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        return payload

    def _verify_token(self, payload: dict[str, Any]) -> None:
        if not self._settings.feishu_verification_token:
            return

        token = extract_token(payload)
        if token != self._settings.feishu_verification_token:
            raise HTTPException(status_code=401, detail="verification token mismatch")

    @staticmethod
    def _parse_payload(raw_body: bytes) -> dict[str, Any]:
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail="invalid request body") from exc

        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="invalid payload type")
        return payload

    def _split_chunks(self, text: str, max_len: int | None = None) -> list[str]:
        configured_max_len = int(getattr(self._settings, "feishu_message_chunk_chars", 1500) or 1500)
        chunk_len = max_len or configured_max_len
        return split_message_text(text, max_chars=chunk_len)

    @staticmethod
    def _log_background_task_result(task: asyncio.Task[None], trace_id: str) -> None:
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            logger.exception(
                "background feishu task failed",
                extra={"trace_id": trace_id, "event": "feishu.background_error"},
            )
