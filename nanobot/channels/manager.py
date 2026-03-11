"""Channel manager for coordinating chat channels."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.channels.channel_plugins import (
    create_channel_by_factory,
    load_channel_factories,
)
from nanobot.config.schema import Config


class ChannelManager:
    """
    Manages chat channels and coordinates message routing.

    Responsibilities:
    - Initialize enabled channels (Telegram, WhatsApp, etc.)
    - Start/stop channels
    - Route outbound messages
    """

    def __init__(self, config: Config, bus: MessageBus):
        self.config = config
        self.bus = bus
        self.channels: dict[str, BaseChannel] = {}
        self._channel_tasks: dict[str, asyncio.Task] = {}
        self._dispatch_task: asyncio.Task | None = None
        self._running = False

        self._reload_definitions_only()

    def _reload_definitions_only(self) -> None:
        """Rebuild channel instances from current config without starting tasks."""
        self.channels = self._build_channels()
        self._validate_allow_from()

    def _build_builtin_channels(self) -> dict[str, BaseChannel]:
        """Build built-in channels based on config."""
        builtins: dict[str, BaseChannel] = {}

        def _try_build(name: str, builder: Callable[[], BaseChannel]) -> None:
            try:
                builtins[name] = builder()
                logger.info("{} channel enabled", name.title())
            except ImportError as e:
                logger.warning("{} channel not available: {}", name.title(), e)

        if self.config.channels.telegram.enabled:
            _try_build(
                "telegram",
                lambda: self._build_telegram_channel(),
            )

        if self.config.channels.whatsapp.enabled:
            _try_build(
                "whatsapp",
                lambda: self._build_whatsapp_channel(),
            )

        if self.config.channels.discord.enabled:
            _try_build(
                "discord",
                lambda: self._build_discord_channel(),
            )

        if self.config.channels.feishu.enabled:
            _try_build(
                "feishu",
                lambda: self._build_feishu_channel(),
            )

        if self.config.channels.mochat.enabled:
            _try_build(
                "mochat",
                lambda: self._build_mochat_channel(),
            )

        if self.config.channels.dingtalk.enabled:
            _try_build(
                "dingtalk",
                lambda: self._build_dingtalk_channel(),
            )

        if self.config.channels.email.enabled:
            _try_build(
                "email",
                lambda: self._build_email_channel(),
            )

        if self.config.channels.slack.enabled:
            _try_build(
                "slack",
                lambda: self._build_slack_channel(),
            )

        if self.config.channels.qq.enabled:
            _try_build(
                "qq",
                lambda: self._build_qq_channel(),
            )

        if self.config.channels.matrix.enabled:
            _try_build(
                "matrix",
                lambda: self._build_matrix_channel(),
            )

        return builtins

    def _build_plugin_channels(self) -> dict[str, BaseChannel]:
        """Build third-party plugin channels from channels.plugins config."""
        plugins: dict[str, BaseChannel] = {}
        factories = load_channel_factories()

        for raw_name, raw_cfg in self.config.channels.plugins.items():
            name = raw_name.replace("-", "_")
            cfg = raw_cfg or {}
            if not isinstance(cfg, dict):
                logger.warning("Ignore channel plugin {}: config must be a JSON object", raw_name)
                continue
            if not cfg.get("enabled", False):
                continue

            factory = factories.get(name)
            if not factory:
                logger.warning(
                    "Plugin channel {} is enabled but no factory found in entry points group nanobot.channel_factories",
                    name,
                )
                continue

            try:
                channel = create_channel_by_factory(
                    factory,
                    config=cfg,
                    bus=self.bus,
                    channel_name=name,
                    app_config=self.config,
                )
            except Exception as exc:
                logger.warning("Failed creating plugin channel {}: {}", name, exc)
                continue

            plugins[name] = channel
            logger.info("Plugin channel enabled: {}", name)

        return plugins

    def _build_channels(self) -> dict[str, BaseChannel]:
        """Build all channels (built-in + plugins)."""
        channels = self._build_builtin_channels()
        plugins = self._build_plugin_channels()
        duplicate_names = set(channels).intersection(plugins)
        for name in sorted(duplicate_names):
            logger.warning("Ignore plugin channel {}: name conflicts with built-in channel", name)
            plugins.pop(name, None)
        channels.update(plugins)
        return channels

    def _build_telegram_channel(self) -> BaseChannel:
        from nanobot.channels.telegram import TelegramChannel

        return TelegramChannel(
            self.config.channels.telegram,
            self.bus,
            groq_api_key=self.config.providers.groq.api_key,
        )

    def _build_whatsapp_channel(self) -> BaseChannel:
        from nanobot.channels.whatsapp import WhatsAppChannel

        return WhatsAppChannel(self.config.channels.whatsapp, self.bus)

    def _build_discord_channel(self) -> BaseChannel:
        from nanobot.channels.discord import DiscordChannel

        return DiscordChannel(self.config.channels.discord, self.bus)

    def _build_feishu_channel(self) -> BaseChannel:
        from nanobot.channels.feishu import FeishuChannel

        return FeishuChannel(
            self.config.channels.feishu,
            self.bus,
            groq_api_key=self.config.providers.groq.api_key,
        )

    def _build_mochat_channel(self) -> BaseChannel:
        from nanobot.channels.mochat import MochatChannel

        return MochatChannel(self.config.channels.mochat, self.bus)

    def _build_dingtalk_channel(self) -> BaseChannel:
        from nanobot.channels.dingtalk import DingTalkChannel

        return DingTalkChannel(self.config.channels.dingtalk, self.bus)

    def _build_email_channel(self) -> BaseChannel:
        from nanobot.channels.email import EmailChannel

        return EmailChannel(self.config.channels.email, self.bus)

    def _build_slack_channel(self) -> BaseChannel:
        from nanobot.channels.slack import SlackChannel

        return SlackChannel(self.config.channels.slack, self.bus)

    def _build_qq_channel(self) -> BaseChannel:
        from nanobot.channels.qq import QQChannel

        return QQChannel(self.config.channels.qq, self.bus)

    def _build_matrix_channel(self) -> BaseChannel:
        from nanobot.channels.matrix import MatrixChannel

        return MatrixChannel(self.config.channels.matrix, self.bus)

    def _validate_allow_from(self) -> None:
        for name, ch in self.channels.items():
            if getattr(ch.config, "allow_from", None) == []:
                raise SystemExit(
                    f'Error: "{name}" has empty allowFrom (denies all). '
                    f'Set ["*"] to allow everyone, or add specific user IDs.'
                )

    async def _start_channel(self, name: str, channel: BaseChannel) -> None:
        """Start a channel and log any exceptions."""
        try:
            await channel.start()
        except Exception as e:
            logger.error("Failed to start channel {}: {}", name, e)

    def _ensure_channel_task(self, name: str, channel: BaseChannel) -> None:
        """Ensure one background task exists for a channel."""
        task = self._channel_tasks.get(name)
        if task and not task.done():
            return
        self._channel_tasks[name] = asyncio.create_task(self._start_channel(name, channel))

    async def _stop_channel_runtime(self, name: str) -> None:
        """Stop one channel and cancel its running task if present."""
        task = self._channel_tasks.pop(name, None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error("Error in {} task while stopping: {}", name, e)

        channel = self.channels.get(name)
        if not channel:
            return
        try:
            await channel.stop()
            logger.info("Stopped {} channel", name)
        except Exception as e:
            logger.error("Error stopping {}: {}", name, e)

    async def start_all(self) -> None:
        """Start all channels and the outbound dispatcher."""
        if not self.channels:
            logger.warning("No channels enabled")
            return

        self._running = True

        # Start outbound dispatcher
        self._dispatch_task = asyncio.create_task(self._dispatch_outbound())

        # Start channels
        for name, channel in self.channels.items():
            logger.info("Starting {} channel...", name)
            self._ensure_channel_task(name, channel)

        while self._running:
            await asyncio.sleep(1.0)

    async def stop_all(self) -> None:
        """Stop all channels and the dispatcher."""
        self._running = False
        logger.info("Stopping all channels...")

        # Stop dispatcher
        if self._dispatch_task:
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except asyncio.CancelledError:
                pass
            finally:
                self._dispatch_task = None

        # Stop all channels
        for name in list(self.channels.keys()):
            await self._stop_channel_runtime(name)
        self._channel_tasks.clear()

    async def reload_channels(self, new_config: Config | None = None) -> dict[str, Any]:
        """Reload channels in-process (built-in + plugin) with hot-restart fallback."""
        if new_config is not None:
            self.config = new_config

        try:
            new_channels = self._build_channels()
            old_channels = self.channels
            old_tasks = self._channel_tasks

            self.channels = new_channels
            self._channel_tasks = {}
            self._validate_allow_from()

            if self._running:
                for name in list(old_tasks.keys()):
                    task = old_tasks.get(name)
                    if task:
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
                        except Exception as e:
                            logger.error("Error in {} task during reload: {}", name, e)
                for name, channel in old_channels.items():
                    try:
                        await channel.stop()
                    except Exception as e:
                        logger.error("Error stopping {} during reload: {}", name, e)

                for name, channel in self.channels.items():
                    logger.info("Starting {} channel after reload...", name)
                    self._ensure_channel_task(name, channel)

            return {
                "ok": True,
                "mode": "online-reload",
                "enabled_channels": sorted(self.channels.keys()),
            }
        except Exception as e:
            logger.error("Online channel reload failed: {}", e)
            if self._running:
                logger.warning("Attempting channel hot restart fallback")
                try:
                    await self.stop_all()
                    self._reload_definitions_only()
                    self._running = True
                    self._dispatch_task = asyncio.create_task(self._dispatch_outbound())
                    for name, channel in self.channels.items():
                        logger.info("Starting {} channel after hot restart...", name)
                        self._ensure_channel_task(name, channel)
                    return {
                        "ok": True,
                        "mode": "hot-restart",
                        "enabled_channels": sorted(self.channels.keys()),
                    }
                except Exception as restart_exc:
                    logger.error("Channel hot restart fallback failed: {}", restart_exc)
            return {
                "ok": False,
                "mode": "failed",
                "error": str(e),
            }

    async def _dispatch_outbound(self) -> None:
        """Dispatch outbound messages to the appropriate channel."""
        logger.info("Outbound dispatcher started")

        while True:
            try:
                msg = await asyncio.wait_for(
                    self.bus.consume_outbound(),
                    timeout=1.0
                )

                if msg.metadata.get("_progress"):
                    if msg.metadata.get("_tool_hint") and not self.config.channels.send_tool_hints:
                        continue
                    if not msg.metadata.get("_tool_hint") and not self.config.channels.send_progress:
                        continue

                channel = self.channels.get(msg.channel)
                if channel:
                    try:
                        await channel.send(msg)
                    except Exception as e:
                        logger.error("Error sending to {}: {}", msg.channel, e)
                else:
                    logger.warning("Unknown channel: {}", msg.channel)

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    def get_channel(self, name: str) -> BaseChannel | None:
        """Get a channel by name."""
        return self.channels.get(name)

    def get_status(self) -> dict[str, Any]:
        """Get status of all channels."""
        return {
            name: {
                "enabled": True,
                "running": channel.is_running
            }
            for name, channel in self.channels.items()
        }

    @property
    def enabled_channels(self) -> list[str]:
        """Get list of enabled channel names."""
        return list(self.channels.keys())
