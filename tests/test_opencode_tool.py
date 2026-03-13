from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.opencode import OpenCodeTool
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus


def test_opencode_tool_execute_returns_plan_for_review() -> None:
    tool = OpenCodeTool()
    tool.set_context("cli", "direct")
    tool._select_model = AsyncMock(return_value="alibaba-cn/qwen3-max")  # type: ignore[method-assign]
    tool._run_plan_sync = MagicMock(  # type: ignore[method-assign]
        return_value={
            "success": True,
            "model": "alibaba-cn/qwen3-max",
            "duration": 1.2,
            "completion": {
                "reason": "stop",
                "final_text": "1. Analyze\n2. Implement\n3. Verify",
                "cost": 0.01,
                "tokens": {"input": 10, "output": 20},
            },
        }
    )

    result = asyncio.run(tool.execute(task="implement feature", retries=0))

    assert "status: plan-required" in result
    assert "plan_id:" in result
    assert "/opencode-approve" in result
    assert "Analyze" in result


def test_opencode_tool_preserves_existing_ulw_prefix_in_plan_stage() -> None:
    tool = OpenCodeTool()
    tool.set_context("cli", "direct")
    tool._select_model = AsyncMock(return_value="alibaba-cn/qwen3-max")  # type: ignore[method-assign]
    tool._run_plan_sync = MagicMock(  # type: ignore[method-assign]
        return_value={
            "success": True,
            "model": "alibaba-cn/qwen3-max",
            "duration": 0.3,
            "completion": {
                "reason": "complete",
                "final_text": "ok",
                "cost": None,
                "tokens": None,
            },
        }
    )

    asyncio.run(tool.execute(task="ulw fix tests", retries=0, timeout=123))

    tool._run_plan_sync.assert_called_once_with(  # type: ignore[attr-defined]
        "ulw fix tests", "alibaba-cn/qwen3-max", 120
    )


def test_opencode_tool_model_selection_failure() -> None:
    tool = OpenCodeTool()
    tool._select_model = AsyncMock(side_effect=RuntimeError("OpenCode CLI not found"))  # type: ignore[method-assign]

    result = asyncio.run(tool.execute(task="do work"))

    assert result == "Error: OpenCode CLI not found"


def test_opencode_tool_status_unknown_job() -> None:
    tool = OpenCodeTool()

    result = tool.status("missing")

    assert "not found" in result


def test_opencode_tool_approve_missing_plan() -> None:
    tool = OpenCodeTool()

    result = asyncio.run(tool.approve("missing"))

    assert "not found" in result


def test_opencode_tool_status_running_job() -> None:
    tool = OpenCodeTool()
    fake_thread = threading.Thread(target=time.sleep, args=(0.2,), daemon=True)
    fake_thread.start()
    tool._jobs["job1"] = SimpleNamespace(
        job_id="job1",
        plan_id="plan1",
        status="running",
        started_at=time.time(),
        finished_at=None,
        thread=fake_thread,
        result=None,
        cancel_requested=False,
        process_pid=None,
    )

    status = tool.status("job1")

    assert "status: running" in status
    assert "thread_alive:" in status


def test_opencode_stop_running_job() -> None:
    tool = OpenCodeTool()
    fake_thread = threading.Thread(target=time.sleep, args=(0.2,), daemon=True)
    fake_thread.start()
    tool._jobs["job1"] = SimpleNamespace(
        job_id="job1",
        plan_id="plan1",
        status="running",
        started_at=time.time(),
        finished_at=None,
        thread=fake_thread,
        result=None,
        cancel_requested=False,
        cancel_event=threading.Event(),
        process=None,
        process_pid=None,
    )

    result = tool.stop("job1")

    assert "stop-requested" in result
    assert tool._jobs["job1"].cancel_requested is True
    assert tool._jobs["job1"].cancel_event.is_set() is True


def test_opencode_stop_missing_job() -> None:
    tool = OpenCodeTool()

    result = tool.stop("missing")

    assert "not found" in result


def test_opencode_auto_approve_scope_toggle() -> None:
    tool = OpenCodeTool()

    assert tool.get_auto_approve("dingtalk", "u1") is False
    tool.set_auto_approve("dingtalk", "u1", True)
    assert tool.get_auto_approve("dingtalk", "u1") is True
    tool.set_auto_approve("dingtalk", "u1", False)
    assert tool.get_auto_approve("dingtalk", "u1") is False


def test_opencode_latest_pending_plan_id_by_scope() -> None:
    tool = OpenCodeTool()
    now = time.time()
    tool._pending_plans["p1"] = SimpleNamespace(plan_id="p1", channel="dingtalk", chat_id="u1", created_at=now)
    tool._pending_plans["p2"] = SimpleNamespace(plan_id="p2", channel="dingtalk", chat_id="u1", created_at=now + 1)
    tool._pending_plans["p3"] = SimpleNamespace(plan_id="p3", channel="dingtalk", chat_id="u2", created_at=now + 2)

    assert tool.latest_pending_plan_id("dingtalk", "u1") == "p2"
    assert tool.latest_pending_plan_id("dingtalk", "u2") == "p3"
    assert tool.latest_pending_plan_id("dingtalk", "none") is None


def test_opencode_latest_job_id_by_scope() -> None:
    tool = OpenCodeTool()
    now = time.time()
    fake_thread = threading.Thread(target=time.sleep, args=(0.1,), daemon=True)
    fake_thread.start()
    tool._jobs["j1"] = SimpleNamespace(job_id="j1", channel="dingtalk", chat_id="u1", started_at=now, thread=fake_thread)
    tool._jobs["j2"] = SimpleNamespace(job_id="j2", channel="dingtalk", chat_id="u1", started_at=now + 1, thread=fake_thread)
    tool._jobs["j3"] = SimpleNamespace(job_id="j3", channel="dingtalk", chat_id="u2", started_at=now + 2, thread=fake_thread)

    assert tool.latest_job_id("dingtalk", "u1") == "j2"
    assert tool.latest_job_id("dingtalk", "u2") == "j3"
    assert tool.latest_job_id("dingtalk", "none") is None


def test_agent_loop_natural_language_intent_toggle_auto_approve(tmp_path) -> None:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
    )

    resp = asyncio.run(loop._process_message(InboundMessage(
        channel="dingtalk",
        sender_id="u1",
        chat_id="u1",
        content="以后无需审批",
    )))
    assert resp is not None
    assert "免审批" in resp.content

    tool = loop.tools.get("opencode")
    assert isinstance(tool, OpenCodeTool)
    assert tool.get_auto_approve("dingtalk", "u1") is True


def test_agent_loop_natural_language_intent_execute_latest_plan(tmp_path) -> None:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
    )

    tool = loop.tools.get("opencode")
    assert isinstance(tool, OpenCodeTool)
    tool.latest_pending_plan_id = MagicMock(return_value="planabc1")  # type: ignore[method-assign]
    tool.approve = AsyncMock(return_value="status: started\njob_id: xyz")  # type: ignore[method-assign]

    resp = asyncio.run(loop._process_message(InboundMessage(
        channel="dingtalk",
        sender_id="u1",
        chat_id="u1",
        content="执行这个计划",
    )))
    assert resp is not None
    assert "status: started" in resp.content
    tool.approve.assert_awaited_once_with("planabc1")  # type: ignore[attr-defined]


def test_agent_loop_natural_language_intent_query_latest_status(tmp_path) -> None:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
    )

    tool = loop.tools.get("opencode")
    assert isinstance(tool, OpenCodeTool)
    tool.latest_job_id = MagicMock(return_value="jobxyz1")  # type: ignore[method-assign]
    tool.status = MagicMock(return_value="job_id: jobxyz1\nstatus: running")  # type: ignore[method-assign]

    resp = asyncio.run(loop._process_message(InboundMessage(
        channel="dingtalk",
        sender_id="u1",
        chat_id="u1",
        content="看看 opencode 进度",
    )))
    assert resp is not None
    assert "status: running" in resp.content
    tool.status.assert_called_once_with("jobxyz1")  # type: ignore[attr-defined]


def test_agent_loop_natural_language_intent_query_status_by_job_id(tmp_path) -> None:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
    )

    tool = loop.tools.get("opencode")
    assert isinstance(tool, OpenCodeTool)
    tool.status = MagicMock(return_value="job_id: abc12345\nstatus: success")  # type: ignore[method-assign]

    resp = asyncio.run(loop._process_message(InboundMessage(
        channel="dingtalk",
        sender_id="u1",
        chat_id="u1",
        content="查询任务状态 abc12345",
    )))
    assert resp is not None
    assert "status: success" in resp.content
    tool.status.assert_called_once_with("abc12345")  # type: ignore[attr-defined]


def test_agent_loop_natural_language_intent_stop_latest_job(tmp_path) -> None:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
    )

    tool = loop.tools.get("opencode")
    assert isinstance(tool, OpenCodeTool)
    tool.latest_job_id = MagicMock(return_value="jobxyz1")  # type: ignore[method-assign]
    tool.stop = MagicMock(return_value="status: stop-requested\njob_id: jobxyz1")  # type: ignore[method-assign]

    resp = asyncio.run(loop._process_message(InboundMessage(
        channel="dingtalk",
        sender_id="u1",
        chat_id="u1",
        content="停止这个opencode任务",
    )))
    assert resp is not None
    assert "stop-requested" in resp.content
    tool.stop.assert_called_once_with("jobxyz1")  # type: ignore[attr-defined]


def test_agent_loop_natural_language_intent_stop_by_job_id(tmp_path) -> None:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
    )

    tool = loop.tools.get("opencode")
    assert isinstance(tool, OpenCodeTool)
    tool.stop = MagicMock(return_value="status: stop-requested\njob_id: abc12345")  # type: ignore[method-assign]

    resp = asyncio.run(loop._process_message(InboundMessage(
        channel="dingtalk",
        sender_id="u1",
        chat_id="u1",
        content="停止任务 abc12345",
    )))
    assert resp is not None
    assert "stop-requested" in resp.content
    tool.stop.assert_called_once_with("abc12345")  # type: ignore[attr-defined]


def test_agent_loop_registers_opencode_tool(tmp_path) -> None:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
    )

    tool = loop.tools.get("opencode")
    assert isinstance(tool, OpenCodeTool)
