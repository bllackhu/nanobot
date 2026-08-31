"""Quick behavioral check for the newSessionPhrases config field."""

import json

from nanobot.command.new_intent import DEFAULT_NEW_SESSION_PHRASES
from nanobot.config.schema import AgentDefaults


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
