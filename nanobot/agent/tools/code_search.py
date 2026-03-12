"""Code search tool for precise in-workspace location lookup."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool


class FindCodeTool(Tool):
    """Search source files for symbols/text with file and line hints."""

    def __init__(self, workspace: Path, timeout: int = 10):
        self.workspace = workspace
        self.timeout = timeout

    @property
    def name(self) -> str:
        return "find_code"

    @property
    def description(self) -> str:
        return "Find matching code lines in workspace. Returns file paths and line numbers."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Text or regex pattern to search for",
                },
                "glob": {
                    "type": "string",
                    "description": "Optional glob filter, e.g. 'nanobot/**/*.py'",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum returned matches",
                    "minimum": 1,
                    "maximum": 200,
                },
            },
            "required": ["query"],
        }

    async def execute(
        self,
        query: str,
        glob: str | None = None,
        max_results: int = 80,
        **kwargs: Any,
    ) -> str:
        max_results = max(1, min(max_results, 200))

        rg_result = await self._try_rg(query, glob, max_results)
        if rg_result is not None:
            return rg_result

        return self._fallback_scan(query, glob, max_results)

    async def _try_rg(self, query: str, glob: str | None, max_results: int) -> str | None:
        cmd = [
            "rg",
            "--line-number",
            "--no-heading",
            "--color",
            "never",
            "--max-count",
            str(max_results),
            query,
            str(self.workspace),
        ]
        if glob:
            cmd.extend(["--glob", glob])

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
        except FileNotFoundError:
            return None
        except asyncio.TimeoutError:
            return "Error: find_code timed out"
        except Exception as e:
            return f"Error: find_code failed: {e}"

        out = stdout.decode("utf-8", errors="replace").strip()
        err = stderr.decode("utf-8", errors="replace").strip()

        if proc.returncode == 0:
            return out if out else "No matches found."
        if proc.returncode == 1:
            return "No matches found."
        return f"Error: rg failed ({proc.returncode}): {err or 'unknown error'}"

    def _fallback_scan(self, query: str, glob: str | None, max_results: int) -> str:
        import fnmatch
        import re

        try:
            pattern = re.compile(query)
            use_regex = True
        except re.error:
            use_regex = False
            pattern = None

        include_glob = glob or "**/*.py"
        matches: list[str] = []

        for path in self.workspace.rglob("*"):
            if len(matches) >= max_results:
                break
            if not path.is_file():
                continue
            rel = str(path.relative_to(self.workspace)).replace("\\", "/")
            if not fnmatch.fnmatch(rel, include_glob):
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except Exception:
                continue
            for idx, line in enumerate(lines, start=1):
                if use_regex and pattern and pattern.search(line):
                    matches.append(f"{rel}:{idx}:{line.strip()}")
                elif (not use_regex) and (query in line):
                    matches.append(f"{rel}:{idx}:{line.strip()}")
                if len(matches) >= max_results:
                    break

        return "\n".join(matches) if matches else "No matches found."
