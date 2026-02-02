"""
Task management tool.

This tool accepts a list of todo items and returns a formatted
summary of their status and priority. It does not persist
state; callers may handle persistence or further processing.
The implementation adapts the behaviour from
``_execute_todo_write`` in the agent.
"""

from __future__ import annotations

from typing import Dict, Any, Optional, List

from sciagent.base_tool import BaseTool


class TodoWriteTool(BaseTool):
    """Create and manage task lists with progress tracking."""

    name = "todo_write"
    description = "Create and manage task lists with progress tracking"
    input_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "content": {"type": "string"},
                        "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                        "priority": {"type": "string", "enum": ["high", "medium", "low"]},
                    },
                    "required": ["id", "content", "status", "priority"],
                },
            }
        },
        "required": ["todos"],
    }

    def run(self, tool_input: Dict[str, Any], agent: Optional[Any] = None) -> Dict[str, Any]:
        try:
            todos_data: List[Dict[str, Any]] = tool_input.get("todos", [])
            status_counts = {"pending": 0, "in_progress": 0, "completed": 0}
            lines = []
            status_symbol = {"pending": "☐", "in_progress": "🔄", "completed": "☒"}
            priority_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}
            for todo in todos_data:
                symbol = status_symbol.get(todo.get("status"), "☐")
                priority = priority_emoji.get(todo.get("priority"), "")
                lines.append(f"{symbol} {todo.get('content', '')} {priority}")
                # update counts
                if todo.get("status") in status_counts:
                    status_counts[todo["status"]] += 1
            summary_line = f"Summary: {status_counts['completed']} completed, {status_counts['in_progress']} in progress, {status_counts['pending']} pending"
            
            # Add header to make todo lists more visible
            header = "📋 Task List:"
            output = "\n".join([header, ""] + lines + ["", summary_line])
            return {
                "success": True,
                "output": output,
                "status_counts": status_counts,
                "total_todos": len(todos_data),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}


def get_tool() -> BaseTool:
    return TodoWriteTool()