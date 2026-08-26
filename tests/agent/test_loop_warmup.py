"""Tests for gateway first-turn warmup in AgentLoop."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.loop import AgentLoop, TurnContext, TurnState
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.utils.llm_runtime import LLMRuntime


def _make_loop(tmp_path: Path) -> AgentLoop:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = SimpleNamespace(max_tokens=4096)
    provider._ensure_client = AsyncMock()
    return AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
    )


@pytest.mark.asyncio
async def test_warmup_idle_path_calls_all_targets(tmp_path: Path, monkeypatch) -> None:
    loop = _make_loop(tmp_path)
    tiktoken_called = False

    def fake_encoding() -> MagicMock:
        nonlocal tiktoken_called
        tiktoken_called = True
        return MagicMock()

    monkeypatch.setattr("nanobot.utils.helpers._get_token_encoding", fake_encoding)
    get_defs = MagicMock(return_value=[{"name": "exec"}])
    loop.tools.get_definitions = get_defs
    build_prompt = MagicMock(return_value="system prompt")
    loop.context.build_system_prompt = build_prompt

    await loop._warmup_idle_path()

    assert tiktoken_called
    get_defs.assert_called_once()
    loop.provider._ensure_client.assert_awaited_once()
    build_prompt.assert_called_once()


@pytest.mark.asyncio
async def test_warmup_step_failure_is_swallowed(tmp_path: Path, monkeypatch) -> None:
    loop = _make_loop(tmp_path)

    def boom() -> None:
        raise ValueError("tiktoken fail")

    monkeypatch.setattr("nanobot.utils.helpers._get_token_encoding", boom)
    get_defs = MagicMock(return_value=[])
    loop.tools.get_definitions = get_defs
    loop.provider._ensure_client = AsyncMock()
    loop.context.build_system_prompt = MagicMock(return_value="system")

    await loop._warmup_idle_path()

    get_defs.assert_called_once()
    loop.provider._ensure_client.assert_awaited_once()
    loop.context.build_system_prompt.assert_called_once()


@pytest.mark.asyncio
async def test_run_schedules_warmup_after_mcp_connect(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    loop._connect_mcp = AsyncMock()
    loop.close_mcp = AsyncMock()
    warmup_started = asyncio.Event()

    async def track_warmup() -> None:
        warmup_started.set()

    loop._warmup_idle_path = track_warmup  # type: ignore[method-assign]

    async def stop_soon() -> None:
        await warmup_started.wait()
        loop.stop()

    stopper = asyncio.create_task(stop_soon())
    await loop.run()
    await stopper

    loop._connect_mcp.assert_awaited_once()
    assert warmup_started.is_set()


@pytest.mark.asyncio
async def test_failed_warmup_does_not_block_inbound_dispatch(tmp_path: Path, monkeypatch) -> None:
    loop = _make_loop(tmp_path)
    loop._connect_mcp = AsyncMock()
    loop.close_mcp = AsyncMock()

    def boom() -> None:
        raise RuntimeError("warmup failed")

    monkeypatch.setattr("nanobot.utils.helpers._get_token_encoding", boom)
    loop.tools.get_definitions = MagicMock(return_value=[])
    loop.provider._ensure_client = AsyncMock()
    loop.context.build_system_prompt = MagicMock(return_value="system")

    dispatched = asyncio.Event()

    async def fake_dispatch(msg: InboundMessage) -> None:
        dispatched.set()

    loop._dispatch = fake_dispatch  # type: ignore[method-assign]

    await loop.bus.publish_inbound(
        InboundMessage(
            channel="feishu",
            sender_id="user-1",
            chat_id="chat-1",
            content="hello",
        )
    )

    async def stop_after_dispatch() -> None:
        await asyncio.wait_for(dispatched.wait(), timeout=2.0)
        loop.stop()

    stopper = asyncio.create_task(stop_after_dispatch())
    await loop.run()
    await stopper

    assert dispatched.is_set()
    if loop._warmup_task is not None:
        await loop._warmup_task


@pytest.mark.asyncio
async def test_state_build_waits_for_in_flight_warmup(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    warmup_release = asyncio.Event()
    warmup_started = asyncio.Event()

    async def slow_warmup() -> None:
        warmup_started.set()
        await warmup_release.wait()

    loop._warmup_task = asyncio.create_task(slow_warmup())
    await warmup_started.wait()

    runtime = loop.llm_runtime()
    ctx = TurnContext(
        msg=InboundMessage(
            channel="feishu",
            sender_id="user-1",
            chat_id="chat-1",
            content="hello",
        ),
        session=None,
        session_key="feishu:chat-1",
        state=TurnState.BUILD,
        turn_id="test-turn",
        runtime=runtime,
        ephemeral=True,
    )
    ctx.session = loop.sessions.get_or_create(ctx.session_key)

    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock()
    loop._history_for_turn = MagicMock(return_value=[])
    loop._runtime_events().record_turn_runtime = MagicMock()
    loop._request_context_for_turn = MagicMock(return_value=MagicMock())
    loop._resolve_runtime_context_for_turn = AsyncMock(return_value=[])
    loop._build_initial_messages = MagicMock(return_value=[{"role": "user", "content": "hello"}])
    loop._persist_user_message_early = MagicMock(return_value=False)
    loop._build_bus_progress_callback = AsyncMock(return_value=None)
    loop._build_retry_wait_callback = AsyncMock(return_value=None)

    build_task = asyncio.create_task(loop._state_build(ctx))
    await asyncio.sleep(0.05)
    assert not build_task.done()

    warmup_release.set()
    await asyncio.wait_for(build_task, timeout=2.0)
