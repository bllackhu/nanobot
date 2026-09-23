"""Quick behavioral check for the newSessionPhrases config field."""

import json
import subprocess
import sys

from nanobot.config.schema import (
    DEFAULT_IDLE_NEW_SESSION_HINT_MESSAGE,
    DEFAULT_NEW_SESSION_PHRASES,
    DEFAULT_NEW_SESSION_STARTED_MESSAGE,
    AgentDefaults,
)


def test_new_session_phrases_default_and_alias() -> None:
    defaults = AgentDefaults()
    assert defaults.new_session_phrases == list(DEFAULT_NEW_SESSION_PHRASES)
    dumped = AgentDefaults().model_dump(by_alias=True)
    assert dumped["newSessionPhrases"] == list(DEFAULT_NEW_SESSION_PHRASES)

    parsed = AgentDefaults.model_validate(
        json.loads('{"newSessionPhrases": ["reset"]}')
    )
    assert parsed.new_session_phrases == ["reset"]

    snake = AgentDefaults.model_validate({"new_session_phrases": ["新对话"]})
    assert snake.new_session_phrases == ["新对话"]

    empty = AgentDefaults.model_validate({"newSessionPhrases": []})
    assert empty.new_session_phrases == []


def test_new_session_started_message_default_and_alias() -> None:
    defaults = AgentDefaults()
    assert defaults.new_session_started_message == DEFAULT_NEW_SESSION_STARTED_MESSAGE
    dumped = AgentDefaults().model_dump(by_alias=True)
    assert dumped["newSessionStartedMessage"] == DEFAULT_NEW_SESSION_STARTED_MESSAGE

    parsed = AgentDefaults.model_validate(
        json.loads('{"newSessionStartedMessage": "已开启新对话"}')
    )
    assert parsed.new_session_started_message == "已开启新对话"

    snake = AgentDefaults.model_validate({"new_session_started_message": "hi"})
    assert snake.new_session_started_message == "hi"

    empty = AgentDefaults.model_validate({"newSessionStartedMessage": ""})
    assert empty.new_session_started_message == ""


def test_idle_new_session_hint_defaults_and_alias() -> None:
    defaults = AgentDefaults()
    assert defaults.idle_new_session_hint_after_hours == 0
    assert defaults.idle_new_session_hint_message == DEFAULT_IDLE_NEW_SESSION_HINT_MESSAGE
    dumped = AgentDefaults().model_dump(by_alias=True)
    assert dumped["idleNewSessionHintAfterHours"] == 0
    assert dumped["idleNewSessionHintMessage"] == DEFAULT_IDLE_NEW_SESSION_HINT_MESSAGE

    parsed = AgentDefaults.model_validate({
        "idleNewSessionHintAfterHours": 0.1,
        "idleNewSessionHintMessage": "请新开对话",
    })
    assert parsed.idle_new_session_hint_after_hours == 0.1
    assert parsed.idle_new_session_hint_message == "请新开对话"

    snake = AgentDefaults.model_validate({
        "idle_new_session_hint_after_hours": 0,
        "idle_new_session_hint_message": "",
    })
    assert snake.idle_new_session_hint_after_hours == 0
    assert snake.idle_new_session_hint_message == ""


def test_schema_import_does_not_circular_import_agent() -> None:
    """config.schema must not import nanobot.command (that loads AgentLoop)."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from nanobot.config.schema import ModelPresetConfig; print(ModelPresetConfig.__name__)",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ModelPresetConfig"
