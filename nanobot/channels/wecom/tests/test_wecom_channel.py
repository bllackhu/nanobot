"""Tests for WeCom channel: helpers, download, upload, send, and message processing."""

import os
import tempfile
from collections import deque
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

try:
    import importlib.util

    WECOM_AVAILABLE = importlib.util.find_spec("wecom_aibot_sdk") is not None
except ImportError:
    WECOM_AVAILABLE = False

if not WECOM_AVAILABLE:
    pytest.skip("WeCom dependencies not installed (wecom_aibot_sdk)", allow_module_level=True)

from nanobot.bus.events import OutboundMessage
from nanobot.bus.outbound_events import ProgressEvent
from nanobot.bus.queue import MessageBus
from nanobot.channels.wecom.runtime import (
    WecomChannel,
    WecomConfig,
    _guess_wecom_media_type,
    _sanitize_filename,
)

# Try to import the real response class; fall back to a stub if unavailable.
try:
    from wecom_aibot_sdk.utils import WsResponse

    _RealWsResponse = WsResponse
except ImportError:
    _RealWsResponse = None


class _FakeResponse:
    """Minimal stand-in for wecom_aibot_sdk WsResponse."""

    def __init__(self, errcode: int = 0, body: dict | None = None, errmsg: str = "ok"):
        self.errcode = errcode
        self.errmsg = errmsg
        self.body = body or {}


class _FakeWsManager:
    """Tracks send_reply calls and returns configurable responses."""

    def __init__(self, responses: list[_FakeResponse] | None = None):
        self.responses = responses or []
        self.calls: list[tuple[str, dict, str]] = []
        self._idx = 0

    async def send_reply(self, req_id: str, data: dict, cmd: str) -> _FakeResponse:
        self.calls.append((req_id, data, cmd))
        if self._idx < len(self.responses):
            resp = self.responses[self._idx]
            self._idx += 1
            return resp
        return _FakeResponse()


class _FakeFrame:
    """Minimal frame object with a body dict."""

    def __init__(self, body: dict | None = None):
        self.body = body or {}


class _FakeWeComClient:
    """Fake WeCom client with mock methods."""

    def __init__(self, ws_responses: list[_FakeResponse] | None = None):
        self._ws_manager = _FakeWsManager(ws_responses)
        self.download_file = AsyncMock(return_value=(None, None))
        self.reply = AsyncMock()
        self.reply_stream = AsyncMock()
        self.send_message = AsyncMock()
        self.reply_welcome = AsyncMock()


# ── Helper function tests (pure, no async) ──────────────────────────


def test_sanitize_filename_strips_path_traversal() -> None:
    assert _sanitize_filename("../../etc/passwd") == "passwd"


def test_sanitize_filename_keeps_chinese_chars() -> None:
    assert _sanitize_filename("文件（1）.jpg") == "文件（1）.jpg"


def test_sanitize_filename_empty_input() -> None:
    assert _sanitize_filename("") == ""


def test_guess_wecom_media_type_image() -> None:
    for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
        assert _guess_wecom_media_type(f"photo{ext}") == "image"


def test_guess_wecom_media_type_video() -> None:
    for ext in (".mp4", ".avi", ".mov"):
        assert _guess_wecom_media_type(f"video{ext}") == "video"


def test_guess_wecom_media_type_voice() -> None:
    for ext in (".amr", ".mp3", ".wav", ".ogg"):
        assert _guess_wecom_media_type(f"audio{ext}") == "voice"


def test_guess_wecom_media_type_file_fallback() -> None:
    for ext in (".pdf", ".doc", ".xlsx", ".zip"):
        assert _guess_wecom_media_type(f"doc{ext}") == "file"


def test_guess_wecom_media_type_case_insensitive() -> None:
    assert _guess_wecom_media_type("photo.PNG") == "image"
    assert _guess_wecom_media_type("photo.Jpg") == "image"


# ── _download_and_save_media() ──────────────────────────────────────


@pytest.mark.asyncio
async def test_download_and_save_success() -> None:
    """Successful download writes file and returns sanitized path."""
    channel = WecomChannel(WecomConfig(bot_id="b", secret="s", allow_from=["*"]), MessageBus())
    client = _FakeWeComClient()
    channel._client = client

    fake_data = b"\x89PNG\r\nfake image"
    client.download_file.return_value = (fake_data, "raw_photo.png")

    with patch("nanobot.channels.wecom.runtime.get_media_dir", return_value=Path(tempfile.gettempdir())):
        path = await channel._download_and_save_media("https://example.com/img.png", "aes_key", "image", "photo.png")

    assert path is not None
    assert os.path.isfile(path)
    assert os.path.basename(path) == "photo.png"
    # Cleanup
    os.unlink(path)


@pytest.mark.asyncio
async def test_download_and_save_oversized_rejected() -> None:
    """Data exceeding 200MB is rejected → returns None."""
    channel = WecomChannel(WecomConfig(bot_id="b", secret="s", allow_from=["*"]), MessageBus())
    client = _FakeWeComClient()
    channel._client = client

    big_data = b"\x00" * (200 * 1024 * 1024 + 1)  # 200MB + 1 byte
    client.download_file.return_value = (big_data, "big.bin")

    with patch("nanobot.channels.wecom.runtime.get_media_dir", return_value=Path(tempfile.gettempdir())):
        result = await channel._download_and_save_media("https://example.com/big.bin", "key", "file", "big.bin")

    assert result is None


@pytest.mark.asyncio
async def test_download_and_save_failure() -> None:
    """SDK returns None data → returns None."""
    channel = WecomChannel(WecomConfig(bot_id="b", secret="s", allow_from=["*"]), MessageBus())
    client = _FakeWeComClient()
    channel._client = client

    client.download_file.return_value = (None, None)

    with patch("nanobot.channels.wecom.runtime.get_media_dir", return_value=Path(tempfile.gettempdir())):
        result = await channel._download_and_save_media("https://example.com/fail.png", "key", "image")

    assert result is None


# ── _upload_media_ws() ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_media_ws_success() -> None:
    """Happy path: init → chunk → finish → returns (media_id, media_type)."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(b"\x89PNG\r\n")
        tmp = f.name

    try:
        responses = [
            _FakeResponse(errcode=0, body={"upload_id": "up_1"}),
            _FakeResponse(errcode=0, body={}),
            _FakeResponse(errcode=0, body={"media_id": "media_abc"}),
        ]

        client = _FakeWeComClient(responses)
        channel = WecomChannel(WecomConfig(bot_id="b", secret="s", allow_from=["*"]), MessageBus())
        channel._client = client

        with patch("wecom_aibot_sdk.utils.generate_req_id", side_effect=lambda x: f"req_{x}"):
            media_id, media_type = await channel._upload_media_ws(client, tmp)

        assert media_id == "media_abc"
        assert media_type == "image"
    finally:
        os.unlink(tmp)


@pytest.mark.asyncio
async def test_upload_media_ws_oversized_file() -> None:
    """File >200MB triggers ValueError → returns (None, None)."""
    # Instead of creating a real 200MB+ file, mock os.path.getsize and open
    with patch("os.path.getsize", return_value=200 * 1024 * 1024 + 1), \
         patch("builtins.open", MagicMock()):
        client = _FakeWeComClient()
        channel = WecomChannel(WecomConfig(bot_id="b", secret="s", allow_from=["*"]), MessageBus())
        channel._client = client

        result = await channel._upload_media_ws(client, "/fake/large.bin")
        assert result == (None, None)


@pytest.mark.asyncio
async def test_upload_media_ws_init_failure() -> None:
    """Init step returns errcode != 0 → returns (None, None)."""
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
        f.write(b"hello")
        tmp = f.name

    try:
        responses = [
            _FakeResponse(errcode=50001, errmsg="invalid"),
        ]

        client = _FakeWeComClient(responses)
        channel = WecomChannel(WecomConfig(bot_id="b", secret="s", allow_from=["*"]), MessageBus())
        channel._client = client

        with patch("wecom_aibot_sdk.utils.generate_req_id", side_effect=lambda x: f"req_{x}"):
            result = await channel._upload_media_ws(client, tmp)

        assert result == (None, None)
    finally:
        os.unlink(tmp)


@pytest.mark.asyncio
async def test_upload_media_ws_chunk_failure() -> None:
    """Chunk step returns errcode != 0 → returns (None, None)."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(b"\x89PNG\r\n")
        tmp = f.name

    try:
        responses = [
            _FakeResponse(errcode=0, body={"upload_id": "up_1"}),
            _FakeResponse(errcode=50002, errmsg="chunk fail"),
        ]

        client = _FakeWeComClient(responses)
        channel = WecomChannel(WecomConfig(bot_id="b", secret="s", allow_from=["*"]), MessageBus())
        channel._client = client

        with patch("wecom_aibot_sdk.utils.generate_req_id", side_effect=lambda x: f"req_{x}"):
            result = await channel._upload_media_ws(client, tmp)

        assert result == (None, None)
    finally:
        os.unlink(tmp)


@pytest.mark.asyncio
async def test_upload_media_ws_finish_no_media_id() -> None:
    """Finish step returns empty media_id → returns (None, None)."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(b"\x89PNG\r\n")
        tmp = f.name

    try:
        responses = [
            _FakeResponse(errcode=0, body={"upload_id": "up_1"}),
            _FakeResponse(errcode=0, body={}),
            _FakeResponse(errcode=0, body={}),  # no media_id
        ]

        client = _FakeWeComClient(responses)
        channel = WecomChannel(WecomConfig(bot_id="b", secret="s", allow_from=["*"]), MessageBus())
        channel._client = client

        with patch("wecom_aibot_sdk.utils.generate_req_id", side_effect=lambda x: f"req_{x}"):
            result = await channel._upload_media_ws(client, tmp)

        assert result == (None, None)
    finally:
        os.unlink(tmp)


# ── send() ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_text_with_frame() -> None:
    """When frame is stored, send uses reply_stream for final text."""
    channel = WecomChannel(WecomConfig(bot_id="b", secret="s", allow_from=["*"]), MessageBus())
    client = _FakeWeComClient()
    channel._client = client
    channel._generate_req_id = lambda x: f"req_{x}"
    channel._chat_frames["chat1"] = _FakeFrame()

    await channel.send(
        OutboundMessage(channel="wecom", chat_id="chat1", content="hello")
    )

    client.reply_stream.assert_called_once()
    call_args = client.reply_stream.call_args
    assert call_args[0][2] == "hello"  # content arg


@pytest.mark.asyncio
async def test_send_progress_without_tool_hint_is_a_noop() -> None:
    """Partial answer text and reasoning must not open another stream."""
    channel = WecomChannel(WecomConfig(bot_id="b", secret="s", allow_from=["*"]), MessageBus())
    client = _FakeWeComClient()
    channel._client = client
    channel._generate_req_id = lambda x: f"req_{x}"
    channel._chat_frames["chat1"] = _FakeFrame()

    await channel.send(
        OutboundMessage(
            channel="wecom",
            chat_id="chat1",
            content="thinking...",
            event=ProgressEvent(content="thinking..."),
        )
    )
    await channel.send(
        OutboundMessage(
            channel="wecom",
            chat_id="chat1",
            content="reasoning",
            event=ProgressEvent(content="reasoning", reasoning_delta=True),
        )
    )

    client.reply_stream.assert_not_called()


@pytest.mark.asyncio
async def test_send_proactive_without_frame() -> None:
    """Without stored frame, send uses send_message with markdown."""
    channel = WecomChannel(WecomConfig(bot_id="b", secret="s", allow_from=["*"]), MessageBus())
    client = _FakeWeComClient()
    channel._client = client

    await channel.send(
        OutboundMessage(channel="wecom", chat_id="chat1", content="proactive msg")
    )

    client.send_message.assert_called_once()
    call_args = client.send_message.call_args
    assert call_args[0][0] == "chat1"
    assert call_args[0][1]["msgtype"] == "markdown"


@pytest.mark.asyncio
async def test_send_media_then_text() -> None:
    """Media files are uploaded and sent before text content."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(b"\x89PNG\r\n")
        tmp = f.name

    try:
        responses = [
            _FakeResponse(errcode=0, body={"upload_id": "up_1"}),
            _FakeResponse(errcode=0, body={}),
            _FakeResponse(errcode=0, body={"media_id": "media_123"}),
        ]

        channel = WecomChannel(WecomConfig(bot_id="b", secret="s", allow_from=["*"]), MessageBus())
        client = _FakeWeComClient(responses)
        channel._client = client
        channel._generate_req_id = lambda x: f"req_{x}"
        channel._chat_frames["chat1"] = _FakeFrame()

        await channel.send(
            OutboundMessage(channel="wecom", chat_id="chat1", content="see image", media=[tmp])
        )

        # Media should have been sent via reply
        media_calls = [c for c in client.reply.call_args_list if c[0][1].get("msgtype") == "image"]
        assert len(media_calls) == 1
        assert media_calls[0][0][1]["image"]["media_id"] == "media_123"

        # Text should have been sent via reply_stream
        client.reply_stream.assert_called_once()
    finally:
        os.unlink(tmp)


@pytest.mark.asyncio
async def test_send_media_file_not_found() -> None:
    """Non-existent media path is skipped with a warning."""
    channel = WecomChannel(WecomConfig(bot_id="b", secret="s", allow_from=["*"]), MessageBus())
    client = _FakeWeComClient()
    channel._client = client
    channel._generate_req_id = lambda x: f"req_{x}"
    channel._chat_frames["chat1"] = _FakeFrame()

    await channel.send(
        OutboundMessage(channel="wecom", chat_id="chat1", content="hello", media=["/nonexistent/file.png"])
    )

    # reply_stream should still be called for the text part
    client.reply_stream.assert_called_once()
    # No media reply should happen
    media_calls = [c for c in client.reply.call_args_list if c[0][1].get("msgtype") in ("image", "file", "video")]
    assert len(media_calls) == 0


@pytest.mark.asyncio
async def test_send_exception_propagates_for_manager_retry() -> None:
    """Delivery failures must propagate to the channel manager."""
    channel = WecomChannel(WecomConfig(bot_id="b", secret="s", allow_from=["*"]), MessageBus())
    client = _FakeWeComClient()
    channel._client = client
    channel._generate_req_id = lambda x: f"req_{x}"
    channel._chat_frames["chat1"] = _FakeFrame()

    # Make reply_stream raise
    client.reply_stream.side_effect = RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await channel.send(
            OutboundMessage(channel="wecom", chat_id="chat1", content="fail test")
        )
    client.reply_stream.assert_called_once()


def _text_frame(msgid: str, chat_id: str = "chat1", content: str = "在不") -> _FakeFrame:
    return _FakeFrame(body={
        "msgid": msgid,
        "chatid": chat_id,
        "chattype": "single",
        "from": {"userid": "user1"},
        "text": {"content": content},
    })


def _channel(**overrides: object) -> WecomChannel:
    config = {
        "bot_id": "b",
        "secret": "s",
        "allow_from": ["user1"],
    }
    config.update(overrides)
    channel = WecomChannel(config, MessageBus())
    channel._client = _FakeWeComClient()
    channel._generate_req_id = lambda kind: f"req_{kind}"
    return channel


@pytest.mark.asyncio
async def test_accepted_text_opens_thinking_bubble() -> None:
    """An accepted message opens one unfinished stream with a single space."""
    channel = _channel()
    await channel._process_message(_text_frame("msg-1"), "text")

    channel._client.reply_stream.assert_awaited_once()
    args, kwargs = channel._client.reply_stream.call_args
    assert args[2] == " "
    assert kwargs["finish"] is False
    assert "msg-1" in channel._open_streams
    inbound = await channel.bus.consume_inbound()
    assert inbound.content == "在不"


@pytest.mark.asyncio
async def test_final_send_reuses_thinking_stream() -> None:
    """The answer finishes the same stream and does not send a second message."""
    channel = _channel()
    await channel._process_message(_text_frame("msg-1"), "text")
    stream_id = channel._open_streams["msg-1"].stream_id

    await channel.send(OutboundMessage(
        channel="wecom",
        chat_id="chat1",
        content="hello",
        metadata={"message_id": "msg-1"},
    ))

    assert channel._client.reply_stream.await_count == 2
    finish_args, finish_kwargs = channel._client.reply_stream.await_args_list[1]
    assert finish_args[1] == stream_id
    assert finish_args[2] == "hello"
    assert finish_kwargs["finish"] is True
    channel._client.send_message.assert_not_called()
    assert "msg-1" not in channel._open_streams


@pytest.mark.asyncio
async def test_empty_final_closes_bubble_without_blank_content() -> None:
    """A turn with no text still finishes the open bubble using the placeholder."""
    channel = _channel()
    await channel._process_message(_text_frame("msg-1"), "text")

    await channel.send(OutboundMessage(
        channel="wecom",
        chat_id="chat1",
        content="  ",
        metadata={"message_id": "msg-1"},
    ))

    finish_args, finish_kwargs = channel._client.reply_stream.await_args
    assert finish_args[2] == " "
    assert finish_kwargs["finish"] is True
    assert "msg-1" not in channel._open_streams


@pytest.mark.asyncio
async def test_tool_hint_rewrites_same_stream_with_3s_clamp() -> None:
    """One tool line every 3 seconds. Bursts, repeats, and reasoning do not send."""
    channel = _channel()
    clock = {"t": 100.0}
    channel._now = lambda: clock["t"]
    await channel._process_message(_text_frame("msg-1"), "text")
    stream_id = channel._open_streams["msg-1"].stream_id

    async def hint(text: str, **event_kwargs: object) -> None:
        await channel.send(OutboundMessage(
            channel="wecom",
            chat_id="chat1",
            content=text,
            metadata={"message_id": "msg-1"},
            event=ProgressEvent(content=text, **event_kwargs),
        ))

    await hint("search \"会议\"", tool_hint=True)
    search_args, search_kwargs = channel._client.reply_stream.await_args
    assert search_args[1] == stream_id
    assert search_args[2] == "search \"会议\""
    assert search_kwargs["finish"] is False

    clock["t"] = 101.0
    await hint("read notes", tool_hint=True)
    assert channel._client.reply_stream.await_count == 2

    clock["t"] = 104.0
    await hint("search \"会议\"", tool_hint=True)
    assert channel._client.reply_stream.await_count == 2

    clock["t"] = 108.0
    await hint("read notes", tool_hint=True)
    assert channel._client.reply_stream.await_count == 3

    await hint("private", reasoning_delta=True, tool_hint=True)
    await hint("partial answer")
    assert channel._client.reply_stream.await_count == 3


@pytest.mark.asyncio
async def test_stream_budget_skips_placeholder_but_sends_final() -> None:
    """Twenty stream frames in the last minute skip the dots. The answer still goes out."""
    channel = _channel()
    now = channel._now()
    channel._stream_sends["chat1"] = deque([now] * 20)

    await channel._process_message(_text_frame("msg-1"), "text")
    channel._client.reply_stream.assert_not_called()

    await channel.send(OutboundMessage(
        channel="wecom",
        chat_id="chat1",
        content="answer",
        metadata={"message_id": "msg-1"},
    ))
    channel._client.reply_stream.assert_awaited_once()
    assert channel._client.reply_stream.await_args.kwargs["finish"] is True


@pytest.mark.asyncio
async def test_fourth_open_placeholder_is_skipped() -> None:
    """A chat keeps at most three loading bubbles. The next answer is still one frame."""
    channel = _channel()
    for index in range(3):
        await channel._process_message(_text_frame(f"msg-{index}"), "text")
    assert channel._client.reply_stream.await_count == 3

    await channel._process_message(_text_frame("msg-3"), "text")
    assert channel._client.reply_stream.await_count == 3
    assert "msg-3" not in channel._open_streams

    await channel.send(OutboundMessage(
        channel="wecom",
        chat_id="chat1",
        content="later",
        metadata={"message_id": "msg-3"},
    ))
    assert channel._client.reply_stream.await_count == 4
    assert channel._client.reply_stream.await_args.kwargs["finish"] is True


@pytest.mark.asyncio
async def test_placeholder_failure_is_not_retried() -> None:
    """A failed placeholder is one attempt and does not open a stream."""
    channel = _channel()
    channel._client.reply_stream.side_effect = RuntimeError("boom")

    await channel._process_message(_text_frame("msg-1"), "text")

    channel._client.reply_stream.assert_awaited_once()
    assert channel._open_streams == {}
    inbound = await channel.bus.consume_inbound()
    assert inbound.content == "在不"


@pytest.mark.asyncio
async def test_frequency_limit_stops_new_placeholders() -> None:
    """A frequency-limit ack blocks further dots and does not open a stream."""
    channel = _channel()
    channel._client.reply_stream.return_value = _FakeResponse(
        errcode=45009,
        errmsg="api freq out of limit",
    )

    await channel._process_message(_text_frame("msg-1"), "text")
    await channel._process_message(_text_frame("msg-2"), "text")

    channel._client.reply_stream.assert_awaited_once()
    assert channel._open_streams == {}


@pytest.mark.asyncio
async def test_finish_ack_timeout_is_not_resent() -> None:
    """A finish frame that times out after it was written is not sent again."""
    channel = _channel()
    await channel._process_message(_text_frame("msg-1"), "text")
    channel._client.reply_stream.side_effect = TimeoutError("reply ack timeout")

    outbound = OutboundMessage(
        channel="wecom",
        chat_id="chat1",
        content="hello",
        metadata={"message_id": "msg-1"},
    )
    await channel.send(outbound)
    await channel.send(outbound)

    assert channel._client.reply_stream.await_count == 2
    assert "msg-1" in channel._finished_msgids


@pytest.mark.asyncio
async def test_thinking_bubble_off_keeps_oneshot_final() -> None:
    """Opt-out skips the dots. Progress still cannot leak an unfinished stream."""
    channel = _channel(thinking_bubble=False)
    await channel._process_message(_text_frame("msg-1"), "text")
    channel._client.reply_stream.assert_not_called()

    await channel.send(OutboundMessage(
        channel="wecom",
        chat_id="chat1",
        content="search",
        metadata={"message_id": "msg-1"},
        event=ProgressEvent(content="search", tool_hint=True),
    ))
    channel._client.reply_stream.assert_not_called()

    await channel.send(OutboundMessage(
        channel="wecom",
        chat_id="chat1",
        content="hello",
        metadata={"message_id": "msg-1"},
    ))
    channel._client.reply_stream.assert_awaited_once()
    assert channel._client.reply_stream.await_args.kwargs["finish"] is True


@pytest.mark.asyncio
async def test_expired_stream_finishes_once() -> None:
    """A bubble still open at the age limit is closed once and not replaced."""
    channel = _channel()
    await channel._process_message(_text_frame("msg-1"), "text")
    channel._open_streams["msg-1"].last_tool_text = "search"

    await channel._close_expired_stream("msg-1")
    await channel._close_expired_stream("msg-1")

    assert channel._client.reply_stream.await_count == 2
    finish_args, finish_kwargs = channel._client.reply_stream.await_args
    assert finish_args[2] == "search"
    assert finish_kwargs["finish"] is True
    assert "msg-1" not in channel._open_streams


def test_omitted_send_tool_hints_defaults_on_for_wecom() -> None:
    """WeCom receives tool hints unless this channel's config turns them off."""
    channel = _channel()
    channel.send_tool_hints = False
    channel._apply_tool_hint_delivery()
    assert channel.send_tool_hints is True

    opted_out = _channel(sendToolHints=False)
    opted_out.send_tool_hints = True
    opted_out._apply_tool_hint_delivery()
    assert opted_out.send_tool_hints is False


# ── _process_message() ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_process_text_message() -> None:
    """Text message is routed to bus with correct fields."""
    channel = WecomChannel(WecomConfig(bot_id="b", secret="s", allow_from=["user1"]), MessageBus())
    client = _FakeWeComClient()
    channel._client = client

    frame = _FakeFrame(body={
        "msgid": "msg_text_1",
        "chatid": "chat1",
        "chattype": "single",
        "from": {"userid": "user1"},
        "text": {"content": "hello wecom"},
    })

    await channel._process_message(frame, "text")

    msg = await channel.bus.consume_inbound()
    assert msg.sender_id == "user1"
    assert msg.chat_id == "chat1"
    assert msg.content == "hello wecom"
    assert msg.metadata["msg_type"] == "text"


@pytest.mark.asyncio
async def test_enter_chat_ignores_unauthorized_user_before_welcome() -> None:
    channel = WecomChannel(WecomConfig(bot_id="b", secret="s", allow_from=["allowed"]), MessageBus())
    client = _FakeWeComClient()
    channel._client = client
    channel.config.welcome_message = "hello"

    await channel._on_enter_chat(_FakeFrame(body={"chatid": "blocked"}))

    client.reply_welcome.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_message_ignores_unauthorized_sender_before_download() -> None:
    channel = WecomChannel(WecomConfig(bot_id="b", secret="s", allow_from=["allowed"]), MessageBus())
    client = _FakeWeComClient()
    channel._client = client
    channel._handle_message = AsyncMock()

    frame = _FakeFrame(body={
        "msgid": "msg_blocked",
        "chatid": "chat1",
        "from": {"userid": "blocked"},
        "image": {"url": "https://example.com/img.png", "aeskey": "key123"},
    })

    await channel._process_message(frame, "image")

    client.download_file.assert_not_awaited()
    channel._handle_message.assert_not_awaited()
    assert channel.bus.inbound_size == 0


@pytest.mark.asyncio
async def test_process_image_message() -> None:
    """Image message: download success → media_paths non-empty."""
    channel = WecomChannel(WecomConfig(bot_id="b", secret="s", allow_from=["user1"]), MessageBus())
    client = _FakeWeComClient()

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(b"\x89PNG\r\n")
        saved = f.name

    client.download_file.return_value = (b"\x89PNG\r\n", "photo.png")
    channel._client = client

    try:
        with patch("nanobot.channels.wecom.runtime.get_media_dir", return_value=Path(os.path.dirname(saved))):
            frame = _FakeFrame(body={
                "msgid": "msg_img_1",
                "chatid": "chat1",
                "from": {"userid": "user1"},
                "image": {"url": "https://example.com/img.png", "aeskey": "key123"},
            })
            await channel._process_message(frame, "image")

        msg = await channel.bus.consume_inbound()
        assert len(msg.media) == 1
        assert msg.media[0].endswith("photo.png")
        assert "[image:" in msg.content
    finally:
        if os.path.exists(saved):
            pass  # may have been overwritten; clean up if exists
        # Clean up any photo.png in tempdir
        p = os.path.join(os.path.dirname(saved), "photo.png")
        if os.path.exists(p):
            os.unlink(p)


@pytest.mark.asyncio
async def test_process_file_message() -> None:
    """File message: download success → media_paths non-empty (critical fix verification)."""
    channel = WecomChannel(WecomConfig(bot_id="b", secret="s", allow_from=["user1"]), MessageBus())
    client = _FakeWeComClient()

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(b"%PDF-1.4 fake")
        saved = f.name

    client.download_file.return_value = (b"%PDF-1.4 fake", "report.pdf")
    channel._client = client

    try:
        with patch("nanobot.channels.wecom.runtime.get_media_dir", return_value=Path(os.path.dirname(saved))):
            frame = _FakeFrame(body={
                "msgid": "msg_file_1",
                "chatid": "chat1",
                "from": {"userid": "user1"},
                "file": {"url": "https://example.com/report.pdf", "aeskey": "key456", "name": "report.pdf"},
            })
            await channel._process_message(frame, "file")

        msg = await channel.bus.consume_inbound()
        assert len(msg.media) == 1
        assert msg.media[0].endswith("report.pdf")
        assert "[file: report.pdf]" in msg.content
    finally:
        p = os.path.join(os.path.dirname(saved), "report.pdf")
        if os.path.exists(p):
            os.unlink(p)


@pytest.mark.asyncio
async def test_process_file_message_uses_sdk_filename_when_name_missing(tmp_path: Path) -> None:
    """Without `file.name`, fall back to SDK fname instead of saving as 'unknown' (#3737)."""
    channel = WecomChannel(WecomConfig(bot_id="b", secret="s", allow_from=["user1"]), MessageBus())
    client = _FakeWeComClient()
    client.download_file.return_value = (b"%PDF-1.4 fake", "real_name.pdf")
    channel._client = client

    with patch("nanobot.channels.wecom.runtime.get_media_dir", return_value=tmp_path):
        frame = _FakeFrame(body={
            "msgid": "msg_file_2", "chatid": "chat1", "from": {"userid": "user1"},
            "file": {"url": "https://example.com/x", "aeskey": "key456"},
        })
        await channel._process_message(frame, "file")

    msg = await channel.bus.consume_inbound()
    assert msg.media == [str(tmp_path / "real_name.pdf")]
    assert "[file: real_name.pdf]" in msg.content


@pytest.mark.asyncio
async def test_process_voice_message() -> None:
    """Voice message: transcribed text is included in content."""
    channel = WecomChannel(WecomConfig(bot_id="b", secret="s", allow_from=["user1"]), MessageBus())
    client = _FakeWeComClient()
    channel._client = client

    frame = _FakeFrame(body={
        "msgid": "msg_voice_1",
        "chatid": "chat1",
        "from": {"userid": "user1"},
        "voice": {"content": "transcribed text here"},
    })

    await channel._process_message(frame, "voice")

    msg = await channel.bus.consume_inbound()
    assert "transcribed text here" in msg.content
    assert "[voice]" in msg.content


@pytest.mark.asyncio
async def test_process_mixed_message() -> None:
    """Mixed message: contains picture and text message types."""
    channel = WecomChannel(WecomConfig(bot_id="b", secret="s", allow_from=["user1"]), MessageBus())
    client = _FakeWeComClient()

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(b"\x89PNG\r\n")
        saved = f.name

    client.download_file.return_value = (b"\x89PNG\r\n", "photo.png")
    channel._client = client

    try:
        with patch("nanobot.channels.wecom.runtime.get_media_dir", return_value=Path(os.path.dirname(saved))):
            frame = _FakeFrame(body={
                "msgid": "msg_mixed_1",
                "chatid": "chat1",
                "msgtype": "mixed",
                "from": {"userid": "user1"},
                "mixed": {
                    "msg_item": [
                        {"msgtype": "text", "text": {"content": "hello wecom"}},
                        {"msgtype": "image", "image": {"url": "https://example.com/img.png", "aeskey": "key123"}}
                    ]
                }
            })
            await channel._process_message(frame, "mixed")

        msg = await channel.bus.consume_inbound()
        assert msg.sender_id == "user1"
        assert msg.chat_id == "chat1"
        assert msg.content.startswith("hello wecom")
        assert msg.metadata["msg_type"] == "mixed"
        assert len(msg.media) == 1
        assert msg.media[0].endswith("photo.png")
        assert "[image:" in msg.content
    finally:
        # Clean up any photo.png in tempdir
        p = os.path.join(os.path.dirname(saved), "photo.png")
        if os.path.exists(p):
            os.unlink(p)


@pytest.mark.asyncio
async def test_process_message_deduplication() -> None:
    """Same msg_id is not processed twice."""
    channel = WecomChannel(WecomConfig(bot_id="b", secret="s", allow_from=["user1"]), MessageBus())
    client = _FakeWeComClient()
    channel._client = client

    frame = _FakeFrame(body={
        "msgid": "msg_dup_1",
        "chatid": "chat1",
        "from": {"userid": "user1"},
        "text": {"content": "once"},
    })

    await channel._process_message(frame, "text")
    await channel._process_message(frame, "text")

    msg = await channel.bus.consume_inbound()
    assert msg.content == "once"

    # Second message should not appear on the bus
    assert channel.bus.inbound.empty()


@pytest.mark.asyncio
async def test_process_message_empty_content_skipped() -> None:
    """Message with empty content produces no bus message."""
    channel = WecomChannel(WecomConfig(bot_id="b", secret="s", allow_from=["user1"]), MessageBus())
    client = _FakeWeComClient()
    channel._client = client

    frame = _FakeFrame(body={
        "msgid": "msg_empty_1",
        "chatid": "chat1",
        "from": {"userid": "user1"},
        "text": {"content": ""},
    })

    await channel._process_message(frame, "text")

    assert channel.bus.inbound.empty()
