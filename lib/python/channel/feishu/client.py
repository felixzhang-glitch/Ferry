from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
from pathlib import Path
import time
from typing import Any

import httpx

from app.config import Settings
from channel.feishu.formatting import build_markdown_card

logger = logging.getLogger(__name__)


class FeishuClientError(RuntimeError):
    pass


# Feishu's file_type enum. mp4/opus are deliberately absent: they additionally
# require a duration field, and "stream" delivers them fine without it.
FILE_TYPE_BY_SUFFIX = {
    ".pdf": "pdf",
    ".doc": "doc",
    ".docx": "doc",
    ".xls": "xls",
    ".xlsx": "xls",
    ".ppt": "ppt",
    ".pptx": "ppt",
}
DEFAULT_FILE_TYPE = "stream"


def resolve_feishu_file_type(file_name: str) -> str:
    return FILE_TYPE_BY_SUFFIX.get(Path(file_name).suffix.lower(), DEFAULT_FILE_TYPE)


class FeishuClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(timeout=20.0)
        self._token_lock = asyncio.Lock()
        self._tenant_access_token = ""
        self._token_expire_at = 0.0

    async def close(self) -> None:
        await self._client.aclose()

    async def reply_text(
        self,
        message_id: str,
        text: str,
        trace_id: str,
        request_uuid: str | None = None,
    ) -> None:
        if not text:
            return

        url = self._settings.feishu_reply_url_template.format(message_id=message_id)

        payload: dict[str, Any] = {
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
        if request_uuid:
            payload["uuid"] = request_uuid

        response, data = await self._post_authenticated_json(
            url=url,
            payload=payload,
            trace_id=trace_id,
            event="feishu.reply",
        )
        if data.get("code") != 0:
            logger.error(
                "feishu reply failed",
                extra={
                    "trace_id": trace_id,
                    "event": "feishu.reply",
                    "status_code": response.status_code,
                    "error_code": data.get("code"),
                },
            )
            raise FeishuClientError(f"feishu reply failed: {data}")

    async def send_text(
        self,
        receive_id: str,
        text: str,
        trace_id: str,
        receive_id_type: str = "chat_id",
        request_uuid: str | None = None,
    ) -> str:
        if not text:
            return ""

        url = self._settings.feishu_send_message_url

        params = {"receive_id_type": receive_id_type}
        payload: dict[str, Any] = {
            "receive_id": receive_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
        if request_uuid:
            payload["uuid"] = request_uuid

        response, data = await self._post_authenticated_json(
            url=url,
            payload=payload,
            trace_id=trace_id,
            event="feishu.send",
            params=params,
        )
        if data.get("code") != 0:
            logger.error(
                "feishu send message failed",
                extra={
                    "trace_id": trace_id,
                    "event": "feishu.send",
                    "status_code": response.status_code,
                    "error_code": data.get("code"),
                },
            )
            raise FeishuClientError(f"feishu send message failed: {data}")

        message = data.get("data")
        if isinstance(message, dict):
            message_id = message.get("message_id")
            if isinstance(message_id, str):
                return message_id
        return ""

    async def reply_markdown(
        self,
        message_id: str,
        markdown: str,
        trace_id: str,
        request_uuid: str | None = None,
    ) -> str | None:
        if not markdown:
            return None

        url = self._settings.feishu_reply_url_template.format(message_id=message_id)
        payload: dict[str, Any] = {
            "msg_type": "interactive",
            "content": json.dumps(build_markdown_card(markdown), ensure_ascii=False),
        }
        if request_uuid:
            payload["uuid"] = request_uuid

        response, data = await self._post_authenticated_json(
            url=url,
            payload=payload,
            trace_id=trace_id,
            event="feishu.reply_markdown",
        )
        if data.get("code") != 0:
            logger.error(
                "feishu markdown reply failed",
                extra={
                    "trace_id": trace_id,
                    "event": "feishu.reply_markdown",
                    "status_code": response.status_code,
                    "error_code": data.get("code"),
                },
            )
            raise FeishuClientError(f"feishu markdown reply failed: {data}")

        sent = data.get("data")
        if isinstance(sent, dict):
            new_id = sent.get("message_id")
            if isinstance(new_id, str):
                return new_id
        return None

    async def update_markdown_card(
        self,
        message_id: str,
        markdown: str,
        trace_id: str,
    ) -> None:
        """Overwrite the content of an interactive card the bot previously sent."""
        if not markdown:
            return

        url = self._settings.feishu_message_url_template.format(message_id=message_id)
        payload: dict[str, Any] = {
            "content": json.dumps(build_markdown_card(markdown), ensure_ascii=False),
        }

        response, data = await self._post_authenticated_json(
            url=url,
            payload=payload,
            trace_id=trace_id,
            event="feishu.update_card",
            method="PATCH",
        )
        if data.get("code") != 0:
            logger.error(
                "feishu card update failed",
                extra={
                    "trace_id": trace_id,
                    "event": "feishu.update_card",
                    "status_code": response.status_code,
                    "error_code": data.get("code"),
                },
            )
            raise FeishuClientError(f"feishu card update failed: {data}")


    async def send_markdown(
        self,
        receive_id: str,
        markdown: str,
        trace_id: str,
        receive_id_type: str = "chat_id",
        request_uuid: str | None = None,
    ) -> str:
        if not markdown:
            return ""

        params = {"receive_id_type": receive_id_type}
        payload: dict[str, Any] = {
            "receive_id": receive_id,
            "msg_type": "interactive",
            "content": json.dumps(build_markdown_card(markdown), ensure_ascii=False),
        }
        if request_uuid:
            payload["uuid"] = request_uuid

        response, data = await self._post_authenticated_json(
            url=self._settings.feishu_send_message_url,
            payload=payload,
            trace_id=trace_id,
            event="feishu.send_markdown",
            params=params,
        )
        if data.get("code") != 0:
            logger.error(
                "feishu markdown send failed",
                extra={
                    "trace_id": trace_id,
                    "event": "feishu.send_markdown",
                    "status_code": response.status_code,
                    "error_code": data.get("code"),
                },
            )
            raise FeishuClientError(f"feishu markdown send failed: {data}")

        message = data.get("data")
        if isinstance(message, dict):
            message_id = message.get("message_id")
            if isinstance(message_id, str):
                return message_id
        return ""

    async def reply_image(
        self,
        message_id: str,
        image_key: str,
        trace_id: str,
        request_uuid: str | None = None,
    ) -> None:
        if not image_key:
            return

        url = self._settings.feishu_reply_url_template.format(message_id=message_id)
        payload: dict[str, Any] = {
            "msg_type": "image",
            "content": json.dumps({"image_key": image_key}, ensure_ascii=False),
        }
        if request_uuid:
            payload["uuid"] = request_uuid

        response, data = await self._post_authenticated_json(
            url=url,
            payload=payload,
            trace_id=trace_id,
            event="feishu.reply_image",
        )
        if data.get("code") != 0:
            logger.error(
                "feishu image reply failed",
                extra={
                    "trace_id": trace_id,
                    "event": "feishu.reply_image",
                    "status_code": response.status_code,
                    "error_code": data.get("code"),
                },
            )
            raise FeishuClientError(f"feishu image reply failed: {data}")

    async def send_image(
        self,
        receive_id: str,
        image_key: str,
        trace_id: str,
        receive_id_type: str = "chat_id",
        request_uuid: str | None = None,
    ) -> str:
        if not image_key:
            return ""

        params = {"receive_id_type": receive_id_type}
        payload: dict[str, Any] = {
            "receive_id": receive_id,
            "msg_type": "image",
            "content": json.dumps({"image_key": image_key}, ensure_ascii=False),
        }
        if request_uuid:
            payload["uuid"] = request_uuid

        response, data = await self._post_authenticated_json(
            url=self._settings.feishu_send_message_url,
            payload=payload,
            trace_id=trace_id,
            event="feishu.send_image",
            params=params,
        )
        if data.get("code") != 0:
            logger.error(
                "feishu image send failed",
                extra={
                    "trace_id": trace_id,
                    "event": "feishu.send_image",
                    "status_code": response.status_code,
                    "error_code": data.get("code"),
                },
            )
            raise FeishuClientError(f"feishu image send failed: {data}")

        message = data.get("data")
        if isinstance(message, dict):
            message_id = message.get("message_id")
            if isinstance(message_id, str):
                return message_id
        return ""

    async def upload_image(self, image_path: str, trace_id: str) -> str:
        path = Path(image_path).expanduser()
        if not path.is_file():
            raise FeishuClientError(f"image file not found: {image_path}")

        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        with path.open("rb") as image_file:
            response, data = await self._post_authenticated_multipart(
                url=self._settings.feishu_image_upload_url,
                data={"image_type": "message"},
                files={"image": (path.name, image_file, content_type)},
                trace_id=trace_id,
                event="feishu.upload_image",
            )

        if data.get("code") != 0:
            logger.error(
                "feishu image upload failed",
                extra={
                    "trace_id": trace_id,
                    "event": "feishu.upload_image",
                    "status_code": response.status_code,
                    "error_code": data.get("code"),
                },
            )
            raise FeishuClientError(f"feishu image upload failed: {data}")

        payload = data.get("data")
        if not isinstance(payload, dict):
            raise FeishuClientError("missing image upload data")
        image_key = payload.get("image_key")
        if not isinstance(image_key, str) or not image_key:
            raise FeishuClientError("missing image_key in upload response")

        return image_key

    async def upload_file(self, file_path: str, trace_id: str) -> str:
        path = Path(file_path).expanduser()
        if not path.is_file():
            raise FeishuClientError(f"file not found: {file_path}")

        size = path.stat().st_size
        if size == 0:
            raise FeishuClientError(f"refusing to upload an empty file: {file_path}")
        max_bytes = int(self._settings.push_file_max_mb) * 1024 * 1024
        if size > max_bytes:
            raise FeishuClientError(f"file too large: {size} bytes exceeds limit {max_bytes}")

        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        file_type = resolve_feishu_file_type(path.name)
        with path.open("rb") as upload_handle:
            response, data = await self._post_authenticated_multipart(
                url=self._settings.feishu_file_upload_url,
                data={"file_type": file_type, "file_name": path.name},
                files={"file": (path.name, upload_handle, content_type)},
                trace_id=trace_id,
                event="feishu.upload_file",
                # The shared client timeout is tuned for small JSON calls.
                timeout=120.0,
                log_extra={"file_type": file_type, "size": size},
            )

        if data.get("code") != 0:
            logger.error(
                "feishu file upload failed",
                extra={
                    "trace_id": trace_id,
                    "event": "feishu.upload_file",
                    "status_code": response.status_code,
                    "error_code": data.get("code"),
                },
            )
            raise FeishuClientError(f"feishu file upload failed: {data}")

        payload = data.get("data")
        if not isinstance(payload, dict):
            raise FeishuClientError("missing file upload data")
        file_key = payload.get("file_key")
        if not isinstance(file_key, str) or not file_key:
            raise FeishuClientError("missing file_key in upload response")

        return file_key

    async def send_file(
        self,
        receive_id: str,
        file_key: str,
        trace_id: str,
        receive_id_type: str = "chat_id",
        request_uuid: str | None = None,
    ) -> str:
        if not file_key:
            return ""

        params = {"receive_id_type": receive_id_type}
        payload: dict[str, Any] = {
            "receive_id": receive_id,
            "msg_type": "file",
            "content": json.dumps({"file_key": file_key}, ensure_ascii=False),
        }
        if request_uuid:
            payload["uuid"] = request_uuid

        response, data = await self._post_authenticated_json(
            url=self._settings.feishu_send_message_url,
            payload=payload,
            trace_id=trace_id,
            event="feishu.send_file",
            params=params,
        )
        if data.get("code") != 0:
            logger.error(
                "feishu file send failed",
                extra={
                    "trace_id": trace_id,
                    "event": "feishu.send_file",
                    "status_code": response.status_code,
                    "error_code": data.get("code"),
                },
            )
            raise FeishuClientError(f"feishu file send failed: {data}")

        message = data.get("data")
        if isinstance(message, dict):
            message_id = message.get("message_id")
            if isinstance(message_id, str):
                return message_id
        return ""

    async def download_message_image(self, message_id: str, image_key: str, trace_id: str) -> tuple[bytes, str]:
        if not message_id or not image_key:
            raise FeishuClientError("message_id and image_key are required")

        url = self._settings.feishu_message_resource_url_template.format(
            message_id=message_id,
            file_key=image_key,
        )
        response, content_type = await self._get_authenticated_bytes(
            url=url,
            params={"type": "image"},
            trace_id=trace_id,
            event="feishu.download_image",
        )
        return response.content, content_type

    async def download_message_file(self, message_id: str, file_key: str, trace_id: str) -> tuple[bytes, str]:
        if not message_id or not file_key:
            raise FeishuClientError("message_id and file_key are required")

        url = self._settings.feishu_message_resource_url_template.format(
            message_id=message_id,
            file_key=file_key,
        )
        response, content_type = await self._get_authenticated_bytes(
            url=url,
            params={"type": "file"},
            trace_id=trace_id,
            event="feishu.download_file",
        )
        return response.content, content_type

    async def create_reaction(
        self,
        message_id: str,
        emoji_type: str,
        trace_id: str,
    ) -> None:
        url = self._settings.feishu_reaction_url_template.format(message_id=message_id)

        response, data = await self._post_authenticated_json(
            url=url,
            trace_id=trace_id,
            event="feishu.reaction",
            payload={
                "reaction_type": {
                    "emoji_type": emoji_type,
                }
            },
        )
        if data.get("code") != 0:
            logger.error(
                "feishu reaction failed",
                extra={
                    "trace_id": trace_id,
                    "event": "feishu.reaction",
                    "status_code": response.status_code,
                    "error_code": data.get("code"),
                },
            )
            raise FeishuClientError(f"feishu reaction failed: {data}")

    async def _get_tenant_access_token(self, trace_id: str) -> str:
        now = time.time()
        if self._tenant_access_token and now < self._token_expire_at - 60:
            return self._tenant_access_token

        async with self._token_lock:
            now = time.time()
            if self._tenant_access_token and now < self._token_expire_at - 60:
                return self._tenant_access_token

            if not self._settings.feishu_app_id or not self._settings.feishu_app_secret:
                raise FeishuClientError("FEISHU_APP_ID / FEISHU_APP_SECRET is not configured")

            response, data = await self._post_json_with_retries(
                self._settings.feishu_tenant_token_url,
                payload={
                    "app_id": self._settings.feishu_app_id,
                    "app_secret": self._settings.feishu_app_secret,
                },
                trace_id=trace_id,
                event="feishu.token",
            )
            if data.get("code") != 0:
                logger.error(
                    "failed to fetch feishu tenant token",
                    extra={
                        "trace_id": trace_id,
                        "event": "feishu.token",
                        "status_code": response.status_code,
                        "error_code": data.get("code"),
                    },
                )
                raise FeishuClientError(f"failed to fetch tenant access token: {data}")

            tenant_token = data.get("tenant_access_token", "")
            expires_in = int(data.get("expire", 7200))
            if not tenant_token:
                raise FeishuClientError("missing tenant_access_token in response")

            self._tenant_access_token = tenant_token
            self._token_expire_at = time.time() + expires_in

            return tenant_token

    def _log_transport(
        self,
        *,
        event: str,
        trace_id: str,
        status_code: int,
        started: float,
        attempt: int,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """One INFO line per outbound call, carrying the latency actually paid.

        Callers used to log their own success line without timing, so outbound
        HTTP cost was only recoverable by subtracting two unrelated events.
        """
        fields: dict[str, Any] = {
            "trace_id": trace_id,
            "event": event,
            "status_code": status_code,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        if attempt:
            fields["attempt"] = attempt
        if extra:
            fields.update(extra)
        logger.info("feishu request completed", extra=fields)

    async def _post_authenticated_json(
        self,
        url: str,
        payload: dict[str, Any],
        trace_id: str,
        event: str,
        params: dict[str, str] | None = None,
        method: str = "POST",
    ) -> tuple[httpx.Response, dict[str, Any]]:
        attempts = self._retry_attempts()
        last_error: Exception | None = None
        started = time.monotonic()

        for attempt in range(attempts + 1):
            token = await self._get_tenant_access_token(trace_id=trace_id)
            try:
                response = await self._client.request(
                    method,
                    url,
                    params=params,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                data = self._parse_response(response)
            except (httpx.RequestError, FeishuClientError) as exc:
                last_error = exc
                if attempt >= attempts:
                    raise FeishuClientError(f"feishu request failed: {exc}") from exc
                await self._sleep_before_retry(attempt)
                continue

            if data.get("code") == 0:
                self._log_transport(
                    event=event,
                    trace_id=trace_id,
                    status_code=response.status_code,
                    started=started,
                    attempt=attempt,
                )
                return response, data

            if response.status_code == 401:
                await self._clear_tenant_access_token()
                if attempt < attempts:
                    logger.warning(
                        "retrying feishu request after token rejection",
                        extra={
                            "trace_id": trace_id,
                            "event": event,
                            "status_code": response.status_code,
                            "error_code": data.get("code"),
                        },
                    )
                    await self._sleep_before_retry(attempt)
                    continue

            if attempt >= attempts or not self._is_retryable_response(response=response, data=data):
                return response, data

            logger.warning(
                "retrying feishu request",
                extra={
                    "trace_id": trace_id,
                    "event": event,
                    "status_code": response.status_code,
                    "error_code": data.get("code"),
                },
            )
            await self._sleep_before_retry(attempt)

        raise FeishuClientError(f"feishu request failed: {last_error}")

    async def _post_authenticated_multipart(
        self,
        url: str,
        data: dict[str, str],
        files: dict[str, Any],
        trace_id: str,
        event: str,
        timeout: float | None = None,
        log_extra: dict[str, Any] | None = None,
    ) -> tuple[httpx.Response, dict[str, Any]]:
        attempts = self._retry_attempts()
        last_error: Exception | None = None
        started = time.monotonic()
        # httpx reads an explicit None as "no timeout", not "client default".
        timeout_kwargs: dict[str, Any] = {"timeout": timeout} if timeout is not None else {}

        for attempt in range(attempts + 1):
            token = await self._get_tenant_access_token(trace_id=trace_id)
            try:
                self._rewind_files(files)
                response = await self._client.post(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    data=data,
                    files=files,
                    **timeout_kwargs,
                )
                data_json = self._parse_response(response)
            except (httpx.RequestError, FeishuClientError) as exc:
                last_error = exc
                if attempt >= attempts:
                    raise FeishuClientError(f"feishu request failed: {exc}") from exc
                await self._sleep_before_retry(attempt)
                continue

            if data_json.get("code") == 0:
                self._log_transport(
                    event=event,
                    trace_id=trace_id,
                    status_code=response.status_code,
                    started=started,
                    attempt=attempt,
                    extra=log_extra,
                )
                return response, data_json

            if response.status_code == 401:
                await self._clear_tenant_access_token()
                if attempt < attempts:
                    await self._sleep_before_retry(attempt)
                    continue

            if attempt >= attempts or not self._is_retryable_response(response=response, data=data_json):
                return response, data_json

            logger.warning(
                "retrying feishu multipart request",
                extra={
                    "trace_id": trace_id,
                    "event": event,
                    "status_code": response.status_code,
                    "error_code": data_json.get("code"),
                },
            )
            await self._sleep_before_retry(attempt)

        raise FeishuClientError(f"feishu request failed: {last_error}")

    async def _get_authenticated_bytes(
        self,
        url: str,
        params: dict[str, str],
        trace_id: str,
        event: str,
    ) -> tuple[httpx.Response, str]:
        attempts = self._retry_attempts()
        last_error: Exception | None = None
        started = time.monotonic()

        for attempt in range(attempts + 1):
            token = await self._get_tenant_access_token(trace_id=trace_id)
            try:
                response = await self._client.get(
                    url,
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.RequestError as exc:
                last_error = exc
                if attempt >= attempts:
                    raise FeishuClientError(f"feishu request failed: {exc}") from exc
                await self._sleep_before_retry(attempt)
                continue

            content_type = response.headers.get("content-type", "")
            if response.status_code < 400 and not content_type.startswith("application/json"):
                self._log_transport(
                    event=event,
                    trace_id=trace_id,
                    status_code=response.status_code,
                    started=started,
                    attempt=attempt,
                )
                return response, content_type

            data = self._try_parse_json(response)
            if response.status_code == 401:
                await self._clear_tenant_access_token()
                if attempt < attempts:
                    await self._sleep_before_retry(attempt)
                    continue

            if attempt >= attempts or not self._is_retryable_response(response=response, data=data):
                logger.error(
                    "feishu resource download failed",
                    extra={
                        "trace_id": trace_id,
                        "event": event,
                        "status_code": response.status_code,
                        "error_code": data.get("code"),
                    },
                )
                raise FeishuClientError(f"feishu resource download failed: {data or response.status_code}")

            await self._sleep_before_retry(attempt)

        raise FeishuClientError(f"feishu request failed: {last_error}")

    async def _post_json_with_retries(
        self,
        url: str,
        payload: dict[str, Any],
        trace_id: str,
        event: str,
    ) -> tuple[httpx.Response, dict[str, Any]]:
        attempts = self._retry_attempts()
        last_error: Exception | None = None
        started = time.monotonic()

        for attempt in range(attempts + 1):
            try:
                response = await self._client.post(
                    url,
                    headers={"Content-Type": "application/json"},
                    json=payload,
                )
                data = self._parse_response(response)
            except (httpx.RequestError, FeishuClientError) as exc:
                last_error = exc
                if attempt >= attempts:
                    raise FeishuClientError(f"feishu request failed: {exc}") from exc
                await self._sleep_before_retry(attempt)
                continue

            if data.get("code") == 0:
                self._log_transport(
                    event=event,
                    trace_id=trace_id,
                    status_code=response.status_code,
                    started=started,
                    attempt=attempt,
                )
                return response, data
            if attempt >= attempts or not self._is_retryable_response(response=response, data=data):
                return response, data

            logger.warning(
                "retrying feishu request",
                extra={
                    "trace_id": trace_id,
                    "event": event,
                    "status_code": response.status_code,
                    "error_code": data.get("code"),
                },
            )
            await self._sleep_before_retry(attempt)

        raise FeishuClientError(f"feishu request failed: {last_error}")

    async def _clear_tenant_access_token(self) -> None:
        async with self._token_lock:
            self._tenant_access_token = ""
            self._token_expire_at = 0.0

    def _retry_attempts(self) -> int:
        return max(0, int(getattr(self._settings, "feishu_max_retries", 2) or 0))

    async def _sleep_before_retry(self, attempt: int) -> None:
        backoff = float(getattr(self._settings, "feishu_retry_backoff_seconds", 0.5) or 0.0)
        if backoff <= 0:
            return
        await asyncio.sleep(backoff * (2**attempt))

    @staticmethod
    def _is_retryable_response(response: httpx.Response, data: dict[str, Any]) -> bool:
        if response.status_code == 429 or response.status_code >= 500:
            return True

        code = data.get("code")
        return isinstance(code, int) and code < 0

    @staticmethod
    def _rewind_files(files: dict[str, Any]) -> None:
        for value in files.values():
            file_obj = None
            if isinstance(value, tuple) and len(value) >= 2:
                file_obj = value[1]
            elif hasattr(value, "seek"):
                file_obj = value
            if file_obj is not None and hasattr(file_obj, "seek"):
                file_obj.seek(0)

    @staticmethod
    def _parse_response(response: httpx.Response) -> dict[str, Any]:
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise FeishuClientError(f"invalid feishu response status={response.status_code}") from exc

    @staticmethod
    def _try_parse_json(response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
        return {}
