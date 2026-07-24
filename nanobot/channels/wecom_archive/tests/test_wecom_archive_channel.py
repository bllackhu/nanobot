"""Tests for WeCom archive inject channel."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from nanobot.bus.events import INBOUND_META_HISTORY_ONLY
from nanobot.bus.queue import MessageBus
from nanobot.channels.wecom_archive.runtime import WecomArchiveChannel, WecomArchiveConfig


@pytest.mark.asyncio
async def test_ingest_batch_history_only() -> None:
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
                    "chatId": "dm:alice:bob",
                    "senderId": "alice",
                    "msgtype": "text",
                    "content": "[archive] hi",
                    "msgtime": 1,
                }
            ],
        }
    )
    assert count == 1
    bus.publish_inbound.assert_awaited_once()
    msg = bus.publish_inbound.await_args.args[0]
    assert msg.channel == "wecom_archive"
    assert msg.chat_id == "dm:alice:bob"
    assert msg.metadata.get(INBOUND_META_HISTORY_ONLY) is True
    assert "hi" in msg.content
    assert msg.media == []


@pytest.mark.asyncio
async def test_ingest_image_downloads_media(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bus = MessageBus()
    bus.publish_inbound = AsyncMock()
    channel = WecomArchiveChannel(
        WecomArchiveConfig(
            enabled=True,
            allow_from=["*"],
            hub_base_url="https://hub.example.com",
            device_id="box-1",
            device_secret="sec",
            download_media=True,
        ),
        bus,
    )
    monkeypatch.setattr(
        "nanobot.channels.wecom_archive.runtime.get_media_dir",
        lambda _ch: tmp_path,
    )

    def _fake_fetch(
        hub: str, device_id: str, secret: str, sdk_file_id: str, file_name: str
    ) -> bytes:
        assert hub == "https://hub.example.com"
        assert device_id == "box-1"
        assert secret == "sec"
        assert sdk_file_id == "SDKFILEID123"
        return b"\xff\xd8\xfffakejpeg"

    with patch.object(channel, "_fetch_media_bytes_sync", side_effect=_fake_fetch):
        count = await channel.ingest_batch(
            {
                "corpId": "ww1",
                "messages": [
                    {
                        "msgid": "m-img",
                        "seq": 2,
                        "chatId": "room:r1",
                        "senderId": "alice",
                        "msgtype": "image",
                        "mediaKind": "image",
                        "sdkFileId": "SDKFILEID123",
                        "fileName": "photo.jpg",
                        "content": "[archive] from=alice msgtype=image\n[image]",
                        "msgtime": 2,
                    }
                ],
            }
        )

    assert count == 1
    msg = bus.publish_inbound.await_args.args[0]
    assert msg.metadata.get(INBOUND_META_HISTORY_ONLY) is True
    assert len(msg.media) == 1
    saved = Path(msg.media[0])
    assert saved.exists()
    assert saved.read_bytes().startswith(b"\xff\xd8")
    assert "photo.jpg" in msg.content or str(saved) in msg.content


@pytest.mark.asyncio
async def test_send_is_noop() -> None:
    bus = MessageBus()
    channel = WecomArchiveChannel(WecomArchiveConfig(enabled=True), bus)
    from nanobot.bus.events import OutboundMessage

    await channel.send(OutboundMessage(channel="wecom_archive", chat_id="x", content="y"))
