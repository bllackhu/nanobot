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
    live_tool_hint_heartbeat_seconds: float = Field(default=0.3, ge=0)
    live_tool_hint_max_length: int = Field(default=160, ge=40, le=500)
    live_tool_hint_processing_note: str = "processing"
    live_tool_hint_done_note: str = "done"
    live_tool_hint_print_frequency_ms: int | None = Field(default=1, ge=1, le=70)
    live_tool_hint_print_step: int | None = Field(default=1, ge=1, le=20)
    live_tool_hint_print_strategy: Literal["fast", "delay"] = Field(default="fast")
    live_tool_hint_typewriter_cap_ms: int = Field(default=100, ge=0, le=2000)
    live_tool_hint_min_dwell_ms: int = Field(default=800, ge=0, le=2000)
    live_tool_hint_done_hold_ms: int = Field(default=200, ge=0, le=2000)
    live_tool_hint_elapsed_after_ms: int = Field(default=3000, ge=0, le=600000)
    # Circuit breaker for non-fatal live-hint heartbeat failures. Fatal
    # CardKit codes 200850 (streaming timeout) and 300309 (streaming mode
    # closed) retire the card on the first response instead of waiting for
    # this count — reopening streaming_mode would start another 10-minute window.
    live_tool_hint_max_consecutive_failures: int = Field(default=5, ge=1, le=50)
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
