"""OpenCode tool integration for ULW coding tasks."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import subprocess
import threading
import time
import uuid
from typing import Any, Callable

from nanobot.agent.tools.base import Tool
from nanobot.bus.events import OutboundMessage


@dataclasses.dataclass
class PendingPlan:
    plan_id: str
    channel: str
    chat_id: str
    task: str
    model: str
    retries: int
    timeout_s: int
    plan_text: str
    created_at: float


@dataclasses.dataclass
class OpenCodeJob:
    job_id: str
    plan_id: str
    channel: str
    chat_id: str
    prompt: str
    model: str
    timeout_s: int
    thread: threading.Thread
    started_at: float
    last_probe_at: float
    status: str = "running"
    result: dict[str, Any] | None = None
    finished_at: float | None = None
    cancel_event: threading.Event = dataclasses.field(default_factory=threading.Event)
    cancel_requested: bool = False
    process: subprocess.Popen[str] | None = None
    process_pid: int | None = None


class OpenCodeTool(Tool):
    """Execute coding tasks via OpenCode ULW mode."""

    def __init__(
        self,
        model: str = "alibaba-cn/qwen3-max",
        timeout: int = 300,
        model_probe_timeout: int = 10,
        plan_timeout: int = 120,
        health_check_interval: int = 20,
        send_callback: Any | None = None,
    ) -> None:
        self.model = model
        self.timeout = timeout
        self.model_probe_timeout = model_probe_timeout
        self.plan_timeout = max(30, plan_timeout)
        self.health_check_interval = max(5, health_check_interval)
        self._send_callback = send_callback

        self._channel = "cli"
        self._chat_id = "direct"
        self._pending_plans: dict[str, PendingPlan] = {}
        self._jobs: dict[str, OpenCodeJob] = {}
        self._auto_approve_scope: dict[str, bool] = {}
        self._lock = threading.Lock()

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
                "approve_plan_id": {
                    "type": "string",
                    "description": "Approve and execute a previously generated plan ID.",
                },
                "status_job_id": {
                    "type": "string",
                    "description": "Check status for a background opencode job ID.",
                },
                "stop_job_id": {
                    "type": "string",
                    "description": "Request stop for a running background opencode job ID.",
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
            "required": [],
        }

    def set_context(self, channel: str, chat_id: str) -> None:
        self._channel = channel
        self._chat_id = chat_id

    @staticmethod
    def _scope_key(channel: str, chat_id: str) -> str:
        return f"{channel}:{chat_id}"

    def set_auto_approve(self, channel: str, chat_id: str, enabled: bool) -> None:
        with self._lock:
            self._auto_approve_scope[self._scope_key(channel, chat_id)] = enabled

    def get_auto_approve(self, channel: str, chat_id: str) -> bool:
        with self._lock:
            return bool(self._auto_approve_scope.get(self._scope_key(channel, chat_id), False))

    def latest_pending_plan_id(self, channel: str, chat_id: str) -> str | None:
        with self._lock:
            candidates = [
                p for p in self._pending_plans.values()
                if p.channel == channel and p.chat_id == chat_id
            ]
        if not candidates:
            return None
        latest = max(candidates, key=lambda p: p.created_at)
        return latest.plan_id

    def latest_job_id(self, channel: str, chat_id: str) -> str | None:
        with self._lock:
            candidates = [
                j for j in self._jobs.values()
                if j.channel == channel and j.chat_id == chat_id
            ]
        if not candidates:
            return None
        latest = max(candidates, key=lambda j: j.started_at)
        return latest.job_id

    def stop(self, job_id: str) -> str:
        jid = (job_id or "").strip()
        if not jid:
            return "Error: job_id is required"
        with self._lock:
            job = self._jobs.get(jid)
            if not job:
                return f"Error: job_id '{jid}' not found"

            if job.status in {"success", "failed", "cancelled"} or not job.thread.is_alive():
                elapsed = max(0.0, (job.finished_at or time.time()) - job.started_at)
                return (
                    f"status: {job.status}\n"
                    f"job_id: {job.job_id}\n"
                    f"elapsed_sec: {elapsed:.1f}\n"
                    "note: job is not running"
                )

            job.cancel_requested = True
            job.cancel_event.set()
            proc = job.process

        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

        return (
            "status: stop-requested\n"
            f"job_id: {jid}\n"
            "detail: cancellation signal sent to background thread/process"
        )

    async def execute(
        self,
        task: str = "",
        approve_plan_id: str | None = None,
        status_job_id: str | None = None,
        stop_job_id: str | None = None,
        retries: int = 1,
        model: str | None = None,
        timeout: int | None = None,
        **kwargs: Any,
    ) -> str:
        if approve_plan_id:
            return await self.approve(approve_plan_id)
        if status_job_id:
            return self.status(status_job_id)
        if stop_job_id:
            return self.stop(stop_job_id)

        task_text = (task or "").strip()
        if not task_text:
            return "Error: task is required (or provide approve_plan_id/status_job_id/stop_job_id)"

        max_retries = max(0, min(retries, 5))
        timeout_s = timeout if timeout is not None else self.timeout
        timeout_s = max(10, min(timeout_s, 1800))

        try:
            selected_model = await self._select_model(model or self.model)
        except RuntimeError as e:
            return f"Error: {e}"

        ulw_prompt = task_text if task_text.lower().startswith("ulw ") else f"ulw {task_text}"

        plan_result = await asyncio.to_thread(
            self._run_plan_sync,
            ulw_prompt,
            selected_model,
            min(timeout_s, self.plan_timeout),
        )
        if not plan_result["success"]:
            failure = self._format_failure(plan_result, 1, 1)
            return f"Error: failed to generate opencode plan. {failure}"

        plan_id = uuid.uuid4().hex[:8]
        plan_text = (plan_result.get("completion", {}) or {}).get("final_text", "").strip()
        if len(plan_text) > 6000:
            plan_text = plan_text[:6000] + "\n... (truncated)"

        with self._lock:
            self._pending_plans[plan_id] = PendingPlan(
                plan_id=plan_id,
                channel=self._channel,
                chat_id=self._chat_id,
                task=ulw_prompt,
                model=selected_model,
                retries=max_retries,
                timeout_s=timeout_s,
                plan_text=plan_text,
                created_at=time.time(),
            )

        if self.get_auto_approve(self._channel, self._chat_id):
            start_msg = await self.approve(plan_id)
            return (
                "status: auto-approved\n"
                f"plan_id: {plan_id}\n"
                "plan:\n"
                f"{plan_text}\n\n"
                f"{start_msg}"
            )

        return (
            "status: plan-required\n"
            f"plan_id: {plan_id}\n"
            f"model: {selected_model}\n"
            f"next_step: review plan then run /opencode-approve {plan_id}\n"
            "plan:\n"
            f"{plan_text}"
        )

    async def approve(self, plan_id: str) -> str:
        pid = (plan_id or "").strip()
        if not pid:
            return "Error: plan_id is required"

        with self._lock:
            plan = self._pending_plans.pop(pid, None)
        if not plan:
            return f"Error: plan_id '{pid}' not found or already approved"

        job_id = uuid.uuid4().hex[:10]
        thread = threading.Thread(
            target=self._thread_run_job,
            args=(job_id, plan),
            name=f"opencode-{job_id}",
            daemon=True,
        )

        job = OpenCodeJob(
            job_id=job_id,
            plan_id=plan.plan_id,
            channel=plan.channel,
            chat_id=plan.chat_id,
            prompt=plan.task,
            model=plan.model,
            timeout_s=plan.timeout_s,
            thread=thread,
            started_at=time.time(),
            last_probe_at=time.time(),
        )
        with self._lock:
            self._jobs[job_id] = job

        thread.start()
        asyncio.create_task(self._monitor_job(job_id))

        return (
            "status: started\n"
            f"plan_id: {plan.plan_id}\n"
            f"job_id: {job_id}\n"
            "execution: running in background thread\n"
            f"check: /opencode-status {job_id}"
        )

    def status(self, job_id: str) -> str:
        jid = (job_id or "").strip()
        if not jid:
            return "Error: job_id is required"
        with self._lock:
            job = self._jobs.get(jid)
        if not job:
            return f"Error: job_id '{jid}' not found"

        elapsed = max(0.0, (job.finished_at or time.time()) - job.started_at)
        base = [
            f"job_id: {job.job_id}",
            f"plan_id: {job.plan_id}",
            f"status: {job.status}",
            f"elapsed_sec: {elapsed:.1f}",
            f"thread_alive: {job.thread.is_alive()}",
            f"cancel_requested: {job.cancel_requested}",
        ]
        if job.process_pid:
            base.append(f"process_pid: {job.process_pid}")
        if job.status == "success" and job.result:
            out = (job.result.get("completion", {}) or {}).get("final_text", "").strip()
            if len(out) > 1200:
                out = out[:1200] + "\n... (truncated)"
            if out:
                base.extend(["output:", out])
        elif job.result:
            base.append("error: " + str(job.result.get("error") or "unknown"))
        return "\n".join(base)

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

    @staticmethod
    def _plan_prompt(task_prompt: str) -> str:
        return (
            "ulw Please produce an execution plan only, do not modify files yet. "
            "Use sections: Goal, Scope, Risks, Steps, Validation, Rollback. "
            "Keep it concise and actionable. Task: "
            + task_prompt
        )

    def _run_plan_sync(self, task_prompt: str, model: str, timeout_s: int) -> dict[str, Any]:
        return self._run_prompt_sync(self._plan_prompt(task_prompt), model, timeout_s)

    def _run_prompt_sync(
        self,
        prompt: str,
        model: str,
        timeout_s: int,
        cancel_event: threading.Event | None = None,
        on_process_start: Callable[[subprocess.Popen[str]], None] | None = None,
    ) -> dict[str, Any]:
        start = time.time()
        try:
            proc = subprocess.Popen(
                [
                    "opencode",
                    "run",
                    prompt,
                    "--model",
                    model,
                    "--format",
                    "json",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            if on_process_start:
                on_process_start(proc)
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

        deadline = time.time() + timeout_s
        while True:
            if cancel_event and cancel_event.is_set():
                try:
                    if proc.poll() is None:
                        proc.terminate()
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                except Exception:
                    pass
                stdout_text = ""
                stderr_text = ""
                try:
                    stdout_text, stderr_text = proc.communicate(timeout=1)
                except Exception:
                    pass
                return {
                    "success": False,
                    "error": "OpenCode cancelled by user",
                    "stderr": stderr_text,
                    "stdout": stdout_text,
                    "exit_code": proc.returncode if proc.returncode is not None else -1,
                    "duration": time.time() - start,
                }

            if proc.poll() is not None:
                break

            if time.time() >= deadline:
                try:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                except Exception:
                    pass
                stdout_text = ""
                stderr_text = ""
                try:
                    stdout_text, stderr_text = proc.communicate(timeout=1)
                except Exception:
                    pass
                return {
                    "success": False,
                    "error": f"OpenCode timed out after {timeout_s}s",
                    "stderr": stderr_text,
                    "stdout": stdout_text,
                    "exit_code": proc.returncode if proc.returncode is not None else -1,
                    "duration": time.time() - start,
                }

            time.sleep(0.2)

        stdout_text, stderr_text = proc.communicate()
        stdout_text = stdout_text or ""
        stderr_text = stderr_text or ""
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

    def _thread_run_job(self, job_id: str, plan: PendingPlan) -> None:
        """Run opencode in a dedicated background thread and store result."""
        attempts = max(1, plan.retries + 1)
        last_failure = "unknown failure"
        result: dict[str, Any] | None = None

        with self._lock:
            job = self._jobs.get(job_id)
        if not job:
            return

        for attempt in range(1, attempts + 1):
            run_result = self._run_prompt_sync(
                plan.task,
                plan.model,
                plan.timeout_s,
                cancel_event=job.cancel_event,
                on_process_start=lambda p: self._register_job_process(job_id, p),
            )
            if run_result["success"]:
                result = {
                    "success": True,
                    "attempt": attempt,
                    "total_attempts": attempts,
                    **run_result,
                }
                break
            last_failure = self._format_failure(run_result, attempt, attempts)
            result = {
                "success": False,
                "attempt": attempt,
                "total_attempts": attempts,
                "failure": last_failure,
                **run_result,
            }
            if job.cancel_event.is_set() or "cancelled" in str(run_result.get("error", "")).lower():
                break

        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job.process = None
            job.process_pid = None
            if job.cancel_event.is_set() and result and not result.get("success"):
                job.status = "cancelled"
                job.result = result
                job.finished_at = time.time()
                return
            if result and result.get("success"):
                job.status = "success"
                job.result = result
            else:
                diagnostic = self._analyze_failure(result or {})
                job.status = "failed"
                job.result = {
                    **(result or {}),
                    "error": f"OpenCode run failed. {last_failure}",
                    "diagnostic": diagnostic,
                }
            job.finished_at = time.time()

    def _register_job_process(self, job_id: str, proc: subprocess.Popen[str]) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job.process = proc
            job.process_pid = proc.pid
            if job.cancel_event.is_set() and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass

    async def _monitor_job(self, job_id: str) -> None:
        while True:
            await asyncio.sleep(self.health_check_interval)
            with self._lock:
                job = self._jobs.get(job_id)
            if not job:
                return

            if job.thread.is_alive():
                with self._lock:
                    job.last_probe_at = time.time()
                elapsed = time.time() - job.started_at
                await self._notify(
                    job.channel,
                    job.chat_id,
                    f"[opencode:{job.job_id}] still running in background ({elapsed:.0f}s).",
                )
                continue

            if job.result and job.result.get("success"):
                text = self._format_success(
                    job.result,
                    int(job.result.get("attempt") or 1),
                    int(job.result.get("total_attempts") or 1),
                )
                await self._notify(job.channel, job.chat_id, f"[opencode:{job.job_id}]\n{text}")
                return

            if job.status == "cancelled":
                await self._notify(
                    job.channel,
                    job.chat_id,
                    f"[opencode:{job.job_id}] cancelled by user.",
                )
                return

            failure = (job.result or {}).get("error") or "OpenCode thread exited without result"
            diagnostic = (job.result or {}).get("diagnostic") or "No diagnostic available."
            stderr = ((job.result or {}).get("stderr") or "").strip()
            if len(stderr) > 1000:
                stderr = stderr[-1000:]
            detail = (
                f"[opencode:{job.job_id}] background thread stopped unexpectedly.\n"
                f"error: {failure}\n"
                f"diagnostic: {diagnostic}"
            )
            if stderr:
                detail += f"\nstderr_tail:\n{stderr}"
            await self._notify(job.channel, job.chat_id, detail)
            return

    async def _notify(self, channel: str, chat_id: str, content: str) -> None:
        if not self._send_callback:
            return
        try:
            await self._send_callback(OutboundMessage(channel=channel, chat_id=chat_id, content=content))
        except Exception:
            pass

    @staticmethod
    def _analyze_failure(result: dict[str, Any]) -> str:
        error = str(result.get("error") or "").lower()
        stderr = str(result.get("stderr") or "").lower()
        exit_code = result.get("exit_code")

        if "not found" in error or "command not found" in stderr or exit_code == 127:
            return "OpenCode CLI missing or not in PATH."
        if "timed out" in error:
            return "Execution exceeded timeout; reduce scope or increase timeout."
        if "cancelled" in error:
            return "Execution cancelled by user request."
        if "rate" in stderr or "429" in stderr:
            return "Model provider rate-limited the request."
        if "auth" in stderr or "unauthorized" in stderr or "401" in stderr:
            return "Authentication failed; verify API key/model access."
        if exit_code not in (None, 0):
            return f"OpenCode process exited with code {exit_code}; inspect stderr for details."
        return "Unknown failure; inspect stderr and task prompt complexity."

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
