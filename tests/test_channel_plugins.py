from types import SimpleNamespace

from typer.testing import CliRunner

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.channels.manager import ChannelManager
from nanobot.cli.commands import app
from nanobot.config.schema import Config

runner = CliRunner()


class _DummyPluginChannel(BaseChannel):
    name = "demo"

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def send(self, msg: OutboundMessage) -> None:
        return None


def test_channel_manager_loads_plugin_channels(monkeypatch):
    def _factory(**kwargs):
        return _DummyPluginChannel(SimpleNamespace(allow_from=["*"]), kwargs["bus"])

    monkeypatch.setattr(
        "nanobot.channels.manager.load_channel_factories",
        lambda: {"demo": _factory},
    )

    cfg = Config()
    cfg.channels.plugins["demo"] = {"enabled": True}

    manager = ChannelManager(cfg, MessageBus())

    assert "demo" in manager.enabled_channels
    assert isinstance(manager.get_channel("demo"), _DummyPluginChannel)


def test_channels_reload_command(monkeypatch):
    monkeypatch.setattr(
        "nanobot.cli.commands._reload_channel_plugins_and_signal_gateway",
        lambda **_: ({"sent": True, "pid": 1234, "reason": "ok"}, "Reloaded channel factories: 1 (plugins: demo)"),
    )

    result = runner.invoke(app, ["channels", "reload"])

    assert result.exit_code == 0
    assert "Channel Reload" in result.stdout
    assert "Reloaded channel factories" in result.stdout
    assert "SIGHUP sent" in result.stdout
