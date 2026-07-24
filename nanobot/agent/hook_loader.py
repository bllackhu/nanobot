"""Discover third-party agent turn hooks via setuptools entry points.

Plugins register under the ``nanobot.hooks`` group. Each entry point must
resolve to an ``AgentTurnHookFactory``
(``Callable[[AgentTurnHookContext], AgentHook | None]``).

The loader injects ``bus`` and ``config`` into ``context.metadata`` under the
keys below so plugins can publish outbound messages without core schema changes.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from importlib.metadata import entry_points
from typing import Any

from loguru import logger

from nanobot.agent.hook import AgentHook, AgentTurnHookContext, AgentTurnHookFactory

# Metadata keys injected into AgentTurnHookContext for external hooks.
HOOK_META_BUS = "_nanobot_bus"
HOOK_META_CONFIG = "_nanobot_config"

ENTRY_POINT_GROUP = "nanobot.hooks"


def _wrap_factory(
    raw: AgentTurnHookFactory,
    *,
    bus: Any,
    config: Any,
    name: str,
) -> AgentTurnHookFactory:
    def factory(context: AgentTurnHookContext) -> AgentHook | None:
        context.metadata[HOOK_META_BUS] = bus
        context.metadata[HOOK_META_CONFIG] = config
        try:
            return raw(context)
        except Exception:
            logger.exception("External hook factory failed: {}", name)
            return None

    factory.__name__ = getattr(raw, "__name__", name)  # type: ignore[attr-defined]
    factory.__qualname__ = getattr(raw, "__qualname__", name)  # type: ignore[attr-defined]
    return factory


def load_external_hook_factories(
    bus: Any,
    config: Any,
    *,
    enabled: bool = True,
    entry_points_loader: Callable[[], Sequence[Any]] | None = None,
) -> list[AgentTurnHookFactory]:
    """Load ``nanobot.hooks`` entry points and wrap them with bus/config injection.

    Failures for individual plugins are logged and skipped so a bad plugin
    cannot prevent gateway startup.
    """
    if not enabled:
        return []

    factories: list[AgentTurnHookFactory] = []

    def _default_eps() -> Sequence[Any]:
        try:
            return list(entry_points(group=ENTRY_POINT_GROUP))
        except Exception:
            logger.exception("Failed to enumerate {} entry points", ENTRY_POINT_GROUP)
            return []

    eps = entry_points_loader() if entry_points_loader is not None else _default_eps()
    for ep in eps:
        name = getattr(ep, "name", repr(ep))
        try:
            loaded = ep.load()
        except Exception:
            logger.exception("Failed to load hook plugin: {}", name)
            continue
        if not callable(loaded):
            logger.error("Hook plugin {} is not callable: {!r}", name, loaded)
            continue
        factories.append(_wrap_factory(loaded, bus=bus, config=config, name=str(name)))
    return factories


def build_hook_factories(
    bus: Any,
    config: Any,
    *builtin: AgentTurnHookFactory,
) -> list[AgentTurnHookFactory]:
    """Combine built-in factories with discovered external hooks."""
    defaults = getattr(getattr(config, "agents", None), "defaults", None)
    enabled = True if defaults is None else bool(getattr(defaults, "load_external_hooks", True))
    return [*builtin, *load_external_hook_factories(bus, config, enabled=enabled)]
