"""WeCom (Enterprise WeChat) channel implementation using wecom_aibot_sdk."""

import asyncio
import base64
import hashlib
import importlib.util
import os
import re
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import Field

from nanobot.bus.events import OutboundMessage
from nanobot.bus.outbound_events import ProgressEvent
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.paths import get_media_dir
from nanobot.config.schema import Base

WECOM_AVAILABLE = importlib.util.find_spec("wecom_aibot_sdk") is not None

# Upload safety limits (matching QQ channel defaults)
WECOM_UPLOAD_MAX_BYTES = 1024 * 1024 * 200  # 200MB

# A single space is the placeholder the WeCom client renders as the three-dot bubble.
WECOM_THINKING_PLACEHOLDER = " "
WECOM_TOOL_REWRITE_INTERVAL_S = 3.0
WECOM_STREAM_BUDGET_PER_MINUTE = 20
WECOM_STREAM_BUDGET_WINDOW_S = 60.0
WECOM_MAX_OPEN_STREAMS_PER_CHAT = 3
WECOM_STREAM_MAX_AGE_S = 9 * 60
WECOM_STREAM_MAP_CAP = 64
# Documented WeCom "api freq out of limit".
_WECOM_FREQ_ERRCODE = 45009

# Replace unsafe characters with "_", keep Chinese and common safe punctuation.
_SAFE_NAME_RE = re.compile(r"[^\w.\-()\[\]（）【】\u4e00-\u9fff]+", re.UNICODE)


def _sanitize_filename(name: str) -> str:
    """Sanitize filename to avoid traversal and problematic chars."""
    name = (name or "").strip()
    name = Path(name).name
    name = _SAFE_NAME_RE.sub("_", name).strip("._ ")
    return name


_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_VIDEO_EXTS = {".mp4", ".avi", ".mov"}
_AUDIO_EXTS = {".amr", ".mp3", ".wav", ".ogg"}


def _guess_wecom_media_type(filename: str) -> str:
    """Classify file extension as WeCom media_type string."""
    ext = Path(filename).suffix.lower()
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _VIDEO_EXTS:
        return "video"
    if ext in _AUDIO_EXTS:
        return "voice"
    return "file"

class _WecomFrequencyLimit(RuntimeError):
    """WeCom rejected a stream frame because the chat is over its send budget."""


@dataclass
class _WecomOpenStream:
    """One unfinished WeCom stream reply bound to a single inbound message."""

    stream_id: str
    frame: Any
    chat_id: str
    msgid: str
    opened_at: float
    last_tool_at: float = 0.0
    last_tool_text: str = ""
    expire_task: asyncio.Task[None] | None = field(default=None, repr=False)


def _explicit_bool(raw: Any, *keys: str) -> bool | None:
    """Return a bool the caller actually set, or None when the key is absent."""
    if isinstance(raw, dict):
        for key in keys:
            value = raw.get(key)
            if isinstance(value, bool):
                return value
        return None
    fields_set = getattr(raw, "model_fields_set", None)
    if isinstance(fields_set, set) and "send_tool_hints" in fields_set:
        value = getattr(raw, "send_tool_hints", None)
        if isinstance(value, bool):
            return value
    return None


def _ack_errcode(ack: Any) -> int | None:
    """Return an integer WeCom ack code. Non-int stand-ins (test mocks) are ignored."""
    code = getattr(ack, "errcode", None)
    if isinstance(code, int):
        return code
    return None


def _is_frequency_limited(ack: Any, code: int | None = None) -> bool:
    """True when WeCom refused the frame for the per-chat send cap."""
    resolved = _ack_errcode(ack) if code is None else code
    if resolved == _WECOM_FREQ_ERRCODE:
        return True
    errmsg = str(getattr(ack, "errmsg", "") or "").lower()
    return "freq" in errmsg or "频率" in errmsg


def _is_ack_timeout(exc: BaseException) -> bool:
    """True when the SDK wrote a frame and then timed out waiting for the ack."""
    if isinstance(exc, TimeoutError | asyncio.TimeoutError):
        return True
    text = f"{type(exc).__name__} {exc}".lower()
    return "timeout" in text or "timed out" in text


def _outbound_msgid(msg: OutboundMessage) -> str:
    """Return the inbound message id carried on an outbound reply, if any."""
    meta = msg.metadata or {}
    for key in ("message_id", "origin_message_id"):
        value = meta.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


class WecomConfig(Base):
    """WeCom (Enterprise WeChat) AI Bot channel configuration."""

    enabled: bool = False
    bot_id: str = ""
    secret: str = ""
    allow_from: list[str] = Field(default_factory=list)
    welcome_message: str = ""
    thinking_bubble: bool = True
    # Channel-local default. The global channels.send_tool_hints stays false.
    send_tool_hints: bool = True


# Message type display mapping
MSG_TYPE_MAP = {
    "image": "[image]",
    "voice": "[voice]",
    "file": "[file]",
    "mixed": "[mixed content]",
}


class WecomChannel(BaseChannel):
    """
    WeCom (Enterprise WeChat) channel using WebSocket long connection.

    Uses WebSocket to receive events - no public IP or webhook required.

    Requires:
    - Bot ID and Secret from WeCom AI Bot platform
    """

    name = "wecom"
    display_name = "WeCom"

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return WecomConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        raw = config
        tool_hint_override = _explicit_bool(raw, "send_tool_hints", "sendToolHints")
        if isinstance(config, dict):
            config = WecomConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: WecomConfig = config
        self._tool_hints_override = tool_hint_override
        self._client: Any = None
        self._processed_message_ids: OrderedDict[str, None] = OrderedDict()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._generate_req_id = None
        # Store frame headers for each chat to enable replies
        self._chat_frames: dict[str, Any] = {}
        # Open loading bubbles, keyed by inbound msgid. Capped LRU.
        self._open_streams: OrderedDict[str, _WecomOpenStream] = OrderedDict()
        self._finished_msgids: OrderedDict[str, None] = OrderedDict()
        # Monotonic timestamps of stream frames sent to each chat.
        self._stream_sends: dict[str, deque[float]] = {}
        # chat_id -> monotonic time until which new placeholders are skipped.
        self._placeholder_blocked_until: dict[str, float] = {}

    async def start(self) -> None:
        """Start the WeCom bot with WebSocket long connection."""
        if not WECOM_AVAILABLE:
            self.logger.error("SDK not installed. Run: nanobot plugins enable wecom")
            return

        if not self.config.bot_id or not self.config.secret:
            self.logger.error("bot_id and secret not configured")
            return

        from wecom_aibot_sdk import WSClient, generate_req_id

        self._running = True
        self._loop = asyncio.get_running_loop()
        self._generate_req_id = generate_req_id
        self._apply_tool_hint_delivery()

        # Create WebSocket client
        self._client = WSClient({
            "bot_id": self.config.bot_id,
            "secret": self.config.secret,
            "reconnect_interval": 1000,
            "max_reconnect_attempts": -1,  # Infinite reconnect
            "heartbeat_interval": 30000,
        })

        # Register event handlers
        self._client.on("connected", self._on_connected)
        self._client.on("authenticated", self._on_authenticated)
        self._client.on("disconnected", self._on_disconnected)
        self._client.on("error", self._on_error)
        self._client.on("message.text", self._on_text_message)
        self._client.on("message.image", self._on_image_message)
        self._client.on("message.voice", self._on_voice_message)
        self._client.on("message.file", self._on_file_message)
        self._client.on("message.mixed", self._on_mixed_message)
        self._client.on("event.enter_chat", self._on_enter_chat)

        self.logger.info("bot starting with WebSocket long connection")
        self.logger.info("No public IP required - using WebSocket to receive events")

        # Connect
        await self._client.connect_async()

        # Keep running until stopped
        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        """Stop the WeCom bot."""
        self._running = False
        self._cancel_open_streams()
        if self._client:
            await self._client.disconnect()
        self.logger.info("bot stopped")

    def _now(self) -> float:
        """Monotonic clock used for send budgets and the tool-step clamp."""
        return time.monotonic()

    def _apply_tool_hint_delivery(self) -> None:
        """Turn tool-hint delivery on for this channel unless config says otherwise.

        The channel manager copies ``channels.send_tool_hints`` (default false)
        onto the instance before ``start``. An omitted WeCom key keeps this
        channel's own default so tool lines can update the loading bubble.
        """
        if self._tool_hints_override is not None:
            self.send_tool_hints = self._tool_hints_override
            return
        if self.config.thinking_bubble and self.config.send_tool_hints:
            self.send_tool_hints = True

    def _ensure_req_id(self) -> bool:
        """Bind the SDK req-id helper. False when the SDK is not importable."""
        if self._generate_req_id is not None:
            return True
        try:
            from wecom_aibot_sdk import generate_req_id
        except Exception:
            self.logger.warning("wecom stream id helper unavailable")
            return False
        self._generate_req_id = generate_req_id
        return True

    async def _on_connected(self, frame: Any) -> None:
        """Handle WebSocket connected event."""
        self.logger.info("WebSocket connected")

    async def _on_authenticated(self, frame: Any) -> None:
        """Handle authentication success event."""
        self.logger.info("authenticated successfully")

    async def _on_disconnected(self, frame: Any) -> None:
        """Handle WebSocket disconnected event."""
        reason = frame.body if hasattr(frame, 'body') else str(frame)
        self.logger.warning("WebSocket disconnected: {}", reason)

    async def _on_error(self, frame: Any) -> None:
        """Handle error event."""
        self.logger.error("error: {}", frame)

    async def _on_text_message(self, frame: Any) -> None:
        """Handle text message."""
        await self._process_message(frame, "text")

    async def _on_image_message(self, frame: Any) -> None:
        """Handle image message."""
        await self._process_message(frame, "image")

    async def _on_voice_message(self, frame: Any) -> None:
        """Handle voice message."""
        await self._process_message(frame, "voice")

    async def _on_file_message(self, frame: Any) -> None:
        """Handle file message."""
        await self._process_message(frame, "file")

    async def _on_mixed_message(self, frame: Any) -> None:
        """Handle mixed content message."""
        await self._process_message(frame, "mixed")

    async def _on_enter_chat(self, frame: Any) -> None:
        """Handle enter_chat event (user opens chat with bot)."""
        try:
            # Extract body from WsFrame dataclass or dict
            if hasattr(frame, 'body'):
                body = frame.body or {}
            elif isinstance(frame, dict):
                body = frame.get("body", frame)
            else:
                body = {}

            chat_id = body.get("chatid", "") if isinstance(body, dict) else ""

            if chat_id and not self.is_allowed(chat_id):
                return

            if chat_id and self.config.welcome_message:
                await self._client.reply_welcome(frame, {
                    "msgtype": "text",
                    "text": {"content": self.config.welcome_message},
                })
        except Exception:
            self.logger.exception("Error handling enter_chat")

    async def _process_message(self, frame: Any, msg_type: str) -> None:
        """Process incoming message and forward to bus."""
        try:
            # Extract body from WsFrame dataclass or dict
            if hasattr(frame, 'body'):
                body = frame.body or {}
            elif isinstance(frame, dict):
                body = frame.get("body", frame)
            else:
                body = {}

            # Ensure body is a dict
            if not isinstance(body, dict):
                self.logger.warning("Invalid body type: {}", type(body))
                return

            # Extract message info
            msg_id = body.get("msgid", "")
            if not msg_id:
                msg_id = f"{body.get('chatid', '')}_{body.get('sendertime', '')}"

            # Extract sender info from "from" field (SDK format)
            from_info = body.get("from", {})
            sender_id = from_info.get("userid", "unknown") if isinstance(from_info, dict) else "unknown"
            if not self.is_allowed(sender_id):
                return

            # Deduplication check
            if msg_id in self._processed_message_ids:
                return
            self._processed_message_ids[msg_id] = None

            # Trim cache
            while len(self._processed_message_ids) > 1000:
                self._processed_message_ids.popitem(last=False)

            # For single chat, chatid is the sender's userid
            # For group chat, chatid is provided in body
            chat_type = body.get("chattype", "single")
            chat_id = body.get("chatid", sender_id)

            content_parts = []
            media_paths: list[str] = []

            if msg_type == "text":
                text = body.get("text", {}).get("content", "")
                if text:
                    content_parts.append(text)

            elif msg_type == "image":
                image_info = body.get("image", {})
                file_url = image_info.get("url", "")
                aes_key = image_info.get("aeskey", "")

                if file_url and aes_key:
                    file_path = await self._download_and_save_media(file_url, aes_key, "image")
                    if file_path:
                        filename = os.path.basename(file_path)
                        content_parts.append(f"[image: {filename}]")
                        media_paths.append(file_path)
                    else:
                        content_parts.append("[image: download failed]")
                else:
                    content_parts.append("[image: download failed]")

            elif msg_type == "voice":
                voice_info = body.get("voice", {})
                # Voice message already contains transcribed content from WeCom
                voice_content = voice_info.get("content", "")
                if voice_content:
                    content_parts.append(f"[voice] {voice_content}")
                else:
                    content_parts.append("[voice]")

            elif msg_type == "file":
                file_info = body.get("file", {})
                file_url = file_info.get("url", "")
                aes_key = file_info.get("aeskey", "")
                file_name = file_info.get("name") or None

                if file_url and aes_key:
                    file_path = await self._download_and_save_media(file_url, aes_key, "file", file_name)
                    if file_path:
                        display_name = os.path.basename(file_path)
                        content_parts.append(f"[file: {display_name}]")
                        media_paths.append(file_path)
                    else:
                        content_parts.append(f"[file: {file_name or 'unknown'}: download failed]")
                else:
                    content_parts.append(f"[file: {file_name or 'unknown'}: download failed]")

            elif msg_type == "mixed":
                # Mixed content contains multiple message items
                msg_items = body.get("mixed", {}).get("msg_item", [])
                for item in msg_items:
                    item_type = item.get("msgtype", "")
                    if item_type == "text":
                        text = item.get("text", {}).get("content", "")
                        if text:
                            content_parts.append(text)
                    elif item_type == "image":
                        file_url = item.get("image", {}).get("url", "")
                        aes_key = item.get("image", {}).get("aeskey", "")
                        if file_url and aes_key:
                            file_path = await self._download_and_save_media(file_url, aes_key, "image")
                            if file_path:
                                filename = os.path.basename(file_path)
                                content_parts.append(f"[image: {filename}]")
                                media_paths.append(file_path)
                    else:
                        content_parts.append(MSG_TYPE_MAP.get(item_type, f"[{item_type}]"))

            else:
                content_parts.append(MSG_TYPE_MAP.get(msg_type, f"[{msg_type}]"))

            content = "\n".join(content_parts) if content_parts else ""

            if not content:
                return

            # Store frame for this chat to enable replies
            self._chat_frames[chat_id] = frame
            await self._open_thinking_bubble(frame, chat_id, msg_id)

            # Forward to message bus
            await self._handle_message(
                sender_id=sender_id,
                chat_id=chat_id,
                content=content,
                media=media_paths or None,
                metadata={
                    "message_id": msg_id,
                    "msg_type": msg_type,
                    "chat_type": chat_type,
                }
            )

        except Exception:
            self.logger.exception("Error processing message")

    async def _download_and_save_media(
        self,
        file_url: str,
        aes_key: str,
        media_type: str,
        filename: str | None = None,
    ) -> str | None:
        """
        Download and decrypt media from WeCom.

        Returns:
            file_path or None if download failed
        """
        try:
            data, fname = await self._client.download_file(file_url, aes_key)

            if not data:
                self.logger.warning("Failed to download media")
                return None

            if len(data) > WECOM_UPLOAD_MAX_BYTES:
                self.logger.warning(
                    "inbound media too large: {} bytes (max {})",
                    len(data),
                    WECOM_UPLOAD_MAX_BYTES,
                )
                return None

            media_dir = get_media_dir("wecom")
            if not filename:
                filename = fname or f"{media_type}_{hash(file_url) % 100000}"
            filename = _sanitize_filename(filename)

            file_path = media_dir / filename
            await asyncio.to_thread(file_path.write_bytes, data)
            self.logger.debug("Downloaded {} to {}", media_type, file_path)
            return str(file_path)

        except Exception:
            self.logger.exception("Error downloading media")
            return None

    async def _upload_media_ws(
        self, client: Any, file_path: str,
    ) -> "tuple[str, str] | tuple[None, None]":
        """Upload a local file to WeCom via WebSocket 3-step protocol (base64).

        Uses the WeCom WebSocket upload commands directly via
        ``client._ws_manager.send_reply()``:

          ``aibot_upload_media_init``   → upload_id
          ``aibot_upload_media_chunk`` × N  (≤512 KB raw per chunk, base64)
          ``aibot_upload_media_finish`` → media_id

        Returns (media_id, media_type) on success, (None, None) on failure.
        """
        from wecom_aibot_sdk.utils import generate_req_id as _gen_req_id

        try:
            fname = os.path.basename(file_path)
            media_type = _guess_wecom_media_type(fname)

            # Read file size and data in a thread to avoid blocking the event loop
            def _read_file():
                file_size = os.path.getsize(file_path)
                if file_size > WECOM_UPLOAD_MAX_BYTES:
                    raise ValueError(
                        f"File too large: {file_size} bytes (max {WECOM_UPLOAD_MAX_BYTES})"
                    )
                with open(file_path, "rb") as f:
                    return file_size, f.read()

            file_size, data = await asyncio.to_thread(_read_file)
            # MD5 is used for file integrity only, not cryptographic security
            md5_hash = hashlib.md5(data).hexdigest()

            chunk_size = 512 * 1024  # 512 KB raw (before base64)
            mv = memoryview(data)
            chunk_list = [bytes(mv[i : i + chunk_size]) for i in range(0, file_size, chunk_size)]
            n_chunks = len(chunk_list)
            del mv, data

            # Step 1: init
            req_id = _gen_req_id("upload_init")
            resp = await client._ws_manager.send_reply(req_id, {
                "type": media_type,
                "filename": fname,
                "total_size": file_size,
                "total_chunks": n_chunks,
                "md5": md5_hash,
            }, "aibot_upload_media_init")
            if resp.errcode != 0:
                self.logger.warning("upload init failed ({}): {}", resp.errcode, resp.errmsg)
                return None, None
            upload_id = resp.body.get("upload_id") if resp.body else None
            if not upload_id:
                self.logger.warning("upload init: no upload_id in response")
                return None, None

            # Step 2: send chunks
            for i, chunk in enumerate(chunk_list):
                req_id = _gen_req_id("upload_chunk")
                resp = await client._ws_manager.send_reply(req_id, {
                    "upload_id": upload_id,
                    "chunk_index": i,
                    "base64_data": base64.b64encode(chunk).decode(),
                }, "aibot_upload_media_chunk")
                if resp.errcode != 0:
                    self.logger.warning("upload chunk {} failed ({}): {}", i, resp.errcode, resp.errmsg)
                    return None, None

            # Step 3: finish
            req_id = _gen_req_id("upload_finish")
            resp = await client._ws_manager.send_reply(req_id, {
                "upload_id": upload_id,
            }, "aibot_upload_media_finish")
            if resp.errcode != 0:
                self.logger.warning("upload finish failed ({}): {}", resp.errcode, resp.errmsg)
                return None, None

            media_id = resp.body.get("media_id") if resp.body else None
            if not media_id:
                self.logger.warning("upload finish: no media_id in response body={}", resp.body)
                return None, None

            suffix = "..." if len(media_id) > 16 else ""
            self.logger.debug("uploaded {} ({}) → media_id={}", fname, media_type, media_id[:16] + suffix)
            return media_id, media_type

        except ValueError as e:
            self.logger.warning("upload skipped for {}: {}", file_path, e)
            return None, None
        except Exception:
            self.logger.exception("_upload_media_ws error for {}", file_path)
            return None, None

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through WeCom."""
        if not self._client:
            raise RuntimeError("WeCom client not initialized")

        event = msg.event
        if isinstance(event, ProgressEvent):
            await self._send_progress(msg, event)
            return

        try:
            msgid = _outbound_msgid(msg)
            if msgid and msgid in self._finished_msgids:
                return

            content = (msg.content or "").strip()
            frame = self._chat_frames.get(msg.chat_id)
            stream = self._lookup_open_stream(msg)

            for file_path in msg.media or []:
                if not os.path.isfile(file_path):
                    self.logger.warning("media file not found: {}", file_path)
                    continue
                media_id, media_type = await self._upload_media_ws(self._client, file_path)
                if media_id:
                    if frame:
                        await self._client.reply(frame, {
                            "msgtype": media_type,
                            media_type: {"media_id": media_id},
                        })
                    else:
                        await self._client.send_message(msg.chat_id, {
                            "msgtype": media_type,
                            media_type: {"media_id": media_id},
                        })
                    self.logger.debug("sent {} → {}", media_type, msg.chat_id)
                else:
                    content += f"\n[file upload failed: {os.path.basename(file_path)}]"

            if stream is not None:
                # An empty final still closes the bubble. A blank finish frame
                # replaces the dots with an empty message, so reuse the placeholder.
                finish_text = content or WECOM_THINKING_PLACEHOLDER
                await self._finish_stream(stream, finish_text)
                return

            if not content:
                return

            if frame:
                # No open bubble (budget skip, opt-out, or a retry after the
                # placeholder failed). One finished stream, same as before.
                await self._send_oneshot(frame, msg.chat_id, content)
                return

            await self._client.send_message(msg.chat_id, {
                "msgtype": "markdown",
                "markdown": {"content": content},
            })
            self.logger.info("proactive send to {}", msg.chat_id)

        except Exception:
            self.logger.exception("Error sending message to chat_id={}", msg.chat_id)
            raise

    async def _send_progress(self, msg: OutboundMessage, event: ProgressEvent) -> None:
        """Rewrite the open bubble with one tool line, or do nothing.

        Reasoning, partial answer text, and tool hints with no open bubble
        must not raise and must not open another stream.
        """
        if not self.config.thinking_bubble or not event.tool_hint:
            return
        if event.reasoning or event.reasoning_delta or event.reasoning_end:
            return
        text = (msg.content or "").strip()
        if not text:
            return
        stream = self._lookup_open_stream(msg)
        if stream is None:
            return
        if text == stream.last_tool_text:
            return
        now = self._now()
        if stream.last_tool_at and now - stream.last_tool_at < WECOM_TOOL_REWRITE_INTERVAL_S:
            return
        if not self._stream_budget_allows(stream.chat_id, now):
            return
        try:
            await self._push_stream(stream.frame, stream.stream_id, text, finish=False)
        except _WecomFrequencyLimit:
            self._block_placeholders(stream.chat_id)
            self.logger.warning(
                "wecom tool rewrite skipped: chat {} is rate limited",
                stream.chat_id,
            )
            return
        except Exception:
            self.logger.warning("wecom tool rewrite failed chat_id={}", stream.chat_id)
            return
        stream.last_tool_at = now
        stream.last_tool_text = text
        self._note_stream_send(stream.chat_id, now)

    async def _open_thinking_bubble(self, frame: Any, chat_id: str, msgid: str) -> None:
        """One unfinished placeholder. A failure does not retry or block the inbound message."""
        if not self.config.thinking_bubble or not self._client or not msgid:
            return
        if msgid in self._open_streams or msgid in self._finished_msgids:
            return
        now = self._now()
        if self._placeholders_blocked(chat_id, now):
            return
        if self._open_count(chat_id) >= WECOM_MAX_OPEN_STREAMS_PER_CHAT:
            return
        if not self._stream_budget_allows(chat_id, now):
            return
        if not self._ensure_req_id():
            return
        stream_id = self._generate_req_id("stream")
        stream = _WecomOpenStream(
            stream_id=stream_id,
            frame=frame,
            chat_id=chat_id,
            msgid=msgid,
            opened_at=now,
        )
        try:
            await self._push_stream(
                stream.frame,
                stream.stream_id,
                WECOM_THINKING_PLACEHOLDER,
                finish=False,
            )
        except _WecomFrequencyLimit:
            self._block_placeholders(stream.chat_id)
            self.logger.warning(
                "wecom thinking bubble skipped: chat {} is rate limited",
                stream.chat_id,
            )
            return
        except Exception as exc:
            if _is_ack_timeout(exc):
                # The placeholder was written and the ack timed out. Keep the id
                # so the final answer closes this bubble instead of opening another.
                self._remember_stream(stream)
                self._note_stream_send(stream.chat_id, self._now())
                return
            self.logger.warning("wecom thinking bubble failed msgid={}", stream.msgid)
            return
        if stream.msgid in self._finished_msgids:
            return
        self._remember_stream(stream)
        self._note_stream_send(stream.chat_id, self._now())

    def _remember_stream(self, stream: _WecomOpenStream) -> None:
        """Store an open bubble and arm a single 9-minute close."""
        while len(self._open_streams) >= WECOM_STREAM_MAP_CAP:
            _msgid, evicted = self._open_streams.popitem(last=False)
            self._cancel_expire(evicted)
        self._open_streams[stream.msgid] = stream
        self._open_streams.move_to_end(stream.msgid)
        if self._running:
            stream.expire_task = asyncio.get_running_loop().create_task(
                self._expire_stream(stream.msgid)
            )

    def _lookup_open_stream(self, msg: OutboundMessage) -> _WecomOpenStream | None:
        """Match a reply to its bubble. An explicit id never takes another message's stream."""
        msgid = _outbound_msgid(msg)
        if msgid:
            stream = self._open_streams.get(msgid)
            if stream is not None and stream.chat_id == msg.chat_id:
                self._open_streams.move_to_end(msgid)
                return stream
            return None
        matches = [item for item in self._open_streams.values() if item.chat_id == msg.chat_id]
        if len(matches) == 1:
            return matches[0]
        return None

    async def _send_oneshot(self, frame: Any, chat_id: str, content: str) -> None:
        """Send one already-finished stream when no loading bubble is open."""
        if not self._ensure_req_id():
            raise RuntimeError("WeCom stream id helper unavailable")
        stream_id = self._generate_req_id("stream")
        await self._push_stream(frame, stream_id, content, finish=True)
        self._note_stream_send(chat_id, self._now())
        self.logger.debug("message sent to {}", chat_id)

    async def _finish_stream(self, stream: _WecomOpenStream, content: str) -> None:
        """Replace the open bubble with the final text on the same stream id."""
        try:
            await self._push_stream(stream.frame, stream.stream_id, content, finish=True)
        except _WecomFrequencyLimit:
            self._block_placeholders(stream.chat_id)
            raise
        except Exception as exc:
            if _is_ack_timeout(exc):
                # The finish frame was written; an ack timeout must not send it again.
                self.logger.warning(
                    "wecom finish ack timed out msgid={}; not resending",
                    stream.msgid,
                )
                self._complete_stream(stream)
                self._note_stream_send(stream.chat_id, self._now())
                return
            raise
        self._note_stream_send(stream.chat_id, self._now())
        self._complete_stream(stream)
        self.logger.debug("message sent to {}", stream.chat_id)

    async def _push_stream(
        self,
        frame: Any,
        stream_id: str,
        content: str,
        *,
        finish: bool,
    ) -> None:
        """Send one stream frame. A frequency-limit ack raises and is not retried here."""
        try:
            ack = await self._client.reply_stream(frame, stream_id, content, finish=finish)
        except Exception as exc:
            if _is_frequency_limited(exc):
                raise _WecomFrequencyLimit(str(exc)) from exc
            raise
        code = _ack_errcode(ack)
        if code not in (None, 0):
            if _is_frequency_limited(ack, code):
                raise _WecomFrequencyLimit(getattr(ack, "errmsg", "") or str(code))
            raise RuntimeError(f"wecom stream ack {code}")

    async def _expire_stream(self, msgid: str) -> None:
        """Close one bubble that is still open after about 9 minutes. One attempt."""
        try:
            await asyncio.sleep(WECOM_STREAM_MAX_AGE_S)
        except asyncio.CancelledError:
            return
        await self._close_expired_stream(msgid)

    async def _close_expired_stream(self, msgid: str) -> None:
        """Finish a stale bubble once, then drop it. No replacement stream."""
        stream = self._open_streams.get(msgid)
        if stream is None:
            return
        content = stream.last_tool_text or WECOM_THINKING_PLACEHOLDER
        try:
            await self._push_stream(stream.frame, stream.stream_id, content, finish=True)
            self._note_stream_send(stream.chat_id, self._now())
        except Exception:
            self.logger.warning("wecom stream expire finish failed msgid={}", msgid)
        self._complete_stream(stream)

    def _complete_stream(self, stream: _WecomOpenStream) -> None:
        """Forget a bubble so a later send cannot open or finish it again."""
        self._open_streams.pop(stream.msgid, None)
        self._finished_msgids[stream.msgid] = None
        while len(self._finished_msgids) > 1000:
            self._finished_msgids.popitem(last=False)
        self._cancel_expire(stream)

    def _cancel_expire(self, stream: _WecomOpenStream) -> None:
        task = stream.expire_task
        stream.expire_task = None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    def _cancel_open_streams(self) -> None:
        for stream in list(self._open_streams.values()):
            self._cancel_expire(stream)

    def _open_count(self, chat_id: str) -> int:
        return sum(1 for stream in self._open_streams.values() if stream.chat_id == chat_id)

    def _placeholders_blocked(self, chat_id: str, now: float) -> bool:
        until = self._placeholder_blocked_until.get(chat_id)
        if until is None:
            return False
        if now >= until:
            self._placeholder_blocked_until.pop(chat_id, None)
            return False
        return True

    def _block_placeholders(self, chat_id: str) -> None:
        """Skip new dots for this chat until the one-minute window moves."""
        self._placeholder_blocked_until[chat_id] = self._now() + WECOM_STREAM_BUDGET_WINDOW_S

    def _stream_budget_allows(self, chat_id: str, now: float) -> bool:
        return self._recent_stream_sends(chat_id, now) < WECOM_STREAM_BUDGET_PER_MINUTE

    def _recent_stream_sends(self, chat_id: str, now: float) -> int:
        sent = self._stream_sends.get(chat_id)
        if not sent:
            return 0
        while sent and now - sent[0] >= WECOM_STREAM_BUDGET_WINDOW_S:
            sent.popleft()
        return len(sent)

    def _note_stream_send(self, chat_id: str, now: float) -> None:
        sent = self._stream_sends.setdefault(chat_id, deque())
        sent.append(now)
        while sent and now - sent[0] >= WECOM_STREAM_BUDGET_WINDOW_S:
            sent.popleft()
