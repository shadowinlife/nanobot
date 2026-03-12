from __future__ import annotations

import asyncio
from pathlib import Path

from nanobot.agent.tools.code_search import FindCodeTool


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_find_code_returns_matches(tmp_path: Path) -> None:
    _write(tmp_path / "nanobot" / "demo.py", "def hello():\n    return 1\n")

    tool = FindCodeTool(workspace=tmp_path)
    result = asyncio.run(tool.execute(query="hello"))

    assert "demo.py" in result


def test_find_code_no_match(tmp_path: Path) -> None:
    _write(tmp_path / "nanobot" / "demo.py", "x = 1\n")

    tool = FindCodeTool(workspace=tmp_path)
    result = asyncio.run(tool.execute(query="not_exists_12345"))

    assert result == "No matches found."
