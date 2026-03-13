"""OpenCode tool integration for ULW coding tasks."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from nanobot.agent.tools.base import Tool


class OpenCodeTool(Tool):
    """Execute coding tasks via OpenCode ULW mode."""

    def __init__(
        self,
        model: str = "alibaba-cn/qwen3-max",
        timeout: int = 300,
        model_probe_timeout: int = 10,
    ) -> None:
        self.model = model
        self.timeout = timeout
        self.model_probe_timeout = model_probe_timeout

    @property
    def name(self) -> str:
        return "opencode"

    @property
    def description(self) -> str:
        return (
            "Run coding work with OpenCode ULW mode. "
            "Use for implementation-heavy tasks and return structured execution output."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Coding task description. ULW prefix is added automatically if missing.",
                },
                "retries": {
                    "type": "integer",
                    "description": "Retry attempts on transient failure.",
                    "minimum": 0,
                    "maximum": 5,
                },
                "model": {
                    "type": "string",
                    "description": "Optional preferred OpenCode model.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Optional timeout in seconds for each run.",
                    "minimum": 10,
                    "maximum": 1800,
                },
            },
            "required": ["task"],
        }

    async def execute(
        self,
        task: str,
        retries: int = 1,
        model: str | None = None,
        timeout: int | None = None,
        **kwargs: Any,
    ) -> str:
        task_text = (task or "").strip()
        if not task_text:
            return "Error: task is required"

        max_retries = max(0, min(retries, 5))
        timeout_s = timeout if timeout is not None else self.timeout
        timeout_s = max(10, min(timeout_s, 1800))

        try:
            selected_model = await self._select_model(model or self.model)
        except RuntimeError as e:
            return f"Error: {e}"

        ulw_prompt = task_text if task_text.lower().startswith("ulw ") else f"ulw {task_text}"

        last_failure = "unknown failure"
        for attempt in range(1, max_retries + 2):
            run_result = await self._run_ulw(ulw_prompt, selected_model, timeout_s)
            if run_result["success"]:
                return self._format_success(run_result, attempt, max_retries + 1)
            last_failure = self._format_failure(run_result, attempt, max_retries + 1)

        return f"Error: OpenCode run failed. {last_failure}"

    async def _get_available_models(self) -> list[str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                "opencode",
                "models",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            raise RuntimeError("OpenCode CLI not found. Install opencode first.")
        except Exception as e:
            raise RuntimeError(f"Failed to query OpenCode models: {e}")

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.model_probe_timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError("OpenCode model query timed out")

        if proc.returncode != 0:
            err_text = stderr.decode("utf-8", errors="replace").strip()
            msg = err_text or f"opencode models failed with exit code {proc.returncode}"
            raise RuntimeError(msg)

        lines = stdout.decode("utf-8", errors="replace").splitlines()
        models = [line.strip() for line in lines if line.strip()]
        if not models:
            raise RuntimeError("No OpenCode models available")
        return models

    async def _select_model(self, preferred: str) -> str:
        models = await self._get_available_models()

        if preferred in models:
            return preferred

        qwen3_max = [m for m in models if "qwen3-max" in m]
        if qwen3_max:
            return max(qwen3_max, key=len)

        qwen3 = [m for m in models if "qwen3" in m and "max" not in m]
        if qwen3:
            return max(qwen3, key=len)

        return models[0]

    async def _run_ulw(self, prompt: str, model: str, timeout_s: int) -> dict[str, Any]:
        start = time.time()
        try:
            proc = await asyncio.create_subprocess_exec(
                "opencode",
                "run",
                prompt,
                "--model",
                model,
                "--format",
                "json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return {
                "success": False,
                "error": "OpenCode CLI not found",
                "stderr": "",
                "stdout": "",
                "exit_code": 127,
                "duration": 0.0,
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to start OpenCode: {e}",
                "stderr": "",
                "stdout": "",
                "exit_code": -1,
                "duration": 0.0,
            }

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {
                "success": False,
                "error": f"OpenCode timed out after {timeout_s}s",
                "stderr": "",
                "stdout": "",
                "exit_code": -1,
                "duration": time.time() - start,
            }

        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")
        events = self._parse_json_events(stdout_text)
        completion = self._extract_completion_info(events)

        ok = (
            proc.returncode == 0
            and completion["completed"]
            and completion["reason"] in {"stop", "complete"}
            and bool(completion["final_text"].strip())
        )

        return {
            "success": ok,
            "exit_code": proc.returncode,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "events": events,
            "completion": completion,
            "model": model,
            "duration": time.time() - start,
            "error": "",
        }

    @staticmethod
    def _parse_json_events(output: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for line in output.splitlines():
            text = line.strip()
            if not text:
                continue
            try:
                event = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
        return events

    @staticmethod
    def _extract_completion_info(events: list[dict[str, Any]]) -> dict[str, Any]:
        completion_event: dict[str, Any] | None = None
        final_parts: list[str] = []

        for event in events:
            event_type = event.get("type")
            part = event.get("part", {})
            if event_type == "text" and isinstance(part, dict):
                text = part.get("text", "")
                if text:
                    final_parts.append(str(text))
            elif event_type == "step_finish" and isinstance(part, dict):
                completion_event = part

        final_text = "".join(final_parts).strip()
        return {
            "completed": completion_event is not None,
            "final_text": final_text,
            "reason": (completion_event or {}).get("reason"),
            "cost": (completion_event or {}).get("cost"),
            "tokens": (completion_event or {}).get("tokens"),
        }

    @staticmethod
    def _format_success(result: dict[str, Any], attempt: int, total_attempts: int) -> str:
        completion = result["completion"]
        output = completion.get("final_text", "")
        if len(output) > 12000:
            output = output[:12000] + "\n... (truncated)"

        lines = [
            "status: success",
            f"model: {result['model']}",
            f"attempt: {attempt}/{total_attempts}",
            f"duration_sec: {result['duration']:.2f}",
            f"reason: {completion.get('reason')}",
        ]
        if completion.get("cost") is not None:
            lines.append(f"cost: {completion['cost']}")
        if completion.get("tokens") is not None:
            lines.append(f"tokens: {completion['tokens']}")
        lines.append("output:")
        lines.append(output)
        return "\n".join(lines)

    @staticmethod
    def _format_failure(result: dict[str, Any], attempt: int, total_attempts: int) -> str:
        error = result.get("error", "")
        stderr = (result.get("stderr") or "").strip()
        if len(stderr) > 600:
            stderr = stderr[:600] + "..."
        reason = result.get("completion", {}).get("reason")
        exit_code = result.get("exit_code")

        details = [f"attempt={attempt}/{total_attempts}", f"exit_code={exit_code}"]
        if reason:
            details.append(f"reason={reason}")
        if error:
            details.append(f"error={error}")
        if stderr:
            details.append(f"stderr={stderr}")
        return "; ".join(details)
