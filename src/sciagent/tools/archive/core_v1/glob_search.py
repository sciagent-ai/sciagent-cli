"""
Glob file search tool.

This tool searches for files matching a given glob pattern. It
supports recursive searching and returns a summary of matching
files along with counts by file extension. The implementation is
adapted from the agent's ``_execute_glob_search`` method.
"""

from __future__ import annotations

import glob
import os
from pathlib import Path
from typing import Dict, Any, Optional

from sciagent.base_tool import BaseTool


class GlobSearchTool(BaseTool):
    """Find files using patterns (like **/*.py, src/**/*.js)."""

    name = "glob_search"
    description = "Find files using patterns (like **/*.py, src/**/*.js)"
    input_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob pattern to search for"},
            "path": {"type": "string", "description": "Base path to search in", "default": "."},
            "recursive": {"type": "boolean", "description": "Search recursively", "default": True},
        },
        "required": ["pattern"],
    }

    def run(self, tool_input: Dict[str, Any], agent: Optional[Any] = None) -> Dict[str, Any]:
        try:
            pattern = tool_input.get("pattern", "*")
            base_path = tool_input.get("path", ".")
            recursive = tool_input.get("recursive", True)
            # Ensure pattern includes ** when recursive
            if recursive and "**" not in pattern:
                search_pattern = os.path.join(base_path, "**", pattern)
            else:
                search_pattern = os.path.join(base_path, pattern)
            files = glob.glob(search_pattern, recursive=recursive)
            files = [f for f in files if os.path.isfile(f)]
            # Sort by modification time descending
            files.sort(key=lambda x: os.path.getmtime(x), reverse=True)
            # Categorise by extension
            categories: Dict[str, list] = {}
            for file_path in files:
                ext = Path(file_path).suffix.lower()
                categories.setdefault(ext, []).append(file_path)
            # Build output summary
            output = (
                f"Found {len(files)} files matching '{pattern}':\n"
                + "\n".join(files[:20])
                + (f"\n... and {len(files) - 20} more files" if len(files) > 20 else "")
            )
            return {
                "success": True,
                "output": output,
                "file_count": len(files),
                "categories": categories,
                "search_pattern": search_pattern,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}


def get_tool() -> BaseTool:
    return GlobSearchTool()