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
    group_policy: Literal["open", "mention", "listen"] = "mention"
    reply_to_message: bool = False
    streaming: bool = True
    domain: Literal["feishu", "lark"] = "feishu"
    topic_isolation: bool = True
    # QR-login `addons`: which scopes/events the scan-to-create confirm page
    # pre-fills. None -> built-in defaults (cardkit:card:write +
    # im.message.recalled_v1); [] -> drop that category; a list -> replaces it.
    qr_login_scopes: list[str] | None = None
    qr_login_events: list[str] | None = None


def feishu_default_config() -> dict[str, object]:
    return FeishuConfig().model_dump(by_alias=True)


__all__ = ["FeishuConfig", "feishu_default_config"]
