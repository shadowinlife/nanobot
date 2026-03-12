"""Route inbound messages to per-session worker processes."""

from __future__ import annotations

import asyncio
import multiprocessing as mp
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.config.loader import load_config, set_config_path
from nanobot.config.schema import Config
from nanobot.cron.service import CronService
from nanobot.gateway.self_update import SelfUpdateManager
from nanobot.providers.base import GenerationSettings, LLMProvider

if TYPE_CHECKING:
    from multiprocessing.connection import Connection


def _make_provider_from_config(config: Config) -> LLMProvider:
    """Create provider from config (worker-safe helper)."""
    model = config.agents.defaults.model
    provider_name = config.get_provider_name(model)
    p = config.get_provider(model)

    from nanobot.providers.azure_openai_provider import AzureOpenAIProvider
    from nanobot.providers.custom_provider import CustomProvider
    from nanobot.providers.litellm_provider import LiteLLMProvider
    from nanobot.providers.openai_codex_provider import OpenAICodexProvider
    from nanobot.providers.provider_plugins import (
        create_provider_by_factory,
        get_provider_factory,
    )
    from nanobot.providers.registry import find_by_name

    plugin_factory = get_provider_factory(provider_name)
    if plugin_factory:
        provider = create_provider_by_factory(
            plugin_factory,
            config=config,
            model=model,
            provider_name=provider_name,
            provider_config=p,
        )
    elif provider_name == "openai_codex" or model.startswith("openai-codex/"):
        provider = OpenAICodexProvider(default_model=model)
    elif provider_name == "custom":
        provider = CustomProvider(
            api_key=p.api_key if p else "no-key",
            api_base=config.get_api_base(model) or "http://localhost:8000/v1",
            default_model=model,
        )
    elif provider_name == "azure_openai":
        if not p or not p.api_key or not p.api_base:
            raise RuntimeError("Azure OpenAI requires api_key and api_base")
        provider = AzureOpenAIProvider(
            api_key=p.api_key,
            api_base=p.api_base,
            default_model=model,
        )
    else:
        spec = find_by_name(provider_name)
        if not model.startswith("bedrock/") and not (p and p.api_key) and not (
            spec and (spec.is_oauth or spec.is_local)
        ):
            raise RuntimeError("No API key configured for selected model")
        provider = LiteLLMProvider(
            api_key=p.api_key if p else None,
            api_base=config.get_api_base(model),
            default_model=model,
            extra_body=p.extra_body if p else None,
            extra_headers=p.extra_headers if p else None,
            provider_name=provider_name,
        )

    if isinstance(provider, LLMProvider):
        defaults = config.agents.defaults
        provider.generation = GenerationSettings(
            temperature=defaults.temperature,
            max_tokens=defaults.max_tokens,
            reasoning_effort=defaults.reasoning_effort,
        )
    return provider


@dataclass
class _WorkerHandle:
    """Parent-side state for one session worker."""

    process: mp.Process
    conn: Connection
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _worker_main(
    conn: Connection,
    config_path: str,
    workspace_override: str | None,
) -> None:
    """Worker process entrypoint handling one session serially."""
    try:
        resolved = Path(config_path).expanduser().resolve()
        set_config_path(resolved)
        config = load_config(resolved)
        if workspace_override:
            config.agents.defaults.workspace = workspace_override

        bus = MessageBus()
        provider = _make_provider_from_config(config)
        cron = CronService(Path.home() / ".nanobot" / "cron" / "jobs.json")
        agent = AgentLoop(
            bus=bus,
            provider=provider,
            workspace=config.workspace_path,
            model=config.agents.defaults.model,
            max_iterations=config.agents.defaults.max_tool_iterations,
            context_window_tokens=config.agents.defaults.context_window_tokens,
            brave_api_key=config.tools.web.search.api_key or None,
            web_proxy=config.tools.web.proxy or None,
            exec_config=config.tools.exec,
            cron_service=cron,
            restrict_to_workspace=config.tools.restrict_to_workspace,
            mcp_servers=config.tools.mcp_servers,
            channels_config=config.channels,
        )

        while True:
            packet = conn.recv()
            cmd = packet.get("cmd")

            if cmd == "shutdown":
                break

            if cmd != "process":
                continue

            request_id = packet["request_id"]
            payload = packet["payload"]

            async def _run_one() -> None:
                async def _progress(text: str, *, tool_hint: bool = False) -> None:
                    conn.send({
                        "type": "progress",
                        "request_id": request_id,
                        "channel": payload["channel"],
                        "chat_id": payload["chat_id"],
                        "content": text,
                        "metadata": {
                            **(payload.get("metadata") or {}),
                            "_progress": True,
                            "_tool_hint": tool_hint,
                        },
                    })

                response = await agent.process_direct(
                    payload["content"],
                    session_key=payload["session_key"],
                    channel=payload["channel"],
                    chat_id=payload["chat_id"],
                    on_progress=_progress,
                )

                while bus.outbound_size > 0:
                    out_msg = await bus.consume_outbound()
                    conn.send({
                        "type": "outbound",
                        "request_id": request_id,
                        "channel": out_msg.channel,
                        "chat_id": out_msg.chat_id,
                        "content": out_msg.content,
                        "media": out_msg.media,
                        "metadata": out_msg.metadata,
                    })

                conn.send({
                    "type": "final",
                    "request_id": request_id,
                    "channel": payload["channel"],
                    "chat_id": payload["chat_id"],
                    "content": response,
                    "metadata": payload.get("metadata") or {},
                })

            try:
                asyncio.run(_run_one())
            except Exception as e:
                conn.send({
                    "type": "final",
                    "request_id": request_id,
                    "channel": payload["channel"],
                    "chat_id": payload["chat_id"],
                    "content": f"Sorry, I encountered an error: {e}",
                    "metadata": payload.get("metadata") or {},
                })
    finally:
        try:
            conn.close()
        except Exception:
            pass


class RouteManager:
    """Gateway router that runs each session in a dedicated worker process."""

    _PROFILE_CMD_RE = re.compile(r"^/session\s+config\s+([A-Za-z0-9_.-]+)\s*$", re.IGNORECASE)
    _PROFILE_PREFIX_RE = re.compile(r"^@config:([A-Za-z0-9_.-]+)\s+", re.IGNORECASE)
    _SELF_UPDATE_RE = re.compile(r"^/self-update\s*(.*)$", re.IGNORECASE)
    _SELF_ROLLBACK_RE = re.compile(r"^/self-rollback\s+([0-9a-fA-F]{7,40})\s*$", re.IGNORECASE)

    def __init__(
        self,
        bus: MessageBus,
        base_config_path: Path,
        workspace_override: str | None = None,
    ):
        self.bus = bus
        self.base_config_path = base_config_path
        self.workspace_override = workspace_override
        self._running = False
        self._workers: dict[str, _WorkerHandle] = {}
        self._session_profiles: dict[str, str] = {}
        if workspace_override:
            self._self_update = SelfUpdateManager(Path(workspace_override).expanduser().resolve())
        else:
            cfg = load_config(base_config_path)
            self._self_update = SelfUpdateManager(cfg.workspace_path)

    @property
    def session_profiles(self) -> dict[str, str]:
        """Return current session -> profile bindings."""
        return dict(self._session_profiles)

    async def run(self) -> None:
        """Main router loop consuming inbound messages."""
        self._running = True
        logger.info("Route manager started")

        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            task = asyncio.create_task(self._dispatch(msg))
            task.add_done_callback(lambda t: t.exception() if t.done() and not t.cancelled() else None)

    def stop(self) -> None:
        """Stop route manager loop."""
        self._running = False

    async def close(self) -> None:
        """Shutdown all workers."""
        for session_key in list(self._workers.keys()):
            await self._shutdown_worker(session_key)

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        on_progress: Any | None = None,
    ) -> str:
        """Direct call interface used by cron/heartbeat."""
        msg = InboundMessage(
            channel=channel,
            sender_id="system",
            chat_id=chat_id,
            content=content,
            metadata={},
            session_key_override=session_key,
        )

        events: list[dict[str, Any]] = []
        await self._process_via_worker(msg, events)
        for evt in events:
            if evt["type"] == "progress" and on_progress:
                await on_progress(evt["content"], tool_hint=evt.get("metadata", {}).get("_tool_hint", False))
            if evt["type"] == "outbound":
                await self.bus.publish_outbound(OutboundMessage(
                    channel=evt["channel"],
                    chat_id=evt["chat_id"],
                    content=evt["content"],
                    media=evt.get("media") or [],
                    metadata=evt.get("metadata") or {},
                ))
        final = next((e for e in reversed(events) if e["type"] == "final"), None)
        return (final or {}).get("content", "")

    async def _dispatch(self, msg: InboundMessage) -> None:
        """Dispatch one inbound message to the mapped worker."""
        try:
            if msg.content.strip().lower() == "/stop":
                await self._handle_stop(msg)
                return

            events: list[dict[str, Any]] = []
            await self._process_via_worker(msg, events)

            # Keep event order for progress / message tool events / final response.
            for evt in events:
                await self.bus.publish_outbound(OutboundMessage(
                    channel=evt["channel"],
                    chat_id=evt["chat_id"],
                    content=evt["content"],
                    media=evt.get("media") or [],
                    metadata=evt.get("metadata") or {},
                ))
        except Exception:
            logger.exception("Route dispatch failed for session {}", msg.session_key)
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content="Sorry, I encountered an error.",
            ))

    async def _process_via_worker(self, msg: InboundMessage, collector: list[dict[str, Any]]) -> None:
        """Process a message through the proper session worker and collect emitted events."""
        rollback_commit = self._parse_self_rollback_command(msg.content)
        if rollback_commit:
            ok, detail, _ = self._self_update.rollback_to_commit(rollback_commit)
            if ok:
                await self._shutdown_worker(msg.session_key)
                text = detail
                text += "\nSession worker restarted."
            else:
                text = detail
            collector.append({
                "type": "final",
                "channel": msg.channel,
                "chat_id": msg.chat_id,
                "content": text,
                "metadata": msg.metadata or {},
            })
            return

        update_match = self._SELF_UPDATE_RE.match(msg.content.strip())
        self_update_requested = update_match is not None
        update_instruction = (update_match.group(1) if update_match else "").strip()
        if self_update_requested and not update_instruction:
            collector.append({
                "type": "final",
                "channel": msg.channel,
                "chat_id": msg.chat_id,
                "content": "Usage: /self-update <instruction>",
                "metadata": msg.metadata or {},
            })
            return

        if self_update_requested:
            precheck_error = self._self_update.validate_workspace_preconditions()
            if precheck_error:
                collector.append({
                    "type": "final",
                    "channel": msg.channel,
                    "chat_id": msg.chat_id,
                    "content": precheck_error,
                    "metadata": msg.metadata or {},
                })
                return

        update_ctx = self._self_update.begin(msg.session_key) if self_update_requested else None

        parsed = self._extract_profile_from_message(msg.content)
        if parsed:
            profile, remaining = parsed
            changed = self._bind_profile(msg.session_key, profile)
            if changed:
                await self._shutdown_worker(msg.session_key)
            if remaining.strip() == "":
                collector.append({
                    "type": "final",
                    "channel": msg.channel,
                    "chat_id": msg.chat_id,
                    "content": f"Session profile set to '{profile}'. Subsequent messages will use this config.",
                    "metadata": msg.metadata or {},
                })
                return
            content = remaining
        else:
            cmd = self._parse_profile_command(msg.content)
            if cmd:
                profile = cmd
                if profile.lower() in {"default", "none"}:
                    self._session_profiles.pop(msg.session_key, None)
                    await self._shutdown_worker(msg.session_key)
                    collector.append({
                        "type": "final",
                        "channel": msg.channel,
                        "chat_id": msg.chat_id,
                        "content": "Session profile cleared. Using default config now.",
                        "metadata": msg.metadata or {},
                    })
                    return
                changed = self._bind_profile(msg.session_key, profile)
                if changed:
                    await self._shutdown_worker(msg.session_key)
                collector.append({
                    "type": "final",
                    "channel": msg.channel,
                    "chat_id": msg.chat_id,
                    "content": f"Session profile set to '{profile}'.",
                    "metadata": msg.metadata or {},
                })
                return
            content = msg.content

        if self_update_requested:
            content = update_instruction

        metadata_profile = str((msg.metadata or {}).get("session_profile") or "").strip()
        if metadata_profile:
            changed = self._bind_profile(msg.session_key, metadata_profile)
            if changed:
                await self._shutdown_worker(msg.session_key)

        profile_name = self._session_profiles.get(msg.session_key)
        config_path = self._resolve_profile_config(profile_name)
        if profile_name and config_path is None:
            collector.append({
                "type": "final",
                "channel": msg.channel,
                "chat_id": msg.chat_id,
                "content": f"Session profile '{profile_name}' was not found under ~/.nanobot/.",
                "metadata": msg.metadata or {},
            })
            return

        handle = await self._ensure_worker(msg.session_key, config_path)
        request_id = str(uuid.uuid4())

        payload = {
            "session_key": msg.session_key,
            "channel": msg.channel,
            "chat_id": msg.chat_id,
            "content": content,
            "metadata": msg.metadata or {},
        }

        async with handle.lock:
            handle.conn.send({"cmd": "process", "request_id": request_id, "payload": payload})

            while True:
                event = await asyncio.to_thread(handle.conn.recv)
                if event.get("request_id") != request_id:
                    continue
                collector.append(event)
                if event.get("type") == "final":
                    break

        if self_update_requested and update_ctx:
            result = self._self_update.finalize(update_ctx, update_instruction)
            final_evt = next((e for e in reversed(collector) if e.get("type") == "final"), None)

            if final_evt is not None:
                if not result.changed_files:
                    final_evt["content"] = (
                        f"{final_evt.get('content', '')}\n\n"
                        "[self-update] No code changes detected."
                    ).strip()
                elif result.rolled_back:
                    final_evt["content"] = (
                        f"{final_evt.get('content', '')}\n\n"
                        "[self-update] Validation failed. Changes were rolled back.\n"
                        f"Reason: {result.validation_error}"
                    ).strip()
                else:
                    commit_line = (
                        f"\nCommit: {result.commit_sha}" if result.commit_sha else ""
                    )
                    final_evt["content"] = (
                        f"{final_evt.get('content', '')}\n\n"
                        f"[self-update] Applied {len(result.changed_files)} file(s), "
                        f"validated compile+tests, committed, and restarting session worker."
                        f"{commit_line}"
                    ).strip()

            if result.restarted:
                await self._shutdown_worker(msg.session_key)

    @classmethod
    def _parse_self_rollback_command(cls, content: str) -> str | None:
        """Parse '/self-rollback <commit_sha>' command."""
        match = cls._SELF_ROLLBACK_RE.match(content.strip())
        if not match:
            return None
        return match.group(1)

    async def _handle_stop(self, msg: InboundMessage) -> None:
        """Stop active worker for the session by terminating its process."""
        key = msg.session_key
        existed = key in self._workers
        await self._shutdown_worker(key)
        content = "⏹ Stopped current session worker." if existed else "No active task to stop."
        await self.bus.publish_outbound(OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=content,
        ))

    async def _ensure_worker(self, session_key: str, profile_config: Path | None) -> _WorkerHandle:
        """Get or spawn worker process for one session."""
        existing = self._workers.get(session_key)
        if existing and existing.process.is_alive():
            return existing

        if existing:
            await self._shutdown_worker(session_key)

        parent_conn, child_conn = mp.Pipe(duplex=True)
        config_path = str((profile_config or self.base_config_path).expanduser().resolve())
        proc = mp.Process(
            target=_worker_main,
            args=(child_conn, config_path, self.workspace_override),
            daemon=True,
            name=f"nanobot-session-{_safe_proc_name(session_key)}",
        )
        proc.start()

        handle = _WorkerHandle(process=proc, conn=parent_conn)
        self._workers[session_key] = handle
        logger.info("Spawned worker pid={} for session {}", proc.pid, session_key)
        return handle

    async def _shutdown_worker(self, session_key: str) -> None:
        """Shutdown worker process for one session if present."""
        handle = self._workers.pop(session_key, None)
        if not handle:
            return

        try:
            if handle.process.is_alive():
                handle.conn.send({"cmd": "shutdown"})
                await asyncio.to_thread(handle.process.join, 1.5)
            if handle.process.is_alive():
                handle.process.terminate()
                await asyncio.to_thread(handle.process.join, 1.0)
        except Exception:
            logger.exception("Failed shutting down worker for session {}", session_key)
        finally:
            try:
                handle.conn.close()
            except Exception:
                pass

    @classmethod
    def _parse_profile_command(cls, content: str) -> str | None:
        """Parse '/session config <profile>' command."""
        match = cls._PROFILE_CMD_RE.match(content.strip())
        if not match:
            return None
        return match.group(1)

    @classmethod
    def _extract_profile_from_message(cls, content: str) -> tuple[str, str] | None:
        """Parse '@config:<profile> ...' message prefix."""
        match = cls._PROFILE_PREFIX_RE.match(content)
        if not match:
            return None
        profile = match.group(1)
        remaining = content[match.end():]
        return profile, remaining

    def _resolve_profile_config(self, profile: str | None) -> Path | None:
        """Resolve profile config path from ~/.nanobot when profile is specified."""
        if not profile:
            return None

        base = Path.home() / ".nanobot"
        candidates = [
            base / f"{profile}.json",
            base / f"config.{profile}.json",
            base / "profiles" / f"{profile}.json",
            base / "configs" / f"{profile}.json",
        ]

        for p in candidates:
            if p.exists() and p.is_file():
                return p
        return None

    def _bind_profile(self, session_key: str, profile: str) -> bool:
        """Bind profile to session and return True when changed."""
        old = self._session_profiles.get(session_key)
        self._session_profiles[session_key] = profile
        return old != profile


def _safe_proc_name(session_key: str) -> str:
    """Build process-safe short name segment from a session key."""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", session_key)
    if not cleaned:
        cleaned = "session"
    return cleaned[:48] + f"_{os.getpid()}"
