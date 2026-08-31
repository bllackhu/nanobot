"""Exact whole-message aliases for the /new slash command."""

from __future__ import annotations

from collections.abc import Sequence

from nanobot.command.router import normalize_command_text

DEFAULT_NEW_SESSION_PHRASES: tuple[str, ...] = (
    "新对话",
    "新会话",
    "新任务",
    "new",
    "new chat",
    "new session",
)

_TRAILING_PUNCTUATION = "。.!！？?"


def normalize_new_session_phrase(text: str) -> str:
    """Strip surrounding whitespace, one trailing punct char, then casefold."""
    stripped = text.strip()
    if stripped and stripped[-1] in _TRAILING_PUNCTUATION:
        stripped = stripped[:-1].rstrip()
    return stripped.casefold()


def is_new_session_phrase(text: str, phrases: Sequence[str]) -> bool:
    """True when *text* is an exact whole-message match against *phrases*.

    An empty *phrases* list disables matching. Slash ``/new`` is not a phrase;
    callers that also want the slash command should use
    ``is_new_session_trigger``.
    """
    if not phrases:
        return False
    needle = normalize_new_session_phrase(text)
    if not needle:
        return False
    return any(
        needle == normalize_new_session_phrase(phrase)
        for phrase in phrases
        if phrase and str(phrase).strip()
    )


def is_new_session_trigger(text: str, phrases: Sequence[str]) -> bool:
    """True when *text* is ``/new`` or an exact configured new-session phrase."""
    if normalize_command_text(text).lower() == "/new":
        return True
    return is_new_session_phrase(text, phrases)
