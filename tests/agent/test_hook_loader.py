"""Tests for nanobot.hooks entry-point discovery."""

from __future__ import annotations

from types import SimpleNamespace

from nanobot.agent.hook import AgentHook, AgentTurnHookContext
from nanobot.agent.hook_loader import (
    HOOK_META_BUS,
    HOOK_META_CONFIG,
    build_hook_factories,
    load_external_hook_factories,
)


class _FakeEp:
    def __init__(self, name: str, load_fn):
        self.name = name
        self._load_fn = load_fn

    def load(self):
        return self._load_fn()


def test_load_external_hook_factories_injects_bus_and_config() -> None:
    captured: dict = {}

    def factory(context: AgentTurnHookContext) -> AgentHook | None:
        captured["bus"] = context.metadata.get(HOOK_META_BUS)
        captured["config"] = context.metadata.get(HOOK_META_CONFIG)
        return AgentHook()

    bus = object()
    config = SimpleNamespace(agents=SimpleNamespace(defaults=SimpleNamespace(load_external_hooks=True)))
    factories = load_external_hook_factories(
        bus,
        config,
        entry_points_loader=lambda: [_FakeEp("demo", lambda: factory)],
    )
    assert len(factories) == 1
    hook = factories[0](AgentTurnHookContext(channel="cli", chat_id="direct"))
    assert isinstance(hook, AgentHook)
    assert captured["bus"] is bus
    assert captured["config"] is config


def test_load_external_hook_factories_skips_broken_plugins() -> None:
    class BoomEp(_FakeEp):
        def load(self):
            raise RuntimeError("boom")

    factories = load_external_hook_factories(
        object(),
        SimpleNamespace(),
        entry_points_loader=lambda: [BoomEp("bad", lambda: None)],
    )
    assert factories == []


def test_load_external_hook_factories_disabled() -> None:
    def factory(context: AgentTurnHookContext) -> AgentHook | None:
        return AgentHook()

    factories = load_external_hook_factories(
        object(),
        SimpleNamespace(),
        enabled=False,
        entry_points_loader=lambda: [_FakeEp("demo", lambda: factory)],
    )
    assert factories == []


def test_build_hook_factories_respects_config_flag() -> None:
    def builtin(context: AgentTurnHookContext) -> AgentHook | None:
        return AgentHook()

    def external(context: AgentTurnHookContext) -> AgentHook | None:
        return AgentHook()

    config = SimpleNamespace(
        agents=SimpleNamespace(defaults=SimpleNamespace(load_external_hooks=False))
    )
    # Monkeypatch via entry_points_loader is not exposed on build_hook_factories;
    # verify disabled path returns only builtins when load_external_hooks is False.
    result = build_hook_factories(object(), config, builtin)
    assert result == [builtin]


def test_build_hook_factories_appends_external(monkeypatch) -> None:
    def builtin(context: AgentTurnHookContext) -> AgentHook | None:
        return AgentHook()

    def external(context: AgentTurnHookContext) -> AgentHook | None:
        return AgentHook()

    config = SimpleNamespace(
        agents=SimpleNamespace(defaults=SimpleNamespace(load_external_hooks=True))
    )

    def fake_load(bus, config, *, enabled=True, entry_points_loader=None):
        assert enabled is True
        return [external]

    monkeypatch.setattr(
        "nanobot.agent.hook_loader.load_external_hook_factories",
        fake_load,
    )
    result = build_hook_factories(object(), config, builtin)
    assert result == [builtin, external]
