"""WeCom Chat Content Archive — inject-only channel (fleet mode).

Receives decrypted archive batches from device-hub via provisiond on a
loopback HTTP endpoint. Does not call WeWork Finance SDK or expose a
public WeCom callback URL.

Media (image/file/voice/video) is downloaded via hub GetMediaData so
HISTORY_ONLY messages still store local media paths for later multimodal
rehydration — same pattern as Feishu groupPolicy=listen.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any
from urllib import error, request

from pydantic import Field

from nanobot.bus.events import INBOUND_META_HISTORY_ONLY, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.paths import get_media_dir
from nanobot.config.schema import Base
from nanobot.utils.helpers import safe_filename

_DEFAULT_DEVICE_ID_PATH = Path("/etc/clawbot/device-id")
_DEFAULT_DEVICE_SECRET_PATH = Path("/etc/clawbot/device-secret")
_MEDIA_KINDS = frozenset({"image", "file", "voice", "video"})


class WecomArchiveConfig(Base):
    """Inject-only WeCom archive channel config."""

    enabled: bool = False
    allow_from: list[str] = Field(default_factory=lambda: ["*"])
    inject_host: str = "127.0.0.1"
    inject_port: int = 18791
    inject_path: str = "/internal/wecom_archive/batch"
    inject_token: str = ""
    # Hub GetMediaData proxy (device-authenticated). Empty → env/files.
    hub_base_url: str = ""
    device_id: str = ""
    device_secret: str = ""
    download_media: bool = True


class WecomArchiveChannel(BaseChannel):
    name = "wecom_archive"
    display_name = "WeCom Archive"

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return WecomArchiveConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = WecomArchiveConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: WecomArchiveConfig = config
        self._server: ThreadingHTTPServer | None = None
        self._thread: Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        self._running = True
        self._loop = asyncio.get_running_loop()
        channel = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
                return

            def do_POST(self) -> None:  # noqa: N802
                if self.path.split("?", 1)[0] != channel.config.inject_path:
                    self.send_response(404)
                    self.end_headers()
                    return
                token = channel.config.inject_token
                if token:
                    auth = self.headers.get("Authorization", "")
                    got = auth.removeprefix("Bearer ").strip()
                    if got != token and self.headers.get("X-Wecom-Archive-Token") != token:
                        self.send_response(401)
                        self.end_headers()
                        return
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length > 0 else b"{}"
                try:
                    payload = json.loads(body.decode("utf-8"))
                except Exception:
                    self.send_response(400)
                    self.end_headers()
                    return
                fut = asyncio.run_coroutine_threadsafe(
                    channel.ingest_batch(payload), channel._loop  # type: ignore[arg-type]
                )
                try:
                    # Media downloads via hub can exceed a few seconds per file.
                    count = fut.result(timeout=120)
                except Exception as exc:
                    channel.logger.exception("ingest failed: {}", exc)
                    self.send_response(500)
                    self.end_headers()
                    return
                data = json.dumps({"ok": True, "ingested": count}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._server = ThreadingHTTPServer(
            (self.config.inject_host, int(self.config.inject_port)),
            Handler,
        )
        self._thread = Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.logger.info(
            "WeCom archive inject listening on http://{}:{}{}",
            self.config.inject_host,
            self.config.inject_port,
            self.config.inject_path,
        )
        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        self._running = False
        if self._server is not None:
            self._server.shutdown()
            self._server = None
        self._thread = None

    async def send(self, msg: OutboundMessage) -> None:
        self.logger.debug("wecom_archive send ignored chat_id={}", msg.chat_id)

    def _resolve_hub_creds(self) -> tuple[str, str, str]:
        hub = (self.config.hub_base_url or "").strip()
        if not hub:
            hub = (
                os.environ.get("CLAWBOT_HUB_BASE_URL", "").strip()
                or os.environ.get("HUB_BASE_URL", "").strip()
            )
        device_id = (self.config.device_id or "").strip()
        if not device_id:
            device_id = os.environ.get("CLAWBOT_DEVICE_ID", "").strip()
            if not device_id and _DEFAULT_DEVICE_ID_PATH.is_file():
                try:
                    device_id = _DEFAULT_DEVICE_ID_PATH.read_text(encoding="utf-8").strip()
                except OSError:
                    device_id = ""
        secret = (self.config.device_secret or "").strip()
        if not secret:
            secret = os.environ.get("CLAWBOT_DEVICE_SECRET", "").strip()
            if not secret and _DEFAULT_DEVICE_SECRET_PATH.is_file():
                try:
                    secret = _DEFAULT_DEVICE_SECRET_PATH.read_text(encoding="utf-8").strip()
                except OSError:
                    secret = ""
        return hub.rstrip("/"), device_id, secret

    def _fetch_media_bytes_sync(
        self, hub: str, device_id: str, secret: str, sdk_file_id: str, file_name: str
    ) -> bytes:
        url = f"{hub}/api/device/v1/wecom/archive/media"
        payload = json.dumps(
            {
                "deviceId": device_id,
                "secret": secret,
                "sdkFileId": sdk_file_id,
                "fileName": file_name or "wecom-media.bin",
            }
        ).encode("utf-8")
        req = request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(req, timeout=90) as resp:  # noqa: S310 - hub URL from config/env
            return resp.read()

    async def _download_and_save_media(
        self, item: dict[str, Any]
    ) -> tuple[str | None, str | None]:
        """Download archive media via hub; return (path, content_suffix)."""
        sdk_file_id = str(item.get("sdkFileId") or item.get("sdk_file_id") or "").strip()
        media_kind = str(item.get("mediaKind") or item.get("msgtype") or "").strip().lower()
        if not sdk_file_id or media_kind not in _MEDIA_KINDS:
            return None, None
        if not self.config.download_media:
            return None, None

        hub, device_id, secret = self._resolve_hub_creds()
        if not hub or not device_id or not secret:
            self.logger.warning(
                "wecom_archive media skip: hub/device credentials missing (sdkFileId set)"
            )
            return None, None

        file_name = str(item.get("fileName") or item.get("filename") or "").strip()
        fallback = f"{media_kind}_{sdk_file_id[:16] or uuid.uuid4().hex}"
        if media_kind == "image" and not Path(file_name).suffix:
            fallback = f"{fallback}.jpg"
        elif media_kind == "voice" and not Path(file_name).suffix:
            fallback = f"{fallback}.amr"
        elif media_kind == "video" and not Path(file_name).suffix:
            fallback = f"{fallback}.mp4"
        name = safe_filename(Path(file_name).name if file_name else "") or safe_filename(fallback)
        if not name or name in (".", ".."):
            name = safe_filename(fallback) or uuid.uuid4().hex

        try:
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(
                None,
                self._fetch_media_bytes_sync,
                hub,
                device_id,
                secret,
                sdk_file_id,
                name,
            )
        except error.HTTPError as exc:
            self.logger.warning("wecom_archive media HTTP {}: {}", exc.code, exc.reason)
            return None, None
        except Exception as exc:
            self.logger.warning("wecom_archive media download failed: {}", exc)
            return None, None

        if not data:
            return None, None

        media_dir = get_media_dir("wecom_archive")
        media_dir.mkdir(parents=True, exist_ok=True)
        path = media_dir / name
        # Avoid clobber when same filename appears twice.
        if path.exists():
            path = media_dir / f"{path.stem}_{uuid.uuid4().hex[:8]}{path.suffix}"
        path.write_bytes(data)
        path_str = str(path)
        self.logger.debug("wecom_archive saved {} to {}", media_kind, path_str)
        return path_str, f"[{media_kind}: {path_str}]"

    async def ingest_batch(self, payload: dict[str, Any]) -> int:
        messages = payload.get("messages") or []
        count = 0
        for item in messages:
            if not isinstance(item, dict):
                continue
            sender_id = str(item.get("senderId") or item.get("from") or "unknown")
            chat_id = str(item.get("chatId") or "")
            if not chat_id:
                continue
            content = str(item.get("content") or "")
            media = item.get("media") or []
            if not isinstance(media, list):
                media = []
            media_paths = [str(m) for m in media if m]

            path_str, media_label = await self._download_and_save_media(item)
            if path_str:
                media_paths.append(path_str)
                media_kind = str(item.get("mediaKind") or item.get("msgtype") or "").lower()
                if media_kind == "voice":
                    transcription = await self.transcribe_audio(path_str)
                    if transcription:
                        content = f"{content}\n[transcription: {transcription}]"
                    elif media_label:
                        content = f"{content}\n{media_label}"
                elif media_label and media_label not in content:
                    content = f"{content}\n{media_label}"

            meta = {
                INBOUND_META_HISTORY_ONLY: True,
                "msgid": item.get("msgid") or item.get("msgId"),
                "msgtype": item.get("msgtype"),
                "msgtime": item.get("msgtime"),
                "corpId": payload.get("corpId"),
                "seq": item.get("seq"),
                "sdkFileId": item.get("sdkFileId") or item.get("sdk_file_id"),
                "mediaKind": item.get("mediaKind"),
            }
            await self._handle_message(
                sender_id=sender_id,
                chat_id=chat_id,
                content=content,
                media=media_paths,
                metadata=meta,
                is_dm=chat_id.startswith("dm:"),
            )
            count += 1
        return count
