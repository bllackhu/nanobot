"""Quick behavioral check for the newSessionPhrases config field."""

import json
import subprocess
import sys

from nanobot.config.schema import DEFAULT_NEW_SESSION_PHRASES, AgentDefaults


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
