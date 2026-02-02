"""
Search tool - combined glob (file patterns) and grep (content search).
"""

from __future__ import annotations

import os
import re
import glob as glob_module
from pathlib import Path
from typing import Dict, Any, Optional, List
from dataclasses import dataclass


@dataclass
class ToolResult:
    """Result from tool execution."""
    success: bool
    output: Any
    error: Optional[str] = None


class SearchTool:
    """Find files (glob) and search content (grep)."""

    name = "search"
    description = "Search for files by pattern (glob) or content (grep). Use command parameter to select."

    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "enum": ["glob", "grep"],
                "description": "Search type: glob for files, grep for content"
            },
            "pattern": {
                "type": "string",
                "description": "Glob pattern (e.g., **/*.py) or regex pattern for grep"
            },
            "path": {
                "type": "string",
                "description": "Base path to search in",
                "default": "."
            },
            "file_pattern": {
                "type": "string",
                "description": "File pattern to limit grep search (e.g., *.py)",
                "default": "*"
            },
            "case_sensitive": {
                "type": "boolean",
                "description": "Case sensitive grep search",
                "default": True
            },
            "context_lines": {
                "type": "integer",
                "description": "Lines of context around grep matches",
                "default": 0
            },
            "recursive": {
                "type": "boolean",
                "description": "Search recursively",
                "default": True
            }
        },
        "required": ["command", "pattern"]
    }

    LANGUAGE_MAP = {
        ".py": "python", ".js": "javascript", ".ts": "typescript",
        ".java": "java", ".cpp": "cpp", ".c": "c", ".go": "go",
        ".rs": "rust", ".rb": "ruby", ".json": "json", ".yaml": "yaml",
    }

    def __init__(self, working_dir: str = "."):
        self.working_dir = working_dir

    def execute(self, command: str, pattern: str, **kwargs) -> ToolResult:
        """Execute search operation."""
        if command == "glob":
            return self._glob(
                pattern,
                kwargs.get("path", "."),
                kwargs.get("recursive", True)
            )
        elif command == "grep":
            return self._grep(
                pattern,
                kwargs.get("path", "."),
                kwargs.get("file_pattern", "*"),
                kwargs.get("case_sensitive", True),
                kwargs.get("context_lines", 0)
            )
        else:
            return ToolResult(success=False, output=None, error=f"Unknown command: {command}")

    def _glob(self, pattern: str, base_path: str = ".", recursive: bool = True) -> ToolResult:
        """Find files matching glob pattern."""
        try:
            # Build search pattern
            if recursive and "**" not in pattern:
                search_pattern = os.path.join(base_path, "**", pattern)
            else:
                search_pattern = os.path.join(base_path, pattern)

            files = glob_module.glob(search_pattern, recursive=recursive)
            files = [f for f in files if os.path.isfile(f)]

            # Sort by modification time (newest first)
            files.sort(key=lambda x: os.path.getmtime(x), reverse=True)

            # Categorize by extension
            categories: Dict[str, int] = {}
            for f in files:
                ext = Path(f).suffix.lower() or "(no ext)"
                categories[ext] = categories.get(ext, 0) + 1

            # Build output
            output_lines = [f"Found {len(files)} files matching '{pattern}'"]
            if categories:
                cat_str = ", ".join(f"{ext}: {cnt}" for ext, cnt in sorted(categories.items()))
                output_lines.append(f"Types: {cat_str}")
            output_lines.append("")
            output_lines.extend(files[:30])
            if len(files) > 30:
                output_lines.append(f"... and {len(files) - 30} more")

            return ToolResult(
                success=True,
                output="\n".join(output_lines),
                error=None
            )

        except Exception as e:
            return ToolResult(success=False, output=None, error=str(e))

    def _grep(
        self,
        pattern: str,
        search_path: str = ".",
        file_pattern: str = "*",
        case_sensitive: bool = True,
        context_lines: int = 0
    ) -> ToolResult:
        """Search for pattern in file contents."""
        try:
            flags = 0 if case_sensitive else re.IGNORECASE
            regex = re.compile(pattern, flags)

            matches: List[Dict[str, Any]] = []
            files_searched = 0

            # Determine files to search
            if os.path.isfile(search_path):
                files_to_search = [search_path]
            else:
                files_to_search = glob_module.glob(
                    os.path.join(search_path, "**", file_pattern),
                    recursive=True
                )
                files_to_search = [f for f in files_to_search if os.path.isfile(f)]

            for file_path in files_to_search:
                try:
                    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                        lines = f.readlines()
                    files_searched += 1

                    for i, line in enumerate(lines):
                        if regex.search(line):
                            match_info = {
                                "file": file_path,
                                "line_number": i + 1,
                                "line": line.strip(),
                                "language": self._detect_language(file_path)
                            }

                            if context_lines > 0:
                                context = []
                                start = max(0, i - context_lines)
                                end = min(len(lines), i + context_lines + 1)
                                for j in range(start, end):
                                    if j != i:
                                        context.append(f"{j+1}: {lines[j].strip()}")
                                match_info["context"] = context

                            matches.append(match_info)
                except Exception:
                    continue

            # Format output
            output_lines = [
                f"Pattern: '{pattern}' ({'case sensitive' if case_sensitive else 'case insensitive'})",
                f"Files searched: {files_searched}, Matches: {len(matches)}",
                ""
            ]

            for match in matches[:20]:
                output_lines.append(f"{match['file']}:{match['line_number']}: {match['line']}")
                if match.get("context"):
                    for ctx in match["context"]:
                        output_lines.append(f"    {ctx}")
                    output_lines.append("")

            if len(matches) > 20:
                output_lines.append(f"... and {len(matches) - 20} more matches")

            return ToolResult(
                success=True,
                output="\n".join(output_lines),
                error=None
            )

        except Exception as e:
            return ToolResult(success=False, output=None, error=str(e))

    def _detect_language(self, file_path: str) -> str:
        """Detect language from extension."""
        ext = Path(file_path).suffix.lower()
        return self.LANGUAGE_MAP.get(ext, "unknown")

    def to_schema(self) -> Dict:
        """Convert to OpenAI-style tool schema."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters
        }


def get_tool(working_dir: str = ".") -> SearchTool:
    """Factory function for tool discovery."""
    return SearchTool(working_dir)
