"""
Progress report update tool.

This tool records the outcome of a completed action into the
agent's progress tracking state and writes updates to the
markdown report. It requires an agent context to function
properly.
"""

from __future__ import annotations

import os
import datetime
from typing import Dict, Any, Optional, List

from sciagent.base_tool import BaseTool

try:
    from sciagent.state import ProgressEntry  # type: ignore
except Exception:
    ProgressEntry = None  # type: ignore


class UpdateProgressMDTool(BaseTool):
    """Update progress.md with timestamped file tracking."""

    name = "update_progress_md"
    description = "Update progress.md with timestamped file tracking"
    input_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "description": "What action was just completed"},
            "files_modified": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Files that were created or modified",
            },
        },
        "required": ["action"],
    }

    def run(self, tool_input: Dict[str, Any], agent: Optional[Any] = None) -> Dict[str, Any]:
        if agent is None or ProgressEntry is None:
            return {"success": False, "error": "Progress update requires an agent context"}
        try:
            action = tool_input.get("action", "")
            files_modified: List[str] = tool_input.get("files_modified", [])
            # Update file tracking metadata
            for file_path in files_modified:
                if os.path.exists(file_path):
                    stat = os.stat(file_path)
                    try:
                        agent.state.files_tracking[file_path] = {
                            "last_modified": datetime.datetime.fromtimestamp(stat.st_mtime).isoformat(),
                            "size": stat.st_size,
                            "action": action,
                            "language": agent._detect_language(file_path) if hasattr(agent, "_detect_language") else "unknown",
                        }
                    except Exception:
                        pass
            # Record progress entry
            details = (
                f"Modified {len(files_modified)} files" if files_modified else "Action completed with full tool suite"
            )
            progress_entry = ProgressEntry(
                timestamp=datetime.datetime.now().isoformat(),
                action=action,
                details=details,
                files_affected=files_modified,
                status="completed",
            )
            agent.state.progress_entries.append(progress_entry)
            # Update markdown report
            if hasattr(agent, "_update_progress_md_file"):
                try:
                    agent._update_progress_md_file()  # type: ignore[attr-defined]
                except Exception:
                    pass
            return {
                "success": True,
                "output": (
                    f"📊 Progress Updated: {action}\n"
                    f"Files tracked: {len(files_modified)}\n"
                    f"Total files: {len(agent.state.files_tracking)}\n"
                    f"Sub-agents: {len(agent.state.sub_agent_results)}"
                ),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}


def get_tool() -> BaseTool:
    return UpdateProgressMDTool()