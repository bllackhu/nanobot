# ruff: noqa: E402

"""Tests for the Feishu live progress card and hint-mode switch.

The Feishu channel lets operators pick either inline hints (append into the
active streaming card / standalone interactive card) or a dedicated live
progress card — never both at once.  The live card also receives status
events (token consolidation) and a periodic heartbeat refresh.
"""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

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


def _status_msg(content: str, chat_id: str = "oc_chat1") -> OutboundMessage:
    return OutboundMessage(
        channel="feishu",
        chat_id=chat_id,
        content=content,
        event=ProgressEvent(content=content, tool_hint=True, tool_events=None),
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
    async def test_live_mode_does_not_append_into_answer_card(self):
        """Live mode only drives the live card — the answer/stream card is untouched."""
        ch = _make_channel()
        _mock_ok_chain(ch)
        ch._stream_bufs["oc_chat1"] = _FeishuStreamBuf(
            text="Partial answer", card_id="card_answer_1", sequence=2, last_edit=0.0,
        )

        await ch.send(_hint_msg('read docs/api.md', [_tool_event("read_file", {"path": "docs/api.md"})]))

        inline = ch._stream_bufs["oc_chat1"]
        assert inline.text == "Partial answer"  # unchanged
        assert "read docs/api.md" not in inline.text
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


class TestHintMode:
    """Either-or: inline mode uses inline hints only, live mode uses the live card only."""

    @pytest.mark.asyncio
    async def test_inline_mode_skips_live_card(self):
        ch = _make_channel(hint_mode="inline")
        _mock_ok_chain(ch)

        await ch.send(_hint_msg('read docs/api.md', [_tool_event("read_file", {"path": "docs/api.md"})]))

        assert "oc_chat1" not in ch._live_hint_bufs
        ch._client.cardkit.v1.card.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_inline_mode_appends_into_streaming_card(self):
        ch = _make_channel(hint_mode="inline")
        _mock_ok_chain(ch)
        ch._stream_bufs["oc_chat1"] = _FeishuStreamBuf(
            text="Partial answer", card_id="card_answer_1", sequence=2, last_edit=0.0,
        )

        await ch.send(_hint_msg('read docs/api.md', [_tool_event("read_file", {"path": "docs/api.md"})]))

        inline = ch._stream_bufs["oc_chat1"]
        assert "Partial answer" in inline.text
        assert "read docs/api.md" in inline.text
        assert "oc_chat1" not in ch._live_hint_bufs

    @pytest.mark.asyncio
    async def test_inline_mode_sends_standalone_card_with_no_stream(self):
        ch = _make_channel(hint_mode="inline")
        _mock_ok_chain(ch)

        await ch.send(_hint_msg('read docs/api.md', [_tool_event("read_file", {"path": "docs/api.md"})]))

        assert "oc_chat1" not in ch._live_hint_bufs
        assert ch._client.im.v1.message.create.called

    @pytest.mark.asyncio
    async def test_live_mode_creates_live_card_not_inline(self):
        ch = _make_channel(hint_mode="live")
        _mock_ok_chain(ch)
        ch._stream_bufs["oc_chat1"] = _FeishuStreamBuf(
            text="Partial answer", card_id="card_answer_1", sequence=2, last_edit=0.0,
        )

        await ch.send(_hint_msg('read docs/api.md', [_tool_event("read_file", {"path": "docs/api.md"})]))

        assert ch._live_hint_bufs["oc_chat1"].card_id == "card_live_1"
        assert ch._stream_bufs["oc_chat1"].text == "Partial answer"  # inline untouched
        assert ch._client.im.v1.message.create.not_called

    @pytest.mark.asyncio
    async def test_status_event_skipped_in_inline_mode(self):
        """Consolidation status (no tool_events) is only for the live card."""
        ch = _make_channel(hint_mode="inline")
        _mock_ok_chain(ch)

        await ch.send(_status_msg("consolidating history (1234/8000 tokens)"))

        assert "oc_chat1" not in ch._live_hint_bufs
        ch._client.cardkit.v1.card.create.assert_not_called()
        ch._client.im.v1.message.create.assert_not_called()


class TestConsolidationHint:
    @pytest.mark.asyncio
    async def test_status_renders_on_live_card(self):
        ch = _make_channel()
        _mock_ok_chain(ch)

        await ch.send(_status_msg("consolidating history (1234/8000 tokens)"))

        buf = ch._live_hint_bufs.get("oc_chat1")
        assert buf is not None
        assert buf.card_id == "card_live_1"
        assert "consolidating history" in buf.text

    @pytest.mark.asyncio
    async def test_completion_status_renders_on_live_card(self):
        ch = _make_channel()
        _mock_ok_chain(ch)

        await ch.send(_status_msg("history consolidated"))

        buf = ch._live_hint_bufs.get("oc_chat1")
        assert buf is not None
        assert "history consolidated" in buf.text

    @pytest.mark.asyncio
    async def test_status_skipped_in_inline_mode(self):
        ch = _make_channel(hint_mode="inline")
        _mock_ok_chain(ch)

        await ch.send(_status_msg("consolidating history (1234/8000 tokens)"))

        assert "oc_chat1" not in ch._live_hint_bufs
        ch._client.cardkit.v1.card.create.assert_not_called()


class TestLiveCardConfig:
    @pytest.mark.asyncio
    async def test_camel_case_aliases(self):
        cfg = FeishuConfig(hintMode="inline", liveToolHintMaxLength=200)
        assert cfg.hint_mode == "inline"
        assert cfg.live_tool_hint_max_length == 200
        dumped = cfg.model_dump(by_alias=True)
        assert dumped["hintMode"] == "inline"
        assert dumped["liveToolHintMaxLength"] == 200

    @pytest.mark.asyncio
    async def test_defaults_to_live_mode(self):
        cfg = FeishuConfig()
        assert cfg.hint_mode == "live"
        assert cfg.live_tool_hint_heartbeat_seconds == 10

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


class TestHeartbeat:
    def _run_beat_once(self, ch: FeishuChannel, stream_key: str) -> asyncio.Task:
        """Run the heartbeat loop in the background, stopping it after one tick."""
        original = ch._stream_update_text_with_reopen_sync

        first_tick = asyncio.Event()
        done = asyncio.Event()

        def patched(*args, **kwargs):
            result = original(*args, **kwargs)
            if not first_tick.is_set():
                first_tick.set()
            return result

        ch._stream_update_text_with_reopen_sync = patched  # type: ignore[method-assign]

        async def _stop_once():
            await first_tick.wait()
            await asyncio.sleep(0.01)
            await ch._finalize_live_hint_card("oc_chat1", {})
            done.set()

        stop_task = asyncio.create_task(_stop_once())
        loop_task = asyncio.create_task(
            ch._live_hint_heartbeat_loop("oc_chat1", stream_key, 0.1)
        )
        return loop_task, stop_task, done

    @pytest.mark.asyncio
    async def test_heartbeat_resends_line_after_idle_interval(self):
        ch = _make_channel(live_tool_hint_heartbeat_seconds=0.1)
        _mock_ok_chain(ch)
        ch._live_hint_bufs["oc_chat1"] = _FeishuStreamBuf(
            text="working…", card_id="card_live_1", sequence=5,
            last_edit=time.monotonic() - 60, last_heartbeat=time.monotonic() - 60,
        )
        loop_task, stop_task, done = self._run_beat_once(ch, "oc_chat1")

        await asyncio.wait_for(done.wait(), timeout=2)
        loop_task.cancel()
        stop_task.cancel()

        sent_texts = [
            call.args[0].body.content
            for call in ch._client.cardkit.v1.card_element.content.call_args_list
        ]
        assert any("working…" in t and "\u00b7" in t for t in sent_texts)  # pulse animated

    @pytest.mark.asyncio
    async def test_zero_disables_heartbeat(self):
        ch = _make_channel(live_tool_hint_heartbeat_seconds=0)
        _mock_ok_chain(ch)

        await ch.send(_hint_msg('read docs/api.md', [_tool_event("read_file", {"path": "docs/api.md"})]))

        assert ch._live_hint_heartbeat_tasks == {}
        assert ch._background_tasks == set()

    @pytest.mark.asyncio
    async def test_heartbeat_cancelled_on_finalize(self):
        ch = _make_channel(live_tool_hint_heartbeat_seconds=30)
        _mock_ok_chain(ch)
        await ch.send(_hint_msg('read docs/api.md', [_tool_event("read_file", {"path": "docs/api.md"})]))
        assert "oc_chat1" in ch._live_hint_heartbeat_tasks

        await ch._finalize_live_hint_card("oc_chat1", {})

        assert "oc_chat1" not in ch._live_hint_heartbeat_tasks
        assert ch._background_tasks == set()

    @pytest.mark.asyncio
    async def test_heartbeat_does_not_clobber_fresh_update(self):
        ch = _make_channel(live_tool_hint_heartbeat_seconds=0.1)
        _mock_ok_chain(ch)
        ch._live_hint_bufs["oc_chat1"] = _FeishuStreamBuf(
            text="old line", card_id="card_live_1", sequence=5,
            last_edit=time.monotonic(),  # a fresh update just landed
            last_heartbeat=time.monotonic() - 60,
        )

        # Run one tick by finalizing after a short wait.
        async def _run_tick():
            await asyncio.sleep(0.15)
            await ch._finalize_live_hint_card("oc_chat1", {})

        stop_task = asyncio.create_task(_run_tick())
        loop_task = asyncio.create_task(
            ch._live_hint_heartbeat_loop("oc_chat1", "oc_chat1", 0.1)
        )
        await asyncio.wait_for(asyncio.gather(loop_task, stop_task), timeout=2)
        loop_task.cancel()
        stop_task.cancel()

        # The fresh update had priority; the loop reset the marker and never
        # sent a pulse (finalize only flushes the current text).
        sent_texts = []
        for call in ch._client.cardkit.v1.card_element.content.call_args_list:
            body = call.args[0].body.content
            sent_texts.append(body)
        assert sent_texts  # finalize flushed the latest line
        assert all("old line" in t for t in sent_texts)
        assert all("\u00b7" not in t for t in sent_texts)  # no heartbeat pulse