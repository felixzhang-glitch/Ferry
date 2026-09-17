from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from channel.wechat.handler import WeChatWebhookHandler
from core.agent.types import AgentClientCancelled
from core.session.deduplicator import MessageDeduplicator
from core.session.manager import SessionManager
from core.session.task_registry import ActiveTaskRegistry


class FakeAgentClient:
    def __init__(self) -> None:
        self.messages: list[dict[str, str]] = []
        self.cancelled = False
        self.session_keys: list[str | None] = []
        self.reset_keys: list[str] = []
        self.image_paths: list[str] | None = None

    async def chat_stream(self, messages: list[dict[str, str]], trace_id: str, *, session_key: str | None = None, image_paths: list[str] | None = None):
        self.messages = messages
        self.session_keys.append(session_key)
        self.image_paths = image_paths
        yield "微信"
        yield "回复"

    async def chat(self, messages: list[dict[str, str]], trace_id: str, *, session_key: str | None = None, image_paths: list[str] | None = None) -> str:
        self.messages = messages
        self.session_keys.append(session_key)
        self.image_paths = image_paths
        return "微信回复"

    def cancel(self, trace_id: str) -> bool:
        self.cancelled = True
        return True

    def reset_session(self, session_key: str) -> None:
        self.reset_keys.append(session_key)

    async def close(self) -> None:
        pass


class BlockingAgentClient(FakeAgentClient):
    def __init__(self) -> None:
        super().__init__()
        import asyncio

        self.started = asyncio.Event()
        self.cancel_event = asyncio.Event()

    async def chat_stream(self, messages: list[dict[str, str]], trace_id: str, *, session_key: str | None = None, image_paths: list[str] | None = None):
        self.started.set()
        await self.cancel_event.wait()
        raise AgentClientCancelled("cancelled")
        if False:
            yield ""

    def cancel(self, trace_id: str) -> bool:
        self.cancelled = True
        self.cancel_event.set()
        return True


def make_handler(agent_client=None, token: str = "") -> WeChatWebhookHandler:
    settings = SimpleNamespace(
        streaming_enabled=True,
        wechat_webhook_token=token,
        wechat_message_chunk_chars=1800,
    )
    return WeChatWebhookHandler(
        settings=settings,
        agent_client=agent_client or FakeAgentClient(),
        session_manager=SessionManager(),
        deduplicator=MessageDeduplicator(ttl_seconds=3600),
        task_registry=ActiveTaskRegistry(),
    )


@pytest.mark.asyncio
async def test_wechat_webhook_returns_agent_reply() -> None:
    agent = FakeAgentClient()
    handler = make_handler(agent_client=agent, token="secret")

    result = await handler.handle_webhook(
        headers={"authorization": "Bearer secret"},
        raw_body=(
            b'{"message_id":"m1","account_id":"bot1","user_id":"u1",'
            b'"text":"hello","context_token":"ctx1"}'
        ),
    )

    assert result["code"] == 0
    assert result["replies"] == ["微信回复"]
    assert agent.messages == [{"role": "user", "content": "hello"}]
    assert agent.session_keys == ["wechat:bot1:u1"]
    assert result["context_token"] == "ctx1"


@pytest.mark.asyncio
async def test_wechat_webhook_handles_session_command_without_agent() -> None:
    agent = FakeAgentClient()
    handler = make_handler(agent_client=agent)

    result = await handler.handle_webhook(
        headers={},
        raw_body=b'{"message_id":"m2","account_id":"bot1","user_id":"u1","text":"/new"}',
    )

    assert result["code"] == 0
    assert "已创建新会话" in result["replies"][0]
    assert agent.messages == []
    assert agent.reset_keys == ["wechat:bot1:u1"]


@pytest.mark.asyncio
async def test_wechat_webhook_rejects_bad_token() -> None:
    handler = make_handler(token="secret")

    with pytest.raises(HTTPException) as exc_info:
        await handler.handle_webhook(
            headers={"authorization": "Bearer wrong"},
            raw_body=b'{"message_id":"m3","account_id":"bot1","user_id":"u1","text":"hello"}',
        )

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_wechat_stop_cancels_running_task() -> None:
    import asyncio

    agent = BlockingAgentClient()
    handler = make_handler(agent_client=agent)

    running = asyncio.create_task(
        handler.handle_webhook(
            headers={},
            raw_body=b'{"message_id":"m4","account_id":"bot1","user_id":"u1","text":"slow"}',
        )
    )
    await asyncio.wait_for(agent.started.wait(), timeout=2.0)

    stop_result = await handler.handle_webhook(
        headers={},
        raw_body=b'{"message_id":"m5","account_id":"bot1","user_id":"u1","text":"/stop"}',
    )
    running_result = await asyncio.wait_for(running, timeout=2.0)

    assert stop_result["replies"] == ["已收到停止请求，正在强制终止当前任务。"]
    assert running_result["replies"] == ["当前任务已终止。"]
    assert agent.cancelled is True


@pytest.mark.asyncio
async def test_wechat_image_only_message_triggers_agent(tmp_path) -> None:
    """An image with no text must still wake the model via `@file` args."""
    image_path = tmp_path / "photo.jpg"
    image_path.write_bytes(b"fake image")
    agent = FakeAgentClient()
    handler = make_handler(agent_client=agent)

    result = await handler.handle_webhook(
        headers={},
        raw_body=(
            b'{"message_id":"m6","account_id":"bot1","user_id":"u1","text":"",'
            b'"images":[{"path":"' + str(image_path).encode() + b'","name":"photo.jpg","size":10}]}'
        ),
    )

    assert result["code"] == 0
    assert result["replies"] == ["微信回复"]
    # Fallback caption keeps the prompt non-empty; the image rides image_paths.
    assert agent.messages == [{"role": "user", "content": "用户发送了一张图片。"}]
    assert agent.image_paths == [str(image_path)]


@pytest.mark.asyncio
async def test_wechat_text_plus_image_both_passed(tmp_path) -> None:
    """Text stays in the prompt while images ride the multimodal args."""
    image_path = tmp_path / "lunch.png"
    image_path.write_bytes(b"fake image")
    agent = FakeAgentClient()
    handler = make_handler(agent_client=agent)

    result = await handler.handle_webhook(
        headers={},
        raw_body=(
            b'{"message_id":"m7","account_id":"bot1","user_id":"u1","text":"\xe8\xae\xb0\xe5\xbd\x95\xe5\x8d\x88\xe9\xa4\x90",'
            b'"images":[{"path":"' + str(image_path).encode() + b'","name":"lunch.png","size":10}]}'
        ),
    )

    assert result["code"] == 0
    assert agent.messages == [{"role": "user", "content": "记录午餐"}]
    assert agent.image_paths == [str(image_path)]


@pytest.mark.asyncio
async def test_wechat_image_and_file_only_wakes_agent_for_image(tmp_path) -> None:
    """A file-only message stays silent, but adding an image wakes the model."""
    image_path = tmp_path / "shot.jpg"
    image_path.write_bytes(b"fake image")
    file_path = tmp_path / "doc.pdf"
    file_path.write_bytes(b"fake pdf")
    agent = FakeAgentClient()
    handler = make_handler(agent_client=agent)

    result = await handler.handle_webhook(
        headers={},
        raw_body=(
            b'{"message_id":"m8","account_id":"bot1","user_id":"u1","text":"",'
            b'"files":[{"path":"' + str(file_path).encode() + b'","name":"doc.pdf","size":8}],'
            b'"images":[{"path":"' + str(image_path).encode() + b'","name":"shot.jpg","size":10}]}'
        ),
    )

    assert result["code"] == 0
    assert agent.image_paths == [str(image_path)]
    # The queued file notice rides along, and the image fallback caption keeps
    # the prompt non-empty; both are expected in the final user text.
    content = agent.messages[-1]["content"]
    assert "用户发送了一张图片。" in content
    assert "doc.pdf" in content
