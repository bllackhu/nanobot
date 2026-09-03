"""Delete unconsumed listen-mode history by Feishu message_id."""

from pathlib import Path

from nanobot.session.manager import Session, SessionManager


def test_delete_unconsumed_history_by_message_id(tmp_path: Path) -> None:
    sm = SessionManager(tmp_path)
    session = Session(key="feishu:oc_abc:om_root")
    session.add_message("user", "keep me", message_id="om_keep", history_only=True)
    session.add_message("user", "drop me", message_id="om_drop", history_only=True)
    sm.save(session)

    assert sm.delete_unconsumed_history_by_message_id(
        "om_drop",
        session_key_prefix="feishu:oc_abc",
    ) is True
    session = sm.get_or_create("feishu:oc_abc:om_root")
    assert [m.get("message_id") for m in session.messages] == ["om_keep"]


def test_delete_unconsumed_skips_after_assistant_turn(tmp_path: Path) -> None:
    sm = SessionManager(tmp_path)
    session = Session(key="feishu:oc_abc")
    session.add_message("user", "seen", message_id="om_seen", history_only=True)
    session.add_message("assistant", "reply")
    sm.save(session)

    assert sm.delete_unconsumed_history_by_message_id(
        "om_seen",
        session_key_prefix="feishu:oc_abc",
    ) is False
    session = sm.get_or_create("feishu:oc_abc")
    assert len(session.messages) == 2


def test_delete_unconsumed_skips_command_assistant(tmp_path: Path) -> None:
    sm = SessionManager(tmp_path)
    session = Session(key="feishu:oc_abc")
    session.add_message("user", "chat", message_id="om_chat", history_only=True)
    session.add_message("assistant", "status", _command=True)
    sm.save(session)

    assert sm.delete_unconsumed_history_by_message_id(
        "om_chat",
        session_key_prefix="feishu:oc_abc",
    ) is True
    session = sm.get_or_create("feishu:oc_abc")
    assert [m.get("role") for m in session.messages] == ["assistant"]


def test_delete_unconsumed_ignores_non_history_only(tmp_path: Path) -> None:
    sm = SessionManager(tmp_path)
    session = Session(key="feishu:oc_abc")
    session.add_message("user", "mention turn", message_id="om_mention")
    sm.save(session)

    assert sm.delete_unconsumed_history_by_message_id(
        "om_mention",
        session_key_prefix="feishu:oc_abc",
    ) is False
    session = sm.get_or_create("feishu:oc_abc")
    assert len(session.messages) == 1


def test_delete_unconsumed_does_not_cross_chat_prefix(tmp_path: Path) -> None:
    sm = SessionManager(tmp_path)
    other = Session(key="feishu:oc_other:om_x")
    other.add_message("user", "other", message_id="om_drop", history_only=True)
    sm.save(other)

    assert sm.delete_unconsumed_history_by_message_id(
        "om_drop",
        session_key_prefix="feishu:oc_abc",
    ) is False
    assert sm.get_or_create("feishu:oc_other:om_x").messages[0]["content"] == "other"
