"""Tests for wecom_archive content cleaning and context merge/dedupe."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from loguru import logger

from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import INBOUND_META_HISTORY_ONLY, InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.agent.autocompact import AutoCompact
from nanobot.channels.wecom_archive.merge import (
    archive_body_only,
    merge_wecom_group_history,
    normalize_wecom_mention_text,
    should_merge_wecom_archive,
    strip_archive_header,
    window_archive_messages,
)
from nanobot.channels.wecom_archive.runtime import WecomArchiveChannel, WecomArchiveConfig
from nanobot.providers.base import LLMResponse
from nanobot.session.webui_turns import WebuiTurnCoordinator


@pytest.fixture
def capture_debug_logs():
    """Capture loguru DEBUG lines for greppable merge prefixes."""
    lines: list[str] = []

    def _sink(message) -> None:
        lines.append(message.record["message"])

    sink_id = logger.add(_sink, level="DEBUG")
    try:
        yield lines
    finally:
        logger.remove(sink_id)


def test_strip_archive_header_and_body_only() -> None:
    raw = (
        "[archive] from=mytheresa msgtype=text msgid=4630023934959697540_1784973297163\n"
        "@ 在不"
    )
    body, meta = strip_archive_header(raw)
    assert body == "@ 在不"
    assert meta["from"] == "mytheresa"
    assert meta["msgid"] == "4630023934959697540_1784973297163"
    assert archive_body_only(raw) == "@ 在不"
    assert archive_body_only("@客服小壹 在不") == "@客服小壹 在不"


def test_normalize_mention_text() -> None:
    live = normalize_wecom_mention_text("@客服小壹 在不", ["客服小壹"])
    archive = normalize_wecom_mention_text("@ 在不", ["客服小壹"])
    assert live == archive == "@ 在不"


def test_merge_dedupes_mention_twins(capture_debug_logs: list[str]) -> None:
    t0 = datetime(2026, 7, 25, 17, 54, 54, tzinfo=timezone.utc)
    live = [
        {
            "role": "user",
            "content": "@客服小壹 在不",
            "timestamp": t0.isoformat(),
        },
        {
            "role": "assistant",
            "content": "在的",
            "timestamp": (t0 + timedelta(seconds=2)).isoformat(),
        },
    ]
    archive = [
        {
            "role": "user",
            "content": "@ 在不",
            "timestamp": (t0 + timedelta(seconds=18)).isoformat(),
            "from": "mytheresa",
            "msgid": "4630_1",
        },
        {
            "role": "user",
            "content": "闲聊一句",
            "timestamp": (t0 - timedelta(seconds=30)).isoformat(),
            "from": "alice",
            "msgid": "m2",
        },
    ]
    merged = merge_wecom_group_history(
        live,
        archive,
        bot_mention_names=["客服小壹"],
    )
    contents = [m["content"] for m in merged]
    assert contents.count("@客服小壹 在不") == 1
    assert "alice: 闲聊一句" in contents
    assert "在的" in contents
    assert not any("msgid" in str(c) for c in contents)
    assert not any(c.startswith("mytheresa: @ 在不") for c in contents)
    assert any(
        "wecom_archive merge dedupe" in line and "dropped_twin=" in line
        for line in capture_debug_logs
    )


def test_merge_skips_bot_user_ids() -> None:
    t0 = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)
    merged = merge_wecom_group_history(
        [],
        [
            {
                "role": "user",
                "content": "bot noise",
                "timestamp": t0.isoformat(),
                "from": "bot_svc",
            }
        ],
        bot_user_ids=["bot_svc"],
    )
    assert merged == []


def test_should_merge_wecom_archive_dm_policy() -> None:
    assert should_merge_wecom_archive("wecom", "wrROOM", {"chat_type": "group"})
    assert not should_merge_wecom_archive("wecom", "alice", {"chat_type": "single"})
    assert not should_merge_wecom_archive("wecom", "dm:a:b", {})
    assert not should_merge_wecom_archive("feishu", "oc_x", {})


@pytest.mark.asyncio
async def test_ingest_batch_persists_clean_content_and_session_extra() -> None:
    bus = MessageBus()
    bus.publish_inbound = AsyncMock()
    channel = WecomArchiveChannel(WecomArchiveConfig(enabled=True, allow_from=["*"]), bus)
    count = await channel.ingest_batch(
        {
            "corpId": "ww1",
            "messages": [
                {
                    "msgid": "m1",
                    "seq": 1,
                    "chatId": "room:wrROOM",
                    "senderId": "mytheresa",
                    "msgtype": "text",
                    "content": (
                        "[archive] from=mytheresa msgtype=text msgid=m1\n@ 在不"
                    ),
                    "msgtime": 1784973297163,
                }
            ],
        }
    )
    assert count == 1
    msg = bus.publish_inbound.await_args.args[0]
    assert msg.content == "@ 在不"
    assert "[archive]" not in msg.content
    extra = msg.metadata.get("_session_message_extra") or {}
    assert extra.get("msgid") == "m1"
    assert extra.get("from") == "mytheresa"
    assert extra.get("msgtype") == "text"
    assert extra.get("timestamp")
    assert msg.metadata.get(INBOUND_META_HISTORY_ONLY) is True


def _make_full_loop(tmp_path: Path) -> AgentLoop:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = SimpleNamespace(max_tokens=4096)
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="ok"))
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")
    WebuiTurnCoordinator(
        bus=loop.bus,
        sessions=loop.sessions,
        schedule_background=lambda coro: loop._schedule_background(coro),
    ).subscribe(loop.runtime_events)
    return loop


@pytest.mark.asyncio
async def test_history_only_never_sets_pending_user_turn(tmp_path: Path) -> None:
    loop = _make_full_loop(tmp_path)
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=False)  # type: ignore[method-assign]

    await loop._dispatch(
        InboundMessage(
            channel="wecom_archive",
            sender_id="alice",
            chat_id="room:wrROOM",
            content="@ 在不",
            metadata={
                INBOUND_META_HISTORY_ONLY: True,
                "_session_message_extra": {
                    "msgid": "m1",
                    "from": "alice",
                    "msgtype": "text",
                },
            },
        )
    )
    session = loop.sessions.get_or_create("wecom_archive:room:wrROOM")
    assert session.metadata.get(AgentLoop._PENDING_USER_TURN_KEY) is None
    assert session.messages[0]["content"] == "@ 在不"
    assert session.messages[0].get("msgid") == "m1"
    assert session.messages[0].get("from") == "alice"


@pytest.mark.asyncio
async def test_history_for_turn_merges_archive_room(
    tmp_path: Path, capture_debug_logs: list[str]
) -> None:
    loop = _make_full_loop(tmp_path)
    loop.channels_config = SimpleNamespace(
        wecom_archive={
            "enabled": True,
            "mergeIntoWecom": True,
            "botMentionNames": ["客服小壹"],
        },
        model_extra={},
    )
    room = "wrROOM"
    t0 = datetime(2026, 7, 25, 17, 54, 54, tzinfo=timezone.utc)
    live = loop.sessions.get_or_create(f"wecom:{room}")
    live.add_message(
        "user",
        "@客服小壹 在不",
        timestamp=t0.isoformat(),
    )
    live.add_message(
        "assistant",
        "在的",
        timestamp=(t0 + timedelta(seconds=2)).isoformat(),
    )
    loop.sessions.save(live)

    archive = loop.sessions.get_or_create(f"wecom_archive:room:{room}")
    archive.add_message(
        "user",
        "先说一句",
        timestamp=(t0 - timedelta(minutes=1)).isoformat(),
        **{"from": "alice", "msgid": "a0"},
    )
    archive.add_message(
        "user",
        "@ 在不",
        timestamp=(t0 + timedelta(seconds=18)).isoformat(),
        **{"from": "mytheresa", "msgid": "a1"},
    )
    loop.sessions.save(archive)

    ctx = SimpleNamespace(
        msg=InboundMessage(
            channel="wecom",
            sender_id="mytheresa",
            chat_id=room,
            content="@客服小壹 hello",
            metadata={"chat_type": "group"},
        ),
        session=live,
        session_key=live.key,
    )
    history = loop._history_for_turn(ctx, max_messages=50, max_tokens=0)
    contents = [m.get("content") for m in history]
    assert any("alice: 先说一句" == c for c in contents)
    assert contents.count("@客服小壹 在不") == 1
    assert "在的" in contents
    assert any("wecom_archive merge ok" in line for line in capture_debug_logs)


@pytest.mark.asyncio
async def test_history_for_turn_logs_archive_missing(
    tmp_path: Path, capture_debug_logs: list[str]
) -> None:
    loop = _make_full_loop(tmp_path)
    loop.channels_config = SimpleNamespace(
        wecom_archive={"enabled": True},
        model_extra={},
    )
    room = "wrMISSING"
    live = loop.sessions.get_or_create(f"wecom:{room}")
    live.add_message("user", "hi")
    loop.sessions.save(live)
    ctx = SimpleNamespace(
        msg=InboundMessage(
            channel="wecom",
            sender_id="alice",
            chat_id=room,
            content="@bot hi",
            metadata={"chat_type": "group"},
        ),
        session=live,
        session_key=live.key,
    )
    history = loop._history_for_turn(ctx, max_messages=50, max_tokens=0)
    assert len(history) >= 1
    assert any(
        "wecom_archive merge skip reason=archive_missing" in line
        for line in capture_debug_logs
    )


def test_window_archive_messages_max_count_and_age() -> None:
    now = datetime(2026, 7, 26, 12, 0, 0, tzinfo=timezone.utc)
    msgs = []
    for i in range(10):
        msgs.append(
            {
                "role": "user",
                "content": f"m{i}",
                "timestamp": (now - timedelta(hours=100 - i)).isoformat(),
            }
        )
    # Last 3 by count among age-filtered (72h keeps roughly last 3 of the 10 if spaced)
    recent = [
        {
            "role": "user",
            "content": "new",
            "timestamp": (now - timedelta(hours=1)).isoformat(),
        },
        {
            "role": "user",
            "content": "newer",
            "timestamp": (now - timedelta(minutes=30)).isoformat(),
        },
        {
            "role": "user",
            "content": "newest",
            "timestamp": now.isoformat(),
        },
    ]
    aged_out = [
        {
            "role": "user",
            "content": "old",
            "timestamp": (now - timedelta(hours=80)).isoformat(),
        }
    ]
    windowed = window_archive_messages(
        aged_out + recent,
        max_messages=2,
        max_age_hours=72,
        now=now,
    )
    assert [m["content"] for m in windowed] == ["newer", "newest"]


@pytest.mark.asyncio
async def test_history_for_turn_applies_merge_max_messages(tmp_path: Path) -> None:
    loop = _make_full_loop(tmp_path)
    loop.channels_config = SimpleNamespace(
        wecom_archive={
            "enabled": True,
            "mergeIntoWecom": True,
            "mergeMaxMessages": 2,
            "mergeMaxAgeHours": 0,  # disable age filter
        },
        model_extra={},
    )
    room = "wrWIN"
    live = loop.sessions.get_or_create(f"wecom:{room}")
    live.add_message("user", "@客服小壹 hi")
    loop.sessions.save(live)
    archive = loop.sessions.get_or_create(f"wecom_archive:room:{room}")
    for i in range(5):
        archive.add_message(
            "user",
            f"room chatter {i}",
            timestamp=datetime(2026, 7, 26, 1, i, tzinfo=timezone.utc).isoformat(),
            **{"from": "alice"},
        )
    loop.sessions.save(archive)
    ctx = SimpleNamespace(
        msg=InboundMessage(
            channel="wecom",
            sender_id="alice",
            chat_id=room,
            content="@客服小壹 list",
            metadata={"chat_type": "group"},
        ),
        session=live,
        session_key=live.key,
    )
    history = loop._history_for_turn(ctx, max_messages=50, max_tokens=0)
    archive_lines = [c for c in (m.get("content") for m in history) if isinstance(c, str) and "room chatter" in c]
    assert len(archive_lines) == 2
    assert "alice: room chatter 3" in archive_lines
    assert "alice: room chatter 4" in archive_lines


@pytest.mark.asyncio
async def test_ingest_history_only_trims_archive_file_cap(tmp_path: Path) -> None:
    loop = _make_full_loop(tmp_path)
    loop.channels_config = SimpleNamespace(
        wecom_archive={"archiveFileMaxMessages": 5},
        model_extra={},
    )
    key = "wecom_archive:room:wrTRIM"
    session = loop.sessions.get_or_create(key)
    for i in range(4):
        session.add_message("user", f"old {i}")
    loop.sessions.save(session)

    await loop._dispatch(
        InboundMessage(
            channel="wecom_archive",
            sender_id="alice",
            chat_id="room:wrTRIM",
            content="new",
            metadata={
                INBOUND_META_HISTORY_ONLY: True,
                "_session_message_extra": {"from": "alice", "msgid": "m-new"},
            },
        )
    )
    session = loop.sessions.get_or_create(key)
    # 4 old + 1 new = 5 at cap; one more ingest should trim
    await loop._dispatch(
        InboundMessage(
            channel="wecom_archive",
            sender_id="alice",
            chat_id="room:wrTRIM",
            content="newest",
            metadata={
                INBOUND_META_HISTORY_ONLY: True,
                "_session_message_extra": {"from": "alice", "msgid": "m-newest"},
            },
        )
    )
    session = loop.sessions.get_or_create(key)
    assert len(session.messages) <= 5
    assert session.messages[-1]["content"] == "newest"


def test_autocompact_skips_wecom_archive_sessions() -> None:
    assert AutoCompact._is_internal_session("wecom_archive:room:wrX")
    assert AutoCompact._is_internal_session("dream:abc")
    assert not AutoCompact._is_internal_session("wecom:wrX")
