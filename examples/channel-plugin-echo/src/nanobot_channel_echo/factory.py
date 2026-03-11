"""Factory entry point for the webhook channel plugin."""

from __future__ import annotations

from typing import Any

from nanobot.bus.queue import MessageBus

from .echo_channel import EchoChannel


def create_channel(*, config: dict[str, Any], bus: MessageBus, channel_name: str, app_config) -> EchoChannel:
    """Entry point target for ``nanobot.channel_factories``.

    Args:
        config: Raw config object from channels.plugins.<channel_name>
        bus: Shared message bus from nanobot runtime
        channel_name: Normalized channel name from entry point key
        app_config: Root nanobot config (unused in this example)
    """
    if not isinstance(config, dict):
        raise ValueError("webhook channel plugin expects config object")
    if not (config.get("outbound_url") or config.get("outboundUrl")):
        raise ValueError("webhook channel plugin requires outbound_url")
    return EchoChannel(config, bus, channel_name=channel_name)
