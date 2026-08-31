"""Exact whole-message aliases for the /new slash command."""

from __future__ import annotations

from collections.abc import Sequence

from nanobot.command.router import normalize_command_text
from nanobot.config.schema import DEFAULT_NEW_SESSION_PHRASES

__all__ = [
    "DEFAULT_NEW_SESSION_PHRASES",
    "configured_new_session_phrases",
    "configured_new_session_started_message",
    "effective_new_session_phrases",
    "is_new_session_phrase",
    "is_new_session_trigger",
    "normalize_new_session_phrase",
    "wakeup_phrases_for_nickname",
]

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


def wakeup_phrases_for_nickname(name: str) -> list[str]:
    """Return ``[name, name+name]`` wakeup forms for a display name.

    Skips empty values, slash commands, and names shorter than 2 characters.
    Does not quadruple a name that is already a doubled token.
    """
    raw = str(name).strip() if name is not None else ""
    if len(raw) < 2 or raw.startswith("/") or "\n" in raw or "\r" in raw:
        return []
    phrases = [raw]
    half, rem = divmod(len(raw), 2)
    already_doubled = rem == 0 and half >= 2 and raw[:half] == raw[half:]
    if not already_doubled:
        phrases.append(raw + raw)
    return phrases


def effective_new_session_phrases(
    phrases: Sequence[str],
    bot_name: str | None = None,
) -> list[str]:
    """Union config phrases with botName and its doubled form.

    An empty *phrases* list is the kill switch: aliases (including botName)
    are disabled. ``/new`` is not a phrase and is unaffected.
    """
    if not phrases:
        return []
    seen: set[str] = set()
    result: list[str] = []

    def _add(item: str) -> None:
        key = normalize_new_session_phrase(item)
        if not key or key in seen:
            return
        seen.add(key)
        result.append(item)

    for phrase in phrases:
        if phrase and str(phrase).strip():
            _add(str(phrase))
    for extra in wakeup_phrases_for_nickname(bot_name or ""):
        _add(extra)
    return result


def configured_new_session_phrases() -> list[str]:
    """Effective /new aliases from the loaded agent defaults."""
    from nanobot.config.loader import load_config

    defaults = load_config().agents.defaults
    return effective_new_session_phrases(defaults.new_session_phrases, defaults.bot_name)


def configured_new_session_started_message() -> str:
    """Confirmation text sent after /new, from the loaded agent defaults."""
    from nanobot.config.loader import load_config

    return (load_config().agents.defaults.new_session_started_message or "").strip()
