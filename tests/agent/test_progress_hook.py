"""Tests for AgentProgressHook progress publishing."""

from __future__ import annotations

import pytest

from nanobot.agent.hook import AgentHookContext
from nanobot.agent.progress_hook import AgentProgressHook
from nanobot.providers.base import LLMResponse, ToolCallRequest


@pytest.mark.asyncio
async def test_before_execute_tools_does_not_publish_interim_thought() -> None:
    """Non-streaming turns must not emit assistant content as Progress before tools."""
    progress_calls: list[tuple[str, bool]] = []

    async def on_progress(content: str, *, tool_hint: bool = False, **kwargs) -> None:
        progress_calls.append((content, tool_hint))

    hook = AgentProgressHook(on_progress=on_progress, on_stream=None)
    ctx = AgentHookContext(iteration=0, messages=[])
    ctx.response = LLMResponse(
        content="Full intro menu text",
        tool_calls=[ToolCallRequest(id="c1", name="write_file", arguments={"path": "x"})],
    )
    ctx.tool_calls = list(ctx.response.tool_calls)

    await hook.before_execute_tools(ctx)

    assert all(content != "Full intro menu text" for content, _ in progress_calls)
    assert any(tool_hint for _, tool_hint in progress_calls)


@pytest.mark.asyncio
async def test_before_execute_tools_still_publishes_tool_hint_without_streaming() -> None:
    progress_calls: list[tuple[str, bool]] = []

    async def on_progress(content: str, *, tool_hint: bool = False, **kwargs) -> None:
        progress_calls.append((content, tool_hint))

    hook = AgentProgressHook(on_progress=on_progress, on_stream=None)
    ctx = AgentHookContext(iteration=0, messages=[])
    ctx.response = LLMResponse(
        content="",
        tool_calls=[ToolCallRequest(id="c1", name="read_file", arguments={"path": "."})],
    )
    ctx.tool_calls = list(ctx.response.tool_calls)

    await hook.before_execute_tools(ctx)

    assert progress_calls
    assert all(tool_hint for _, tool_hint in progress_calls)


@pytest.mark.asyncio
async def test_before_iteration_emits_thinking_hint() -> None:
    """before_iteration publishes the default 'AI thinking ...' status hint."""
    progress_calls: list[tuple[str, bool]] = []

    async def on_progress(content: str, *, tool_hint: bool = False, **kwargs) -> None:
        progress_calls.append((content, tool_hint))

    hook = AgentProgressHook(on_progress=on_progress, on_stream=None)
    ctx = AgentHookContext(iteration=0, messages=[])

    await hook.before_iteration(ctx)

    assert progress_calls == [("AI thinking ...", True)]


@pytest.mark.asyncio
async def test_before_iteration_uses_configured_thinking_hint() -> None:
    """A custom thinking hint replaces the default text."""
    progress_calls: list[tuple[str, bool]] = []

    async def on_progress(content: str, *, tool_hint: bool = False, **kwargs) -> None:
        progress_calls.append((content, tool_hint))

    hook = AgentProgressHook(
        on_progress=on_progress,
        on_stream=None,
        thinking_hint="Hmm, let me think...",
    )
    ctx = AgentHookContext(iteration=0, messages=[])

    await hook.before_iteration(ctx)

    assert progress_calls == [("Hmm, let me think...", True)]


@pytest.mark.asyncio
async def test_before_iteration_empty_thinking_hint_disables_emission() -> None:
    """An empty thinking hint suppresses the status event entirely."""
    progress_calls: list[tuple[str, bool]] = []

    async def on_progress(content: str, *, tool_hint: bool = False, **kwargs) -> None:
        progress_calls.append((content, tool_hint))

    hook = AgentProgressHook(on_progress=on_progress, on_stream=None, thinking_hint="")
    ctx = AgentHookContext(iteration=0, messages=[])

    await hook.before_iteration(ctx)

    assert progress_calls == []


@pytest.mark.asyncio
async def test_before_iteration_emits_thinking_hint_before_each_llm_request() -> None:
    """The thinking hint is emitted on both the first iteration and after tool calls."""
    progress_calls: list[tuple[str, bool]] = []

    async def on_progress(content: str, *, tool_hint: bool = False, **kwargs) -> None:
        progress_calls.append((content, tool_hint))

    hook = AgentProgressHook(on_progress=on_progress, on_stream=None)

    for iteration in (0, 1, 2):
        ctx = AgentHookContext(iteration=iteration, messages=[])
        await hook.before_iteration(ctx)

    assert progress_calls == [("AI thinking ...", True)] * 3
