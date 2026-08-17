# ruff: noqa: E402

"""Tests for the Feishu live progress card (dual tool-hint UX).

The inline tool-hint path (append into the active streaming card / standalone
interactive card) is unchanged; on top of it a dedicated live progress card is
created on the first tool and each subsequent tool replaces its single line.
"""
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("lark_oapi")

from nanobot.bus.events import OutboundMessage
from nanobot.bus.outbound_events import ProgressEvent
from nanobot.bus.queue import MessageBus
from nanobot.channels.feishu.runtime import FeishuChannel, FeishuConfig, _FeishuStreamBuf
from nanobot.utils.tool_hints import format_tool_hints

# ruff: noqa: E501


def _make_channel(**overrides) -> FeishuChannel:
    config = FeishuConfig(
        enabled=True,
        app_id="cli_test",
        app_secret="secret",
        allow_from=["*"],
        streaming=True,
        **overrides,
    )
    ch = FeishuChannel(config, MessageBus())
    ch._client = MagicMock()
    ch._loop = None
    return ch


def _mock_create_card_response(card_id: str = "card_live_001"):
    resp = MagicMock()
    resp.success.return_value = True
    resp.data = SimpleNamespace(card_id=card_id)
    return resp


def _mock_send_response(message_id: str = "om_live_001"):
    resp = MagicMock()
    resp.success.return_value = True
    resp.data = SimpleNamespace(message_id=message_id)
    return resp


def _mock_content_response(success: bool = True):
    resp = MagicMock()
    resp.success.return_value = success
    resp.code = 0 if success else 99999
    resp.msg = "ok" if success else "error"
    return resp


def _mock_ok_chain(ch: FeishuChannel) -> None:
    ch._client.cardkit.v1.card.create.return_value = _mock_create_card_response("card_live_1")
    ch._client.im.v1.message.create.return_value = _mock_send_response("om_live_1")
    ch._client.cardkit.v1.card_element.content.return_value = _mock_content_response()
    ch._client.cardkit.v1.card.settings.return_value = _mock_content_response()


def _tool_event(name: str, arguments: dict, call_id: str = "call_1") -> dict:
    return {
        "version": 1,
        "phase": "start",
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
        "result": None,
        "error": None,
        "files": [],
        "embeds": [],
    }


def _hint_msg(content: str, tool_events: list[dict] | None, chat_id: str = "oc_chat1") -> OutboundMessage:
    return OutboundMessage(
        channel="feishu",
        chat_id=chat_id,
        content=content,
        event=ProgressEvent(content=content, tool_hint=True, tool_events=tool_events),
    )


class TestLiveCardCreateReplace:
    @pytest.mark.asyncio
    async def test_first_tool_creates_live_card(self):
        ch = _make_channel()
        _mock_ok_chain(ch)
        events = [_tool_event("read_file", {"path": "docs/api.md"}, "call_1")]

        await ch.send(_hint_msg('read docs/api.md', events))

        buf = ch._live_hint_bufs.get("oc_chat1")
        assert buf is not None
        assert buf.card_id == "card_live_1"
        assert "read docs/api.md" in buf.text
        ch._client.cardkit.v1.card.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_second_tool_replaces_same_card(self):
        ch = _make_channel()
        _mock_ok_chain(ch)
        await ch.send(_hint_msg('read docs/api.md', [_tool_event("read_file", {"path": "docs/api.md"}, "call_1")]))
        await ch.send(_hint_msg('grep "TODO"', [_tool_event("grep", {"pattern": "TODO"}, "call_2")]))

        buf = ch._live_hint_bufs.get("oc_chat1")
        assert buf is not None
        assert buf.card_id == "card_live_1"  # no second create
        assert 'grep "TODO"' in buf.text
        assert "read docs/api.md" not in buf.text  # replaced, not appended
        ch._client.cardkit.v1.card.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_batch_shows_latest_line_with_single_create(self):
        ch = _make_channel()
        _mock_ok_chain(ch)
        events = [
            _tool_event("read_file", {"path": "docs/api.md"}, "call_1"),
            _tool_event("grep", {"pattern": "TODO"}, "call_2"),
        ]

        await ch.send(_hint_msg('read docs/api.md, grep "TODO"', events))

        buf = ch._live_hint_bufs.get("oc_chat1")
        assert buf is not None
        assert buf.card_id == "card_live_1"
        assert 'grep "TODO"' in buf.text  # final line of the batch is flushed
        ch._client.cardkit.v1.card.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_live_card_coexists_with_inline_stream(self):
        """Inline append into the answer card is untouched while the live card is created."""
        ch = _make_channel()
        _mock_ok_chain(ch)
        ch._stream_bufs["oc_chat1"] = _FeishuStreamBuf(
            text="Partial answer", card_id="card_answer_1", sequence=2, last_edit=0.0,
        )

        await ch.send(_hint_msg('read docs/api.md', [_tool_event("read_file", {"path": "docs/api.md"})]))

        inline = ch._stream_bufs["oc_chat1"]
        assert "Partial answer" in inline.text
        assert "read docs/api.md" in inline.text
        live = ch._live_hint_bufs["oc_chat1"]
        assert live.card_id == "card_live_1"
        assert live.card_id != "card_answer_1"


class TestLiveCardLifecycle:
    @pytest.mark.asyncio
    async def test_survives_resuming_stream_end(self):
        ch = _make_channel()
        _mock_ok_chain(ch)
        await ch.send(_hint_msg('read docs/api.md', [_tool_event("read_file", {"path": "docs/api.md"})]))

        await ch.send_delta("oc_chat1", "", stream_end=True, resuming=True)

        buf = ch._live_hint_bufs.get("oc_chat1")
        assert buf is not None
        assert buf.card_id == "card_live_1"

    @pytest.mark.asyncio
    async def test_finalized_on_final_stream_end(self):
        ch = _make_channel()
        _mock_ok_chain(ch)
        await ch.send(_hint_msg('read docs/api.md', [_tool_event("read_file", {"path": "docs/api.md"})]))
        assert "oc_chat1" in ch._live_hint_bufs

        await ch.send_delta("oc_chat1", "Answer", stream_end=True, resuming=False)

        assert "oc_chat1" not in ch._live_hint_bufs
        # final content flushed + streaming closed
        assert ch._client.cardkit.v1.card.settings.called

    @pytest.mark.asyncio
    async def test_finalized_on_plain_final_send(self):
        ch = _make_channel()
        _mock_ok_chain(ch)
        await ch.send(_hint_msg('read docs/api.md', [_tool_event("read_file", {"path": "docs/api.md"})]))

        await ch.send(
            OutboundMessage(
                channel="feishu", chat_id="oc_chat1",
                content="Here is the answer.",
            )
        )

        assert "oc_chat1" not in ch._live_hint_bufs
        assert ch._client.cardkit.v1.card.settings.called

    @pytest.mark.asyncio
    async def test_mid_turn_progress_does_not_finalize(self):
        ch = _make_channel()
        _mock_ok_chain(ch)
        await ch.send(_hint_msg('read docs/api.md', [_tool_event("read_file", {"path": "docs/api.md"})]))

        await ch.send(
            OutboundMessage(
                channel="feishu", chat_id="oc_chat1",
                content="working…",
                event=ProgressEvent(content="working…", tool_hint=False),
            )
        )

        assert "oc_chat1" in ch._live_hint_bufs


class TestLiveCardConfig:
    @pytest.mark.asyncio
    async def test_disabled_skips_live_card(self):
        ch = _make_channel(live_tool_hint_card=False)
        _mock_ok_chain(ch)

        await ch.send(_hint_msg('read docs/api.md', [_tool_event("read_file", {"path": "docs/api.md"})]))

        assert "oc_chat1" not in ch._live_hint_bufs
        ch._client.cardkit.v1.card.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_camel_case_aliases(self):
        cfg = FeishuConfig(liveToolHintCard=True, liveToolHintMaxLength=200)
        assert cfg.live_tool_hint_card is True
        assert cfg.live_tool_hint_max_length == 200
        dumped = cfg.model_dump(by_alias=True)
        assert dumped["liveToolHintCard"] is True
        assert dumped["liveToolHintMaxLength"] == 200

    @pytest.mark.asyncio
    async def test_live_length_config_formats_longer_lines(self):
        """The live card re-formats from raw tool_events with its own (longer) limit."""
        cmd = "cd /home/user/projects/deep/nested/directory && python run_pipeline.py --mode full --verbose"
        tool_events = [_tool_event("exec", {"command": cmd}, "call_1")]
        inline_hint = format_tool_hints(
            [SimpleNamespace(id="call_1", name="exec", arguments={"command": cmd})],
            max_length=40,
        )

        ch = _make_channel(live_tool_hint_max_length=200)
        _mock_ok_chain(ch)
        await ch.send(_hint_msg(inline_hint, tool_events))

        buf = ch._live_hint_bufs.get("oc_chat1")
        assert buf is not None
        assert len(buf.text) > len(inline_hint)
        assert "run_pipeline.py" in buf.text

    @pytest.mark.asyncio
    async def test_fallback_to_content_when_no_tool_events(self):
        ch = _make_channel()
        _mock_ok_chain(ch)

        await ch.send(_hint_msg('read docs/api.md', None))

        buf = ch._live_hint_bufs.get("oc_chat1")
        assert buf is not None
        assert buf.card_id == "card_live_1"
        assert "read docs/api.md" in buf.text


class TestThrottle:
    @pytest.mark.asyncio
    async def test_rapid_updates_hold_latest_and_force_flushes_last(self):
        ch = _make_channel()
        _mock_ok_chain(ch)
        ch._live_hint_bufs["oc_chat1"] = _FeishuStreamBuf(
            text="", card_id="card_live_1", sequence=5, last_edit=time.monotonic(),
        )

        # Within throttle window: text is held locally, not pushed.
        await ch._update_live_hint_card("oc_chat1", {}, "hint a")
        assert ch._live_hint_bufs["oc_chat1"].text == "hint a"
        before = ch._client.cardkit.v1.card_element.content.call_count

        # force=True bypasses the throttle and pushes the latest line.
        await ch._update_live_hint_card("oc_chat1", {}, "hint b", force=True)
        assert ch._client.cardkit.v1.card_element.content.call_count == before + 1
        update_call = ch._client.cardkit.v1.card_element.content.call_args[0][0]
        assert "hint b" in update_call.body.content
