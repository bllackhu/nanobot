"""Idle new-session reminder after a long gap since the last assistant reply."""

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import INBOUND_META_HISTORY_ONLY, InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import DEFAULT_IDLE_NEW_SESSION_HINT_MESSAGE
from nanobot.providers.base import LLMResponse
from nanobot.session.webui_turns import WebuiTurnCoordinator

HINT = "It's been a while. Start a new session if you want a clean context."


def _make_full_loop(tmp_path: Path, **kwargs) -> AgentLoop:
    kwargs.setdefault("idle_new_session_hint_message", HINT)
    # The schema default is 0 (disabled); these tests exercise the enabled path.
    kwargs.setdefault("idle_new_session_hint_after_hours", 8)
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = SimpleNamespace(max_tokens=4096)
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="fresh reply"))
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        **kwargs,
    )
    WebuiTurnCoordinator(
        bus=loop.bus,
        sessions=loop.sessions,
        schedule_background=lambda coro: loop._schedule_background(coro),
    ).subscribe(loop.runtime_events)
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=False)  # type: ignore[method-assign]
    return loop


def _seed_old_assistant(loop: AgentLoop, *, hours: float = 9) -> None:
    session = loop.sessions.get_or_create("feishu:oc_group")
    old = (datetime.now() - timedelta(hours=hours)).isoformat()
    session.add_message("user", "old question")
    session.messages[-1]["timestamp"] = old
    session.add_message("assistant", "old answer")
    session.messages[-1]["timestamp"] = old
    loop.sessions.save(session)


def _inbound(
    *,
    content: str = "hello again",
    history_only: bool = False,
    metadata: dict | None = None,
) -> InboundMessage:
    meta = dict(metadata or {})
    if history_only:
        meta[INBOUND_META_HISTORY_ONLY] = True
    return InboundMessage(
        channel="feishu",
        sender_id="ou_alice",
        chat_id="oc_group",
        content=content,
        metadata=meta,
    )


async def _capture_outbound(loop: AgentLoop) -> list:
    outbound: list = []
    original = loop.bus.publish_outbound

    async def capture(msg):
        outbound.append(msg)
        await original(msg)

    loop.bus.publish_outbound = capture  # type: ignore[method-assign]
    return outbound


def _install_turn_stub(loop: AgentLoop) -> AsyncMock:
    async def fake_process(msg, **kwargs):
        session = loop.sessions.get_or_create(msg.session_key)
        session.add_message("assistant", "fresh reply")
        loop.sessions.save(session)
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content="fresh reply",
        )

    stub = AsyncMock(side_effect=fake_process)
    loop._process_message = stub  # type: ignore[method-assign]
    return stub


@pytest.mark.asyncio
async def test_idle_hint_sent_once_per_gap_then_turn_runs(tmp_path: Path) -> None:
    loop = _make_full_loop(tmp_path)
    _seed_old_assistant(loop)
    stub = _install_turn_stub(loop)
    outbound = await _capture_outbound(loop)

    await loop._dispatch(_inbound(metadata={"thread_id": "omt_topic"}))

    stub.assert_awaited_once()
    texts = [getattr(m, "content", None) for m in outbound]
    assert HINT in texts
    assert "fresh reply" in texts
    hint_msg = next(m for m in outbound if getattr(m, "content", None) == HINT)
    assert getattr(hint_msg, "metadata", {}).get("thread_id") == "omt_topic"
    session = loop.sessions.get_or_create("feishu:oc_group")
    assert HINT not in [m.get("content") for m in session.messages]

    outbound.clear()
    await loop._dispatch(_inbound(content="follow up"))

    assert HINT not in [getattr(m, "content", None) for m in outbound]
    assert stub.await_count == 2


@pytest.mark.asyncio
async def test_idle_hint_skips_empty_session(tmp_path: Path) -> None:
    loop = _make_full_loop(tmp_path)
    stub = _install_turn_stub(loop)
    outbound = await _capture_outbound(loop)

    await loop._dispatch(_inbound())

    stub.assert_awaited_once()
    assert HINT not in [getattr(m, "content", None) for m in outbound]


@pytest.mark.asyncio
async def test_idle_hint_skips_when_hours_zero(tmp_path: Path) -> None:
    loop = _make_full_loop(tmp_path, idle_new_session_hint_after_hours=0)
    _seed_old_assistant(loop)
    stub = _install_turn_stub(loop)
    outbound = await _capture_outbound(loop)

    await loop._dispatch(_inbound())

    stub.assert_awaited_once()
    assert HINT not in [getattr(m, "content", None) for m in outbound]


@pytest.mark.asyncio
async def test_idle_hint_skips_when_message_empty(tmp_path: Path) -> None:
    loop = _make_full_loop(tmp_path, idle_new_session_hint_message="")
    _seed_old_assistant(loop)
    stub = _install_turn_stub(loop)
    outbound = await _capture_outbound(loop)

    await loop._dispatch(_inbound())

    stub.assert_awaited_once()
    assert not any((getattr(m, "content", None) or "").strip() == HINT for m in outbound)


@pytest.mark.asyncio
async def test_idle_hint_skips_history_only(tmp_path: Path) -> None:
    loop = _make_full_loop(tmp_path)
    _seed_old_assistant(loop)
    stub = _install_turn_stub(loop)
    outbound = await _capture_outbound(loop)

    await loop._dispatch(_inbound(content="group chatter", history_only=True))

    stub.assert_not_awaited()
    assert outbound == []


@pytest.mark.asyncio
async def test_idle_hint_skips_new_command(tmp_path: Path) -> None:
    loop = _make_full_loop(tmp_path)
    _seed_old_assistant(loop)
    outbound = await _capture_outbound(loop)

    await loop._dispatch(_inbound(content="/new"))

    assert HINT not in [getattr(m, "content", None) for m in outbound]
    assert any(getattr(m, "content", None) == "New session started." for m in outbound)


@pytest.mark.asyncio
async def test_idle_hint_skips_new_phrase(tmp_path: Path) -> None:
    loop = _make_full_loop(tmp_path)
    _seed_old_assistant(loop)
    outbound = await _capture_outbound(loop)

    await loop._dispatch(_inbound(content="新对话"))

    assert HINT not in [getattr(m, "content", None) for m in outbound]
    assert any(getattr(m, "content", None) == "New session started." for m in outbound)


@pytest.mark.asyncio
async def test_idle_hint_skips_command_assistant_timestamp(tmp_path: Path) -> None:
    loop = _make_full_loop(tmp_path)
    session = loop.sessions.get_or_create("feishu:oc_group")
    old = (datetime.now() - timedelta(hours=9)).isoformat()
    session.add_message("assistant", "status output", _command=True)
    session.messages[-1]["timestamp"] = old
    loop.sessions.save(session)
    stub = _install_turn_stub(loop)
    outbound = await _capture_outbound(loop)

    await loop._dispatch(_inbound())

    stub.assert_awaited_once()
    assert HINT not in [getattr(m, "content", None) for m in outbound]


def test_default_idle_hint_copy() -> None:
    assert "long-term memory" in DEFAULT_IDLE_NEW_SESSION_HINT_MESSAGE
    assert "/new" in DEFAULT_IDLE_NEW_SESSION_HINT_MESSAGE
