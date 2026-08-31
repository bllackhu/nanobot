"""Exact-match /new phrase helper."""

from nanobot.command.new_intent import (
    DEFAULT_NEW_SESSION_PHRASES,
    is_new_session_phrase,
    is_new_session_trigger,
    normalize_new_session_phrase,
)

PHRASES = DEFAULT_NEW_SESSION_PHRASES


def test_default_list_contains_agreed_phrases() -> None:
    assert PHRASES == (
        "新对话",
        "新会话",
        "新任务",
        "new",
        "new chat",
        "new session",
    )


def test_matches_exact_defaults() -> None:
    for phrase in PHRASES:
        assert is_new_session_phrase(phrase, PHRASES)


def test_strips_whitespace_and_trailing_punctuation() -> None:
    assert is_new_session_phrase("  新对话  ", PHRASES)
    assert is_new_session_phrase("新任务。", PHRASES)
    assert is_new_session_phrase("new!", PHRASES)
    assert is_new_session_phrase("New Chat?", PHRASES)
    assert is_new_session_phrase("New Session！", PHRASES)


def test_english_is_case_insensitive() -> None:
    assert is_new_session_phrase("NEW", PHRASES)
    assert is_new_session_phrase("New Chat", PHRASES)
    assert is_new_session_phrase("NEW SESSION", PHRASES)


def test_rejects_longer_sentences() -> None:
    assert not is_new_session_phrase("新对话，帮我写周报", PHRASES)
    assert not is_new_session_phrase("new laptop", PHRASES)
    assert not is_new_session_phrase("a new session", PHRASES)
    assert not is_new_session_phrase("start a new chat", PHRASES)
    assert not is_new_session_phrase("重新开始", PHRASES)
    assert not is_new_session_phrase("换个话题", PHRASES)


def test_empty_phrase_list_disables_matching() -> None:
    assert not is_new_session_phrase("新对话", [])
    assert not is_new_session_phrase("new", ())
    assert not is_new_session_trigger("新对话", [])
    assert is_new_session_trigger("/new", [])


def test_slash_new_is_a_trigger_not_a_phrase() -> None:
    assert not is_new_session_phrase("/new", PHRASES)
    assert is_new_session_trigger("/new", PHRASES)
    assert is_new_session_trigger("  /NEW  ", PHRASES)


def test_normalize_strips_one_trailing_punct_only() -> None:
    assert normalize_new_session_phrase("new!") == "new"
    assert normalize_new_session_phrase("new!!") != "new"
