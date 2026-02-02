"""
Directory listing tool.

List directory contents with optional recursion and hidden file
visibility. Returns metadata including size and whether each
item is a directory. This tool mirrors the behaviour of the
original ``list_directory`` implementation but avoids direct
printing to the console.
"""

from __future__ import annotations

import os
from typing import Dict, Any, Optional, List
from pathlib import Path

from sciagent.base_tool import BaseTool


class ListDirectoryTool(BaseTool):
    """List directory contents with detailed information."""

    name = "list_directory"
    description = "List directory contents with detailed information"
    input_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory path", "default": "."},
            "show_hidden": {"type": "boolean", "description": "Include hidden files", "default": False},
            "recursive": {"type": "boolean", "description": "List recursively", "default": False},
        },
    }

    def run(self, tool_input: Dict[str, Any], agent: Optional[Any] = None) -> Dict[str, Any]:
        try:
            path = tool_input.get("path", ".")
            show_hidden = tool_input.get("show_hidden", False)
            recursive = tool_input.get("recursive", False)
            items: List[Dict[str, Any]] = []
            total_size = 0
            base_path = Path(path)
            if recursive:
                for root, dirs, files in os.walk(path):
                    # Filter hidden names if not showing
                    if not show_hidden:
                        dirs[:] = [d for d in dirs if not d.startswith(".")]
                        files = [f for f in files if not f.startswith(".")]
                    for name in dirs + files:
                        full_path = os.path.join(root, name)
                        is_dir = os.path.isdir(full_path)
                        size = 0 if is_dir else os.path.getsize(full_path)
                        total_size += size
                        items.append(
                            {
                                "path": os.path.relpath(full_path, path),
                                "is_dir": is_dir,
                                "size": size,
                            }
                        )
            else:
                for name in os.listdir(path):
                    if not show_hidden and name.startswith("."):
                        continue
                    full_path = os.path.join(path, name)
                    is_dir = os.path.isdir(full_path)
                    size = 0 if is_dir else os.path.getsize(full_path)
                    total_size += size
                    items.append(
                        {
                            "path": name,
                            "is_dir": is_dir,
                            "size": size,
                        }
                    )
            # Sort directories first then files, alphabetically
            items.sort(key=lambda i: (not i["is_dir"], i["path"]))
            output_lines = []
            for item in items:
                indicator = "/" if item["is_dir"] else ""
                size_str = "-" if item["is_dir"] else f"{item['size']} bytes"
                output_lines.append(f"{item['path']}{indicator} {size_str}")
            output = "\n".join(output_lines)
            return {
                "success": True,
                "output": output,
                "total_items": len(items),
                "total_size": total_size,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}


def get_tool() -> BaseTool:
    return ListDirectoryTool()