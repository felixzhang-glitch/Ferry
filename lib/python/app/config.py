from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_LEGACY_KEYS = {
    "PI_WORK_DIR": "CODEX_WORK_DIR",
    "PI_STREAM_READ_LIMIT_BYTES": "CODEX_STREAM_READ_LIMIT_BYTES",
    "PI_MAX_RETRIES": "CODEX_MAX_RETRIES",
    "PI_RETRY_BACKOFF_SECONDS": "CODEX_RETRY_BACKOFF_SECONDS",
    "PI_CIRCUIT_BREAKER_THRESHOLD": "CODEX_CIRCUIT_BREAKER_THRESHOLD",
    "PI_CIRCUIT_BREAKER_COOLDOWN_SECONDS": "CODEX_CIRCUIT_BREAKER_COOLDOWN_SECONDS",
    "GENERATED_IMAGES_DIR": "CODEX_GENERATED_IMAGES_DIR",
}


def _split_csv(raw: str) -> list[str]:
    """Parse a comma-separated config value into a deduplicated ordered list."""
    items: list[str] = []
    for chunk in (raw or "").split(","):
        value = chunk.strip()
        if value and value not in items:
            items.append(value)
    return items


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file="conf/.env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        populate_by_name=True,
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
    ):
        def legacy_env() -> dict[str, Any]:
            # Keep legacy keys distinct until validation: a new key in .env must
            # also win over an old key in the shell, regardless of settings version.
            values = {**dotenv_settings.env_vars, **env_settings.env_vars}
            return {
                old: values[old.lower()]
                for old in _LEGACY_KEYS.values()
                if values.get(old.lower()) is not None
            }

        return init_settings, env_settings, dotenv_settings, file_secret_settings, legacy_env

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_keys(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = data.copy()
        for new, old in _LEGACY_KEYS.items():
            if new not in data and new.lower() not in data and old in data:
                value = data[old]
                # The old key named the parent; PI_WORK_DIR names the final cwd.
                data[new] = os.path.join(value, "pi") if new == "PI_WORK_DIR" else value
        return data

    feishu_app_id: str = Field(default="", validation_alias="FEISHU_APP_ID")
    feishu_app_secret: str = Field(default="", validation_alias="FEISHU_APP_SECRET")
    feishu_verification_token: str = Field(default="", validation_alias="FEISHU_VERIFICATION_TOKEN")
    feishu_encrypt_key: str = Field(default="", validation_alias="FEISHU_ENCRYPT_KEY")
    feishu_api_base: str = Field(default="https://open.feishu.cn", validation_alias="FEISHU_API_BASE")
    feishu_bot_open_id: str = Field(default="", validation_alias="FEISHU_BOT_OPEN_ID")
    feishu_group_require_mention: bool = Field(default=True, validation_alias="FEISHU_GROUP_REQUIRE_MENTION")
    feishu_max_retries: int = Field(default=2, validation_alias="FEISHU_MAX_RETRIES")
    feishu_retry_backoff_seconds: float = Field(default=0.5, validation_alias="FEISHU_RETRY_BACKOFF_SECONDS")
    feishu_received_images_dir: str = Field(
        default="./runtime/feishu-images",
        validation_alias="FEISHU_RECEIVED_IMAGES_DIR",
    )
    file_archive_dir: str = Field(default="/data/file", validation_alias="FILE_ARCHIVE_DIR")

    # Historical defaults preserve native pi sessions and existing image discovery.
    # Renaming configuration never moves or deletes user files.
    pi_work_dir: str = Field(default="./runtime/codex-workdir/pi", validation_alias="PI_WORK_DIR")
    generated_images_dir: str = Field(
        default="~/.codex/generated_images",
        validation_alias="GENERATED_IMAGES_DIR",
    )
    # pi --mode json 会在事件行内回显图片 base64（原图×1.33），发图时单行可达数十 MB。
    # 此值是 asyncio readline 的单行上限，过小会招 LimitOverrunError 导致“服务繁忙”。
    pi_stream_read_limit_bytes: int = Field(default=33554432, validation_alias="PI_STREAM_READ_LIMIT_BYTES")
    pi_max_retries: int = Field(default=2, validation_alias="PI_MAX_RETRIES")
    pi_retry_backoff_seconds: float = Field(default=1.0, validation_alias="PI_RETRY_BACKOFF_SECONDS")
    pi_circuit_breaker_threshold: int = Field(default=5, validation_alias="PI_CIRCUIT_BREAKER_THRESHOLD")
    pi_circuit_breaker_cooldown_seconds: int = Field(
        default=30,
        validation_alias="PI_CIRCUIT_BREAKER_COOLDOWN_SECONDS",
    )

    pi_cli_bin: str = Field(default="pi", validation_alias="PI_CLI_BIN")
    # Opt in after configuring Node and the installed pi SDK entry.
    # false retains the original CLI transport for operational rollback.
    pi_persistent_enabled: bool = Field(default=False, validation_alias="PI_PERSISTENT_ENABLED")
    pi_node_bin: str = Field(default="node", validation_alias="PI_NODE_BIN")
    pi_sdk_module: str = Field(default="", validation_alias="PI_SDK_MODULE")
    pi_worker_pool_size: int = Field(default=2, ge=1, le=8, validation_alias="PI_WORKER_POOL_SIZE")
    pi_worker_startup_timeout_seconds: float = Field(
        default=30.0, gt=0, validation_alias="PI_WORKER_STARTUP_TIMEOUT_SECONDS",
    )
    pi_model: str = Field(default="", validation_alias="PI_MODEL")
    # pi resolves "$DASHSCOPE_API_KEY" from models.json at request time, so the
    # key travels as an environment variable rather than a CLI argument.
    pi_api_key: str = Field(default="", validation_alias="DASHSCOPE_API_KEY")
    # Same resolution for the DeepSeek official endpoint (provider "deepseek").
    pi_deepseek_api_key: str = Field(default="", validation_alias="DEEPSEEK_API_KEY")
    pi_thinking: str = Field(default="high", validation_alias="PI_THINKING")
    pi_tools: str = Field(default="", validation_alias="PI_TOOLS")
    pi_agent_dir: str = Field(default="", validation_alias="PI_CODING_AGENT_DIR")
    pi_offline: bool = Field(default=True, validation_alias="PI_OFFLINE")
    pi_approve_project: bool = Field(default=True, validation_alias="PI_APPROVE_PROJECT")
    pi_timeout_seconds: float = Field(default=180.0, validation_alias="PI_TIMEOUT_SECONDS")
    pi_idle_timeout_seconds: float = Field(default=120.0, validation_alias="PI_IDLE_TIMEOUT_SECONDS")
    pi_session_store_path: str = Field(
        default="./runtime/server/pi-sessions.json",
        validation_alias="PI_SESSION_STORE_PATH",
    )

    streaming_enabled: bool = Field(default=True, validation_alias="STREAMING_ENABLED")
    # Feishu-only: progressively edit an interactive card as pi streams tokens.
    # WeChat is unaffected. Disable to restore the single buffered reply.
    feishu_streaming_edit_enabled: bool = Field(default=True, validation_alias="FEISHU_STREAMING_EDIT")
    # Min seconds between card updates and min newly-accumulated chars per update;
    # together they throttle PATCH calls to stay under Feishu update rate limits.
    feishu_stream_min_interval_seconds: float = Field(
        default=0.7, ge=0.0, validation_alias="FEISHU_STREAM_MIN_INTERVAL_SECONDS",
    )
    feishu_stream_min_chars: int = Field(default=40, ge=1, validation_alias="FEISHU_STREAM_MIN_CHARS")
    feishu_message_chunk_chars: int = Field(default=1500, validation_alias="FEISHU_MESSAGE_CHUNK_CHARS")
    # Feishu caps im/v1/files at 30 MB, the tighter of the two channels.
    push_file_max_mb: int = Field(default=30, validation_alias="PUSH_FILE_MAX_MB")
    # The server binds 0.0.0.0, so /push/file needs its own bearer secret.
    push_api_token: str = Field(default="", validation_alias="PUSH_API_TOKEN")
    wechat_webhook_token: str = Field(default="", validation_alias="WECHAT_WEBHOOK_TOKEN")
    wechat_message_chunk_chars: int = Field(default=1800, validation_alias="WECHAT_MESSAGE_CHUNK_CHARS")
    wechat_sidecar_base_url: str = Field(default="http://127.0.0.1:8787", validation_alias="WECHAT_SIDECAR_BASE_URL")
    deduplicate_ttl_seconds: int = Field(default=3600, validation_alias="DEDUPLICATE_TTL_SECONDS")
    reminder_store_path: str = Field(default="./runtime/server/reminders.json", validation_alias="REMINDER_STORE_PATH")
    daily_task_store_path: str = Field(
        default="./runtime/server/daily-tasks.json",
        validation_alias="DAILY_TASK_STORE_PATH",
    )

    memory_enabled: bool = Field(default=True, validation_alias="MEMORY_ENABLED")
    memory_dir: str = Field(default="./memory", validation_alias="MEMORY_DIR")
    memory_categories: str = Field(
        default="basic,health,preference,work,finance,recent",
        validation_alias="MEMORY_CATEGORIES",
    )
    memory_always_inject: str = Field(
        default="basic,health,recent",
        validation_alias="MEMORY_ALWAYS_INJECT",
    )
    memory_max_inject_chars: int = Field(default=4000, validation_alias="MEMORY_MAX_INJECT_CHARS")
    memory_git_auto_commit: bool = Field(default=True, validation_alias="MEMORY_GIT_AUTO_COMMIT")
    memory_git_dir: str = Field(
        default="./runtime/memory-git",
        validation_alias="MEMORY_GIT_DIR",
    )
    memory_context_path: str = Field(
        default="./runtime/server/memory-context.md",
        validation_alias="MEMORY_CONTEXT_PATH",
    )

    server_host: str = Field(default="0.0.0.0", validation_alias="SERVER_HOST")
    server_port: int = Field(default=8080, validation_alias="SERVER_PORT")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")

    @property
    def memory_category_list(self) -> list[str]:
        """Categories the agent is allowed to write, in declared order."""
        return _split_csv(self.memory_categories)

    @property
    def memory_always_inject_list(self) -> list[str]:
        """Categories injected into every turn's context."""
        return _split_csv(self.memory_always_inject)

    @property
    def feishu_tenant_token_url(self) -> str:
        return f"{self.feishu_api_base.rstrip('/')}/open-apis/auth/v3/tenant_access_token/internal"

    @property
    def feishu_reply_url_template(self) -> str:
        base = self.feishu_api_base.rstrip("/")
        return f"{base}/open-apis/im/v1/messages/{{message_id}}/reply"

    @property
    def feishu_send_message_url(self) -> str:
        base = self.feishu_api_base.rstrip("/")
        return f"{base}/open-apis/im/v1/messages"

    @property
    def feishu_message_url_template(self) -> str:
        # PATCH here to update the content of an interactive card the bot sent.
        base = self.feishu_api_base.rstrip("/")
        return f"{base}/open-apis/im/v1/messages/{{message_id}}"

    @property
    def feishu_image_upload_url(self) -> str:
        base = self.feishu_api_base.rstrip("/")
        return f"{base}/open-apis/im/v1/images"

    @property
    def feishu_file_upload_url(self) -> str:
        base = self.feishu_api_base.rstrip("/")
        return f"{base}/open-apis/im/v1/files"

    @property
    def feishu_message_resource_url_template(self) -> str:
        base = self.feishu_api_base.rstrip("/")
        return f"{base}/open-apis/im/v1/messages/{{message_id}}/resources/{{file_key}}"

    @property
    def feishu_reaction_url_template(self) -> str:
        base = self.feishu_api_base.rstrip("/")
        return f"{base}/open-apis/im/v1/messages/{{message_id}}/reactions"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
