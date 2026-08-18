"""Quick behavioral check for the thinkingHint config field."""

import json

from nanobot.config.schema import AgentDefaults


def test_thinking_hint_default_and_alias():
    defaults = AgentDefaults()
    assert defaults.thinking_hint == "AI thinking ..."
    assert AgentDefaults().model_dump(by_alias=True)["thinkingHint"] == "AI thinking ..."
    parsed = AgentDefaults.model_validate(json.loads('{"thinkingHint": "Thinking now..."}'))
    assert parsed.thinking_hint == "Thinking now..."
    assert AgentDefaults.model_validate({}).thinking_hint == "AI thinking ..."