"""Channel plugin loading helpers."""

from __future__ import annotations

import importlib.metadata as importlib_metadata
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from nanobot.bus.queue import MessageBus
    from nanobot.channels.base import BaseChannel
    from nanobot.config.schema import Config

ChannelFactory = Callable[..., "BaseChannel"]


def _iter_entry_points(group: str) -> list[Any]:
    """Return entry points for a group, compatible with Python 3.11+ APIs."""
    entry_points = importlib_metadata.entry_points()
    if hasattr(entry_points, "select"):
        return list(entry_points.select(group=group))
    return list(entry_points.get(group, []))


def load_channel_factories() -> dict[str, ChannelFactory]:
    """Load channel factories from ``nanobot.channel_factories`` entry points."""
    factories: dict[str, ChannelFactory] = {}
    for ep in _iter_entry_points("nanobot.channel_factories"):
        try:
            factory = ep.load()
        except Exception as exc:
            logger.warning("Failed loading channel factory plugin {}: {}", ep.name, exc)
            continue

        if not callable(factory):
            logger.warning("Ignore channel factory plugin {}: object is not callable", ep.name)
            continue

        name = ep.name.replace("-", "_")
        if name in factories:
            logger.warning("Ignore channel factory plugin {}: duplicate channel name", name)
            continue
        factories[name] = factory
    return factories


def get_channel_factory(name: str | None) -> ChannelFactory | None:
    """Get channel factory by channel name (normalized to snake_case)."""
    if not name:
        return None
    normalized = name.replace("-", "_")
    return load_channel_factories().get(normalized)


def create_channel_by_factory(
    factory: ChannelFactory,
    *,
    config: Any,
    bus: "MessageBus",
    channel_name: str,
    app_config: "Config",
) -> "BaseChannel":
    """Create channel from plugin factory using a stable keyword contract."""
    return factory(
        config=config,
        bus=bus,
        channel_name=channel_name,
        app_config=app_config,
    )
