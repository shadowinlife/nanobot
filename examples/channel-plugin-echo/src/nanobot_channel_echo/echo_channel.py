"""Webhook channel plugin with real HTTP integration."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel


class EchoChannel(BaseChannel):
    """Webhook-based channel with optional inbound polling.

    Outbound: POST JSON payload to `outbound_url`.
    Inbound (optional): GET JSON events from `inbound_poll_url`.
    """

    name = "echo_dev"

    def __init__(self, config: dict[str, Any], bus: MessageBus, *, channel_name: str):
        cfg = dict(config)
        allow_from = cfg.get("allow_from", cfg.get("allowFrom", ["*"]))
        outbound_url = str(cfg.get("outbound_url", cfg.get("outboundUrl", ""))).strip()
        inbound_poll_url = str(cfg.get("inbound_poll_url", cfg.get("inboundPollUrl", ""))).strip()
        auth_token = str(cfg.get("auth_token", cfg.get("authToken", "")).strip())
        timeout_seconds = int(cfg.get("timeout_seconds", cfg.get("timeoutSeconds", 10)))
        poll_interval_seconds = float(cfg.get("poll_interval_seconds", cfg.get("pollIntervalSeconds", 2.0)))
        default_chat_id = str(cfg.get("default_chat_id", cfg.get("defaultChatId", "webhook-default"))).strip()

        runtime_config = SimpleNamespace(
            allow_from=allow_from,
            outbound_url=outbound_url,
            inbound_poll_url=inbound_poll_url,
            auth_token=auth_token,
            timeout_seconds=max(1, timeout_seconds),
            poll_interval_seconds=max(0.5, poll_interval_seconds),
            default_chat_id=default_chat_id,
        )
        super().__init__(runtime_config, bus)
        self.name = channel_name
        self._stop_event = asyncio.Event()
        self._poll_task: asyncio.Task | None = None

    async def start(self) -> None:
        if not self.config.outbound_url:
            raise ValueError(f"{self.name}: outbound_url is required")

        self._running = True
        self._stop_event.clear()
        if self.config.inbound_poll_url:
            self._poll_task = asyncio.create_task(self._poll_inbound_loop())
        logger.info("{} channel started (webhook plugin)", self.name)
        await self._stop_event.wait()

    async def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        logger.info("{} channel stopped (webhook plugin)", self.name)

    async def send(self, msg: OutboundMessage) -> None:
        payload = {
            "channel": self.name,
            "chat_id": msg.chat_id or self.config.default_chat_id,
            "content": msg.content,
            "metadata": msg.metadata,
            "sender_id": "nanobot",
        }
        status, body = await self._http_json_request(
            method="POST",
            url=self.config.outbound_url,
            payload=payload,
        )
        if status >= 400:
            raise RuntimeError(f"webhook send failed: status={status}, body={body[:200]}")

    async def _poll_inbound_loop(self) -> None:
        while self._running and self.config.inbound_poll_url:
            try:
                status, body = await self._http_json_request(
                    method="GET",
                    url=self.config.inbound_poll_url,
                    payload=None,
                )
                if status >= 400:
                    logger.warning("{} inbound poll failed: status={}", self.name, status)
                else:
                    await self._consume_events(body)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("{} inbound poll error: {}", self.name, exc)
            await asyncio.sleep(self.config.poll_interval_seconds)

    async def _consume_events(self, body: str) -> None:
        if not body:
            return
        data = json.loads(body)
        events = data.get("events") if isinstance(data, dict) else data
        if not isinstance(events, list):
            return
        for event in events:
            if not isinstance(event, dict):
                continue
            sender_id = str(event.get("sender_id") or event.get("senderId") or "")
            chat_id = str(event.get("chat_id") or event.get("chatId") or self.config.default_chat_id)
            content = str(event.get("content") or "")
            if not sender_id or not content:
                continue
            metadata = event.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}
            await self._handle_message(
                sender_id=sender_id,
                chat_id=chat_id,
                content=content,
                metadata=metadata,
            )

    async def _http_json_request(self, *, method: str, url: str, payload: dict[str, Any] | None) -> tuple[int, str]:
        return await asyncio.to_thread(self._http_json_request_sync, method=method, url=url, payload=payload)

    def _http_json_request_sync(self, *, method: str, url: str, payload: dict[str, Any] | None) -> tuple[int, str]:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "nanobot-webhook-channel/0.1",
        }
        if self.config.auth_token:
            headers["Authorization"] = f"Bearer {self.config.auth_token}"
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = Request(url=url, data=data, headers=headers, method=method.upper())
        try:
            with urlopen(req, timeout=self.config.timeout_seconds) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                return int(resp.status), body
        except URLError as exc:
            raise RuntimeError(f"request error: {exc}") from exc
