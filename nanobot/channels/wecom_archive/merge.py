"""Merge wecom_archive room history into live wecom group context."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from loguru import logger

_ARCHIVE_HEADER_RE = re.compile(
    r"^\[archive\](?:\s+from=(?P<from>\S+))?(?:\s+msgtype=(?P<msgtype>\S+))?"
    r"(?:\s+msgid=(?P<msgid>\S+))?\s*\n?",
    re.IGNORECASE,
)
_AT_COLLAPSE_RE = re.compile(r"@\s+")
_DEFAULT_WINDOW_SECONDS = 120
_DEFAULT_MERGE_MAX_MESSAGES = 500
_DEFAULT_MERGE_MAX_AGE_HOURS = 72.0


def strip_archive_header(content: str) -> tuple[str, dict[str, str]]:
    """Split legacy hub header from body. Returns (body, parsed_fields)."""
    text = content if isinstance(content, str) else str(content or "")
    match = _ARCHIVE_HEADER_RE.match(text)
    if not match:
        return text, {}
    meta = {k: v for k, v in match.groupdict().items() if v}
    return text[match.end() :], meta


def archive_body_only(content: str) -> str:
    """Return message body with any `[archive] …` header removed."""
    body, _ = strip_archive_header(content)
    return body


def normalize_wecom_mention_text(
    content: str,
    bot_mention_names: Sequence[str] | None = None,
) -> str:
    """Normalize user text for live/archive twin comparison."""
    text = archive_body_only(content).strip()
    names = [n.strip() for n in (bot_mention_names or ()) if isinstance(n, str) and n.strip()]
    for name in names:
        text = re.sub(rf"@{re.escape(name)}\s*", "@ ", text)
    text = _AT_COLLAPSE_RE.sub("@ ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _parse_timestamp(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # Heuristic: WeCom msgtime is ms; session iso timestamps are not numeric here.
        ts = float(value)
        if ts > 1e12:
            return ts / 1000.0
        if ts > 1e10:
            return ts / 1000.0
        return ts
    if isinstance(value, str) and value.strip():
        raw = value.strip()
        try:
            return datetime.fromisoformat(raw).timestamp()
        except ValueError:
            return None
    return None


def _message_sort_key(msg: Mapping[str, Any], index: int) -> tuple[float, int]:
    ts = _parse_timestamp(msg.get("timestamp"))
    if ts is None:
        ts = _parse_timestamp(msg.get("msgtime"))
    if ts is None:
        ts = float(index)
    return ts, index


def window_archive_messages(
    messages: Sequence[Mapping[str, Any]],
    *,
    max_messages: int = _DEFAULT_MERGE_MAX_MESSAGES,
    max_age_hours: float | None = _DEFAULT_MERGE_MAX_AGE_HOURS,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Keep a recent archive window for merge (count + optional max age).

    Order is preserved. Messages without a parseable timestamp are kept when
    applying the age filter (they still count toward ``max_messages`` from the
    tail).
    """
    rows = [dict(m) for m in messages]
    if max_age_hours is not None and max_age_hours > 0:
        now_ts = (now or datetime.now(timezone.utc)).timestamp()
        cutoff = now_ts - float(max_age_hours) * 3600.0
        aged: list[dict[str, Any]] = []
        for msg in rows:
            ts = _parse_timestamp(msg.get("timestamp"))
            if ts is None:
                ts = _parse_timestamp(msg.get("msgtime"))
            if ts is None or ts >= cutoff:
                aged.append(msg)
        rows = aged
    if max_messages > 0 and len(rows) > max_messages:
        rows = rows[-max_messages:]
    return rows


def format_archive_for_llm(msg: Mapping[str, Any]) -> dict[str, Any]:
    """Build an LLM history row from an archive session message."""
    body, header_meta = strip_archive_header(str(msg.get("content") or ""))
    sender = (
        str(msg.get("from") or header_meta.get("from") or "").strip()
        or str(msg.get("sender_id") or "").strip()
    )
    content = f"{sender}: {body}" if sender and body else (body or sender)
    entry: dict[str, Any] = {
        "role": msg.get("role") or "user",
        "content": content,
        "timestamp": msg.get("timestamp"),
    }
    if msg.get("media"):
        entry["media"] = msg["media"]
    return entry


def merge_wecom_group_history(
    live_msgs: Sequence[Mapping[str, Any]],
    archive_msgs: Sequence[Mapping[str, Any]],
    *,
    bot_user_ids: Sequence[str] | None = None,
    bot_mention_names: Sequence[str] | None = None,
    window_seconds: int = _DEFAULT_WINDOW_SECONDS,
) -> list[dict[str, Any]]:
    """Time-sort live + archive messages; drop archive twins / bot senders.

    Returns session-shaped message dicts (with archive content rewritten for LLM).
    Live rows are kept as-is (including assistant turns). Archive twins of live
    user text within ``window_seconds`` are dropped.
    """
    bot_ids = {str(x).strip() for x in (bot_user_ids or ()) if str(x).strip()}
    names = list(bot_mention_names or ())

    live_users: list[tuple[float, str]] = []
    for msg in live_msgs:
        if msg.get("role") != "user":
            continue
        ts = _parse_timestamp(msg.get("timestamp"))
        if ts is None:
            continue
        live_users.append((ts, normalize_wecom_mention_text(str(msg.get("content") or ""), names)))

    merged: list[tuple[tuple[float, int], dict[str, Any], str]] = []
    for i, msg in enumerate(live_msgs):
        merged.append((_message_sort_key(msg, i), dict(msg), "live"))

    kept_archive = 0
    dropped_twin = 0
    dropped_bot = 0
    archive_offset = len(live_msgs)
    for i, msg in enumerate(archive_msgs):
        if msg.get("role") != "user":
            continue
        body, header_meta = strip_archive_header(str(msg.get("content") or ""))
        sender = str(msg.get("from") or header_meta.get("from") or "").strip()
        if sender and sender in bot_ids:
            dropped_bot += 1
            continue
        norm = normalize_wecom_mention_text(body, names)
        ts = _parse_timestamp(msg.get("timestamp"))
        if ts is None:
            ts = _parse_timestamp(msg.get("msgtime"))
        if ts is not None and norm:
            twin = False
            for live_ts, live_norm in live_users:
                if live_norm == norm and abs(live_ts - ts) <= window_seconds:
                    twin = True
                    break
            if twin:
                dropped_twin += 1
                continue
        formatted = format_archive_for_llm(msg)
        # Preserve fields used by get_history (media rehydration).
        if "media" in msg and "media" not in formatted:
            formatted["media"] = msg["media"]
        merged.append((_message_sort_key(msg, archive_offset + i), formatted, "archive"))
        kept_archive += 1

    merged.sort(key=lambda item: item[0])
    logger.debug(
        "wecom_archive merge dedupe kept_archive={} dropped_twin={} dropped_bot={} "
        "live_in={} archive_in={} out={}",
        kept_archive,
        dropped_twin,
        dropped_bot,
        len(live_msgs),
        len(archive_msgs),
        len(merged),
    )
    return [item[1] for item in merged]


def archive_room_session_key(room_id: str) -> str:
    """Session key for a WeCom group archive transcript."""
    return f"wecom_archive:room:{room_id}"


def should_merge_wecom_archive(channel: str, chat_id: str, metadata: Mapping[str, Any] | None) -> bool:
    """True for wecom group turns that may have a sibling archive room session."""
    if channel != "wecom":
        return False
    chat_id = (chat_id or "").strip()
    if not chat_id:
        return False
    meta = metadata or {}
    chat_type = str(meta.get("chat_type") or meta.get("chattype") or "").strip().lower()
    if chat_type in ("single", "dm", "private"):
        return False
    # Archive DMs use dm:… keys; live DMs use userid chat_id — never merge those.
    if chat_id.startswith("dm:"):
        return False
    return True
