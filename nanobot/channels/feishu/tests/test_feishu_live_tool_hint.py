# ruff: noqa: E402

"""Tests for the Feishu live progress card and hint-mode switch.

The Feishu channel lets operators pick either inline hints (append into the
active streaming card / standalone interactive card) or a dedicated live
progress card — never both at once.  The live card also receives status
events (token consolidation) and a periodic heartbeat refresh.
"""
import asyncio
import json
import re
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("lark_oapi")

from nanobot.bus.events import OutboundMessage
from nanobot.bus.outbound_events import ProgressEvent, StreamedResponseEvent
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


def _tool_event(name: str, arguments: dict, call_id: str = "call_1", phase: str = "start") -> dict:
    return {
        "version": 1,
        "phase": phase,
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
        "result": "ok" if phase == "end" else None,
        "error": None if phase != "error" else "Tool execution failed",
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


def _finish_msg(tool_events: list[dict], chat_id: str = "oc_chat1") -> OutboundMessage:
    return OutboundMessage(
        channel="feishu",
        chat_id=chat_id,
        content="",
        event=ProgressEvent(content="", tool_hint=False, tool_events=tool_events),
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
        assert buf.tool_in_flight is True
        ch._client.cardkit.v1.card.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_second_tool_replaces_same_card(self):
        ch = _make_channel(live_tool_hint_heartbeat_seconds=0, live_tool_hint_min_dwell_ms=0)
        _mock_ok_chain(ch)
        ch._live_typewriter_seconds = lambda text: 0.0  # type: ignore[method-assign]
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
        ch = _make_channel(live_tool_hint_heartbeat_seconds=0, live_tool_hint_min_dwell_ms=0)
        _mock_ok_chain(ch)
        # Instant typewriter so the batch does not sleep between commits.
        ch._live_typewriter_seconds = lambda text: 0.0  # type: ignore[method-assign]
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
    async def test_batch_commits_every_tool_line_in_order(self):
        """Five tools in one ProgressEvent each get a CardKit write (sequential)."""
        ch = _make_channel(live_tool_hint_heartbeat_seconds=0, live_tool_hint_min_dwell_ms=0)
        _mock_ok_chain(ch)
        ch._live_typewriter_seconds = lambda text: 0.0  # type: ignore[method-assign]
        events = [
            _tool_event("read_file", {"path": f"f{i}.md"}, f"call_{i}")
            for i in range(5)
        ]
        await ch.send(_hint_msg("batch", events))

        sent_texts = [
            call.args[0].body.content
            for call in ch._client.cardkit.v1.card_element.content.call_args_list
        ]
        full_lines = [t for t in sent_texts if " - processing" in t]
        assert len(full_lines) == 5
        for i, line in enumerate(full_lines):
            assert f"f{i}.md" in line
        assert "f4.md" in ch._live_hint_bufs["oc_chat1"].text
        ch._client.cardkit.v1.card.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_waits_for_typewriter_before_next_tool_line(self, monkeypatch):
        ch = _make_channel(
            live_tool_hint_heartbeat_seconds=0,
            live_tool_hint_min_dwell_ms=0,
        )
        _mock_ok_chain(ch)
        clock = {"t": 1000.0}
        monkeypatch.setattr(
            "nanobot.channels.feishu.runtime.time.monotonic", lambda: clock["t"]
        )
        ch._live_typewriter_seconds = lambda text: 0.15  # type: ignore[method-assign]

        sleeps: list[float] = []
        real_sleep = asyncio.sleep

        async def spy_sleep(delay, *args, **kwargs):
            sleeps.append(float(delay))
            clock["t"] += max(float(delay), 0.0)
            await real_sleep(0)

        monkeypatch.setattr("nanobot.channels.feishu.runtime.asyncio.sleep", spy_sleep)

        events = [
            _tool_event("read_file", {"path": "a.md"}, "call_1"),
            _tool_event("read_file", {"path": "b.md"}, "call_2"),
        ]
        await ch.send(_hint_msg("a, b", events))

        assert any(s == pytest.approx(0.15, abs=0.01) for s in sleeps)
        sent_texts = [
            call.args[0].body.content
            for call in ch._client.cardkit.v1.card_element.content.call_args_list
        ]
        full_lines = [t for t in sent_texts if " - processing" in t]
        assert len(full_lines) == 2
        assert "a.md" in full_lines[0]
        assert "b.md" in full_lines[1]

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


class TestPostTurnConsolidationReopen:
    """After the answer freezes the live card, post-turn consolidation reopens one."""

    @pytest.mark.asyncio
    async def test_consolidating_after_finalize_reopens_live_card(self):
        ch = _make_channel(live_tool_hint_min_dwell_ms=0, live_tool_hint_done_hold_ms=0)
        _mock_ok_chain(ch)
        ch._live_typewriter_seconds = lambda text: 0.0  # type: ignore[method-assign]
        await ch.send(_status_msg("AI thinking ..."))
        await ch._finalize_live_hint_card("oc_chat1", {})
        assert "oc_chat1" not in ch._live_hint_bufs
        assert "oc_chat1" in ch._live_hint_finalized
        create_count = ch._client.cardkit.v1.card.create.call_count

        await ch.send(_status_msg("consolidating history (82389/65536 tokens)"))

        assert "oc_chat1" not in ch._live_hint_finalized
        buf = ch._live_hint_bufs.get("oc_chat1")
        assert buf is not None
        assert buf.card_id == "card_live_1"
        assert buf.post_turn_consolidation is True
        assert "consolidating history" in buf.text
        assert ch._client.cardkit.v1.card.create.call_count == create_count + 1
        assert "oc_chat1" in ch._live_hint_heartbeat_tasks

    @pytest.mark.asyncio
    async def test_history_consolidated_finalizes_reopened_card(self):
        ch = _make_channel(live_tool_hint_min_dwell_ms=0, live_tool_hint_done_hold_ms=0)
        _mock_ok_chain(ch)
        ch._live_typewriter_seconds = lambda text: 0.0  # type: ignore[method-assign]
        await ch.send(_status_msg("AI thinking ..."))
        await ch._finalize_live_hint_card("oc_chat1", {})
        await ch.send(_status_msg("consolidating history (82389/65536 tokens)"))
        assert "oc_chat1" in ch._live_hint_bufs
        assert "oc_chat1" not in ch._live_hint_finalized

        await ch.send(_status_msg("history consolidated"))

        sent_texts = [
            call.args[0].body.content
            for call in ch._client.cardkit.v1.card_element.content.call_args_list
        ]
        assert any(t == "history consolidated" for t in sent_texts)
        assert "oc_chat1" not in ch._live_hint_bufs
        assert "oc_chat1" in ch._live_hint_finalized
        assert "oc_chat1" not in ch._live_hint_heartbeat_tasks
        assert ch._client.cardkit.v1.card.settings.called

    @pytest.mark.asyncio
    async def test_pre_turn_history_consolidated_does_not_finalize(self):
        ch = _make_channel(live_tool_hint_min_dwell_ms=0, live_tool_hint_done_hold_ms=0)
        _mock_ok_chain(ch)
        ch._live_typewriter_seconds = lambda text: 0.0  # type: ignore[method-assign]
        await ch.send(_status_msg("consolidating history (1234/8000 tokens)"))
        await ch.send(_status_msg("history consolidated"))

        buf = ch._live_hint_bufs.get("oc_chat1")
        assert buf is not None
        assert "history consolidated" in buf.text
        assert buf.post_turn_consolidation is False
        assert "oc_chat1" not in ch._live_hint_finalized


class TestThinkingHint:
    """The 'AI thinking ...' status hint renders on the live card like consolidation."""

    @pytest.mark.asyncio
    async def test_thinking_status_renders_on_live_card(self):
        ch = _make_channel()
        _mock_ok_chain(ch)

        await ch.send(_status_msg("AI thinking ..."))

        buf = ch._live_hint_bufs.get("oc_chat1")
        assert buf is not None
        assert buf.text == "AI thinking"
        assert buf.thinking_status is True

    @pytest.mark.asyncio
    async def test_thinking_status_does_not_get_processing_note(self):
        """The thinking hint is a status event — no trailing ' - processing' note."""
        ch = _make_channel()
        _mock_ok_chain(ch)

        await ch.send(_status_msg("AI thinking ..."))

        buf = ch._live_hint_bufs.get("oc_chat1")
        assert buf is not None
        assert buf.text == "AI thinking"
        assert not buf.text.endswith(" - processing")

    @pytest.mark.asyncio
    async def test_thinking_status_replaced_by_next_tool_hint(self):
        """The live card swaps from the thinking line to the next tool line."""
        ch = _make_channel()
        _mock_ok_chain(ch)

        await ch.send(_status_msg("AI thinking ..."))
        await ch.send(_hint_msg('read docs/api.md', [_tool_event("read_file", {"path": "docs/api.md"})]))

        buf = ch._live_hint_bufs.get("oc_chat1")
        assert buf is not None
        assert 'read docs/api.md' in buf.text
        assert "AI thinking" not in buf.text
        assert buf.thinking_status is False

    @pytest.mark.asyncio
    async def test_thinking_to_tool_skips_seed_when_fast(self):
        """With fast strategy, non-prefix takeover writes the full tool line only."""
        ch = _make_channel(live_tool_hint_print_strategy="fast")
        _mock_ok_chain(ch)
        ch._live_hint_bufs["oc_chat1"] = _FeishuStreamBuf(
            text="AI thinking",
            card_id="card_live_1",
            sequence=5,
            thinking_status=True,
            last_payload="AI thinking \u00b7\u00b7\u00b7\u00b7\u00b7",
            last_edit=time.monotonic(),
        )
        payloads: list[str] = []

        def helper(card_id, content, sequence):
            payloads.append(content)
            return True, sequence

        ch._stream_update_text_with_reopen_sync = helper  # type: ignore[method-assign]
        await ch.send(_hint_msg(
            "read docs/api.md",
            [_tool_event("read_file", {"path": "docs/api.md"})],
        ))
        assert len(payloads) == 1
        assert "read docs/api.md" in payloads[0]
        seed = ch._live_stream_reset_seed(payloads[0])
        assert payloads[0] != seed
        assert seed not in payloads

    @pytest.mark.asyncio
    async def test_thinking_to_tool_writes_seed_then_tool_when_delay(self):
        """With delay strategy, non-prefix takeover still seeds then writes the tool line."""
        ch = _make_channel(live_tool_hint_print_strategy="delay")
        _mock_ok_chain(ch)
        ch._live_hint_bufs["oc_chat1"] = _FeishuStreamBuf(
            text="AI thinking",
            card_id="card_live_1",
            sequence=5,
            thinking_status=True,
            last_payload="AI thinking \u00b7\u00b7\u00b7\u00b7\u00b7",
            last_edit=time.monotonic(),
        )
        payloads: list[str] = []

        def helper(card_id, content, sequence):
            payloads.append(content)
            return True, sequence

        ch._stream_update_text_with_reopen_sync = helper  # type: ignore[method-assign]
        await ch.send(_hint_msg(
            "read docs/api.md",
            [_tool_event("read_file", {"path": "docs/api.md"})],
        ))
        assert len(payloads) >= 2
        seed = ch._live_stream_reset_seed(payloads[1])
        assert payloads[0] == seed
        assert payloads[0] != "-"
        assert payloads[1].startswith(seed)
        assert "read docs/api.md" in payloads[1]
        assert "AI thinking" not in payloads[1]
        assert ch._live_hint_bufs["oc_chat1"].thinking_status is False

    @pytest.mark.asyncio
    async def test_empty_content_with_tool_events_updates_card(self):
        ch = _make_channel()
        _mock_ok_chain(ch)
        await ch.send(_status_msg("AI thinking ..."))
        await ch.send(_hint_msg("", [_tool_event("read_file", {"path": "docs/api.md"})]))
        buf = ch._live_hint_bufs.get("oc_chat1")
        assert buf is not None
        assert "read docs/api.md" in buf.text
        assert buf.thinking_status is False

    @pytest.mark.asyncio
    async def test_thinking_status_skipped_in_inline_mode(self):
        ch = _make_channel(hint_mode="inline")
        _mock_ok_chain(ch)

        await ch.send(_status_msg("AI thinking ..."))

        assert "oc_chat1" not in ch._live_hint_bufs
        ch._client.cardkit.v1.card.create.assert_not_called()
        ch._client.im.v1.message.create.assert_not_called()


class TestLiveCardProcessingNote:
    """Configurable trailing note on live tool-hint lines; swapped to 'done' on finalize."""

    @pytest.mark.asyncio
    async def test_tool_hint_renders_with_default_note(self):
        ch = _make_channel()
        _mock_ok_chain(ch)

        await ch.send(_hint_msg('read docs/api.md', [_tool_event("read_file", {"path": "docs/api.md"})]))

        buf = ch._live_hint_bufs.get("oc_chat1")
        assert buf is not None
        assert buf.text.endswith(" - processing")

    @pytest.mark.asyncio
    async def test_empty_note_disables_suffix(self):
        ch = _make_channel(live_tool_hint_processing_note="")
        _mock_ok_chain(ch)

        await ch.send(_hint_msg('read docs/api.md', [_tool_event("read_file", {"path": "docs/api.md"})]))

        buf = ch._live_hint_bufs.get("oc_chat1")
        assert buf is not None
        assert not buf.text.endswith(" - ")

    @pytest.mark.asyncio
    async def test_custom_note_text(self):
        ch = _make_channel(live_tool_hint_processing_note="working")
        _mock_ok_chain(ch)

        await ch.send(_hint_msg('read docs/api.md', [_tool_event("read_file", {"path": "docs/api.md"})]))

        buf = ch._live_hint_bufs.get("oc_chat1")
        assert buf is not None
        assert buf.text.endswith(" - working")

    @pytest.mark.asyncio
    async def test_status_events_do_not_get_note(self):
        """Consolidation status keeps its own text — no trailing processing note."""
        ch = _make_channel()
        _mock_ok_chain(ch)

        await ch.send(_status_msg("consolidating history (1234/8000 tokens)"))

        buf = ch._live_hint_bufs.get("oc_chat1")
        assert buf is not None
        assert "consolidating history" in buf.text
        assert not buf.text.endswith(" - processing")

    @pytest.mark.asyncio
    async def test_finalize_swaps_note_to_done(self):
        ch = _make_channel()
        _mock_ok_chain(ch)
        await ch.send(_hint_msg('read docs/api.md', [_tool_event("read_file", {"path": "docs/api.md"})]))
        assert ch._live_hint_bufs["oc_chat1"].text.endswith(" - processing")

        await ch._finalize_live_hint_card("oc_chat1", {})

        assert "oc_chat1" not in ch._live_hint_bufs
        sent_texts = [
            call.args[0].body.content
            for call in ch._client.cardkit.v1.card_element.content.call_args_list
        ]
        # The final flush updated the card with the done note in place of processing.
        assert any(
            t.endswith(" - done") and " - processing" not in t for t in sent_texts
        )

    @pytest.mark.asyncio
    async def test_finalize_uses_custom_done_note(self):
        ch = _make_channel(live_tool_hint_processing_note="working", live_tool_hint_done_note="finished")
        _mock_ok_chain(ch)
        await ch.send(_hint_msg('read docs/api.md', [_tool_event("read_file", {"path": "docs/api.md"})]))
        assert ch._live_hint_bufs["oc_chat1"].text.endswith(" - working")

        await ch._finalize_live_hint_card("oc_chat1", {})

        sent_texts = [
            call.args[0].body.content
            for call in ch._client.cardkit.v1.card_element.content.call_args_list
        ]
        assert any(t.endswith(" - finished") and " - working" not in t for t in sent_texts)

    @pytest.mark.asyncio
    async def test_finalize_thinking_flushes_thinking_done(self):
        ch = _make_channel()
        _mock_ok_chain(ch)
        await ch.send(_status_msg("AI thinking ..."))
        assert ch._live_hint_bufs["oc_chat1"].thinking_status is True

        await ch._finalize_live_hint_card("oc_chat1", {})

        assert "oc_chat1" not in ch._live_hint_bufs
        sent_texts = [
            call.args[0].body.content
            for call in ch._client.cardkit.v1.card_element.content.call_args_list
        ]
        assert "AI thinking - done" in sent_texts
        assert all(t != "AI thinking ... - done" for t in sent_texts)

    @pytest.mark.asyncio
    async def test_streamed_response_finalizes_thinking_done(self):
        ch = _make_channel()
        _mock_ok_chain(ch)
        await ch.send(_status_msg("AI thinking ..."))
        await ch.send(
            OutboundMessage(
                channel="feishu",
                chat_id="oc_chat1",
                content="Here is the answer.",
                event=StreamedResponseEvent(),
            )
        )
        assert "oc_chat1" not in ch._live_hint_bufs
        sent_texts = [
            call.args[0].body.content
            for call in ch._client.cardkit.v1.card_element.content.call_args_list
        ]
        assert "AI thinking - done" in sent_texts
        # Must not re-send the answer as a new chat message.
        assert ch._client.im.v1.message.create.call_count == 1

    @pytest.mark.asyncio
    async def test_stale_thinking_after_finalize_does_not_reopen_card(self):
        ch = _make_channel()
        _mock_ok_chain(ch)
        await ch.send(_status_msg("AI thinking ..."))
        await ch._finalize_live_hint_card("oc_chat1", {})
        assert "oc_chat1" not in ch._live_hint_bufs
        create_count = ch._client.cardkit.v1.card.create.call_count
        await ch.send(_status_msg("AI thinking ..."))
        assert "oc_chat1" not in ch._live_hint_bufs
        assert ch._client.cardkit.v1.card.create.call_count == create_count

    @pytest.mark.asyncio
    async def test_finalize_tool_line_writes_done(self):
        ch = _make_channel()
        _mock_ok_chain(ch)
        await ch.send(_hint_msg("read docs/api.md", [_tool_event("read_file", {"path": "docs/api.md"})]))
        await ch._finalize_live_hint_card("oc_chat1", {})
        sent_texts = [
            call.args[0].body.content
            for call in ch._client.cardkit.v1.card_element.content.call_args_list
        ]
        assert any(t.endswith(" - done") for t in sent_texts)

    @pytest.mark.asyncio
    async def test_finalize_thinking_uses_custom_done_note(self):
        ch = _make_channel(live_tool_hint_done_note="finished")
        _mock_ok_chain(ch)
        await ch.send(_status_msg("AI thinking ..."))
        await ch._finalize_live_hint_card("oc_chat1", {})
        sent_texts = [
            call.args[0].body.content
            for call in ch._client.cardkit.v1.card_element.content.call_args_list
        ]
        assert "AI thinking - finished" in sent_texts

    @pytest.mark.asyncio
    async def test_finalize_tool_line_does_not_double_append_done(self):
        ch = _make_channel()
        _mock_ok_chain(ch)
        await ch.send(_hint_msg("read docs/api.md", [_tool_event("read_file", {"path": "docs/api.md"})]))
        await ch._finalize_live_hint_card("oc_chat1", {})
        sent_texts = [
            call.args[0].body.content
            for call in ch._client.cardkit.v1.card_element.content.call_args_list
        ]
        done_lines = [t for t in sent_texts if t.endswith(" - done")]
        assert done_lines
        assert all(t.count(" - done") == 1 for t in done_lines)
        assert all("AI thinking" not in t for t in done_lines)

    @pytest.mark.asyncio
    async def test_finalize_consolidation_does_not_append_done(self):
        ch = _make_channel()
        _mock_ok_chain(ch)
        await ch.send(_status_msg("consolidating history (1234/8000 tokens)"))
        await ch._finalize_live_hint_card("oc_chat1", {})
        sent_texts = [
            call.args[0].body.content
            for call in ch._client.cardkit.v1.card_element.content.call_args_list
        ]
        assert any("consolidating history" in t for t in sent_texts)
        assert all(not t.endswith(" - done") for t in sent_texts)

    @pytest.mark.asyncio
    async def test_heartbeat_keeps_note_then_done(self):
        ch = _make_channel(live_tool_hint_heartbeat_seconds=0.1)
        _mock_ok_chain(ch)
        ch._live_hint_bufs["oc_chat1"] = _FeishuStreamBuf(
            text="🔧 read docs/api.md - processing", card_id="card_live_1", sequence=5,
            last_edit=time.monotonic() - 60, last_heartbeat=time.monotonic() - 60,
        )

        loop_task, stop_task, done = _run_beat_once(ch, "oc_chat1")

        await asyncio.wait_for(done.wait(), timeout=2)
        loop_task.cancel()
        stop_task.cancel()

        sent_texts = [
            call.args[0].body.content
            for call in ch._client.cardkit.v1.card_element.content.call_args_list
        ]
        # During the turn the heartbeat pulse rides along with the note.
        assert any("processing" in t and "\u00b7" in t for t in sent_texts)
        # After finalize the note is swapped to done.
        assert any(t.endswith(" - done") for t in sent_texts)


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
    async def test_note_config_camel_case_aliases(self):
        cfg = FeishuConfig(
            liveToolHintProcessingNote="working",
            liveToolHintDoneNote="finished",
        )
        assert cfg.live_tool_hint_processing_note == "working"
        assert cfg.live_tool_hint_done_note == "finished"
        dumped = cfg.model_dump(by_alias=True)
        assert dumped["liveToolHintProcessingNote"] == "working"
        assert dumped["liveToolHintDoneNote"] == "finished"

    @pytest.mark.asyncio
    async def test_defaults_to_live_mode(self):
        cfg = FeishuConfig()
        assert cfg.hint_mode == "live"
        assert cfg.live_tool_hint_heartbeat_seconds == 0.3
        assert cfg.live_tool_hint_processing_note == "processing"
        assert cfg.live_tool_hint_done_note == "done"
        assert cfg.live_tool_hint_print_frequency_ms == 1
        assert cfg.live_tool_hint_print_step == 1
        assert cfg.live_tool_hint_print_strategy == "fast"
        assert cfg.live_tool_hint_typewriter_cap_ms == 100
        assert cfg.live_tool_hint_min_dwell_ms == 800
        assert cfg.live_tool_hint_done_hold_ms == 200
        assert cfg.live_tool_hint_elapsed_after_ms == 3000

    @pytest.mark.asyncio
    async def test_typewriter_config_camel_case_aliases(self):
        cfg = FeishuConfig(
            liveToolHintPrintFrequencyMs=10,
            liveToolHintPrintStep=8,
            liveToolHintPrintStrategy="delay",
            liveToolHintTypewriterCapMs=120,
            liveToolHintMinDwellMs=80,
            liveToolHintDoneHoldMs=400,
            liveToolHintElapsedAfterMs=5000,
        )
        assert cfg.live_tool_hint_print_frequency_ms == 10
        assert cfg.live_tool_hint_print_step == 8
        assert cfg.live_tool_hint_print_strategy == "delay"
        assert cfg.live_tool_hint_typewriter_cap_ms == 120
        assert cfg.live_tool_hint_min_dwell_ms == 80
        assert cfg.live_tool_hint_done_hold_ms == 400
        assert cfg.live_tool_hint_elapsed_after_ms == 5000
        dumped = cfg.model_dump(by_alias=True)
        assert dumped["liveToolHintPrintFrequencyMs"] == 10
        assert dumped["liveToolHintPrintStep"] == 8
        assert dumped["liveToolHintPrintStrategy"] == "delay"
        assert dumped["liveToolHintTypewriterCapMs"] == 120
        assert dumped["liveToolHintMinDwellMs"] == 80
        assert dumped["liveToolHintDoneHoldMs"] == 400
        assert dumped["liveToolHintElapsedAfterMs"] == 5000

    @pytest.mark.asyncio
    async def test_typewriter_config_allows_none(self):
        cfg = FeishuConfig(
            live_tool_hint_print_frequency_ms=None,
            live_tool_hint_print_step=None,
            live_tool_hint_print_strategy="fast",
        )
        assert cfg.live_tool_hint_print_frequency_ms is None
        assert cfg.live_tool_hint_print_step is None

    @pytest.mark.asyncio
    async def test_typewriter_cap_zero_disables_cap(self):
        cfg = FeishuConfig(live_tool_hint_typewriter_cap_ms=0)
        assert cfg.live_tool_hint_typewriter_cap_ms == 0

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
    async def test_non_thinking_commits_within_edit_interval(self):
        """Tool/status lines always flush after the typewriter wait (no throttle drop)."""
        ch = _make_channel(
            live_tool_hint_heartbeat_seconds=0,
            live_tool_hint_min_dwell_ms=0,
        )
        _mock_ok_chain(ch)
        ch._live_typewriter_seconds = lambda text: 0.0  # type: ignore[method-assign]
        ch._live_hint_bufs["oc_chat1"] = _FeishuStreamBuf(
            text="old",
            card_id="card_live_1",
            sequence=5,
            last_edit=time.monotonic(),
            last_line_write=time.monotonic() - 60,
            last_payload="old",
        )
        before = ch._client.cardkit.v1.card_element.content.call_count
        await ch._update_live_hint_card("oc_chat1", {}, "hint a")
        assert ch._live_hint_bufs["oc_chat1"].text == "hint a"
        assert ch._client.cardkit.v1.card_element.content.call_count > before

    @pytest.mark.asyncio
    async def test_thinking_updates_respect_throttle_without_force(self):
        ch = _make_channel(live_tool_hint_heartbeat_seconds=0)
        _mock_ok_chain(ch)
        now = time.monotonic()
        ch._live_hint_bufs["oc_chat1"] = _FeishuStreamBuf(
            text="AI thinking",
            card_id="card_live_1",
            sequence=5,
            last_edit=now,
            last_line_write=now - 60,
            last_payload="AI thinking",
            thinking_status=True,
        )
        before = ch._client.cardkit.v1.card_element.content.call_count
        await ch._update_live_hint_card(
            "oc_chat1", {}, "AI thinking", thinking_status=True
        )
        assert ch._client.cardkit.v1.card_element.content.call_count == before
        await ch._update_live_hint_card(
            "oc_chat1", {}, "AI thinking", force=True, thinking_status=True
        )
        assert ch._client.cardkit.v1.card_element.content.call_count == before + 1
        update_call = ch._client.cardkit.v1.card_element.content.call_args[0][0]
        assert "AI thinking" in update_call.body.content


def _run_beat_once(ch: FeishuChannel, stream_key: str) -> tuple[asyncio.Task, asyncio.Task, asyncio.Event]:
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


class TestHeartbeat:

    @pytest.mark.asyncio
    async def test_heartbeat_resends_line_after_idle_interval(self):
        ch = _make_channel(live_tool_hint_heartbeat_seconds=0.1)
        _mock_ok_chain(ch)
        ch._live_hint_bufs["oc_chat1"] = _FeishuStreamBuf(
            text="working…", card_id="card_live_1", sequence=5,
            last_edit=time.monotonic() - 60, last_heartbeat=time.monotonic() - 60,
        )
        loop_task, stop_task, done = _run_beat_once(ch, "oc_chat1")

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

        # The first pulse is immediate. Finalize after that tick still
        # flushes the base line without the pulse suffix.
        async def _run_tick():
            await asyncio.sleep(0.05)
            await ch._finalize_live_hint_card("oc_chat1", {})

        stop_task = asyncio.create_task(_run_tick())
        loop_task = asyncio.create_task(
            ch._live_hint_heartbeat_loop("oc_chat1", "oc_chat1", 0.1)
        )
        await asyncio.wait_for(asyncio.gather(loop_task, stop_task), timeout=2)
        loop_task.cancel()
        stop_task.cancel()

        sent_texts = []
        for call in ch._client.cardkit.v1.card_element.content.call_args_list:
            body = call.args[0].body.content
            sent_texts.append(body)
        assert sent_texts  # finalize flushed the latest line
        assert all("old line" in t for t in sent_texts)
        assert any("\u00b7" in t for t in sent_texts)  # immediate first pulse
        assert "\u00b7" not in sent_texts[-1]  # finalize is un-pulsed


def _mock_fatal_content_response(code: int = 200850):
    """A CardKit content response whose streaming session was killed by Feishu."""
    resp = MagicMock()
    resp.success.return_value = False
    resp.code = code
    resp.msg = "card streaming timeout"
    return resp


class TestHeartbeatCircuitBreaker:
    """The heartbeat must stop overheating a card whose streaming session died."""

    @pytest.mark.asyncio
    async def test_stops_after_max_consecutive_failures(self):
        ch = _make_channel(
            live_tool_hint_heartbeat_seconds=0.05,
            live_tool_hint_max_consecutive_failures=3,
        )
        # Non-fatal (99999). Settings fail so the reopen does not issue a
        # second content write — one content call per failure.
        ch._client.cardkit.v1.card_element.content.return_value = _mock_content_response(False)
        ch._client.cardkit.v1.card.settings.return_value = _mock_content_response(False)
        ch._live_hint_bufs["oc_chat1"] = _FeishuStreamBuf(
            text="working…",
            card_id="card_live_1",
            sequence=5,
            last_edit=time.monotonic() - 60,
            last_heartbeat=time.monotonic() - 60,
        )

        # Run the loop; it should self-terminate after 3 failures, not loop forever.
        loop_task = asyncio.create_task(
            ch._live_hint_heartbeat_loop("oc_chat1", "oc_chat1", 0.05)
        )
        await asyncio.wait_for(loop_task, timeout=2)
        assert loop_task.done()

        # The dead buffer is dropped so the next hint creates a fresh card.
        assert "oc_chat1" not in ch._live_hint_bufs
        content_calls = ch._client.cardkit.v1.card_element.content.call_count
        assert content_calls == 3  # one per non-fatal failure, no second content

    @pytest.mark.asyncio
    async def test_success_resets_failure_counter(self):
        ch = _make_channel(
            live_tool_hint_heartbeat_seconds=0.05,
            live_tool_hint_max_consecutive_failures=3,
        )
        ch._client.cardkit.v1.card_element.content.side_effect = [
            _mock_content_response(False),
            _mock_content_response(False),
            _mock_content_response(True),  # transient blip resolved
            _mock_content_response(False),
            _mock_content_response(False),
            _mock_content_response(False),
        ]
        ch._client.cardkit.v1.card.settings.return_value = _mock_content_response(False)
        ch._live_hint_bufs["oc_chat1"] = _FeishuStreamBuf(
            text="working…",
            card_id="card_live_1",
            sequence=5,
            last_edit=time.monotonic() - 60,
            last_heartbeat=time.monotonic() - 60,
        )

        loop_task = asyncio.create_task(
            ch._live_hint_heartbeat_loop("oc_chat1", "oc_chat1", 0.05)
        )
        await asyncio.wait_for(loop_task, timeout=2)
        assert loop_task.done()
        # Sequence: fail, fail, ok(reset), fail, fail, fail -> stops at the 6th.
        assert ch._client.cardkit.v1.card_element.content.call_count == 6

    @pytest.mark.asyncio
    @pytest.mark.parametrize("code", [200850, 300309])
    async def test_stops_on_first_fatal_code(self, code: int):
        ch = _make_channel(
            live_tool_hint_heartbeat_seconds=0.05,
            live_tool_hint_max_consecutive_failures=5,
        )
        ch._client.cardkit.v1.card_element.content.return_value = _mock_fatal_content_response(code)
        ch._client.cardkit.v1.card.settings.return_value = _mock_content_response(True)
        ch._live_hint_bufs["oc_chat1"] = _FeishuStreamBuf(
            text="working…",
            card_id="card_live_1",
            sequence=5,
            last_edit=time.monotonic() - 60,
            last_heartbeat=time.monotonic() - 60,
        )

        loop_task = asyncio.create_task(
            ch._live_hint_heartbeat_loop("oc_chat1", "oc_chat1", 0.05)
        )
        await asyncio.wait_for(loop_task, timeout=2)
        assert loop_task.done()
        assert "oc_chat1" not in ch._live_hint_bufs
        assert "card_live_1" in ch._dead_card_ids
        assert ch._client.cardkit.v1.card_element.content.call_count == 1
        assert ch._client.cardkit.v1.card.settings.call_count == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("code", [200850, 300309])
    async def test_fatal_code_skips_reopen_retry(self, code: int):
        ch = _make_channel()
        ch._client.cardkit.v1.card_element.content.return_value = _mock_fatal_content_response(code)
        ch._client.cardkit.v1.card.settings.return_value = _mock_content_response(True)

        ok, seq = ch._stream_update_text_with_reopen_sync("card_1", "hello", 4)
        assert ok is False
        assert seq == 4  # no bump from the skipped reopen retry
        # Only one content call; no set-streaming-mode reopen.
        assert ch._client.cardkit.v1.card_element.content.call_count == 1
        assert ch._client.cardkit.v1.card.settings.call_count == 0
        assert "card_1" in ch._dead_card_ids

    @pytest.mark.asyncio
    async def test_fatal_seed_skips_second_content_write(self):
        ch = _make_channel(live_tool_hint_print_strategy="delay")
        ch._client.cardkit.v1.card_element.content.return_value = _mock_fatal_content_response(300309)
        ch._client.cardkit.v1.card.settings.return_value = _mock_content_response(True)
        buf = _FeishuStreamBuf(last_payload="previous line", sequence=4)
        loop = asyncio.get_running_loop()

        ok = await ch._commit_live_card_content(buf, loop, "card_1", "brand new line", 5)
        assert ok is False
        assert ch._client.cardkit.v1.card_element.content.call_count == 1
        assert ch._client.cardkit.v1.card.settings.call_count == 0
        assert "card_1" in ch._dead_card_ids

    @pytest.mark.asyncio
    async def test_fatal_update_drops_buffer_without_rearm(self):
        ch = _make_channel(live_tool_hint_heartbeat_seconds=0.3)
        ch._client.cardkit.v1.card_element.content.return_value = _mock_fatal_content_response(300309)
        ch._client.cardkit.v1.card.settings.return_value = _mock_content_response(True)
        ch._live_hint_bufs["oc_chat1"] = _FeishuStreamBuf(
            text="old line",
            card_id="card_live_1",
            chat_id="oc_chat1",
            sequence=5,
            last_edit=time.monotonic() - 60,
            last_payload="old line",
        )

        await ch._update_live_hint_card("oc_chat1", {}, "new line", force=True)
        assert "oc_chat1" not in ch._live_hint_bufs
        assert "oc_chat1" not in ch._live_hint_heartbeat_tasks
        assert "card_live_1" in ch._dead_card_ids
        assert ch._client.cardkit.v1.card.settings.call_count == 0

    @pytest.mark.asyncio
    async def test_nonfatal_update_keeps_buffer_and_does_not_arm(self):
        ch = _make_channel(live_tool_hint_heartbeat_seconds=0.3)
        ch._client.cardkit.v1.card_element.content.return_value = _mock_content_response(False)
        ch._client.cardkit.v1.card.settings.return_value = _mock_content_response(False)
        ch._live_hint_bufs["oc_chat1"] = _FeishuStreamBuf(
            text="old line",
            card_id="card_live_1",
            chat_id="oc_chat1",
            sequence=5,
            last_edit=time.monotonic() - 60,
            last_payload="old line",
        )

        await ch._update_live_hint_card("oc_chat1", {}, "new line", force=True)
        assert "oc_chat1" in ch._live_hint_bufs
        assert "oc_chat1" not in ch._live_hint_heartbeat_tasks
        assert "card_live_1" not in ch._dead_card_ids

    @pytest.mark.asyncio
    async def test_finalize_without_message_id_retires_message_keyed_card(self):
        ch = _make_channel(live_tool_hint_heartbeat_seconds=0.05)
        ch._client.cardkit.v1.card_element.content.return_value = _mock_fatal_content_response()
        ch._client.cardkit.v1.card.settings.return_value = _mock_content_response(True)
        ch._dead_card_ids.add("card_live_1")
        ch._live_hint_bufs["om_trigger"] = _FeishuStreamBuf(
            text="working…",
            card_id="card_live_1",
            chat_id="oc_chat1",
            sequence=5,
            last_edit=time.monotonic() - 60,
        )
        ch._arm_live_hint_heartbeat("oc_chat1", "om_trigger")
        task = ch._live_hint_heartbeat_tasks.get("om_trigger")
        assert task is not None

        await ch._finalize_live_hint_card("oc_chat1", {})
        if task is not None and not task.done():
            task.cancel()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

        assert "om_trigger" not in ch._live_hint_bufs
        assert "om_trigger" not in ch._live_hint_heartbeat_tasks
        assert ch._client.cardkit.v1.card_element.content.call_count == 0
        assert ch._client.cardkit.v1.card.settings.call_count == 0

    @pytest.mark.asyncio
    async def test_non_fatal_code_still_reopens_and_retries(self):
        ch = _make_channel()
        ch._client.cardkit.v1.card_element.content.side_effect = [
            _mock_content_response(False),  # code 99999 (not fatal)
            _mock_content_response(True),
        ]
        ch._client.cardkit.v1.card.settings.return_value = _mock_content_response(True)

        ok, seq = ch._stream_update_text_with_reopen_sync("card_1", "hello", 4)
        assert ok is True
        assert seq == 6
        assert ch._client.cardkit.v1.card_element.content.call_count == 2
        assert ch._client.cardkit.v1.card.settings.call_count == 1

    @pytest.mark.asyncio
    async def test_arm_does_not_resurrect_dropped_buffer(self):
        ch = _make_channel(live_tool_hint_heartbeat_seconds=0.1)
        ch._live_hint_bufs.clear()
        ch._arm_live_hint_heartbeat("oc_chat1", "oc_chat1")
        # No heartbeat task should be created for a stream with no buffer.
        assert "oc_chat1" not in ch._live_hint_heartbeat_tasks
        assert ch._background_tasks == set()


class TestLiveCardStreamingConfig:
    """The per-card typewriter speed is set once at card creation."""

    @pytest.mark.asyncio
    async def test_create_card_includes_typewriter_config(self):
        ch = _make_channel()
        _mock_ok_chain(ch)

        await ch.send(_hint_msg('read docs/api.md', [_tool_event("read_file", {"path": "docs/api.md"})]))

        create_call = ch._client.cardkit.v1.card.create.call_args[0][0]
        card_json = json.loads(create_call.body.data)
        streaming_config = card_json["config"]["streaming_config"]
        assert streaming_config["print_frequency_ms"] == {"default": 1}
        assert streaming_config["print_step"] == {"default": 1}
        assert streaming_config["print_strategy"] == "fast"

    @pytest.mark.asyncio
    async def test_create_card_honors_custom_typewriter_config(self):
        ch = _make_channel(
            live_tool_hint_print_frequency_ms=12,
            live_tool_hint_print_step=6,
            live_tool_hint_print_strategy="delay",
        )
        _mock_ok_chain(ch)

        await ch.send(_hint_msg('read docs/api.md', [_tool_event("read_file", {"path": "docs/api.md"})]))

        create_call = ch._client.cardkit.v1.card.create.call_args[0][0]
        card_json = json.loads(create_call.body.data)
        streaming_config = card_json["config"]["streaming_config"]
        assert streaming_config["print_frequency_ms"] == {"default": 12}
        assert streaming_config["print_step"] == {"default": 6}
        assert streaming_config["print_strategy"] == "delay"

    @pytest.mark.asyncio
    async def test_create_card_omits_streaming_config_when_unset(self):
        ch = _make_channel(
            live_tool_hint_print_frequency_ms=None,
            live_tool_hint_print_step=None,
        )
        _mock_ok_chain(ch)

        await ch.send(_hint_msg('read docs/api.md', [_tool_event("read_file", {"path": "docs/api.md"})]))

        create_call = ch._client.cardkit.v1.card.create.call_args[0][0]
        card_json = json.loads(create_call.body.data)
        assert "streaming_config" not in card_json["config"]


class TestLiveCardLocking:
    """The per-stream lock serializes updates so sequence never regresses."""

    @pytest.mark.asyncio
    async def test_update_and_heartbeat_share_one_lock(self):
        """A heartbeat tick must wait for an in-flight hint update (no 300317)."""
        ch = _make_channel(live_tool_hint_heartbeat_seconds=0.05)
        _mock_ok_chain(ch)
        buf = _FeishuStreamBuf(
            text="working…", card_id="card_live_1", sequence=5,
            last_edit=time.monotonic() - 60, last_heartbeat=time.monotonic() - 60,
        )
        ch._live_hint_bufs["oc_chat1"] = buf

        # Hold the lock on behalf of an in-flight update.
        barrier = asyncio.Event()
        update_holding = asyncio.Event()
        heartbeat_started = asyncio.Event()
        heartbeat_calls = []

        async def _slow_update():
            async with buf.lock:
                update_holding.set()
                await barrier.wait()
                await asyncio.sleep(0)

        async def _heartbeat_tick():
            heartbeat_started.set()
            async with buf.lock:
                heartbeat_calls.append(time.monotonic())
                await asyncio.sleep(0)

        slow_task = asyncio.create_task(_slow_update())
        await update_holding.wait()
        hb_task = asyncio.create_task(_heartbeat_tick())
        await heartbeat_started.wait()
        await asyncio.sleep(0.05)
        assert heartbeat_calls == []  # blocked: the update holds the lock

        barrier.set()
        await asyncio.gather(slow_task, hb_task)
        assert len(heartbeat_calls) == 1  # only after the update released

    @pytest.mark.asyncio
    async def test_sequence_never_regresses_on_helper_return(self):
        """buf.sequence is max()-clamped to the helper's return even on failure."""
        ch = _make_channel()
        _mock_ok_chain(ch)
        buf = _FeishuStreamBuf(
            text="working…", card_id="card_live_1", sequence=5,
            last_edit=time.monotonic() - 60,  # allow immediate update
            last_heartbeat=time.monotonic() - 60,
        )
        ch._live_hint_bufs["oc_chat1"] = buf

        # The helper reports a higher sequence (e.g. reopen path advanced it).
        def _helper(*args, **kwargs):
            return True, 9

        ch._stream_update_text_with_reopen_sync = _helper  # type: ignore[method-assign]
        await ch._update_live_hint_card("oc_chat1", {}, "new line", force=True)
        assert buf.sequence == 9  # max(5, 9) = 9

        # A lower return never moves the counter backward.
        def _helper_low(*args, **kwargs):
            return True, 2

        ch._stream_update_text_with_reopen_sync = _helper_low  # type: ignore[method-assign]
        await ch._update_live_hint_card("oc_chat1", {}, "newer line", force=True)
        assert buf.sequence == 9  # max(9, 2) = 9


class TestThinkingPulseHeartbeat:
    """Thinking and tool lines share the ·· ↔ ····· pulse."""

    @pytest.mark.asyncio
    async def test_thinking_send_strips_dots_and_marks_status(self):
        ch = _make_channel()
        _mock_ok_chain(ch)
        await ch.send(_status_msg("AI thinking ..."))
        buf = ch._live_hint_bufs.get("oc_chat1")
        assert buf is not None
        assert buf.thinking_status is True
        assert buf.text == "AI thinking"

    @pytest.mark.asyncio
    async def test_consolidation_is_not_thinking_status(self):
        ch = _make_channel()
        _mock_ok_chain(ch)
        await ch.send(_status_msg("consolidating history (1234/8000 tokens)"))
        buf = ch._live_hint_bufs.get("oc_chat1")
        assert buf is not None
        assert buf.thinking_status is False

    @pytest.mark.asyncio
    async def test_tool_hint_clears_thinking_status(self):
        ch = _make_channel()
        _mock_ok_chain(ch)
        await ch.send(_hint_msg("read docs/api.md", [_tool_event("read_file", {"path": "docs/api.md"})]))
        buf = ch._live_hint_bufs.get("oc_chat1")
        assert buf is not None
        assert buf.thinking_status is False

    @pytest.mark.asyncio
    async def test_thinking_heartbeat_uses_middle_dot_pulse(self, monkeypatch):
        ch = _make_channel()
        _mock_ok_chain(ch)
        ch._live_hint_bufs["oc_chat1"] = _FeishuStreamBuf(
            text="AI thinking",
            card_id="card_live_1",
            sequence=5,
            thinking_status=True,
        )
        payloads: list[str] = []

        def helper(card_id, content, sequence):
            payloads.append(content)
            return True, sequence

        ch._stream_update_text_with_reopen_sync = helper  # type: ignore[method-assign]

        buf = ch._live_hint_bufs["oc_chat1"]
        sleeps: list[float] = []
        real_sleep = asyncio.sleep

        async def spy_sleep(delay, *args, **kwargs):
            sleeps.append(float(delay))
            if len(sleeps) > 2:
                ch._live_hint_bufs.pop("oc_chat1", None)
            await real_sleep(0)

        monkeypatch.setattr("nanobot.channels.feishu.runtime.asyncio.sleep", spy_sleep)
        await ch._live_hint_heartbeat_loop("oc_chat1", "oc_chat1", 0.1)

        assert sleeps[0] == pytest.approx(0.0, abs=0.01)
        assert payloads[0] == "AI thinking \u00b7\u00b7\u00b7\u00b7\u00b7"
        assert payloads[1] == "AI thinking \u00b7\u00b7"
        assert all(p != "-" for p in payloads)
        assert all("AI thinking ...." not in t for t in payloads)
        assert all("\u00b7" in t for t in payloads)
        assert buf.text == "AI thinking"

    @pytest.mark.asyncio
    async def test_thinking_heartbeat_one_content_call_per_tick(self):
        ch = _make_channel(live_tool_hint_heartbeat_seconds=0)
        _mock_ok_chain(ch)
        await ch.send(_status_msg("AI thinking ..."))
        before = ch._client.cardkit.v1.card_element.content.call_count

        loop_task, stop_task, done = _run_beat_once(ch, "oc_chat1")
        await asyncio.wait_for(done.wait(), timeout=2)
        loop_task.cancel()
        stop_task.cancel()

        sent_texts = [
            call.args[0].body.content
            for call in ch._client.cardkit.v1.card_element.content.call_args_list
        ]
        heartbeat_and_finalize = sent_texts[before:]
        assert "\u00b7" in heartbeat_and_finalize[0]
        assert heartbeat_and_finalize[0].startswith("AI thinking")
        assert heartbeat_and_finalize[0] != "A"
        assert "AI thinking ...." not in sent_texts
        # Pulsed thinking → "AI thinking - done"; fast skips the one-char seed.
        assert heartbeat_and_finalize[-1] == "AI thinking - done"
        assert "A" not in heartbeat_and_finalize
        assert "oc_chat1" not in ch._live_hint_bufs

    @pytest.mark.asyncio
    async def test_tool_hint_heartbeat_still_pulses(self):
        ch = _make_channel(live_tool_hint_heartbeat_seconds=0)
        _mock_ok_chain(ch)
        await ch.send(_hint_msg("read docs/api.md", [_tool_event("read_file", {"path": "docs/api.md"})]))
        buf = ch._live_hint_bufs["oc_chat1"]
        assert buf.thinking_status is False

        loop_task, stop_task, done = _run_beat_once(ch, "oc_chat1")
        await asyncio.wait_for(done.wait(), timeout=2)
        loop_task.cancel()
        stop_task.cancel()

        sent_texts = [
            call.args[0].body.content
            for call in ch._client.cardkit.v1.card_element.content.call_args_list
        ]
        assert any("\u00b7" in t and "read docs/api.md" in t for t in sent_texts)
        assert "A" not in sent_texts
        assert all(not t.startswith("AI thinking") for t in sent_texts if "\u00b7" in t)

    @pytest.mark.asyncio
    async def test_thinking_heartbeat_yields_to_tool_hint(self):
        ch = _make_channel(live_tool_hint_heartbeat_seconds=0.2)
        _mock_ok_chain(ch)
        payloads: list[str] = []
        original = ch._stream_update_text_with_reopen_sync

        def capture(card_id, content, sequence):
            payloads.append(content)
            return original(card_id, content, sequence)

        ch._stream_update_text_with_reopen_sync = capture  # type: ignore[method-assign]

        await ch.send(_status_msg("AI thinking ..."))
        buf = ch._live_hint_bufs["oc_chat1"]
        assert buf.thinking_status is True
        await asyncio.sleep(0.05)

        await ch.send(_hint_msg(
            "read docs/api.md",
            [_tool_event("read_file", {"path": "docs/api.md"})],
        ))
        buf = ch._live_hint_bufs["oc_chat1"]
        assert buf.thinking_status is False
        assert "read docs/api.md" in buf.text
        assert "AI thinking" not in buf.text

        # Long enough that a stale thinking tick would have fired.
        await asyncio.sleep(0.25)
        await ch._finalize_live_hint_card("oc_chat1", {})

        saw_tool = False
        thinking_after_tool = False
        seed_before_tool = False
        tool_line = ""
        for content in payloads:
            if "read docs/api.md" in content:
                if not saw_tool:
                    tool_line = content
                saw_tool = True
            elif saw_tool and content.startswith("AI thinking"):
                thinking_after_tool = True
        assert saw_tool
        seed = ch._live_stream_reset_seed(tool_line)
        for content in payloads:
            if content == seed:
                seed_before_tool = True
                break
            if "read docs/api.md" in content:
                break
        # Default print_strategy is fast: no one-char seed before the tool line.
        assert not seed_before_tool
        assert seed != "-"
        assert not thinking_after_tool
        assert "A" not in payloads
        assert seed not in payloads

    @pytest.mark.asyncio
    async def test_heartbeat_skips_missed_frames_instead_of_catch_up(self, monkeypatch):
        """When API RTT exceeds the interval, skip frames; never burst."""
        ch = _make_channel()
        _mock_ok_chain(ch)
        ch._live_hint_bufs["oc_chat1"] = _FeishuStreamBuf(
            text="working…",
            card_id="card_live_1",
            sequence=5,
        )
        api_calls: list[float] = []

        def slow_helper(*args, **kwargs):
            api_calls.append(time.monotonic())
            time.sleep(0.45)
            return True, 8

        ch._stream_update_text_with_reopen_sync = slow_helper  # type: ignore[method-assign]

        sleeps: list[float] = []
        real_sleep = asyncio.sleep

        async def spy_sleep(delay, *args, **kwargs):
            sleeps.append(float(delay))
            if len(sleeps) >= 2:
                ch._live_hint_bufs.pop("oc_chat1", None)
            await real_sleep(0)

        monkeypatch.setattr("nanobot.channels.feishu.runtime.asyncio.sleep", spy_sleep)
        await ch._live_hint_heartbeat_loop("oc_chat1", "oc_chat1", 0.2)

        assert sleeps[0] == pytest.approx(0.0, abs=0.01)
        assert len(api_calls) == 1  # one tick, not a catch-up burst
        # RTT > interval: skip missed frames so the next wait is never 0.
        assert sleeps[1] > 0.0

    @pytest.mark.asyncio
    async def test_heartbeat_defers_while_typewriter_busy(self, monkeypatch):
        """Do not pulse until the last hint line's typewriter estimate ends."""
        ch = _make_channel(
            live_tool_hint_print_frequency_ms=20,
            live_tool_hint_print_step=1,
            live_tool_hint_typewriter_cap_ms=0,  # disable cap so 200ms estimate applies
            live_tool_hint_min_dwell_ms=0,
        )
        _mock_ok_chain(ch)
        text = "abcdefghij"  # 10 chars → 200ms at 20ms/step
        write_at = 1000.0
        clock = {"t": write_at}
        ch._live_hint_bufs["oc_chat1"] = _FeishuStreamBuf(
            text=text,
            card_id="card_live_1",
            sequence=5,
            last_line_write=write_at,
            last_payload=text,
        )
        payloads: list[str] = []

        def helper(card_id, content, sequence):
            payloads.append(content)
            return True, sequence

        ch._stream_update_text_with_reopen_sync = helper  # type: ignore[method-assign]
        monkeypatch.setattr(
            "nanobot.channels.feishu.runtime.time.monotonic", lambda: clock["t"]
        )

        sleeps: list[float] = []
        real_sleep = asyncio.sleep

        async def spy_sleep(delay, *args, **kwargs):
            sleeps.append(float(delay))
            clock["t"] += max(float(delay), 0.0)
            if len(payloads) >= 1:
                ch._live_hint_bufs.pop("oc_chat1", None)
            await real_sleep(0)

        monkeypatch.setattr("nanobot.channels.feishu.runtime.asyncio.sleep", spy_sleep)
        await ch._live_hint_heartbeat_loop("oc_chat1", "oc_chat1", 0.05)

        assert sleeps[0] == pytest.approx(0.0, abs=0.01)
        # First wake is still inside the typewriter window → defer (~200ms), then pulse.
        assert sleeps[1] == pytest.approx(0.2, abs=0.01)
        assert len(payloads) == 1
        assert payloads[0].startswith(text)
        assert "\u00b7" in payloads[0]

    @pytest.mark.asyncio
    async def test_typewriter_cap_limits_wait_for_long_line(self):
        ch = _make_channel(
            live_tool_hint_print_frequency_ms=1,
            live_tool_hint_print_step=1,
            live_tool_hint_typewriter_cap_ms=80,
        )
        # 160 chars at 1ms/step would be 0.16s; cap keeps it at 0.08s.
        assert ch._live_typewriter_seconds("x" * 160) == pytest.approx(0.08, abs=0.001)

    @pytest.mark.asyncio
    async def test_typewriter_cap_zero_keeps_full_estimate(self):
        ch = _make_channel(
            live_tool_hint_print_frequency_ms=1,
            live_tool_hint_print_step=1,
            live_tool_hint_typewriter_cap_ms=0,
        )
        assert ch._live_typewriter_seconds("x" * 160) == pytest.approx(0.16, abs=0.001)

    @pytest.mark.asyncio
    async def test_batch_inter_line_wait_respects_cap(self, monkeypatch):
        ch = _make_channel(
            live_tool_hint_heartbeat_seconds=0,
            live_tool_hint_print_frequency_ms=1,
            live_tool_hint_print_step=1,
            live_tool_hint_typewriter_cap_ms=80,
            live_tool_hint_min_dwell_ms=0,
        )
        _mock_ok_chain(ch)
        clock = {"t": 1000.0}
        monkeypatch.setattr(
            "nanobot.channels.feishu.runtime.time.monotonic", lambda: clock["t"]
        )
        sleeps: list[float] = []
        real_sleep = asyncio.sleep

        async def spy_sleep(delay, *args, **kwargs):
            sleeps.append(float(delay))
            clock["t"] += max(float(delay), 0.0)
            await real_sleep(0)

        monkeypatch.setattr("nanobot.channels.feishu.runtime.asyncio.sleep", spy_sleep)
        # Long paths so raw typewriter estimate exceeds the 80ms cap.
        events = [
            _tool_event("read_file", {"path": "a" * 120 + ".md"}, "call_1"),
            _tool_event("read_file", {"path": "b" * 120 + ".md"}, "call_2"),
        ]
        await ch.send(_hint_msg("batch", events))
        assert sleeps
        assert all(s <= 0.08 + 0.001 for s in sleeps)
        assert any(s == pytest.approx(0.08, abs=0.01) for s in sleeps)
        sent_texts = [
            call.args[0].body.content
            for call in ch._client.cardkit.v1.card_element.content.call_args_list
        ]
        full_lines = [t for t in sent_texts if " - processing" in t]
        assert len(full_lines) == 2

    @pytest.mark.asyncio
    async def test_first_thinking_is_single_full_line_write(self):
        ch = _make_channel()
        _mock_ok_chain(ch)
        await ch.send(_status_msg("AI thinking ..."))
        sent_texts = [
            call.args[0].body.content
            for call in ch._client.cardkit.v1.card_element.content.call_args_list
        ]
        assert sent_texts == ["AI thinking"]
        assert "A" not in sent_texts

    @pytest.mark.asyncio
    async def test_thinking_skipped_while_tool_line_typewriting(self):
        ch = _make_channel(live_tool_hint_heartbeat_seconds=0)
        _mock_ok_chain(ch)
        payloads: list[str] = []
        original = ch._stream_update_text_with_reopen_sync

        def capture(card_id, content, sequence):
            payloads.append(content)
            return original(card_id, content, sequence)

        ch._stream_update_text_with_reopen_sync = capture  # type: ignore[method-assign]
        await ch.send(_hint_msg(
            "read docs/api.md",
            [_tool_event("read_file", {"path": "docs/api.md"})],
        ))
        after_tool = list(payloads)
        buf = ch._live_hint_bufs["oc_chat1"]
        tool_text = buf.text
        await ch.send(_status_msg("AI thinking ..."))
        assert payloads == after_tool
        assert buf.thinking_status is False
        assert buf.text == tool_text
        assert "AI thinking" not in buf.text
        assert "A" not in payloads

    @pytest.mark.asyncio
    async def test_thinking_dropped_while_tool_in_flight_after_typewriter(self):
        """After the typewriter cap, thinking still must not replace an in-flight tool."""
        ch = _make_channel(
            live_tool_hint_heartbeat_seconds=0,
            live_tool_hint_done_hold_ms=0,
            live_tool_hint_min_dwell_ms=0,
        )
        _mock_ok_chain(ch)
        await ch.send(_hint_msg(
            "read docs/api.md",
            [_tool_event("read_file", {"path": "docs/api.md"})],
        ))
        buf = ch._live_hint_bufs["oc_chat1"]
        buf.last_line_write = time.monotonic() - 60
        tool_text = buf.text
        await ch.send(_status_msg("AI thinking ..."))
        assert buf.text == tool_text
        assert buf.thinking_status is False
        assert buf.tool_in_flight is True
        assert "processing" in buf.text
        assert "AI thinking" not in buf.text

    @pytest.mark.asyncio
    async def test_finish_swaps_processing_to_done_without_chat_send(self):
        ch = _make_channel(
            live_tool_hint_heartbeat_seconds=0,
            live_tool_hint_done_hold_ms=0,
            live_tool_hint_min_dwell_ms=0,
        )
        _mock_ok_chain(ch)
        await ch.send(_hint_msg(
            "read docs/api.md",
            [_tool_event("read_file", {"path": "docs/api.md"})],
        ))
        create_count = ch._client.im.v1.message.create.call_count
        await ch.send(_finish_msg([
            _tool_event("read_file", {"path": "docs/api.md"}, phase="end"),
        ]))
        buf = ch._live_hint_bufs["oc_chat1"]
        assert buf.tool_in_flight is False
        assert buf.thinking_status is False
        assert buf.text.endswith(" - done")
        assert "processing" not in buf.text
        assert "read docs/api.md" in buf.text
        assert ch._client.im.v1.message.create.call_count == create_count
        assert "oc_chat1" in ch._live_hint_bufs

    @pytest.mark.asyncio
    async def test_finish_error_phase_also_marks_done(self):
        ch = _make_channel(
            live_tool_hint_heartbeat_seconds=0,
            live_tool_hint_min_dwell_ms=0,
            live_tool_hint_done_hold_ms=0,
        )
        _mock_ok_chain(ch)
        await ch.send(_hint_msg(
            "read docs/api.md",
            [_tool_event("read_file", {"path": "docs/api.md"})],
        ))
        await ch.send(_finish_msg([
            _tool_event("read_file", {"path": "docs/api.md"}, phase="error"),
        ]))
        buf = ch._live_hint_bufs["oc_chat1"]
        assert buf.tool_in_flight is False
        assert buf.text.endswith(" - done")

    @pytest.mark.asyncio
    async def test_finish_then_thinking_after_done_hold(self, monkeypatch):
        ch = _make_channel(
            live_tool_hint_heartbeat_seconds=0,
            live_tool_hint_done_hold_ms=300,
            live_tool_hint_min_dwell_ms=0,
        )
        _mock_ok_chain(ch)
        ch._live_typewriter_seconds = lambda text: 0.0  # type: ignore[method-assign]
        clock = {"t": 1000.0}
        monkeypatch.setattr(
            "nanobot.channels.feishu.runtime.time.monotonic", lambda: clock["t"]
        )
        sleeps: list[float] = []
        real_sleep = asyncio.sleep

        async def spy_sleep(delay, *args, **kwargs):
            sleeps.append(float(delay))
            clock["t"] += max(float(delay), 0.0)
            await real_sleep(0)

        monkeypatch.setattr("nanobot.channels.feishu.runtime.asyncio.sleep", spy_sleep)

        await ch.send(_hint_msg(
            "read docs/api.md",
            [_tool_event("read_file", {"path": "docs/api.md"})],
        ))
        await ch.send(_finish_msg([
            _tool_event("read_file", {"path": "docs/api.md"}, phase="end"),
        ]))
        buf = ch._live_hint_bufs["oc_chat1"]
        assert buf.text.endswith(" - done")
        await ch.send(_status_msg("AI thinking ..."))
        assert any(s == pytest.approx(0.3, abs=0.01) for s in sleeps)
        assert buf.thinking_status is True
        assert buf.text == "AI thinking"
        assert buf.tool_in_flight is False

    @pytest.mark.asyncio
    async def test_tool_hint_still_updates_during_typewriter_window(self):
        """Tool→tool waits for the estimate, then replaces (does not drop)."""
        ch = _make_channel(
            live_tool_hint_heartbeat_seconds=0,
            live_tool_hint_min_dwell_ms=0,
        )
        _mock_ok_chain(ch)
        ch._live_typewriter_seconds = lambda text: 0.0  # type: ignore[method-assign]
        await ch.send(_hint_msg(
            "read docs/api.md",
            [_tool_event("read_file", {"path": "docs/api.md"})],
        ))
        await ch.send(_hint_msg(
            "write out.txt",
            [_tool_event("write_file", {"path": "out.txt"}, call_id="call_2")],
        ))
        buf = ch._live_hint_bufs["oc_chat1"]
        assert "out.txt" in buf.text
        assert "read docs/api.md" not in buf.text
        assert buf.thinking_status is False

    @pytest.mark.asyncio
    async def test_tool_to_thinking_skips_seed_when_fast(self):
        ch = _make_channel(
            live_tool_hint_heartbeat_seconds=0,
            live_tool_hint_print_strategy="fast",
            live_tool_hint_done_hold_ms=0,
            live_tool_hint_min_dwell_ms=0,
        )
        _mock_ok_chain(ch)
        await ch.send(_hint_msg(
            "read docs/api.md",
            [_tool_event("read_file", {"path": "docs/api.md"})],
        ))
        await ch.send(_finish_msg([
            _tool_event("read_file", {"path": "docs/api.md"}, phase="end"),
        ]))
        ch._live_hint_bufs["oc_chat1"].last_line_write = time.monotonic() - 60
        ch._live_hint_bufs["oc_chat1"].done_hold_until = 0.0
        await ch.send(_status_msg("AI thinking ..."))
        sent_texts = [
            call.args[0].body.content
            for call in ch._client.cardkit.v1.card_element.content.call_args_list
        ]
        assert sent_texts[-1] == "AI thinking"
        assert "A" not in sent_texts
        buf = ch._live_hint_bufs["oc_chat1"]
        assert buf.thinking_status is True
        assert buf.text == "AI thinking"

    @pytest.mark.asyncio
    async def test_tool_to_thinking_writes_seed_then_line_when_delay(self):
        ch = _make_channel(
            live_tool_hint_heartbeat_seconds=0,
            live_tool_hint_print_strategy="delay",
            live_tool_hint_done_hold_ms=0,
            live_tool_hint_min_dwell_ms=0,
        )
        _mock_ok_chain(ch)
        await ch.send(_hint_msg(
            "read docs/api.md",
            [_tool_event("read_file", {"path": "docs/api.md"})],
        ))
        await ch.send(_finish_msg([
            _tool_event("read_file", {"path": "docs/api.md"}, phase="end"),
        ]))
        ch._live_hint_bufs["oc_chat1"].last_line_write = time.monotonic() - 60
        ch._live_hint_bufs["oc_chat1"].done_hold_until = 0.0
        await ch.send(_status_msg("AI thinking ..."))
        sent_texts = [
            call.args[0].body.content
            for call in ch._client.cardkit.v1.card_element.content.call_args_list
        ]
        assert sent_texts[-2:] == ["A", "AI thinking"]
        assert sent_texts.count("AI thinking") == 1
        assert sent_texts.count("A") == 1
        assert "AI thinking " not in sent_texts
        buf = ch._live_hint_bufs["oc_chat1"]
        assert buf.thinking_status is True
        assert buf.text == "AI thinking"

    @pytest.mark.asyncio
    async def test_finalize_does_not_reuse_in_flight_heartbeat_sequence(self, monkeypatch):
        """Finalize waits for an in-flight heartbeat write so sequence stays unique."""
        ch = _make_channel()
        _mock_ok_chain(ch)
        buf = _FeishuStreamBuf(
            text="working…",
            card_id="card_live_1",
            sequence=5,
        )
        ch._live_hint_bufs["oc_chat1"] = buf

        started = threading.Event()
        release = threading.Event()
        sequences: list[int] = []

        def blocking_helper(card_id, content, sequence):
            sequences.append(sequence)
            started.set()
            assert release.wait(timeout=5)
            return True, sequence

        ch._stream_update_text_with_reopen_sync = blocking_helper  # type: ignore[method-assign]

        real_sleep = asyncio.sleep

        async def spy_sleep(delay, *args, **kwargs):
            await real_sleep(0)

        monkeypatch.setattr("nanobot.channels.feishu.runtime.asyncio.sleep", spy_sleep)
        loop_task = asyncio.create_task(
            ch._live_hint_heartbeat_loop("oc_chat1", "oc_chat1", 0.05)
        )
        for _ in range(200):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()

        fin_task = asyncio.create_task(ch._finalize_live_hint_card("oc_chat1", {}))
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.wait_for(fin_task, timeout=2)
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass

        assert sequences == sorted(sequences)
        assert len(sequences) == len(set(sequences)), sequences
        assert sequences[0] == 6
        assert 7 in sequences


_ELAPSED_LABEL = re.compile(r"(?<!\d)\d+\.\d+s")


class TestLiveCardElapsed:
    def _processing_buf(self, started_at: float) -> _FeishuStreamBuf:
        buf = _FeishuStreamBuf()
        buf.text = "🔧 read foo.md - processing"
        buf.tool_in_flight = True
        buf.elapsed_started_at = started_at
        return buf

    @pytest.mark.asyncio
    async def test_hidden_under_threshold(self):
        ch = _make_channel()
        buf = self._processing_buf(100.0)
        payload = ch._live_hint_heartbeat_payload(buf, now=102.9)
        assert "\u00b7" in payload
        assert _ELAPSED_LABEL.search(payload) is None
        assert payload.startswith(buf.text)
        assert buf.text == "🔧 read foo.md - processing"

    @pytest.mark.asyncio
    async def test_shown_at_threshold_between_note_and_pulse(self):
        ch = _make_channel()
        buf = self._processing_buf(100.0)
        payload = ch._live_hint_heartbeat_payload(buf, now=103.0)
        assert payload.startswith("🔧 read foo.md - processing 3.0s ")
        assert payload.endswith("\u00b7\u00b7\u00b7\u00b7\u00b7")
        assert buf.text == "🔧 read foo.md - processing"
        later = ch._live_hint_heartbeat_payload(buf, now=103.3)
        assert later.startswith("🔧 read foo.md - processing 3.3s ")
        assert later.endswith("\u00b7\u00b7")
        assert buf.text == "🔧 read foo.md - processing"

    @pytest.mark.asyncio
    async def test_zero_threshold_shows_from_first_tick_but_omits_zero(self):
        ch = _make_channel(live_tool_hint_elapsed_after_ms=0)
        buf = self._processing_buf(100.0)
        at_commit = ch._live_hint_heartbeat_payload(buf, now=100.0)
        assert "0.0s" not in at_commit
        assert "\u00b7" in at_commit
        assert buf.text == "🔧 read foo.md - processing"
        first = ch._live_hint_heartbeat_payload(buf, now=100.4)
        assert " 0.4s " in first
        assert first.startswith("🔧 read foo.md - processing 0.4s ")
        assert "\u00b7" in first
        assert buf.text == "🔧 read foo.md - processing"

    @pytest.mark.asyncio
    async def test_sequential_tool_resets_counter(self):
        ch = _make_channel(live_tool_hint_heartbeat_seconds=0, live_tool_hint_min_dwell_ms=0)
        _mock_ok_chain(ch)
        ch._live_typewriter_seconds = lambda text: 0.0  # type: ignore[method-assign]
        await ch.send(_hint_msg(
            "read foo.md",
            [_tool_event("read_file", {"path": "foo.md"}, "call_1")],
        ))
        buf = ch._live_hint_bufs["oc_chat1"]
        first_start = buf.elapsed_started_at
        assert first_start > 0
        buf.elapsed_started_at = time.monotonic() - 10.0
        stale = buf.elapsed_started_at
        await ch.send(_hint_msg(
            'grep "TODO"',
            [_tool_event("grep", {"pattern": "TODO"}, "call_2")],
        ))
        assert buf.elapsed_started_at > stale
        hidden = ch._live_hint_heartbeat_payload(buf, now=buf.elapsed_started_at + 2.9)
        assert _ELAPSED_LABEL.search(hidden) is None
        shown = ch._live_hint_heartbeat_payload(buf, now=buf.elapsed_started_at + 3.0)
        assert " 3.0s " in shown
        assert "10." not in shown

    @pytest.mark.asyncio
    async def test_finish_has_no_elapsed_thinking_starts_clock(self):
        ch = _make_channel(
            live_tool_hint_heartbeat_seconds=0,
            live_tool_hint_done_hold_ms=0,
            live_tool_hint_min_dwell_ms=0,
        )
        _mock_ok_chain(ch)
        await ch.send(_hint_msg(
            "read foo.md",
            [_tool_event("read_file", {"path": "foo.md"})],
        ))
        buf = ch._live_hint_bufs["oc_chat1"]
        assert buf.elapsed_started_at > 0
        await ch.send(_finish_msg([
            _tool_event("read_file", {"path": "foo.md"}, phase="end"),
        ]))
        assert buf.elapsed_started_at == 0.0
        assert buf.tool_in_flight is False
        assert buf.text.endswith(" - done")
        assert _ELAPSED_LABEL.search(buf.text) is None
        done_payload = ch._live_hint_heartbeat_payload(buf, now=time.monotonic() + 10)
        assert _ELAPSED_LABEL.search(done_payload) is None

        buf.last_line_write = time.monotonic() - 60
        buf.done_hold_until = 0.0
        before = time.monotonic()
        await ch.send(_status_msg("AI thinking ..."))
        assert buf.thinking_status is True
        assert buf.elapsed_started_at >= before
        assert buf.elapsed_started_at <= time.monotonic()
        assert buf.text == "AI thinking"
        assert _ELAPSED_LABEL.search(buf.text) is None
        thinking_payload = ch._live_hint_heartbeat_payload(buf, now=buf.elapsed_started_at)
        assert thinking_payload.startswith("AI thinking ")
        assert _ELAPSED_LABEL.search(thinking_payload) is None
        shown = ch._live_hint_heartbeat_payload(buf, now=buf.elapsed_started_at + 3.0)
        assert shown.startswith("AI thinking 3.0s ")

    @pytest.mark.asyncio
    async def test_first_tool_commit_starts_elapsed_clock(self):
        ch = _make_channel(live_tool_hint_heartbeat_seconds=0)
        _mock_ok_chain(ch)
        before = time.monotonic()
        await ch.send(_hint_msg(
            "read foo.md",
            [_tool_event("read_file", {"path": "foo.md"})],
        ))
        buf = ch._live_hint_bufs["oc_chat1"]
        assert buf.tool_in_flight is True
        assert buf.elapsed_started_at >= before
        assert buf.elapsed_started_at <= time.monotonic()


class TestLiveCardMinDwell:
    @pytest.mark.asyncio
    async def test_line_hold_adds_dwell_to_typewriter(self):
        ch = _make_channel(
            live_tool_hint_print_frequency_ms=1,
            live_tool_hint_print_step=1,
            live_tool_hint_typewriter_cap_ms=80,
            live_tool_hint_min_dwell_ms=150,
        )
        assert ch._live_typewriter_seconds("x" * 160) == pytest.approx(0.08, abs=0.001)
        assert ch._live_line_hold_seconds("x" * 160) == pytest.approx(0.23, abs=0.001)

    @pytest.mark.asyncio
    async def test_sequential_wait_is_typewriter_plus_dwell(self, monkeypatch):
        ch = _make_channel(
            live_tool_hint_heartbeat_seconds=0,
            live_tool_hint_min_dwell_ms=150,
        )
        _mock_ok_chain(ch)
        clock = {"t": 1000.0}
        monkeypatch.setattr(
            "nanobot.channels.feishu.runtime.time.monotonic", lambda: clock["t"]
        )
        ch._live_typewriter_seconds = lambda text: 0.05  # type: ignore[method-assign]
        sleeps: list[float] = []
        real_sleep = asyncio.sleep

        async def spy_sleep(delay, *args, **kwargs):
            sleeps.append(float(delay))
            clock["t"] += max(float(delay), 0.0)
            await real_sleep(0)

        monkeypatch.setattr("nanobot.channels.feishu.runtime.asyncio.sleep", spy_sleep)
        events = [
            _tool_event("read_file", {"path": "a.md"}, "call_1"),
            _tool_event("read_file", {"path": "b.md"}, "call_2"),
        ]
        await ch.send(_hint_msg("a, b", events))
        assert any(s == pytest.approx(0.20, abs=0.01) for s in sleeps)
        sent_texts = [
            call.args[0].body.content
            for call in ch._client.cardkit.v1.card_element.content.call_args_list
        ]
        full_lines = [t for t in sent_texts if " - processing" in t]
        assert len(full_lines) == 2
        assert "a.md" in full_lines[0]
        assert "b.md" in full_lines[1]

    @pytest.mark.asyncio
    async def test_sequential_dwell_survives_cardkit_rtt_longer_than_hold(self, monkeypatch):
        """CardKit RTT must not eat min_dwell: hold starts when the write returns."""
        ch = _make_channel(
            live_tool_hint_heartbeat_seconds=0,
            live_tool_hint_min_dwell_ms=150,
        )
        _mock_ok_chain(ch)
        clock = {"t": 1000.0}
        monkeypatch.setattr(
            "nanobot.channels.feishu.runtime.time.monotonic", lambda: clock["t"]
        )
        ch._live_typewriter_seconds = lambda text: 0.0  # type: ignore[method-assign]
        rtt = 0.30
        original_commit = ch._commit_live_card_content

        async def slow_commit(*args, **kwargs):
            clock["t"] += rtt
            return await original_commit(*args, **kwargs)

        ch._commit_live_card_content = slow_commit  # type: ignore[method-assign]
        sleeps: list[float] = []
        real_sleep = asyncio.sleep

        async def spy_sleep(delay, *args, **kwargs):
            sleeps.append(float(delay))
            clock["t"] += max(float(delay), 0.0)
            await real_sleep(0)

        monkeypatch.setattr("nanobot.channels.feishu.runtime.asyncio.sleep", spy_sleep)
        events = [
            _tool_event("read_file", {"path": "a.md"}, "call_1"),
            _tool_event("read_file", {"path": "b.md"}, "call_2"),
        ]
        await ch.send(_hint_msg("a, b", events))
        assert any(s == pytest.approx(0.15, abs=0.01) for s in sleeps)
        sent_texts = [
            call.args[0].body.content
            for call in ch._client.cardkit.v1.card_element.content.call_args_list
        ]
        full_lines = [t for t in sent_texts if " - processing" in t]
        assert len(full_lines) == 2
        assert "a.md" in full_lines[0]
        assert "b.md" in full_lines[1]

    @pytest.mark.asyncio
    async def test_zero_dwell_and_zero_typewriter_replaces_immediately(self, monkeypatch):
        ch = _make_channel(
            live_tool_hint_heartbeat_seconds=0,
            live_tool_hint_min_dwell_ms=0,
        )
        _mock_ok_chain(ch)
        ch._live_typewriter_seconds = lambda text: 0.0  # type: ignore[method-assign]
        sleeps: list[float] = []
        real_sleep = asyncio.sleep

        async def spy_sleep(delay, *args, **kwargs):
            sleeps.append(float(delay))
            await real_sleep(0)

        monkeypatch.setattr("nanobot.channels.feishu.runtime.asyncio.sleep", spy_sleep)
        events = [
            _tool_event("read_file", {"path": "a.md"}, "call_1"),
            _tool_event("read_file", {"path": "b.md"}, "call_2"),
        ]
        await ch.send(_hint_msg("a, b", events))
        assert not any(s > 0.001 for s in sleeps)
        buf = ch._live_hint_bufs["oc_chat1"]
        assert "b.md" in buf.text

    @pytest.mark.asyncio
    async def test_finish_waits_dwell_before_done_then_thinking_waits_done_hold(
        self, monkeypatch
    ):
        ch = _make_channel(
            live_tool_hint_heartbeat_seconds=0,
            live_tool_hint_min_dwell_ms=150,
            live_tool_hint_done_hold_ms=200,
        )
        _mock_ok_chain(ch)
        ch._live_typewriter_seconds = lambda text: 0.0  # type: ignore[method-assign]
        clock = {"t": 1000.0}
        monkeypatch.setattr(
            "nanobot.channels.feishu.runtime.time.monotonic", lambda: clock["t"]
        )
        sleeps: list[float] = []
        real_sleep = asyncio.sleep

        async def spy_sleep(delay, *args, **kwargs):
            sleeps.append(float(delay))
            clock["t"] += max(float(delay), 0.0)
            await real_sleep(0)

        monkeypatch.setattr("nanobot.channels.feishu.runtime.asyncio.sleep", spy_sleep)
        await ch.send(_hint_msg(
            "read foo.md",
            [_tool_event("read_file", {"path": "foo.md"})],
        ))
        buf = ch._live_hint_bufs["oc_chat1"]
        assert "processing" in buf.text
        await ch.send(_finish_msg([
            _tool_event("read_file", {"path": "foo.md"}, phase="end"),
        ]))
        assert any(s == pytest.approx(0.15, abs=0.01) for s in sleeps)
        assert buf.text.endswith(" - done")
        assert buf.tool_in_flight is False
        await ch.send(_status_msg("AI thinking ..."))
        assert any(s == pytest.approx(0.20, abs=0.01) for s in sleeps)
        assert buf.thinking_status is True
        assert buf.text == "AI thinking"


class TestLiveCardConsolidationElapsed:
    def _consolidating_buf(self, started_at: float) -> _FeishuStreamBuf:
        buf = _FeishuStreamBuf()
        buf.text = "consolidating history (1234/8000 tokens)"
        buf.elapsed_started_at = started_at
        return buf

    @pytest.mark.asyncio
    async def test_first_commit_starts_clock_without_label_in_text(self):
        ch = _make_channel(live_tool_hint_heartbeat_seconds=0)
        _mock_ok_chain(ch)
        before = time.monotonic()
        await ch.send(_status_msg("consolidating history (1234/8000 tokens)"))
        buf = ch._live_hint_bufs["oc_chat1"]
        assert buf.thinking_status is False
        assert buf.tool_in_flight is False
        assert buf.text == "consolidating history (1234/8000 tokens)"
        assert _ELAPSED_LABEL.search(buf.text) is None
        assert buf.elapsed_started_at >= before
        assert buf.elapsed_started_at <= time.monotonic()

    @pytest.mark.asyncio
    async def test_hidden_under_threshold_shown_after(self):
        ch = _make_channel()
        buf = self._consolidating_buf(100.0)
        hidden = ch._live_hint_heartbeat_payload(buf, now=102.9)
        assert "\u00b7" in hidden
        assert _ELAPSED_LABEL.search(hidden) is None
        assert buf.text == "consolidating history (1234/8000 tokens)"
        shown = ch._live_hint_heartbeat_payload(buf, now=103.0)
        assert shown.startswith("consolidating history (1234/8000 tokens) 3.0s ")
        assert "\u00b7" in shown
        assert buf.text == "consolidating history (1234/8000 tokens)"

    @pytest.mark.asyncio
    async def test_later_round_keeps_elapsed_start(self):
        ch = _make_channel(
            live_tool_hint_heartbeat_seconds=0,
            live_tool_hint_min_dwell_ms=0,
        )
        _mock_ok_chain(ch)
        ch._live_typewriter_seconds = lambda text: 0.0  # type: ignore[method-assign]
        await ch.send(_status_msg("consolidating history (1234/8000 tokens)"))
        buf = ch._live_hint_bufs["oc_chat1"]
        first_start = buf.elapsed_started_at
        assert first_start > 0
        await ch.send(_status_msg("consolidating history (4000/8000 tokens)"))
        assert buf.text == "consolidating history (4000/8000 tokens)"
        assert buf.elapsed_started_at == first_start
        shown = ch._live_hint_heartbeat_payload(buf, now=first_start + 3.0)
        assert " 3.0s " in shown

    @pytest.mark.asyncio
    async def test_history_consolidated_clears_clock_thinking_starts_new(self):
        ch = _make_channel(
            live_tool_hint_heartbeat_seconds=0,
            live_tool_hint_min_dwell_ms=0,
            live_tool_hint_done_hold_ms=0,
        )
        _mock_ok_chain(ch)
        ch._live_typewriter_seconds = lambda text: 0.0  # type: ignore[method-assign]
        await ch.send(_status_msg("consolidating history (1234/8000 tokens)"))
        buf = ch._live_hint_bufs["oc_chat1"]
        assert buf.elapsed_started_at > 0
        await ch.send(_status_msg("history consolidated"))
        assert buf.text == "history consolidated"
        assert buf.elapsed_started_at == 0.0
        done_payload = ch._live_hint_heartbeat_payload(buf, now=time.monotonic() + 10)
        assert _ELAPSED_LABEL.search(done_payload) is None

        buf.last_line_write = time.monotonic() - 60
        stale = time.monotonic() - 10.0
        buf.elapsed_started_at = stale
        await ch.send(_status_msg("AI thinking ..."))
        assert buf.thinking_status is True
        assert buf.elapsed_started_at > stale
        assert buf.text == "AI thinking"
        assert _ELAPSED_LABEL.search(buf.text) is None
        thinking_payload = ch._live_hint_heartbeat_payload(buf, now=buf.elapsed_started_at)
        assert thinking_payload.startswith("AI thinking ")
        assert _ELAPSED_LABEL.search(thinking_payload) is None


class TestLiveCardThinkingElapsed:
    def _thinking_buf(self, started_at: float) -> _FeishuStreamBuf:
        buf = _FeishuStreamBuf()
        buf.text = "AI thinking"
        buf.thinking_status = True
        buf.elapsed_started_at = started_at
        return buf

    @pytest.mark.asyncio
    async def test_first_commit_starts_clock_without_label_in_text(self):
        ch = _make_channel(live_tool_hint_heartbeat_seconds=0)
        _mock_ok_chain(ch)
        before = time.monotonic()
        await ch.send(_status_msg("AI thinking ..."))
        buf = ch._live_hint_bufs["oc_chat1"]
        assert buf.thinking_status is True
        assert buf.tool_in_flight is False
        assert buf.text == "AI thinking"
        assert _ELAPSED_LABEL.search(buf.text) is None
        assert buf.elapsed_started_at >= before
        assert buf.elapsed_started_at <= time.monotonic()

    @pytest.mark.asyncio
    async def test_hidden_under_threshold_shown_after(self):
        ch = _make_channel()
        buf = self._thinking_buf(100.0)
        hidden = ch._live_hint_heartbeat_payload(buf, now=102.9)
        assert hidden.startswith("AI thinking ")
        assert _ELAPSED_LABEL.search(hidden) is None
        assert buf.text == "AI thinking"
        shown = ch._live_hint_heartbeat_payload(buf, now=103.0)
        assert shown.startswith("AI thinking 3.0s ")
        assert "\u00b7" in shown
        assert buf.text == "AI thinking"

    @pytest.mark.asyncio
    async def test_later_thinking_send_keeps_elapsed_start(self):
        ch = _make_channel(
            live_tool_hint_heartbeat_seconds=0,
            live_tool_hint_min_dwell_ms=0,
            live_tool_hint_done_hold_ms=0,
        )
        _mock_ok_chain(ch)
        await ch.send(_status_msg("AI thinking ..."))
        buf = ch._live_hint_bufs["oc_chat1"]
        first_start = buf.elapsed_started_at
        assert first_start > 0
        await ch.send(_status_msg("AI thinking ..."))
        assert buf.text == "AI thinking"
        assert buf.thinking_status is True
        assert buf.elapsed_started_at == first_start
        shown = ch._live_hint_heartbeat_payload(buf, now=first_start + 3.0)
        assert shown.startswith("AI thinking 3.0s ")

    @pytest.mark.asyncio
    async def test_tool_then_thinking_resets_clock(self):
        ch = _make_channel(
            live_tool_hint_heartbeat_seconds=0,
            live_tool_hint_min_dwell_ms=0,
            live_tool_hint_done_hold_ms=0,
        )
        _mock_ok_chain(ch)
        ch._live_typewriter_seconds = lambda text: 0.0  # type: ignore[method-assign]
        await ch.send(_hint_msg(
            "read foo.md",
            [_tool_event("read_file", {"path": "foo.md"})],
        ))
        buf = ch._live_hint_bufs["oc_chat1"]
        buf.elapsed_started_at = time.monotonic() - 10.0
        stale = buf.elapsed_started_at
        await ch.send(_finish_msg([
            _tool_event("read_file", {"path": "foo.md"}, phase="end"),
        ]))
        assert buf.elapsed_started_at == 0.0
        buf.last_line_write = time.monotonic() - 60
        buf.done_hold_until = 0.0
        await ch.send(_status_msg("AI thinking ..."))
        assert buf.thinking_status is True
        assert buf.elapsed_started_at > stale
        hidden = ch._live_hint_heartbeat_payload(buf, now=buf.elapsed_started_at + 2.9)
        assert _ELAPSED_LABEL.search(hidden) is None
        shown = ch._live_hint_heartbeat_payload(buf, now=buf.elapsed_started_at + 3.0)
        assert shown.startswith("AI thinking 3.0s ")
        assert "10." not in shown
