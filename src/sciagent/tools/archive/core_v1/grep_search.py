"""
Regex search tool.

Search for patterns within files using regular expressions. This
tool can operate on a single file or recursively through
directories. It supports case sensitivity and configurable
context lines around matches. Results include file path, line
number, matched line and optional context.
"""

from __future__ import annotations

import os
import re
import glob
from typing import Dict, Any, Optional, List
from pathlib import Path

from sciagent.base_tool import BaseTool


class GrepSearchTool(BaseTool):
    """Search for patterns within files using regex."""

    name = "grep_search"
    description = "Search for patterns within files using regex"
    input_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regex pattern to search for"},
            "path": {"type": "string", "description": "File or directory to search in", "default": "."},
            "file_pattern": {"type": "string", "description": "File pattern to limit search", "default": "*"},
            "case_sensitive": {"type": "boolean", "description": "Case sensitive search", "default": True},
            "context_lines": {"type": "number", "description": "Lines of context around matches", "default": 0},
        },
        "required": ["pattern"],
    }

    def _detect_language(self, file_path: str) -> str:
        ext = Path(file_path).suffix.lower()
        language_map = {
            ".py": "python",
            ".js": "javascript",
            ".ts": "typescript",
            ".java": "java",
            ".cpp": "cpp",
            ".c": "c",
            ".cs": "csharp",
            ".go": "go",
            ".rs": "rust",
            ".php": "php",
            ".rb": "ruby",
            ".swift": "swift",
            ".kt": "kotlin",
            ".scala": "scala",
            ".html": "html",
            ".css": "css",
            ".scss": "scss",
            ".md": "markdown",
            ".json": "json",
            ".yaml": "yaml",
            ".yml": "yaml",
            ".txt": "text",
        }
        return language_map.get(ext, "unknown")

    def run(self, tool_input: Dict[str, Any], agent: Optional[Any] = None) -> Dict[str, Any]:
        try:
            pattern = tool_input.get("pattern", "")
            search_path = tool_input.get("path", ".")
            file_pattern = tool_input.get("file_pattern", "*")
            case_sensitive = tool_input.get("case_sensitive", True)
            context_lines = int(tool_input.get("context_lines", 0))
            flags = 0 if case_sensitive else re.IGNORECASE
            regex = re.compile(pattern, flags)
            matches: List[Dict[str, Any]] = []
            files_searched = 0
            # Determine files to search
            if os.path.isfile(search_path):
                files_to_search = [search_path]
            else:
                files_to_search = glob.glob(os.path.join(search_path, "**", file_pattern), recursive=True)
                files_to_search = [f for f in files_to_search if os.path.isfile(f)]
            for file_path in files_to_search:
                try:
                    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                        lines = f.readlines()
                    files_searched += 1
                    for i, line in enumerate(lines):
                        if regex.search(line):
                            match_info: Dict[str, Any] = {
                                "file": file_path,
                                "line_number": i + 1,
                                "line": line.strip(),
                                "language": self._detect_language(file_path),
                            }
                            if context_lines > 0:
                                context: List[str] = []
                                start = max(0, i - context_lines)
                                end = min(len(lines), i + context_lines + 1)
                                for j in range(start, end):
                                    if j != i:
                                        context.append(f"{j+1}: {lines[j].strip()}")
                                match_info["context"] = context
                            matches.append(match_info)
                except Exception:
                    continue
            # Format output summary
            output = (
                f"🔍 Pattern: '{pattern}' (case {'sensitive' if case_sensitive else 'insensitive'})\n"
                f"Files searched: {files_searched}, Matches found: {len(matches)}\n\n"
            )
            for i, match in enumerate(matches[:15]):
                output += f"📄 {match['file']}:{match['line_number']}: {match['line']}\n"
                if match.get("context"):
                    for ctx in match["context"]:
                        output += f"     {ctx}\n"
                    output += "\n"
            if len(matches) > 15:
                output += f"... and {len(matches) - 15} more matches"
            return {
                "success": True,
                "output": output,
                "matches": matches,
                "files_searched": files_searched,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}


def get_tool() -> BaseTool:
    return GrepSearchTool()