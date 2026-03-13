from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.opencode import OpenCodeTool
from nanobot.bus.queue import MessageBus


def test_opencode_tool_execute_success() -> None:
    tool = OpenCodeTool()
    tool._select_model = AsyncMock(return_value="alibaba-cn/qwen3-max")  # type: ignore[method-assign]
    tool._run_ulw = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "success": True,
            "model": "alibaba-cn/qwen3-max",
            "duration": 1.2,
            "completion": {
                "reason": "stop",
                "final_text": "done",
                "cost": 0.01,
                "tokens": {"input": 10, "output": 20},
            },
        }
    )

    result = asyncio.run(tool.execute(task="implement feature", retries=0))

    assert "status: success" in result
    assert "output:\ndone" in result
    tool._run_ulw.assert_awaited_once_with(  # type: ignore[attr-defined]
        "ulw implement feature", "alibaba-cn/qwen3-max", 300
    )


def test_opencode_tool_preserves_existing_ulw_prefix() -> None:
    tool = OpenCodeTool()
    tool._select_model = AsyncMock(return_value="alibaba-cn/qwen3-max")  # type: ignore[method-assign]
    tool._run_ulw = AsyncMock(  # type: ignore[method-assign]
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

    asyncio.run(tool.execute(task="ulw fix tests", retries=0))

    tool._run_ulw.assert_awaited_once_with(  # type: ignore[attr-defined]
        "ulw fix tests", "alibaba-cn/qwen3-max", 300
    )


def test_opencode_tool_model_selection_failure() -> None:
    tool = OpenCodeTool()
    tool._select_model = AsyncMock(side_effect=RuntimeError("OpenCode CLI not found"))  # type: ignore[method-assign]

    result = asyncio.run(tool.execute(task="do work"))

    assert result == "Error: OpenCode CLI not found"


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
