"""
Jupyter notebook editing tool.

Supports creating new notebooks, reading basic metadata and
adding cells. Edit and execution of cells are intentionally
limited to keep the tool safe and deterministic. The caller can
extend this functionality by modifying this module.
"""

from __future__ import annotations

import os
import json
import datetime
from typing import Dict, Any, Optional, List

from sciagent.base_tool import BaseTool


class NotebookEditTool(BaseTool):
    """Create, read, and edit Jupyter notebooks."""

    name = "notebook_edit"
    description = "Create, read, and edit Jupyter notebooks"
    input_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "enum": ["create", "read", "add_cell", "edit_cell", "run_cell"],
                "description": "Notebook operation",
            },
            "path": {"type": "string", "description": "Notebook path"},
            "cell_content": {"type": "string", "description": "Cell content for add/edit"},
            "cell_type": {
                "type": "string",
                "enum": ["code", "markdown"],
                "default": "code",
            },
            "cell_index": {"type": "number", "description": "Cell index for edit operations"},
        },
        "required": ["command", "path"],
    }

    def run(self, tool_input: Dict[str, Any], agent: Optional[Any] = None) -> Dict[str, Any]:
        try:
            command = tool_input.get("command")
            path = tool_input.get("path")
            if command == "create":
                notebook = {
                    "cells": [],
                    "metadata": {
                        "kernelspec": {
                            "display_name": "Python 3",
                            "language": "python",
                            "name": "python3",
                        },
                        "language_info": {
                            "name": "python",
                            "version": "3.8.0",
                        },
                    },
                    "nbformat": 4,
                    "nbformat_minor": 4,
                }
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(notebook, f, indent=2)
                # Track creation
                if agent is not None:
                    try:
                        agent.state.files_tracking[path] = {
                            "created": datetime.datetime.now().isoformat(),
                            "size": len(json.dumps(notebook)),
                            "action": "created",
                            "type": "jupyter_notebook",
                        }
                    except Exception:
                        pass
                return {"success": True, "output": f"Created Jupyter notebook: {path}"}
            elif command == "read":
                with open(path, "r", encoding="utf-8") as f:
                    notebook = json.load(f)
                cell_count = len(notebook.get("cells", []))
                cell_types: Dict[str, int] = {}
                for cell in notebook.get("cells", []):
                    cell_type = cell.get("cell_type", "unknown")
                    cell_types[cell_type] = cell_types.get(cell_type, 0) + 1
                return {
                    "success": True,
                    "output": f"📓 Notebook: {path}\nCells: {cell_count} total ({dict(cell_types)})",
                    "cell_count": cell_count,
                    "cell_types": cell_types,
                }
            elif command == "add_cell":
                cell_content = tool_input.get("cell_content", "")
                cell_type = tool_input.get("cell_type", "code")
                with open(path, "r", encoding="utf-8") as f:
                    notebook = json.load(f)
                new_cell: Dict[str, Any] = {
                    "cell_type": cell_type,
                    "source": cell_content.split("\n"),
                    "metadata": {},
                }
                if cell_type == "code":
                    new_cell["execution_count"] = None
                    new_cell["outputs"] = []
                notebook["cells"].append(new_cell)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(notebook, f, indent=2)
                return {
                    "success": True,
                    "output": f"Added {cell_type} cell to {path} (now {len(notebook['cells'])} cells)",
                }
            else:
                return {"success": False, "error": f"Notebook command '{command}' not implemented"}
        except Exception as e:
            return {"success": False, "error": str(e)}


def get_tool() -> BaseTool:
    return NotebookEditTool()