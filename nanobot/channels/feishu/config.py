"""Dependency-free Feishu configuration model shared by management and runtime."""

from typing import Literal

from pydantic import Field

from nanobot.config.schema import Base


class FeishuConfig(Base):
    """Feishu/Lark channel configuration using WebSocket long connection."""

    instance_id: str = "default"
    name: str = "nanobot"
    identity_key: str = ""
    enabled: bool = False
    app_id: str = ""
    app_secret: str = ""
    encrypt_key: str = ""
    verification_token: str = ""
    allow_from: list[str] = Field(default_factory=list)
    react_emoji: str = "THUMBSUP"
    done_emoji: str | None = None
    listen_emoji: str = "Pin"  # Ack on listen history-only ingest; set "" to disable
    tool_hint_prefix: str = "\U0001f527"
    hint_mode: Literal["inline", "live"] = "live"
    live_tool_hint_heartbeat_seconds: float = Field(default=10, ge=0)
    live_tool_hint_max_length: int = Field(default=160, ge=40, le=500)
    live_tool_hint_processing_note: str = "processing"
    live_tool_hint_done_note: str = "done"
    group_policy: Literal["open", "mention", "listen"] = "mention"
    reply_to_message: bool = False
    streaming: bool = True
    domain: Literal["feishu", "lark"] = "feishu"
    topic_isolation: bool = True


def feishu_default_config() -> dict[str, object]:
    return FeishuConfig().model_dump(by_alias=True)


__all__ = ["FeishuConfig", "feishu_default_config"]
